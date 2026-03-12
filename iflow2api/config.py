"""iflow2api 运行时配置读取器。"""

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from pydantic import BaseModel, Field

from .crypto import ConfigEncryption

logger = logging.getLogger("iflow2api")

DEFAULT_BASE_URL = "https://apis.iflow.cn/v1"


class IFlowConfig(BaseModel):
    """运行时使用的上游配置。"""

    api_key: str
    base_url: str = DEFAULT_BASE_URL
    model_name: Optional[str] = None
    cna: Optional[str] = None
    installation_id: Optional[str] = None
    auth_type: Optional[str] = Field(
        default=None,
        description="认证类型: oauth-iflow, cookie, api-key, openai-compatible",
    )
    oauth_access_token: Optional[str] = Field(default=None, description="OAuth 访问令牌")
    oauth_refresh_token: Optional[str] = Field(default=None, description="OAuth 刷新令牌")
    oauth_expires_at: Optional[datetime] = Field(default=None, description="OAuth token 过期时间")
    api_key_expires_at: Optional[datetime] = Field(
        default=None,
        description="apiKey 过期时间（OAuth 模式下与 token 同步）",
    )
    cookie: Optional[str] = Field(default=None, description="Cookie 登录凭据（仅 BXAuth）")
    cookie_email: Optional[str] = Field(default=None, description="Cookie 登录绑定邮箱")
    cookie_expires_at: Optional[str] = Field(
        default=None,
        description="Cookie 模式下 apiKey 过期时间（原始字符串）",
    )


def get_app_config_path() -> Path:
    """获取应用配置文件路径。"""
    return Path.home() / ".iflow2api" / "config.json"


def _decrypt_token(value: Any) -> str:
    """解密 enc: 前缀的敏感字段。"""
    if not isinstance(value, str) or not value:
        return ""
    if not value.startswith("enc:"):
        return value

    encryption = ConfigEncryption()
    if not encryption.is_available:
        return value

    try:
        return encryption.decrypt(value[4:])
    except ValueError:
        logger.warning("配置字段解密失败，将保留原始值")
        return value


def _parse_datetime(value: Any) -> Optional[datetime]:
    """解析 ISO 时间字符串。"""
    if not value or not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _select_primary_account(data: dict[str, Any]) -> Optional[dict[str, Any]]:
    """从账号池中选出当前主账号。"""
    raw_accounts = data.get("upstream_accounts")
    if not isinstance(raw_accounts, list) or not raw_accounts:
        return None

    accounts = [account for account in raw_accounts if isinstance(account, dict)]
    if not accounts:
        return None

    primary_account_id = (data.get("primary_account_id") or "").strip()
    accounts_by_id = {
        str(account.get("id") or "").strip(): account
        for account in accounts
        if str(account.get("id") or "").strip()
    }

    if primary_account_id and primary_account_id in accounts_by_id:
        return accounts_by_id[primary_account_id]

    for account in accounts:
        if account.get("enabled", True) and str(account.get("api_key") or "").strip():
            return account

    for account in accounts:
        if str(account.get("api_key") or "").strip():
            return account

    return accounts[0]


def _build_config_from_mapping(raw: dict[str, Any]) -> IFlowConfig:
    """把配置字典转换为 IFlowConfig。"""
    oauth_expires_at = _parse_datetime(raw.get("oauth_expires_at"))

    api_key = (
        str(raw.get("api_key") or raw.get("apiKey") or raw.get("searchApiKey") or "")
        .strip()
    )
    base_url = str(raw.get("base_url") or raw.get("baseUrl") or DEFAULT_BASE_URL).strip() or DEFAULT_BASE_URL

    return IFlowConfig(
        api_key=api_key,
        base_url=base_url,
        model_name=(raw.get("model_name") or raw.get("modelName") or None),
        cna=(raw.get("cna") or None),
        installation_id=None,
        auth_type=(raw.get("auth_type") or raw.get("selectedAuthType") or None),
        oauth_access_token=_decrypt_token(raw.get("oauth_access_token")) or None,
        oauth_refresh_token=_decrypt_token(raw.get("oauth_refresh_token")) or None,
        oauth_expires_at=oauth_expires_at,
        api_key_expires_at=oauth_expires_at,
        cookie=_decrypt_token(raw.get("cookie")) or None,
        cookie_email=(raw.get("cookie_email") or None),
        cookie_expires_at=(raw.get("cookie_expires_at") or None),
    )


def load_iflow_config() -> IFlowConfig:
    """从 `~/.iflow2api/config.json` 加载运行时主账号。"""
    config_path = get_app_config_path()
    if not config_path.exists():
        raise FileNotFoundError(
            f"iflow2api 配置文件不存在: {config_path}\n请先通过 WebUI 完成登录"
        )

    try:
        with open(config_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except json.JSONDecodeError as e:
        raise ValueError(f"iflow2api 配置文件格式错误: {e}") from e

    config_data = _select_primary_account(data) or data
    config = _build_config_from_mapping(config_data)

    if not config.api_key:
        raise ValueError("iflow2api 配置中缺少 API Key\n请先通过 WebUI 完成登录")

    return config


def check_iflow_login() -> bool:
    """检查 iflow2api 是否已配置上游账号。"""
    try:
        return bool(load_iflow_config().api_key)
    except (FileNotFoundError, ValueError):
        return False
