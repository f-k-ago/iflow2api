"""iFlow OAuth 认证实现"""

import base64
import secrets
from typing import Optional, Dict, Any
from datetime import datetime, timedelta

from .transport import BaseUpstreamTransport, create_upstream_transport


class IFlowOAuth:
    """iFlow OAuth 认证客户端"""

    # iFlow OAuth 配置
    CLIENT_ID = "10009311001"
    CLIENT_SECRET = "4Z3YjXycVsQvyGF1etiNlIBB4RsqSDtW"
    TOKEN_URL = "https://iflow.cn/oauth/token"
    USER_INFO_URL = "https://iflow.cn/api/oauth/getUserInfo"
    AUTH_URL = "https://iflow.cn/oauth"

    # Cookie 认证配置
    API_KEY_ENDPOINT = "https://platform.iflow.cn/api/openapi/apikey"

    def __init__(self):
        self._client: Optional[BaseUpstreamTransport] = None

    @staticmethod
    def normalize_cookie(raw_cookie: str) -> str:
        """标准化 Cookie 字符串。"""
        if not raw_cookie or not raw_cookie.strip():
            raise ValueError("Cookie 不能为空")

        normalized = " ".join(raw_cookie.strip().split())
        if not normalized.endswith(";"):
            normalized += ";"
        return normalized

    @staticmethod
    def extract_bxauth(cookie: str) -> str:
        """从 Cookie 中提取 BXAuth 值。"""
        for item in cookie.split(";"):
            part = item.strip()
            if part.startswith("BXAuth="):
                return part[len("BXAuth="):]
        return ""

    @classmethod
    def cookie_for_storage(cls, raw_cookie: str) -> str:
        """
        生成可持久化的 Cookie（仅保留 BXAuth 字段）。

        与 cpa 项目的做法保持一致，降低落盘敏感面。
        """
        normalized = cls.normalize_cookie(raw_cookie)
        bxauth = cls.extract_bxauth(normalized)
        if not bxauth:
            raise ValueError("Cookie 必须包含 BXAuth 字段")
        return f"BXAuth={bxauth};"

    async def _get_client(self) -> BaseUpstreamTransport:
        """获取或创建上游传输层客户端。"""
        if self._client is None:
            from .settings import get_effective_upstream_transport_backend, load_settings

            settings = load_settings()
            proxy = settings.upstream_proxy if settings.upstream_proxy_enabled and settings.upstream_proxy else None
            self._client = create_upstream_transport(
                backend=get_effective_upstream_transport_backend(settings),
                timeout=30.0,
                follow_redirects=True,
                proxy=proxy,
                trust_env=False,
                impersonate=settings.tls_impersonate,
            )
        return self._client

    async def close(self):
        """关闭 HTTP 客户端"""
        if self._client:
            await self._client.close()
            self._client = None

    async def get_token(
        self, code: str, redirect_uri: str = "http://localhost:11451/oauth2callback"
    ) -> Dict[str, Any]:
        """
        使用授权码获取 OAuth token

        Args:
            code: OAuth 授权码
            redirect_uri: 回调地址

        Returns:
            包含 access_token, refresh_token, expires_in 等字段的字典

        Raises:
            ValueError: 响应数据格式错误
        """
        client = await self._get_client()

        # 使用 Basic Auth
        credentials = base64.b64encode(
            f"{self.CLIENT_ID}:{self.CLIENT_SECRET}".encode()
        ).decode()

        response = await client.post(
            self.TOKEN_URL,
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect_uri,
                "client_id": self.CLIENT_ID,
                "client_secret": self.CLIENT_SECRET,
            },
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
                "Authorization": f"Basic {credentials}",
            },
            timeout=30.0,
        )
        response.raise_for_status()

        token_data = response.json()

        if "access_token" not in token_data:
            raise ValueError("OAuth 响应缺少 access_token")

        if "expires_in" in token_data:
            expires_in = token_data["expires_in"]
            token_data["expires_at"] = datetime.now() + timedelta(seconds=expires_in)

        return token_data

    async def refresh_token(self, refresh_token: str) -> Dict[str, Any]:
        """刷新 token
        
        注意：iFlow 服务器可能返回 HTTP 200 但响应体中 success=false 的情况，
        这通常表示服务器过载，需要重试。
        """
        client = await self._get_client()

        credentials = base64.b64encode(
            f"{self.CLIENT_ID}:{self.CLIENT_SECRET}".encode()
        ).decode()

        response = await client.post(
            self.TOKEN_URL,
            data={
                "grant_type": "refresh_token",
                "client_id": self.CLIENT_ID,
                "client_secret": self.CLIENT_SECRET,
                "refresh_token": refresh_token,
            },
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
                "Authorization": f"Basic {credentials}",
                            },
            timeout=30.0,
        )

        if response.status_code == 400:
            error_data = response.json()
            if "invalid_grant" in error_data.get("error", ""):
                raise ValueError("refresh_token 无效或已过期")

        response.raise_for_status()

        token_data = response.json()

        # 检查 iFlow 特有的响应格式：HTTP 200 但 success=false
        if token_data.get("success") is False:
            error_msg = token_data.get("message", "未知错误")
            error_code = token_data.get("code", "")
            # 服务器过载错误，需要重试
            if "太多" in error_msg or error_code == "500":
                raise ValueError(f"服务器过载: {error_msg}")
            else:
                raise ValueError(f"OAuth 刷新失败: {error_msg}")

        if "access_token" not in token_data:
            raise ValueError("OAuth 响应缺少 access_token")

        if "expires_in" in token_data:
            expires_in = token_data["expires_in"]
            token_data["expires_at"] = datetime.now() + timedelta(seconds=expires_in)

        return token_data

    async def get_user_info(self, access_token: str) -> Dict[str, Any]:
        """
        获取用户信息（包含 API Key）

        Args:
            access_token: 访问令牌

        Returns:
            用户信息字典

        Raises:
            ValueError: 响应数据格式错误或 access_token 无效
        """
        client = await self._get_client()

        # iFlow API 要求 accessToken 作为 URL 查询参数传递
        # 参考 iflow-cli 实现
        response = await client.get(
            f"{self.USER_INFO_URL}?accessToken={access_token}",
            headers={
                "Accept": "application/json",
                "User-Agent": "iFlow-Cli",
            },
            timeout=30.0,
        )

        if response.status_code == 401:
            raise ValueError("access_token 无效或已过期")

        response.raise_for_status()

        result = response.json()

        if result.get("success") and result.get("data"):
            return result["data"]
        else:
            raise ValueError("获取用户信息失败")

    def get_auth_url(
        self,
        redirect_uri: str = "http://localhost:11451/oauth2callback",
        state: Optional[str] = None,
    ) -> str:
        """
        生成 OAuth 授权 URL

        Args:
            redirect_uri: 回调地址
            state: CSRF 防护令牌

        Returns:
            OAuth 授权 URL
        """
        if state is None:
            state = secrets.token_urlsafe(16)

        return (
            f"{self.AUTH_URL}?"
            f"client_id={self.CLIENT_ID}&"
            f"loginMethod=phone&"
            f"type=phone&"
            f"redirect={redirect_uri}&"
            f"state={state}"
        )

    async def validate_token(self, access_token: str) -> bool:
        """验证 access_token 是否有效"""
        try:
            await self.get_user_info(access_token)
            return True
        except Exception:
            return False

    async def refresh_api_key_with_cookie(
        self,
        cookie: str,
        email: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        使用 BXAuth cookie 刷新 API Key

        Args:
            cookie: BXAuth cookie 字符串（必须包含 BXAuth=xxx）
            email: 用户邮箱（可选；不传时会先通过 GET 接口自动获取 name）

        Returns:
            包含 apiKey、expired、email 的字典
        """
        normalized_cookie = self.cookie_for_storage(cookie)
        resolved_email = (email or "").strip()
        client = await self._get_client()

        # email 未提供时，先通过 GET 接口获取 name（对齐 cpa 行为）
        if not resolved_email:
            key_info = await self.get_api_key_info_with_cookie(normalized_cookie)
            resolved_email = (key_info.get("name") or "").strip()
            if not resolved_email:
                raise ValueError("无法自动获取邮箱/名称，请检查 Cookie 是否有效")

        return await self._refresh_api_key_with_cookie(
            client=client,
            cookie=normalized_cookie,
            email=resolved_email,
        )

    async def get_api_key_info_with_cookie(self, cookie: str) -> Dict[str, Any]:
        """
        使用 BXAuth cookie 获取当前 API Key 信息（GET）。

        返回 name 字段用于后续刷新请求。
        """
        normalized_cookie = self.cookie_for_storage(cookie)
        client = await self._get_client()

        response = await client.get(
            self.API_KEY_ENDPOINT,
            headers={
                "Cookie": normalized_cookie,
                "Accept": "application/json, text/plain, */*",
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36",
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                "Accept-Encoding": "gzip, deflate, br",
                "Connection": "keep-alive",
                "Referer": "https://platform.iflow.cn/",
            },
            timeout=30.0,
        )

        if response.status_code == 401:
            raise ValueError("Cookie 无效或已过期")
        response.raise_for_status()

        result = response.json()
        if result.get("success") and result.get("data"):
            data = result["data"]
            return {
                "apiKey": data.get("apiKey", "") or data.get("apiKeyMask", ""),
                "expired": data.get("expired", "") or data.get("expireTime", ""),
                "name": data.get("name", ""),
            }

        error_msg = result.get("message", "未知错误")
        raise ValueError(f"获取 API Key 信息失败: {error_msg}")

    async def _refresh_api_key_with_cookie(
        self,
        client: BaseUpstreamTransport,
        cookie: str,
        email: str,
    ) -> Dict[str, Any]:
        """执行 Cookie 刷新请求（POST）。"""
        request_body = {"name": email}

        response = await client.post(
            self.API_KEY_ENDPOINT,
            json_body=request_body,
            headers={
                "Cookie": cookie,
                "Content-Type": "application/json",
                "Accept": "application/json, text/plain, */*",
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36",
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                "Accept-Encoding": "gzip, deflate, br",
                "Connection": "keep-alive",
                "Origin": "https://platform.iflow.cn",
                "Referer": "https://platform.iflow.cn/",
            },
            timeout=30.0,
        )

        if response.status_code == 401:
            raise ValueError("Cookie 无效或已过期")

        response.raise_for_status()

        result = response.json()

        # 检查响应格式
        if result.get("success") and result.get("data"):
            data = result["data"]
            return {
                "apiKey": data.get("apiKey", ""),
                "expired": data.get("expired", "") or data.get("expireTime", ""),
                "email": email,
            }
        else:
            error_msg = result.get("message", "未知错误")
            raise ValueError(f"刷新 API Key 失败: {error_msg}")

    def is_token_expired(
        self, expires_at: Optional[datetime], buffer_seconds: int = 300
    ) -> bool:
        """检查 token 是否即将过期"""
        if expires_at is None:
            return False
        return datetime.now() >= (expires_at - timedelta(seconds=buffer_seconds))
