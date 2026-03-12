"""Web 管理界面路由"""
import platform
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel

from .auth import get_auth_manager
from .websocket import get_connection_manager


# 创建路由器
admin_router = APIRouter(prefix="/admin", tags=["Admin"])

# HTTP Bearer 认证方案
security = HTTPBearer(auto_error=False)


# 请求/响应模型
class LoginRequest(BaseModel):
    """登录请求"""
    username: str
    password: str


class ChangePasswordRequest(BaseModel):
    """修改密码请求"""
    old_password: str
    new_password: str


class CreateUserRequest(BaseModel):
    """创建用户请求"""
    username: str
    password: str


class SettingsUpdate(BaseModel):
    """设置更新请求"""
    host: Optional[str] = None
    port: Optional[int] = None
    theme_mode: Optional[str] = None
    preserve_reasoning_content: Optional[bool] = None
    api_concurrency: Optional[int] = None
    language: Optional[str] = None
    api_key: Optional[str] = None
    base_url: Optional[str] = None
    custom_api_key: Optional[str] = None
    custom_auth_header: Optional[str] = None
    # 上游代理设置
    upstream_proxy: Optional[str] = None
    upstream_proxy_enabled: Optional[bool] = None


class OAuthCallbackRequest(BaseModel):
    """OAuth 回调请求"""
    code: str
    state: Optional[str] = None


class CookieLoginRequest(BaseModel):
    """Cookie 登录请求"""
    cookie: str


class UpstreamAccountCreateRequest(BaseModel):
    """新增上游账号请求。"""

    label: Optional[str] = None
    api_key: str
    base_url: Optional[str] = None


class UpstreamAccountToggleRequest(BaseModel):
    """启停上游账号请求。"""

    enabled: bool


# 认证依赖
async def get_current_user(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
) -> str:
    """获取当前认证用户"""
    if credentials is None:
        raise HTTPException(status_code=401, detail="未提供认证令牌")
    
    auth_manager = get_auth_manager()
    username = auth_manager.verify_token(credentials.credentials)
    
    if username is None:
        raise HTTPException(status_code=401, detail="无效或过期的令牌")
    
    return username


# ==================== 认证相关 ====================

@admin_router.post("/login")
async def login(request: LoginRequest) -> dict[str, Any]:
    """用户登录"""
    auth_manager = get_auth_manager()
    
    # 如果没有用户，创建第一个用户
    if not auth_manager.has_users():
        auth_manager.create_user(request.username, request.password)
        token = auth_manager.authenticate(request.username, request.password)
        return {
            "success": True,
            "token": token,
            "message": "首次登录，已创建管理员账户",
            "is_first_login": True,
        }
    
    token = auth_manager.authenticate(request.username, request.password)
    if token is None:
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    
    return {
        "success": True,
        "token": token,
        "message": "登录成功",
        "is_first_login": False,
    }


@admin_router.post("/logout")
async def logout(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
) -> dict[str, Any]:
    """用户登出"""
    if credentials:
        auth_manager = get_auth_manager()
        auth_manager.logout(credentials.credentials)
    
    return {"success": True, "message": "已登出"}


@admin_router.post("/change-password")
async def change_password(
    request: ChangePasswordRequest,
    username: str = Depends(get_current_user),
) -> dict[str, Any]:
    """修改密码"""
    auth_manager = get_auth_manager()
    success = auth_manager.change_password(username, request.old_password, request.new_password)
    
    if not success:
        raise HTTPException(status_code=400, detail="原密码错误")
    
    return {"success": True, "message": "密码已修改"}


@admin_router.get("/check-setup")
async def check_setup() -> dict[str, Any]:
    """检查是否需要初始化设置"""
    auth_manager = get_auth_manager()
    return {
        "needs_setup": not auth_manager.has_users(),
        "has_users": auth_manager.has_users(),
    }


# ==================== 用户管理 ====================

@admin_router.get("/users")
async def get_users(username: str = Depends(get_current_user)) -> list[dict]:
    """获取用户列表"""
    auth_manager = get_auth_manager()
    return auth_manager.get_users()


@admin_router.post("/users")
async def create_user(
    request: CreateUserRequest,
    username: str = Depends(get_current_user),
) -> dict[str, Any]:
    """创建新用户"""
    auth_manager = get_auth_manager()
    success = auth_manager.create_user(request.username, request.password)
    
    if not success:
        raise HTTPException(status_code=400, detail="用户名已存在")
    
    return {"success": True, "message": "用户已创建"}


@admin_router.delete("/users/{target_username}")
async def delete_user(
    target_username: str,
    username: str = Depends(get_current_user),
) -> dict[str, Any]:
    """删除用户"""
    if target_username == username:
        raise HTTPException(status_code=400, detail="不能删除自己")
    
    auth_manager = get_auth_manager()
    success = auth_manager.delete_user(target_username)
    
    if not success:
        raise HTTPException(status_code=404, detail="用户不存在")
    
    return {"success": True, "message": "用户已删除"}


# ==================== 系统状态 ====================

def _check_service_health(port: int, host: str = "127.0.0.1") -> tuple[bool, str]:
    """
    检查服务健康状态（L-04 修复：改用非阻塞 socket 替代同步 http.client，
    避免在 asyncio event loop 中阻塞）

    Returns:
        (is_healthy, error_message)
    """
    import socket

    # 只做端口连通性检查（纯 socket，非阻塞）
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(1)
            result = s.connect_ex((host, port))
            if result != 0:
                return False, f"端口 {port} 未监听"
            return True, ""
    except Exception as e:
        return False, f"端口检查失败: {str(e)}"


@admin_router.get("/status")
async def get_status(username: str = Depends(get_current_user)) -> dict[str, Any]:
    """获取系统状态"""
    from ..settings import load_settings

    # 获取配置的端口
    settings = load_settings()
    configured_port = settings.port

    # 实际检查服务健康状态
    is_healthy, health_error = _check_service_health(configured_port)

    # 获取系统信息
    system_info = {
        "platform": platform.system(),
        "platform_version": platform.version(),
        "python_version": sys.version,
        "architecture": platform.machine(),
    }
    
    # 获取进程信息
    process_info = {
        "start_time": _get_process_start_time(),
        "uptime": time.time() - _process_start_time,
    }
    
    # 获取连接管理器状态
    connection_manager = get_connection_manager()
    
    return {
        "server": {
            "state": "running" if is_healthy else "error",
            "error_message": "" if is_healthy else health_error,
            "configured_port": configured_port,
        },
        "system": system_info,
        "process": process_info,
        "connections": {
            "websocket_count": connection_manager.connection_count,
        },
    }


# ==================== 配置管理 ====================

def _mask_secret(value: str, head: int = 8, tail: int = 4) -> str:
    """掩码敏感字符串，避免在页面中直接泄露完整凭据。"""
    raw = (value or "").strip()
    if not raw:
        return ""
    if len(raw) <= 2:
        return raw[0] + "*"
    if len(raw) <= head + tail:
        keep = max(1, len(raw) // 3)
        return f"{raw[:keep]}...{raw[-tail:]}"
    return f"{raw[:head]}...{raw[-tail:]}"


def _serialize_upstream_account(account, *, primary_account_id: str) -> dict[str, Any]:
    """序列化账号池条目给前端使用。"""
    from ..concurrency_limiter import get_concurrency_limiter
    from ..settings import DEFAULT_BASE_URL

    stats = get_concurrency_limiter().get_stats(account.id)
    return {
        "id": account.id,
        "label": account.label,
        "enabled": account.enabled,
        "is_primary": account.id == primary_account_id,
        "auth_type": account.auth_type,
        "api_key_masked": _mask_secret(account.api_key),
        "base_url": account.base_url or DEFAULT_BASE_URL,
        "email": account.email or account.cookie_email,
        "phone": account.phone,
        "cookie_expires_at": account.cookie_expires_at,
        "created_at": account.created_at,
        "updated_at": account.updated_at,
        "stats": stats,
    }


@admin_router.get("/account-info")
async def get_account_info(username: str = Depends(get_current_user)) -> dict[str, Any]:
    """获取账号池概览与主账号信息。"""
    from ..settings import get_enabled_upstream_accounts, get_primary_account, list_upstream_accounts, load_settings

    settings = load_settings()
    accounts = list_upstream_accounts(settings)
    primary_account = get_primary_account(settings, include_disabled=True)

    if not accounts or primary_account is None:
        return {
            "auth_type": "not_logged_in",
            "has_api_key": False,
            "api_key_masked": "",
            "accounts": [],
            "total_accounts": 0,
            "enabled_accounts": 0,
        }

    account_info = {
        "auth_type": primary_account.auth_type or "not_logged_in",
        "has_api_key": bool((primary_account.api_key or "").strip()),
        "api_key_masked": _mask_secret(primary_account.api_key),
        "email": primary_account.email or primary_account.cookie_email,
        "phone": primary_account.phone,
        "cookie_expires_at": primary_account.cookie_expires_at,
        "accounts": [
            _serialize_upstream_account(account, primary_account_id=settings.primary_account_id)
            for account in accounts
        ],
        "total_accounts": len(accounts),
        "enabled_accounts": len(get_enabled_upstream_accounts(settings)),
        "primary_account_id": settings.primary_account_id,
    }
    return account_info


@admin_router.get("/settings")
async def get_settings(username: str = Depends(get_current_user)) -> dict[str, Any]:
    """获取应用设置"""
    from ..settings import load_settings

    settings = load_settings()
    return {
        "host": settings.host,
        "port": settings.port,
        "theme_mode": settings.theme_mode,
        "preserve_reasoning_content": settings.preserve_reasoning_content,
        "api_concurrency": settings.api_concurrency,
        "language": settings.language,
        "api_key": settings.api_key,
        "base_url": settings.base_url,
        "custom_api_key": settings.custom_api_key,
        "custom_auth_header": settings.custom_auth_header,
        # 上游代理设置
        "upstream_proxy": settings.upstream_proxy,
        "upstream_proxy_enabled": settings.upstream_proxy_enabled,
        "primary_account_id": settings.primary_account_id,
        # 不返回 OAuth 敏感信息
    }


@admin_router.put("/settings")
async def update_settings(
    request: SettingsUpdate,
    username: str = Depends(get_current_user),
) -> dict[str, Any]:
    """更新应用设置"""
    from ..settings import get_primary_account, load_settings, save_settings, upsert_upstream_account
    
    settings = load_settings()
    
    # 更新设置
    if request.host is not None:
        settings.host = request.host
    if request.port is not None:
        settings.port = request.port
    if request.theme_mode is not None:
        settings.theme_mode = request.theme_mode
    if request.preserve_reasoning_content is not None:
        settings.preserve_reasoning_content = request.preserve_reasoning_content
    if request.api_concurrency is not None:
        settings.api_concurrency = request.api_concurrency
    if request.language is not None:
        settings.language = request.language
    if request.api_key is not None:
        if settings.upstream_accounts:
            primary_account = get_primary_account(settings, include_disabled=True)
            if primary_account and request.api_key.strip():
                primary_account.api_key = request.api_key.strip()
                upsert_upstream_account(settings, primary_account, make_primary=True)
        else:
            settings.api_key = request.api_key
    if request.base_url is not None:
        if settings.upstream_accounts:
            primary_account = get_primary_account(settings, include_disabled=True)
            if primary_account and request.base_url.strip():
                primary_account.base_url = request.base_url.strip()
                upsert_upstream_account(settings, primary_account, make_primary=True)
        else:
            settings.base_url = request.base_url
    if request.custom_api_key is not None:
        settings.custom_api_key = request.custom_api_key
    if request.custom_auth_header is not None:
        settings.custom_auth_header = request.custom_auth_header
    # 上游代理设置
    if request.upstream_proxy is not None:
        settings.upstream_proxy = request.upstream_proxy
    if request.upstream_proxy_enabled is not None:
        settings.upstream_proxy_enabled = request.upstream_proxy_enabled
    
    save_settings(settings)
    
    # 广播设置变更
    connection_manager = get_connection_manager()
    await connection_manager.broadcast({
        "type": "settings_updated",
        "timestamp": datetime.now().isoformat(),
    })

    from ..app import reload_proxy

    reload_proxy()
    
    return {"success": True, "message": "设置已保存"}


# ==================== iFlow 配置 ====================

@admin_router.get("/upstream-accounts")
async def get_upstream_accounts(username: str = Depends(get_current_user)) -> dict[str, Any]:
    """获取上游账号池列表。"""
    from ..settings import list_upstream_accounts, load_settings

    settings = load_settings()
    accounts = list_upstream_accounts(settings)
    return {
        "accounts": [
            _serialize_upstream_account(account, primary_account_id=settings.primary_account_id)
            for account in accounts
        ],
        "primary_account_id": settings.primary_account_id,
    }


@admin_router.post("/upstream-accounts")
async def create_upstream_account(
    request: UpstreamAccountCreateRequest,
    username: str = Depends(get_current_user),
) -> dict[str, Any]:
    """手动新增 API Key 账号。"""
    from ..settings import DEFAULT_BASE_URL, UpstreamAccount, load_settings, save_settings, upsert_upstream_account

    settings = load_settings()
    account = UpstreamAccount(
        label=(request.label or "").strip() or "",
        auth_type="api-key",
        api_key=request.api_key.strip(),
        base_url=(request.base_url or settings.base_url or DEFAULT_BASE_URL).strip() or DEFAULT_BASE_URL,
    )
    saved_account = upsert_upstream_account(settings, account, make_primary=not settings.primary_account_id)
    save_settings(settings)

    from ..app import reload_proxy

    reload_proxy()
    return {
        "success": True,
        "message": "账号已添加到账号池",
        "account": _serialize_upstream_account(saved_account, primary_account_id=settings.primary_account_id),
    }


@admin_router.patch("/upstream-accounts/{account_id}/enabled")
async def toggle_upstream_account(
    account_id: str,
    request: UpstreamAccountToggleRequest,
    username: str = Depends(get_current_user),
) -> dict[str, Any]:
    """启用或停用上游账号。"""
    from ..settings import build_legacy_account, load_settings, save_settings

    settings = load_settings()
    if not settings.upstream_accounts and account_id == "legacy-primary":
        legacy_account = build_legacy_account(settings)
        if legacy_account is None:
            raise HTTPException(status_code=404, detail="账号不存在")
        legacy_account.enabled = request.enabled
        settings.upstream_accounts = [legacy_account]

    for account in settings.upstream_accounts:
        if account.id == account_id:
            account.enabled = request.enabled
            save_settings(settings)
            from ..app import reload_proxy

            reload_proxy()
            return {"success": True, "message": "账号状态已更新"}

    raise HTTPException(status_code=404, detail="账号不存在")


@admin_router.post("/upstream-accounts/{account_id}/activate")
async def activate_upstream_account(
    account_id: str,
    username: str = Depends(get_current_user),
) -> dict[str, Any]:
    """切换当前主账号。"""
    from ..settings import build_legacy_account, load_settings, save_settings

    settings = load_settings()
    if not settings.upstream_accounts and account_id == "legacy-primary":
        legacy_account = build_legacy_account(settings)
        if legacy_account is None:
            raise HTTPException(status_code=404, detail="账号不存在")
        settings.upstream_accounts = [legacy_account]
    if settings.upstream_accounts and not any(account.id == account_id for account in settings.upstream_accounts):
        raise HTTPException(status_code=404, detail="账号不存在")

    settings.primary_account_id = account_id
    save_settings(settings)

    from ..app import reload_proxy

    reload_proxy()
    return {"success": True, "message": "主账号已切换"}


@admin_router.delete("/upstream-accounts/{account_id}")
async def delete_upstream_account(
    account_id: str,
    username: str = Depends(get_current_user),
) -> dict[str, Any]:
    """删除上游账号。"""
    from ..settings import DEFAULT_BASE_URL, load_settings, remove_upstream_account, save_settings

    settings = load_settings()
    if not settings.upstream_accounts and account_id == "legacy-primary":
        settings.primary_account_id = ""
        settings.api_key = ""
        settings.base_url = DEFAULT_BASE_URL
        settings.auth_type = "api-key"
        settings.oauth_access_token = ""
        settings.oauth_refresh_token = ""
        settings.oauth_expires_at = None
        settings.cookie = ""
        settings.cookie_email = ""
        settings.cookie_expires_at = None
        save_settings(settings)
        from ..app import reload_proxy

        reload_proxy()
        return {"success": True, "message": "账号已删除"}

    if not remove_upstream_account(settings, account_id):
        raise HTTPException(status_code=404, detail="账号不存在")

    save_settings(settings)

    from ..app import reload_proxy

    reload_proxy()
    return {"success": True, "message": "账号已删除"}

@admin_router.get("/oauth/url")
async def get_oauth_url(
    request: Request,
    username: str = Depends(get_current_user),
) -> dict[str, Any]:
    """获取 iFlow OAuth 登录 URL"""
    from ..oauth import IFlowOAuth
    from ..settings import load_settings

    settings = load_settings()
    oauth = IFlowOAuth()

    # 构建回调地址
    if settings.oauth_callback_base_url:
        # 使用配置的公网地址
        redirect_uri = f"{settings.oauth_callback_base_url.rstrip('/')}/admin/oauth/callback"
    else:
        # 使用 localhost（默认行为）
        port = request.url.port or 28000
        redirect_uri = f"http://localhost:{port}/admin/oauth/callback"

    auth_url = oauth.get_auth_url(redirect_uri=redirect_uri)

    return {
        "success": True,
        "auth_url": auth_url,
        "redirect_uri": redirect_uri,
    }


@admin_router.get("/oauth/callback")
async def oauth_callback_get(code: str, state: Optional[str] = None):
    """处理 OAuth 回调（GET 请求 - 从 iFlow 重定向回来）
    
    返回一个 HTML 页面，通过 postMessage 将授权码发送回父窗口
    """
    from fastapi.responses import HTMLResponse
    
    html_content = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>OAuth 回调</title>
        <style>
            body {{
                font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
                display: flex;
                justify-content: center;
                align-items: center;
                height: 100vh;
                margin: 0;
                background: #f5f5f5;
            }}
            .container {{
                text-align: center;
                padding: 40px;
                background: white;
                border-radius: 8px;
                box-shadow: 0 2px 10px rgba(0,0,0,0.1);
            }}
            .spinner {{
                width: 40px;
                height: 40px;
                border: 3px solid #f3f3f3;
                border-top: 3px solid #3498db;
                border-radius: 50%;
                animation: spin 1s linear infinite;
                margin: 0 auto 20px;
            }}
            @keyframes spin {{
                0% {{ transform: rotate(0deg); }}
                100% {{ transform: rotate(360deg); }}
            }}
        </style>
    </head>
    <body>
        <div class="container">
            <div class="spinner"></div>
            <p>正在处理登录...</p>
        </div>
        <script>
            // 将授权码发送回父窗口
            if (window.opener) {{
                window.opener.postMessage({{
                    type: 'oauth_callback',
                    code: '{code}',
                    state: '{state or ''}'
                }}, '*');
                // 关闭当前窗口
                setTimeout(function() {{
                    window.close();
                }}, 1000);
            }} else {{
                // 如果没有 opener，显示错误
                document.querySelector('.container').innerHTML =
                    '<p style="color: red;">错误：无法与父窗口通信</p>' +
                    '<p>请手动关闭此窗口</p>';
            }}
        </script>
    </body>
    </html>
    """
    return HTMLResponse(content=html_content)


@admin_router.post("/cookie/login")
async def cookie_login(
    request: CookieLoginRequest,
    username: str = Depends(get_current_user),
) -> dict[str, Any]:
    """使用 BXAuth Cookie 登录"""
    from ..oauth import IFlowOAuth
    from ..settings import UpstreamAccount, load_settings, save_settings, upsert_upstream_account

    oauth = IFlowOAuth()

    try:
        # 使用 cookie 刷新 API Key
        key_data = await oauth.refresh_api_key_with_cookie(request.cookie)

        api_key = key_data.get("apiKey", "")
        expired = key_data.get("expired", "")
        resolved_email = key_data.get("email", "")

        if not api_key:
            return {
                "success": False,
                "message": "获取 API Key 失败"
            }

        settings = load_settings()
        account = UpstreamAccount(
            label=resolved_email.strip() or "Cookie 账号",
            auth_type="cookie",
            api_key=api_key,
            base_url=settings.base_url,
            cookie=IFlowOAuth.cookie_for_storage(request.cookie),
            cookie_email=resolved_email.strip(),
            cookie_expires_at=expired or None,
            email=resolved_email.strip(),
        )
        saved_account = upsert_upstream_account(settings, account, make_primary=True)
        save_settings(settings)

        # 重新加载代理实例
        from ..app import reload_proxy
        reload_proxy()

        return {
            "success": True,
            "message": "Cookie 登录成功",
            "data": {
                "account_id": saved_account.id,
                "expired": expired,
                "email": resolved_email,
            }
        }

    except ValueError as e:
        return {
            "success": False,
            "message": str(e)
        }
    except Exception as e:
        return {
            "success": False,
            "message": f"Cookie 登录失败: {str(e)}"
        }


@admin_router.post("/oauth/callback")
async def oauth_callback(
    callback_request: OAuthCallbackRequest,
    fastapi_request: Request,
    username: str = Depends(get_current_user),
) -> dict[str, Any]:
    """处理 OAuth 回调（POST 请求 - 从前端发送）"""
    from ..oauth import IFlowOAuth
    from ..settings import UpstreamAccount, load_settings, save_settings, upsert_upstream_account

    settings = load_settings()
    oauth = IFlowOAuth()

    # 构建回调地址（必须与获取 auth_url 时一致）
    if settings.oauth_callback_base_url:
        redirect_uri = f"{settings.oauth_callback_base_url.rstrip('/')}/admin/oauth/callback"
    else:
        port = fastapi_request.url.port or 28000
        redirect_uri = f"http://localhost:{port}/admin/oauth/callback"
    
    try:
        # 使用授权码获取 token
        token_data = await oauth.get_token(callback_request.code, redirect_uri=redirect_uri)
        access_token = token_data.get("access_token")
        
        if not access_token:
            raise HTTPException(status_code=400, detail="OAuth 响应缺少 access_token")
        
        # 获取用户信息（包含 API Key）
        user_info = await oauth.get_user_info(access_token)
        api_key = user_info.get("apiKey")
        
        if not api_key:
            raise HTTPException(status_code=400, detail="无法获取 API Key")
        
        settings = load_settings()
        account = UpstreamAccount(
            label=(user_info.get("email") or user_info.get("phone") or "OAuth 账号").strip(),
            auth_type="oauth-iflow",
            api_key=api_key,
            base_url=settings.base_url,
            oauth_access_token=access_token,
            oauth_refresh_token=token_data.get("refresh_token", "") or "",
            oauth_expires_at=token_data["expires_at"].isoformat() if token_data.get("expires_at") else None,
            email=(user_info.get("email") or "").strip(),
            phone=(user_info.get("phone") or "").strip(),
        )
        saved_account = upsert_upstream_account(settings, account, make_primary=True)
        save_settings(settings)

        # 重新加载代理实例
        from ..app import reload_proxy
        reload_proxy()

        return {
            "success": True,
            "message": "登录成功！配置已自动更新",
            "api_key": api_key,
            "account_id": saved_account.id,
        }
        
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"OAuth 登录失败: {str(e)}")

# ==================== 日志查看 ====================

@admin_router.get("/logs")
async def get_logs(
    lines: int = 100,
    username: str = Depends(get_current_user),
) -> dict[str, Any]:
    """获取日志"""
    log_path = Path.home() / ".iflow2api" / "logs" / "app.log"
    
    if not log_path.exists():
        return {"logs": [], "message": "日志文件不存在"}
    
    try:
        with open(log_path, "r", encoding="utf-8") as f:
            all_lines = f.readlines()
            recent_lines = all_lines[-lines:] if len(all_lines) > lines else all_lines
        
        return {
            "logs": [line.strip() for line in recent_lines],
            "total_lines": len(all_lines),
        }
    except Exception as e:
        return {"logs": [], "error": str(e)}


# ==================== WebSocket ====================

@admin_router.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    """WebSocket 连接端点（M-10 修复：连接建立时即验证 Token）"""
    # 在 HTTP Upgrade 阶段验证 token（来自查询参数）
    token = websocket.query_params.get("token")
    if not token:
        await websocket.close(code=4001, reason="Missing token")
        return

    auth_manager = get_auth_manager()
    username = auth_manager.verify_token(token)
    if not username:
        await websocket.close(code=4001, reason="Invalid or expired token")
        return

    connection_manager = get_connection_manager()
    await connection_manager.connect(websocket)

    try:
        while True:
            data = await websocket.receive_json()

            # 处理心跳
            if data.get("type") == "ping":
                await connection_manager.send_personal(websocket, {
                    "type": "pong",
                    "timestamp": datetime.now().isoformat(),
                })
            # 支持旧版客户端通过消息中的 auth 命令认证（向后兼容）
            elif data.get("type") == "auth":
                await connection_manager.send_personal(websocket, {
                    "type": "auth_success",
                    "username": username,
                })
    except WebSocketDisconnect:
        await connection_manager.disconnect(websocket)
    except Exception:
        await connection_manager.disconnect(websocket)


# ==================== 辅助函数 ====================

# 进程启动时间
_process_start_time = time.time()


def _get_process_start_time() -> str:
    """获取进程启动时间"""
    return datetime.fromtimestamp(_process_start_time).isoformat()
