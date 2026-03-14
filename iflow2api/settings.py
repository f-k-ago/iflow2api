"""应用配置管理。"""

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Optional
from uuid import uuid4

from pydantic import BaseModel, Field

from .crypto import ConfigEncryption

logger = logging.getLogger("iflow2api")


DEFAULT_BASE_URL = "https://apis.iflow.cn/v1"
SUPPORTED_UPSTREAM_AUTH_TYPES = {"oauth-iflow", "cookie"}


class UpstreamAccount(BaseModel):
    """上游账号池中的单个账号。"""

    id: str = Field(default_factory=lambda: uuid4().hex)
    label: str = ""
    enabled: bool = True
    auth_type: str = "api-key"
    api_key: str = ""
    base_url: str = DEFAULT_BASE_URL
    oauth_access_token: str = ""
    oauth_refresh_token: str = ""
    oauth_expires_at: Optional[str] = None
    cookie: str = ""
    cookie_email: str = ""
    cookie_expires_at: Optional[str] = None
    email: str = ""
    phone: str = ""
    session_id: str = ""
    conversation_id: str = ""
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _normalize_account(account: UpstreamAccount) -> UpstreamAccount:
    """规范化账号字段，保证后续逻辑只面对一种形状。"""
    if not account.id:
        account.id = uuid4().hex

    account.label = (account.label or "").strip()
    account.auth_type = ((account.auth_type or "api-key").strip() or "api-key")
    account.api_key = (account.api_key or "").strip()
    account.base_url = (account.base_url or DEFAULT_BASE_URL).strip() or DEFAULT_BASE_URL
    account.oauth_access_token = (account.oauth_access_token or "").strip()
    account.oauth_refresh_token = (account.oauth_refresh_token or "").strip()
    account.cookie = (account.cookie or "").strip()
    account.cookie_email = (account.cookie_email or "").strip()
    account.email = (account.email or "").strip()
    account.phone = (account.phone or "").strip()
    account.session_id = (account.session_id or "").strip()
    account.conversation_id = (account.conversation_id or "").strip()

    if account.auth_type == "cookie" and not account.email and account.cookie_email:
        account.email = account.cookie_email

    if not account.session_id:
        account.session_id = f"session-{uuid4()}"
    if not account.conversation_id:
        account.conversation_id = str(uuid4())

    if not account.created_at:
        account.created_at = _now_iso()
    if not account.updated_at:
        account.updated_at = account.created_at

    if not account.label:
        if account.email:
            account.label = account.email
        elif account.phone:
            account.label = account.phone
        elif account.auth_type == "api-key" and account.api_key:
            account.label = f"API Key {account.api_key[-4:]}"
        else:
            account.label = f"{account.auth_type}-{account.id[:6]}"

    return account


def _account_has_credentials(account: UpstreamAccount) -> bool:
    return bool(
        (account.api_key or "").strip()
        or (account.oauth_access_token or "").strip()
        or (account.cookie or "").strip()
    )


def is_supported_upstream_auth_type(auth_type: Any) -> bool:
    """判断上游账号认证类型是否仍受支持。"""
    return str(auth_type or "").strip() in SUPPORTED_UPSTREAM_AUTH_TYPES


class AppSettings(BaseModel):
    """应用配置"""

    # 服务器配置（M-08: 添加范围校验）
    host: str = "0.0.0.0"
    port: int = Field(default=28000, ge=1, le=65535)

    # OAuth 回调地址配置
    # 如果部署在远程服务器，需要设置为公网地址
    # 例如: https://your-domain.com 或 http://your-ip:28000
    oauth_callback_base_url: str = ""  # 空字符串表示使用 localhost

    # 上游兼容字段（无账号池时回退使用）
    api_key: str = ""
    base_url: str = DEFAULT_BASE_URL

    # OAuth / Cookie 字段
    auth_type: str = "api-key"
    oauth_access_token: str = ""
    oauth_refresh_token: str = ""
    oauth_expires_at: Optional[str] = None
    cookie: str = ""
    cookie_email: str = ""
    cookie_expires_at: Optional[str] = None

    # 界面设置
    theme_mode: str = "system"

    # 思考链设置
    preserve_reasoning_content: bool = True

    # 官方并发限制设置
    # 官方规则：每个用户（API Key）最多同时 1 个请求
    # - 流式请求：主动取消后立即释放令牌
    # - 非流式请求：主动取消后需等待运行完毕才释放令牌
    enable_concurrency_limit: bool = True
    max_concurrent_requests: int = 1
    max_queued_requests: int = Field(default=100, ge=0, le=10000)

    # 上游 API 并发设置（已废弃，由 enable_concurrency_limit 替代）
    # 注意：过高的并发数可能导致上游 API 返回 429 限流错误
    # 默认值为 1，表示串行处理；建议范围 1-10
    api_concurrency: int = Field(default=1, ge=1, le=10)

    # 语言设置
    language: str = "zh"

    # 更新检查设置
    check_update_on_startup: bool = True
    skip_version: str = ""

    # 自定义 API 鉴权设置
    custom_api_key: str = ""
    custom_auth_header: str = ""

    # 上游代理设置
    # 用于访问 iFlow API 时通过代理服务器
    # 格式: "http://host:port" 或 "socks5://host:port"
    upstream_proxy: str = ""
    upstream_proxy_enabled: bool = False

    # 上游传输层配置（用于尽量对齐官方 iFlow CLI 行为）
    # - node_fetch: 使用 Node.js fetch，与官方 CLI 更接近
    # - httpx: 使用 Python/OpenSSL 默认 TLS 栈
    # - curl_cffi: 使用 curl-impersonate，可伪装为 Chrome/Node 风格握手
    upstream_transport_backend: str = "node_fetch"
    tls_impersonate: str = "chrome124"

    # 多账号池配置
    upstream_accounts: list[UpstreamAccount] = Field(default_factory=list)


def build_legacy_account(settings: AppSettings) -> Optional[UpstreamAccount]:
    """从旧单账号字段构造兼容账号。"""
    if not is_supported_upstream_auth_type(settings.auth_type):
        return None

    if not any(
        [
            (settings.api_key or "").strip(),
            (settings.oauth_access_token or "").strip(),
            (settings.cookie or "").strip(),
        ]
    ):
        return None

    return _normalize_account(
        UpstreamAccount(
            id="legacy-primary",
            label="兼容账号",
            enabled=True,
            auth_type=(settings.auth_type or "oauth-iflow"),
            api_key=settings.api_key,
            base_url=settings.base_url,
            oauth_access_token=settings.oauth_access_token,
            oauth_refresh_token=settings.oauth_refresh_token,
            oauth_expires_at=settings.oauth_expires_at,
            cookie=settings.cookie,
            cookie_email=settings.cookie_email,
            cookie_expires_at=settings.cookie_expires_at,
            email=settings.cookie_email,
        )
    )


def prune_unsupported_upstream_accounts(settings: AppSettings) -> int:
    """移除已不再支持的上游账号类型（如 api-key 直登）。"""
    original_count = len(settings.upstream_accounts)
    if original_count:
        settings.upstream_accounts = [
            _normalize_account(account)
            for account in settings.upstream_accounts
            if is_supported_upstream_auth_type(account.auth_type)
        ]

    removed_count = original_count - len(settings.upstream_accounts)

    if not is_supported_upstream_auth_type(settings.auth_type):
        settings.auth_type = ""
        settings.api_key = ""
        settings.base_url = DEFAULT_BASE_URL
        settings.oauth_access_token = ""
        settings.oauth_refresh_token = ""
        settings.oauth_expires_at = None
        settings.cookie = ""
        settings.cookie_email = ""
        settings.cookie_expires_at = None

    return removed_count


def get_effective_upstream_transport_backend(settings: AppSettings) -> str:
    """解析当前实际生效的上游传输层。"""
    return settings.upstream_transport_backend


def list_upstream_accounts(settings: AppSettings) -> list[UpstreamAccount]:
    """返回当前设置中的账号列表；无池配置时回退到旧单账号字段。"""
    if settings.upstream_accounts:
        return [
            _normalize_account(account)
            for account in settings.upstream_accounts
            if is_supported_upstream_auth_type(account.auth_type)
        ]

    legacy_account = build_legacy_account(settings)
    return [legacy_account] if legacy_account else []


def get_primary_account(settings: AppSettings, include_disabled: bool = False) -> Optional[UpstreamAccount]:
    """获取兼容代表账号，供当前配置和信息展示使用。"""
    accounts = list_upstream_accounts(settings)
    if not accounts:
        return None

    for account in accounts:
        if include_disabled or account.enabled:
            return account

    return accounts[0]


def get_enabled_upstream_accounts(settings: AppSettings) -> list[UpstreamAccount]:
    """返回可参与调度的账号。"""
    return [
        account
        for account in list_upstream_accounts(settings)
        if account.enabled and _account_has_credentials(account) and account.api_key
    ]


def sync_legacy_auth_fields(settings: AppSettings) -> AppSettings:
    """把兼容代表账号同步回旧单账号字段，保证旧代码路径继续工作。"""
    primary_account = get_primary_account(settings)
    if not primary_account:
        settings.api_key = ""
        settings.base_url = DEFAULT_BASE_URL
        settings.auth_type = ""
        settings.oauth_access_token = ""
        settings.oauth_refresh_token = ""
        settings.oauth_expires_at = None
        settings.cookie = ""
        settings.cookie_email = ""
        settings.cookie_expires_at = None
        return settings

    settings.api_key = primary_account.api_key
    settings.base_url = primary_account.base_url
    settings.auth_type = primary_account.auth_type
    settings.oauth_access_token = primary_account.oauth_access_token
    settings.oauth_refresh_token = primary_account.oauth_refresh_token
    settings.oauth_expires_at = primary_account.oauth_expires_at
    settings.cookie = primary_account.cookie
    settings.cookie_email = primary_account.cookie_email or primary_account.email
    settings.cookie_expires_at = primary_account.cookie_expires_at
    return settings


def upsert_upstream_account(
    settings: AppSettings,
    account: UpstreamAccount,
) -> UpstreamAccount:
    """新增或更新账号池中的账号。"""
    normalized = _normalize_account(account)
    if not is_supported_upstream_auth_type(normalized.auth_type):
        raise ValueError(f"不支持的上游账号类型: {normalized.auth_type}")
    now = _now_iso()
    normalized.updated_at = now
    if not normalized.created_at:
        normalized.created_at = now

    match_index = -1
    for index, existing in enumerate(settings.upstream_accounts):
        if normalized.id and existing.id == normalized.id:
            match_index = index
            break
        if normalized.auth_type == "oauth-iflow" and normalized.oauth_refresh_token:
            if existing.oauth_refresh_token and existing.oauth_refresh_token == normalized.oauth_refresh_token:
                match_index = index
                break
        if normalized.auth_type == "cookie" and normalized.cookie:
            if existing.cookie and existing.cookie == normalized.cookie:
                match_index = index
                break
        if normalized.api_key and existing.api_key and existing.api_key == normalized.api_key:
            match_index = index
            break

    if match_index >= 0:
        existing = _normalize_account(settings.upstream_accounts[match_index])
        created_at = existing.created_at
        merged = existing.model_copy(update=normalized.model_dump())
        merged.session_id = existing.session_id or normalized.session_id
        merged.conversation_id = existing.conversation_id or normalized.conversation_id
        merged.created_at = created_at or normalized.created_at or now
        merged.updated_at = now
        settings.upstream_accounts[match_index] = _normalize_account(merged)
        saved_account = settings.upstream_accounts[match_index]
    else:
        normalized.created_at = normalized.created_at or now
        normalized.updated_at = now
        settings.upstream_accounts.append(normalized)
        saved_account = normalized

    sync_legacy_auth_fields(settings)
    return saved_account


def remove_upstream_account(settings: AppSettings, account_id: str) -> bool:
    """从账号池中删除账号。"""
    original_count = len(settings.upstream_accounts)
    settings.upstream_accounts = [account for account in settings.upstream_accounts if account.id != account_id]
    removed = len(settings.upstream_accounts) != original_count
    if removed:
        sync_legacy_auth_fields(settings)
    return removed


# lazy singleton for token encryption
_config_encryption: Optional[ConfigEncryption] = None


def _get_encryption() -> ConfigEncryption:
    """返回全局加密实例（懒初始化）"""
    global _config_encryption
    if _config_encryption is None:
        _config_encryption = ConfigEncryption()
    return _config_encryption


def _encrypt_token(token: str) -> str:
    """加密 OAuth token；若 cryptography 不可用则原样返回"""
    if not token:
        return token
    if token.startswith("enc:"):
        return token  # 已加密
    enc = _get_encryption()
    if not enc.is_available:
        return token
    return f"enc:{enc.encrypt(token)}"


def _decrypt_token(value: str) -> str:
    """解密 OAuth token；若无 enc: 前缀则视为明文直接返回"""
    if not value or not value.startswith("enc:"):
        return value
    try:
        return _get_encryption().decrypt(value[4:])
    except Exception:
        logger.warning("OAuth token 解密失败，将使用原始值")
        return value


def get_config_dir() -> Path:
    """获取应用配置目录"""
    return Path.home() / ".iflow2api"


def get_config_path() -> Path:
    """获取应用配置文件路径"""
    return get_config_dir() / "config.json"


def load_settings() -> AppSettings:
    """加载配置"""
    settings = AppSettings()
    migrated_legacy_transport_backend = False
    removed_unsupported_accounts = 0

    # 首先从 ~/.iflow2api/config.json 加载所有设置（包括 api_key）
    app_config_path = get_config_path()
    if app_config_path.exists():
        try:
            with open(app_config_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                # 加载所有设置
                if "host" in data:
                    settings.host = data["host"]
                if "port" in data:
                    settings.port = data["port"]
                if "oauth_callback_base_url" in data:
                    settings.oauth_callback_base_url = data["oauth_callback_base_url"]
                # api_key 和 base_url 也保存在 iflow2api/config.json 中
                if "api_key" in data:
                    settings.api_key = data["api_key"]
                if "base_url" in data:
                    settings.base_url = data["base_url"]
                if "upstream_accounts" in data and isinstance(data["upstream_accounts"], list):
                    loaded_accounts: list[UpstreamAccount] = []
                    for raw_account in data["upstream_accounts"]:
                        if not isinstance(raw_account, dict):
                            continue
                        account_data: dict[str, Any] = dict(raw_account)
                        if "oauth_access_token" in account_data:
                            account_data["oauth_access_token"] = _decrypt_token(account_data["oauth_access_token"])
                        if "oauth_refresh_token" in account_data:
                            account_data["oauth_refresh_token"] = _decrypt_token(account_data["oauth_refresh_token"])
                        if "cookie" in account_data:
                            account_data["cookie"] = _decrypt_token(account_data["cookie"])
                        try:
                            loaded_accounts.append(_normalize_account(UpstreamAccount(**account_data)))
                        except Exception as account_error:
                            logger.warning("读取上游账号配置失败，已跳过: %s", account_error)
                    settings.upstream_accounts = loaded_accounts
                if "theme_mode" in data:
                    settings.theme_mode = data["theme_mode"]
                # 语言设置
                if "language" in data:
                    settings.language = data["language"]
                # 思考链设置
                if "preserve_reasoning_content" in data:
                    settings.preserve_reasoning_content = data["preserve_reasoning_content"]
                # 上游 API 并发设置
                if "api_concurrency" in data:
                    settings.api_concurrency = data["api_concurrency"]
                if "max_queued_requests" in data:
                    settings.max_queued_requests = data["max_queued_requests"]
                # OAuth 设置
                if "auth_type" in data:
                    settings.auth_type = data["auth_type"]
                if "oauth_access_token" in data:
                    settings.oauth_access_token = _decrypt_token(data["oauth_access_token"])
                if "oauth_refresh_token" in data:
                    settings.oauth_refresh_token = _decrypt_token(data["oauth_refresh_token"])
                if "oauth_expires_at" in data:
                    settings.oauth_expires_at = data["oauth_expires_at"]
                if "cookie" in data:
                    settings.cookie = _decrypt_token(data["cookie"])
                if "cookie_email" in data:
                    settings.cookie_email = data["cookie_email"]
                if "cookie_expires_at" in data:
                    settings.cookie_expires_at = data["cookie_expires_at"]
                # 更新检查设置
                if "check_update_on_startup" in data:
                    settings.check_update_on_startup = data["check_update_on_startup"]
                if "skip_version" in data:
                    settings.skip_version = data["skip_version"]
                # 自定义 API 鉴权设置
                if "custom_api_key" in data:
                    settings.custom_api_key = data["custom_api_key"]
                if "custom_auth_header" in data:
                    settings.custom_auth_header = data["custom_auth_header"]
                # 上游代理设置
                if "upstream_proxy" in data:
                    settings.upstream_proxy = data["upstream_proxy"]
                if "upstream_proxy_enabled" in data:
                    settings.upstream_proxy_enabled = data["upstream_proxy_enabled"]
                # 上游传输层设置（TLS 指纹对齐）
                if "upstream_transport_backend" in data:
                    settings.upstream_transport_backend = data["upstream_transport_backend"]
                if "tls_impersonate" in data:
                    settings.tls_impersonate = data["tls_impersonate"]
        except Exception as _e:
            logger.warning("读取应用配置文件失败: %s", _e)

    removed_unsupported_accounts = prune_unsupported_upstream_accounts(settings)
    if removed_unsupported_accounts:
        logger.info("检测到并清理了 %d 个已废弃的 API Key 上游账号", removed_unsupported_accounts)

    if (settings.base_url or DEFAULT_BASE_URL).rstrip("/") == DEFAULT_BASE_URL and settings.upstream_transport_backend in {
        "httpx",
        "curl_cffi",
    }:
        logger.info(
            "检测到 legacy 传输层配置 backend=%s，已自动切换为 node_fetch 以对齐官方 iFlow CLI",
            settings.upstream_transport_backend,
        )
        settings.upstream_transport_backend = "node_fetch"
        migrated_legacy_transport_backend = True

    if settings.upstream_accounts:
        sync_legacy_auth_fields(settings)

    if migrated_legacy_transport_backend or removed_unsupported_accounts:
        try:
            save_settings(settings)
        except Exception as persist_error:
            logger.warning("配置自动迁移/清理已生效，但持久化失败: %s", persist_error)

    return settings


def save_settings(settings: AppSettings) -> None:
    """
    保存配置

    所有设置都保存到 ~/.iflow2api/config.json
    """
    config_dir = get_config_dir()
    config_dir.mkdir(parents=True, exist_ok=True)
    settings.upstream_accounts = [_normalize_account(account) for account in settings.upstream_accounts]
    prune_unsupported_upstream_accounts(settings)
    sync_legacy_auth_fields(settings)

    serialized_accounts = []
    for account in settings.upstream_accounts:
        serialized = account.model_dump()
        serialized["oauth_access_token"] = _encrypt_token(account.oauth_access_token)
        serialized["oauth_refresh_token"] = _encrypt_token(account.oauth_refresh_token)
        serialized["cookie"] = _encrypt_token(account.cookie)
        serialized_accounts.append(serialized)

    app_data = {
        "host": settings.host,
        "port": settings.port,
        "oauth_callback_base_url": settings.oauth_callback_base_url,
        # iFlow 配置也保存到 iflow2api/config.json
        "api_key": settings.api_key,
        "base_url": settings.base_url,
        "upstream_accounts": serialized_accounts,
        # OAuth 配置
        "auth_type": settings.auth_type,
        "oauth_access_token": _encrypt_token(settings.oauth_access_token),
        "oauth_refresh_token": _encrypt_token(settings.oauth_refresh_token),
        "oauth_expires_at": settings.oauth_expires_at,
        "cookie": _encrypt_token(settings.cookie),
        "cookie_email": settings.cookie_email,
        "cookie_expires_at": settings.cookie_expires_at,
        # 界面设置
        "theme_mode": settings.theme_mode,
        # 思考链设置
        "preserve_reasoning_content": settings.preserve_reasoning_content,
        # 上游 API 并发设置
        "api_concurrency": settings.api_concurrency,
        "max_queued_requests": settings.max_queued_requests,
        # 语言设置
        "language": settings.language,
        # 更新检查设置
        "check_update_on_startup": settings.check_update_on_startup,
        "skip_version": settings.skip_version,
        # 自定义 API 鉴权设置
        "custom_api_key": settings.custom_api_key,
        "custom_auth_header": settings.custom_auth_header,
        # 上游代理设置
        "upstream_proxy": settings.upstream_proxy,
        "upstream_proxy_enabled": settings.upstream_proxy_enabled,
        # 上游传输层设置（TLS 指纹对齐）
        "upstream_transport_backend": settings.upstream_transport_backend,
        "tls_impersonate": settings.tls_impersonate,
    }

    config_path = get_config_path()
    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(app_data, f, indent=2, ensure_ascii=False)
