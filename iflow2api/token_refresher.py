"""认证凭据自动刷新后台任务

刷新策略：
1. OAuth 模式：检查 token 过期时间，小于24小时自动刷新（与 iflow-cli 一致）
2. Cookie 模式：检查 API Key 过期时间，小于48小时自动刷新（对齐 cpa）
2. 每6小时检查一次
3. 增加重试机制：服务器过载时自动重试（重试5次，指数退避）
4. 刷新失败时给出明确提示
"""

import asyncio
import logging
import threading
from datetime import datetime
from typing import Optional, Callable, Tuple

logger = logging.getLogger("iflow2api")

from .oauth import IFlowOAuth
from .config import load_iflow_config, save_iflow_config, IFlowConfig
from .transport import create_upstream_transport


# 刷新配置常量
# 注意：iflow-cli 使用 24 小时刷新缓冲，我们保持一致
CHECK_INTERVAL_SECONDS = 6 * 60 * 60  # 每6小时检查一次
REFRESH_BUFFER_SECONDS = 24 * 60 * 60  # 提前24小时刷新（与 iflow-cli 一致）
COOKIE_REFRESH_BUFFER_SECONDS = 48 * 60 * 60  # 提前48小时刷新（对齐 cpa）
RETRY_COUNT = 5  # 重试次数（增加到5次）
RETRY_DELAY_SECONDS = 30  # 重试间隔（增加到30秒）
RETRY_EXPONENTIAL_BACKOFF = True  # 启用指数退避


class OAuthTokenRefresher:
    """OAuth token 自动刷新器"""

    def __init__(
        self,
        check_interval: int = CHECK_INTERVAL_SECONDS,
        refresh_buffer: int = REFRESH_BUFFER_SECONDS,
        cookie_refresh_buffer: int = COOKIE_REFRESH_BUFFER_SECONDS,
        retry_count: int = RETRY_COUNT,
        retry_delay: int = RETRY_DELAY_SECONDS,
    ):
        """
        初始化 token 刷新器

        Args:
            check_interval: 检查间隔（秒），默认6小时
            refresh_buffer: OAuth 提前刷新的缓冲时间（秒），默认24小时
            cookie_refresh_buffer: Cookie 提前刷新的缓冲时间（秒），默认48小时
            retry_count: 重试次数，默认5次
            retry_delay: 重试间隔（秒），默认30秒（启用指数退避）
        """
        self.check_interval = check_interval
        self.refresh_buffer = refresh_buffer
        self.cookie_refresh_buffer = cookie_refresh_buffer
        self.retry_count = retry_count
        self.retry_delay = retry_delay
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._on_refresh_callback: Optional[Callable] = None
        # 保存主事件循环引用，用于在后台线程中提交 coroutine（M-05 修复）
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        # 上次刷新失败的时间，用于避免频繁重试
        self._last_failure_time: Optional[datetime] = None
        self._failure_count = 0

    def set_refresh_callback(self, callback: Callable[[dict], None]):
        """
        设置刷新回调函数

        Args:
            callback: 回调函数，接收 token_data 参数
        """
        self._on_refresh_callback = callback

    def start(self):
        """启动 token 刷新后台任务，捕获当前事件循环引用（M-05 修复）"""
        if self._running:
            return

        # 在 FastAPI lifespan（asyncio 上下文）中调用时捕获当前循环
        try:
            self._loop = asyncio.get_event_loop()
        except RuntimeError:
            self._loop = None

        self._running = True
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()
        logger.info("OAuth token 刷新器已启动，检查间隔: %d小时", self.check_interval // 3600)

    def stop(self):
        """停止 token 刷新后台任务"""
        if not self._running:
            return

        self._running = False
        self._stop_event.set()

        if self._thread:
            self._thread.join(timeout=5.0)
            self._thread = None

        logger.info("OAuth token 刷新器已停止")

    def _run_loop(self):
        """运行刷新循环（在后台线程中）"""
        while not self._stop_event.is_set():
            try:
                config = load_iflow_config()

                if config.auth_type == "oauth-iflow":
                    if self._should_refresh(config):
                        logger.info("OAuth token 即将过期，开始刷新...")
                        self._schedule_refresh(config)
                elif config.auth_type == "cookie":
                    if self._should_refresh_cookie(config):
                        logger.info("Cookie 模式 API Key 即将过期，开始刷新...")
                        self._schedule_cookie_refresh(config)

            except Exception as e:
                logger.warning("检查 token 状态时出错: %s", e)

            self._stop_event.wait(self.check_interval)

    def _should_refresh(self, config: IFlowConfig) -> bool:
        """
        检查是否需要刷新 token

        刷新条件：
        1. 有 refresh_token
        2. 有过期时间
        3. 距离过期时间小于 refresh_buffer（24小时，与 iflow-cli 一致）

        Args:
            config: 当前 iFlow 配置

        Returns:
            True 表示需要刷新
        """
        if not config.oauth_refresh_token:
            return False

        # 使用 api_key_expires_at 或 oauth_expires_at
        expires_at = config.api_key_expires_at or config.oauth_expires_at
        if not expires_at:
            return False

        # 计算距离过期的时间
        now = datetime.now()
        time_until_expiry = expires_at - now

        # 如果已经过期，需要刷新
        if time_until_expiry.total_seconds() <= 0:
            logger.info("apiKey 已过期，需要刷新")
            return True

        # 如果距离过期时间小于缓冲时间，需要刷新
        if time_until_expiry.total_seconds() < self.refresh_buffer:
            hours_until_expiry = time_until_expiry.total_seconds() / 3600
            logger.info(
                "apiKey 将在 %.1f 小时后过期，需要刷新",
                hours_until_expiry
            )
            return True

        return False

    def _parse_cookie_expire_time(self, expire_str: str) -> Optional[datetime]:
        """解析 Cookie 模式下的过期时间字符串。"""
        if not expire_str:
            return None

        value = expire_str.strip()
        if not value:
            return None

        for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S"):
            try:
                return datetime.strptime(value, fmt)
            except ValueError:
                continue

        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None

    def _should_refresh_cookie(self, config: IFlowConfig) -> bool:
        """
        检查 Cookie 模式是否需要刷新 API Key。

        与 cpa 策略对齐：距离过期小于 48 小时时刷新。
        """
        if not config.cookie:
            return False
        if not config.cookie_email:
            logger.warning("Cookie 模式缺少邮箱信息，将在刷新时自动探测")
            return True

        if not config.cookie_expires_at:
            logger.warning("Cookie 模式缺少过期时间，保守执行刷新")
            return True

        expires_at = self._parse_cookie_expire_time(config.cookie_expires_at)
        if not expires_at:
            logger.warning("无法解析 Cookie 过期时间（%s），保守执行刷新", config.cookie_expires_at)
            return True

        time_until_expiry = expires_at - datetime.now()
        if time_until_expiry.total_seconds() <= 0:
            logger.info("Cookie 模式 API Key 已过期，需要刷新")
            return True

        if time_until_expiry.total_seconds() < self.cookie_refresh_buffer:
            hours_until_expiry = time_until_expiry.total_seconds() / 3600
            logger.info(
                "Cookie 模式 API Key 将在 %.1f 小时后过期，需要刷新",
                hours_until_expiry,
            )
            return True

        return False

    def _schedule_refresh(self, config: IFlowConfig) -> None:
        """
        安排 token 刷新任务（M-05 修复）

        优先使用 run_coroutine_threadsafe 注入主事件循环，
        避免在主进程中重复创建事件循环引发资源冲突。
        如果没有可用的主循环，则回退到 asyncio.run()。
        """
        coro = self._refresh_token_with_retry(config)

        if self._loop and self._loop.is_running():
            # 在主事件循环中运行，避免创建新循环
            future = asyncio.run_coroutine_threadsafe(coro, self._loop)
            try:
                future.result(timeout=60)  # 最多等待 60 秒
            except Exception as e:
                logger.error("Token 刷新失败: %s", e)
        else:
            # 回退：创建临时隔离事件循环（仅后台线程使用）
            asyncio.run(coro)

    def _schedule_cookie_refresh(self, config: IFlowConfig) -> None:
        """安排 Cookie 模式刷新任务。"""
        coro = self._refresh_cookie_with_retry(config)

        if self._loop and self._loop.is_running():
            future = asyncio.run_coroutine_threadsafe(coro, self._loop)
            try:
                future.result(timeout=60)
            except Exception as e:
                logger.error("Cookie 模式刷新失败: %s", e)
        else:
            asyncio.run(coro)

    async def _refresh_token_with_retry(self, config: IFlowConfig) -> bool:
        """
        带重试机制的 token 刷新（支持指数退避）

        Args:
            config: 当前 iFlow 配置

        Returns:
            True 表示刷新成功
        """
        if not config.oauth_refresh_token:
            logger.error("没有 refresh_token，无法刷新")
            return False

        oauth = IFlowOAuth()
        last_error = None
        # 追踪失败原因：True=服务器临时问题，False=凭证无效等需要重新登录的问题
        failed_due_to_overload = False

        for attempt in range(1, self.retry_count + 1):
            try:
                logger.info(
                    "尝试刷新 token (第 %d/%d 次)...",
                    attempt,
                    self.retry_count
                )

                token_data = await oauth.refresh_token(config.oauth_refresh_token)

                # 更新配置
                config.api_key = token_data.get("access_token", "")
                config.oauth_access_token = token_data.get("access_token", "")
                if token_data.get("refresh_token"):
                    config.oauth_refresh_token = token_data["refresh_token"]
                if token_data.get("expires_at"):
                    config.oauth_expires_at = token_data["expires_at"]
                    config.api_key_expires_at = token_data["expires_at"]

                # 保存配置
                save_iflow_config(config)

                # 重置失败计数
                self._failure_count = 0
                self._last_failure_time = None

                logger.info("Token 刷新成功！")

                # 调用回调
                if self._on_refresh_callback:
                    token_data["auth_type"] = "oauth-iflow"
                    token_data["api_key"] = config.api_key
                    self._on_refresh_callback(token_data)

                return True

            except Exception as e:
                last_error = e
                error_msg = str(e)
                logger.warning(
                    "Token 刷新失败 (第 %d/%d 次): %s",
                    attempt,
                    self.retry_count,
                    error_msg
                )

                # 检查是否是服务器临时问题（过载、超时、网络等），这类错误 token 本身仍然有效
                is_transient_error = (
                    "太多" in error_msg
                    or "服务器过载" in error_msg
                    or "overload" in error_msg.lower()
                    or "timeout" in error_msg.lower()
                    or "timed out" in error_msg.lower()
                    or "connect" in error_msg.lower()
                    or "网络" in error_msg
                    or "503" in error_msg
                    or "502" in error_msg
                    or "429" in error_msg
                )
                # 检查是否是凭证无效（需要重新登录）
                is_invalid_grant = (
                    "invalid_grant" in error_msg
                    or "invalid_token" in error_msg
                    or "refresh_token 无效" in error_msg
                    or "已过期" in error_msg
                )

                if is_transient_error:
                    failed_due_to_overload = True
                    if attempt < self.retry_count:
                        # 指数退避：30s, 60s, 120s, 240s...
                        delay = self.retry_delay * (2 ** (attempt - 1))
                        # 最大延迟 5 分钟
                        delay = min(delay, 300)
                        logger.info("服务器暂时不可用，等待 %d 秒后重试（指数退避）...", delay)
                        await asyncio.sleep(delay)
                        continue
                elif is_invalid_grant:
                    # 凭证无效，立即停止重试
                    failed_due_to_overload = False
                    logger.error("refresh_token 无效或已过期，需要重新登录: %s", error_msg)
                    break
                else:
                    # 未知错误，记录并停止重试
                    failed_due_to_overload = False
                    logger.error("Token 刷新遇到未知错误，停止重试: %s", error_msg)
                    break

        # 所有重试都失败
        self._failure_count += 1
        self._last_failure_time = datetime.now()

        if failed_due_to_overload:
            # 服务器临时问题，token 本身仍然有效，不需要重新登录
            logger.warning(
                "Token 刷新因服务器暂时不可用而失败（已重试 %d 次）。"
                "现有 token 仍然有效，将在下次定时检查时自动重试。",
                self.retry_count
            )
            if self._on_refresh_callback:
                self._on_refresh_callback({
                    "error": True,
                    "transient": True,
                    "message": f"服务器暂时不可用，将自动重试: {last_error}",
                    "attempts": self.retry_count
                })
        else:
            # 凭证问题，需要用户介入
            logger.error(
                "Token 刷新失败，已重试 %d 次。请手动重新登录 iflow。",
                self.retry_count
            )
            if self._on_refresh_callback:
                self._on_refresh_callback({
                    "error": True,
                    "transient": False,
                    "message": f"Token 刷新失败，请重新登录: {last_error}",
                    "attempts": self.retry_count
                })

        return False

    async def _refresh_cookie_with_retry(self, config: IFlowConfig) -> bool:
        """
        带重试机制的 Cookie 模式 API Key 刷新。

        与 cpa 刷新策略一致：使用 `cookie + email` 调用刷新接口。
        """
        if not config.cookie:
            logger.error("Cookie 缺失，无法刷新 Cookie 模式 API Key")
            return False

        oauth = IFlowOAuth()
        last_error = None
        failed_due_to_overload = False

        for attempt in range(1, self.retry_count + 1):
            try:
                logger.info(
                    "尝试刷新 Cookie 模式 API Key (第 %d/%d 次)...",
                    attempt,
                    self.retry_count,
                )

                key_data = await oauth.refresh_api_key_with_cookie(
                    config.cookie,
                    config.cookie_email,
                )

                api_key = key_data.get("apiKey", "").strip()
                expired = key_data.get("expired", "").strip()
                if not api_key:
                    raise ValueError("刷新响应缺少 apiKey")

                config.api_key = api_key
                config.cookie_expires_at = expired or None
                resolved_email = (key_data.get("email") or "").strip()
                if resolved_email:
                    config.cookie_email = resolved_email
                config.api_key_expires_at = self._parse_cookie_expire_time(expired)

                save_iflow_config(config)

                self._failure_count = 0
                self._last_failure_time = None
                logger.info("Cookie 模式 API Key 刷新成功！")

                if self._on_refresh_callback:
                    self._on_refresh_callback({
                        "auth_type": "cookie",
                        "api_key": api_key,
                        "cookie": config.cookie,
                        "cookie_email": config.cookie_email,
                        "cookie_expires_at": config.cookie_expires_at,
                    })

                return True

            except Exception as e:
                last_error = e
                error_msg = str(e)
                logger.warning(
                    "Cookie 模式刷新失败 (第 %d/%d 次): %s",
                    attempt,
                    self.retry_count,
                    error_msg,
                )

                is_transient_error = (
                    "太多" in error_msg
                    or "服务器过载" in error_msg
                    or "overload" in error_msg.lower()
                    or "timeout" in error_msg.lower()
                    or "timed out" in error_msg.lower()
                    or "connect" in error_msg.lower()
                    or "网络" in error_msg
                    or "503" in error_msg
                    or "502" in error_msg
                    or "429" in error_msg
                )
                is_invalid_credential = (
                    "Cookie 无效" in error_msg
                    or "401" in error_msg
                    or "已过期" in error_msg
                )

                if is_transient_error:
                    failed_due_to_overload = True
                    if attempt < self.retry_count:
                        delay = self.retry_delay * (2 ** (attempt - 1))
                        delay = min(delay, 300)
                        logger.info("服务器暂时不可用，等待 %d 秒后重试（指数退避）...", delay)
                        await asyncio.sleep(delay)
                        continue
                elif is_invalid_credential:
                    failed_due_to_overload = False
                    logger.error("Cookie 无效或已过期，需要重新登录: %s", error_msg)
                    break
                else:
                    failed_due_to_overload = False
                    logger.error("Cookie 模式刷新遇到未知错误，停止重试: %s", error_msg)
                    break

        self._failure_count += 1
        self._last_failure_time = datetime.now()

        if failed_due_to_overload:
            logger.warning(
                "Cookie 模式刷新因服务器暂时不可用而失败（已重试 %d 次），将在下次定时检查时自动重试。",
                self.retry_count,
            )
            if self._on_refresh_callback:
                self._on_refresh_callback({
                    "error": True,
                    "transient": True,
                    "auth_type": "cookie",
                    "message": f"服务器暂时不可用，将自动重试: {last_error}",
                    "attempts": self.retry_count,
                })
        else:
            logger.error(
                "Cookie 模式刷新失败，已重试 %d 次。请手动重新登录。",
                self.retry_count,
            )
            if self._on_refresh_callback:
                self._on_refresh_callback({
                    "error": True,
                    "transient": False,
                    "auth_type": "cookie",
                    "message": f"Cookie 模式刷新失败，请重新登录: {last_error}",
                    "attempts": self.retry_count,
                })

        return False

    async def _refresh_token(self, config: IFlowConfig):
        """
        刷新 token（兼容旧接口）

        Args:
            config: 当前 iFlow 配置
        """
        await self._refresh_token_with_retry(config)

    def is_running(self) -> bool:
        """
        检查是否正在运行

        Returns:
            True 表示正在运行
        """
        return self._running

    def should_refresh_now(self) -> bool:
        """
        检查是否需要立即刷新 token

        Returns:
            True 表示需要立即刷新
        """
        try:
            config = load_iflow_config()
            if config.auth_type == "oauth-iflow":
                return self._should_refresh(config)
            if config.auth_type == "cookie":
                return self._should_refresh_cookie(config)
            return False
        except Exception:
            return False

    def get_status(self) -> dict:
        """
        获取刷新器状态

        Returns:
            包含刷新器状态的字典
        """
        try:
            config = load_iflow_config()
            expires_at = config.api_key_expires_at or config.oauth_expires_at
            if config.auth_type == "cookie" and config.cookie_expires_at:
                parsed_cookie_expire = self._parse_cookie_expire_time(config.cookie_expires_at)
                if parsed_cookie_expire:
                    expires_at = parsed_cookie_expire

            time_until_expiry = None
            if expires_at:
                time_until_expiry = (expires_at - datetime.now()).total_seconds()

            return {
                "running": self._running,
                "check_interval_hours": self.check_interval / 3600,
                "refresh_buffer_hours": self.refresh_buffer / 3600,
                "auth_type": config.auth_type,
                "has_refresh_token": bool(config.oauth_refresh_token),
                "has_cookie": bool(config.cookie),
                "expires_at": expires_at.isoformat() if expires_at else None,
                "time_until_expiry_seconds": time_until_expiry,
                "needs_refresh": (
                    self._should_refresh(config)
                    if config and config.auth_type == "oauth-iflow"
                    else self._should_refresh_cookie(config)
                    if config and config.auth_type == "cookie"
                    else False
                ),
                "failure_count": self._failure_count,
                "last_failure_time": self._last_failure_time.isoformat() if self._last_failure_time else None,
            }
        except Exception as e:
            return {
                "running": self._running,
                "error": str(e)
            }


async def check_api_key_validity(api_key: str, base_url: str = "https://apis.iflow.cn/v1") -> Tuple[bool, str]:
    """
    检查 apiKey 是否有效

    Args:
        api_key: API 密钥
        base_url: API 基础 URL

    Returns:
        (是否有效, 错误信息或成功消息)
    """
    client = None
    try:
        # 加载代理与传输层配置
        from .settings import load_settings

        settings = load_settings()
        proxy = settings.upstream_proxy if settings.upstream_proxy_enabled and settings.upstream_proxy else None

        client = create_upstream_transport(
            backend=settings.upstream_transport_backend,
            timeout=10.0,
            follow_redirects=True,
            proxy=proxy,
            trust_env=False,
            impersonate=settings.tls_impersonate,
        )

        response = await client.get(
            f"{base_url}/models",
            headers={
                "Authorization": f"Bearer {api_key}",
                "User-Agent": "iFlow-Cli",
            },
            timeout=10.0,
        )

        if response.status_code == 200:
            return True, "API Key 有效"
        if response.status_code == 401:
            return False, "API Key 无效或已过期"
        return False, f"API 返回错误: {response.status_code}"

    except Exception as e:
        # 含超时在内统一处理，避免绑定具体 HTTP 客户端异常类型
        msg = str(e).lower()
        if "timeout" in msg or "timed out" in msg:
            return False, "API 请求超时"
        return False, f"检查失败: {str(e)}"
    finally:
        if client is not None:
            try:
                await client.close()
            except Exception:
                pass


# 全局刷新器实例
_global_refresher: Optional[OAuthTokenRefresher] = None


def get_global_refresher() -> OAuthTokenRefresher:
    """
    获取全局 token 刷新器实例

    Returns:
        OAuthTokenRefresher 实例
    """
    global _global_refresher

    if _global_refresher is None:
        _global_refresher = OAuthTokenRefresher()

    return _global_refresher


def start_global_refresher():
    """启动全局 token 刷新器"""
    refresher = get_global_refresher()
    refresher.start()


def stop_global_refresher():
    """停止全局 token 刷新器"""
    global _global_refresher

    if _global_refresher:
        _global_refresher.stop()
        _global_refresher = None
