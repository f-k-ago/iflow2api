"""FastAPI 应用 - OpenAI 兼容 API 服务 + Anthropic 兼容"""

import sys
import json
import logging
import os as _os
import asyncio
import time
from contextlib import asynccontextmanager, suppress
from typing import Any, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict

from .account_pool import NoUpstreamAccountError, UpstreamQueueFullError, acquire_account_lease
from .anthropic_compat import (
    anthropic_to_openai_request,
    openai_to_anthropic_response,
)
from .config import load_iflow_config, check_iflow_login, IFlowConfig
from .messages_adapter import create_anthropic_streaming_response
from .proxy import IFlowProxy
from .token_refresher import OAuthTokenRefresher
from .upstream_diagnostics import (
    log_upstream_failure,
    log_upstream_request_context,
)
from .vision import (
    get_vision_models_list,
)
from .version import get_version, get_startup_info, get_diagnostic_info, is_docker

logger = logging.getLogger("iflow2api")

# 全局代理实例
_proxy: Optional[IFlowProxy] = None
_config: Optional[IFlowConfig] = None
_refresher: Optional[OAuthTokenRefresher] = None

# 上游 API 并发信号量 - 在 lifespan 中根据配置初始化
_api_request_lock: Optional[asyncio.Semaphore] = None


class IFlowNotConfiguredError(Exception):
    """iFlow 未配置异常"""
    pass


def reload_proxy() -> None:
    """重新加载代理实例（用于配置更新后刷新）"""
    global _proxy, _config
    if _proxy:
        asyncio.create_task(_proxy.close())
    _proxy = None
    _config = None


def get_proxy() -> IFlowProxy:
    """获取代理实例。"""
    global _proxy, _config
    if _proxy is None:
        try:
            _config = load_iflow_config()
        except (FileNotFoundError, ValueError) as e:
            raise IFlowNotConfiguredError(
                "iFlow 未配置，请通过 WebUI 完成登录: http://localhost:28000/admin"
            ) from e

        _proxy = IFlowProxy(_config)
    return _proxy


def update_proxy_token(token_data: dict):
    """Token 刷新回调，刷新运行时缓存。"""
    account_id = token_data.get("account_id")
    auth_type = token_data.get("auth_type") or "unknown"
    logger.info("检测到账号凭据刷新: account_id=%s, auth_type=%s", account_id, auth_type)
    reload_proxy()


def _extract_upstream_error(exc: Exception) -> tuple[int, str, str]:
    """提取上游异常中的状态码和错误消息。"""
    error_msg = str(exc)
    status_code = 500
    error_type = "api_error"

    custom_status_code = getattr(exc, "status_code", None)
    if custom_status_code is not None:
        status_code = custom_status_code
        error_type = getattr(exc, "error_type", error_type)

    response = getattr(exc, "response", None)
    if response is not None:
        try:
            status_code = response.status_code
            error_data = response.json()
            error_msg = error_data.get("msg", error_msg)
        except Exception:
            pass
    return _normalize_error_status_code(status_code), error_msg, error_type


def _normalize_error_status_code(status_code: object) -> int:
    """将上游错误状态码规范为合法 HTTP 错误码。"""
    try:
        normalized = int(status_code)
    except (TypeError, ValueError):
        normalized = 502

    if 400 <= normalized <= 599:
        return normalized

    logger.warning("检测到非法上游状态码 %r，已回退为 502", status_code)
    return 502


def _map_upstream_exception(
    exc: Exception,
    *,
    lease: Any = None,
    request_body: Optional[dict[str, Any]] = None,
    endpoint: str = "unknown",
    stream: bool = False,
) -> JSONResponse:
    """将上游异常映射为兼容响应。"""
    status_code, error_msg, error_type = _extract_upstream_error(exc)
    log_upstream_failure(
        exc,
        status_code=status_code,
        error_msg=error_msg,
        error_type=error_type,
        lease=lease,
        request_body=request_body,
        endpoint=endpoint,
        stream=stream,
    )
    return create_error_response(status_code, error_msg, error_type)


async def _acquire_upstream_lease():
    try:
        return await acquire_account_lease()
    except NoUpstreamAccountError as exc:
        raise IFlowNotConfiguredError(str(exc)) from exc


def _get_upstream_busy_message(exc: BaseException) -> str:
    """提取账号池忙碌/排队相关提示。"""
    message = str(exc).strip()
    return message or "当前所有上游账号都在忙，请稍后重试"


def _create_openai_busy_response(exc: BaseException) -> JSONResponse:
    """返回 OpenAI 兼容的 429 忙碌响应。"""
    return create_error_response(429, _get_upstream_busy_message(exc), "rate_limit_exceeded")


def _create_anthropic_busy_response(exc: BaseException) -> JSONResponse:
    """返回 Anthropic 兼容的 429 忙碌响应。"""
    return JSONResponse(
        status_code=429,
        content={
            "type": "error",
            "error": {
                "type": "rate_limit_exceeded",
                "message": _get_upstream_busy_message(exc),
            },
        },
    )


async def _run_nonstream_with_lease(lease, request_body: dict) -> dict:
    """执行非流式请求；客户端取消时等待上游运行结束后再释放账号。"""
    task = asyncio.create_task(
        lease.proxy.chat_completions(
            request_body,
            stream=False,
            apply_concurrency_limit=False,
        )
    )
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        logger.info("客户端取消非流式请求，等待上游运行完成后释放账号: %s", lease.account.id)
        with suppress(Exception):
            await task
        raise


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期管理
    
    支持无配置启动：如果 iFlow 配置不存在，服务仍可启动，
    用户可通过 WebUI OAuth 登录完成配置。
    """
    global _refresher, _proxy, _api_request_lock, _config
    # 启动时打印版本和系统信息
    logger.info("%s", get_startup_info())
    
    # 初始化设置与并发信号量
    from .settings import get_enabled_upstream_accounts, load_settings
    settings = load_settings()
    enabled_accounts = get_enabled_upstream_accounts(settings)
    effective_concurrency = max(len(enabled_accounts), 1)
    _api_request_lock = asyncio.Semaphore(effective_concurrency)
    logger.info(
        "账号池状态: enabled=%d, configured_total=%d, runtime_concurrency=%d",
        len(enabled_accounts),
        len(settings.upstream_accounts),
        effective_concurrency,
    )
    
    # 尝试加载当前主账号配置（可选）
    try:
        config = load_iflow_config()
        _config = config
        logger.info("已加载 iFlow 配置")
        logger.info("API Base URL: %s", config.base_url)
        logger.info("API Key: ****%s (masked)", config.api_key[-4:])
        if config.model_name:
            logger.info("默认模型: %s", config.model_name)
            
    except FileNotFoundError:
        logger.warning("iflow2api 配置文件不存在，请通过 WebUI 完成登录")
        logger.warning("访问 http://localhost:%d/admin 进入管理界面", settings.port)
    except ValueError as e:
        logger.warning("iflow2api 配置无效: %s", e)
        logger.warning("请通过 WebUI 重新登录")

    # 启动 Token 刷新任务（无账号时也允许启动，后台会自行跳过）
    _refresher = OAuthTokenRefresher()
    _refresher.set_refresh_callback(update_proxy_token)
    _refresher.start()
    logger.info("已启动 Token 自动刷新任务")

    yield

    # 关闭时清理
    if _refresher:
        _refresher.stop()
        _refresher = None
        
    if _proxy:
        await _proxy.close()
        _proxy = None


# 创建 FastAPI 应用
app = FastAPI(
    title="iflow2api",
    description="""
## iflow2api - iFlow AI 服务代理

将 iFlow 账号服务暴露为 OpenAI 兼容 API，支持多种 AI 模型。

### 功能特性

- **OpenAI 兼容 API**: 支持 `/v1/chat/completions` 端点
- **Anthropic 兼容 API**: 支持 `/v1/messages` 端点（Claude Code 兼容）
- **多模型支持**: GLM-4.6/4.7/5、DeepSeek-V3.2、Qwen3-Coder-Plus、Kimi-K2/K2.5、MiniMax-M2.5
- **流式响应**: 支持 SSE 流式输出
- **OAuth 认证**: 支持 iFlow OAuth 登录

### 支持的模型

| 模型 ID | 名称 | 说明 |
|---------|------|------|
| `glm-4.6` | GLM-4.6 | 智谱 GLM-4.6 |
| `glm-4.7` | GLM-4.7 | 智谱 GLM-4.7 |
| `glm-5` | GLM-5 | 智谱 GLM-5 (推荐) |
| `deepseek-v3.2-chat` | DeepSeek-V3.2 | DeepSeek V3.2 对话模型 |
| `qwen3-coder-plus` | Qwen3-Coder-Plus | 通义千问 Qwen3 Coder |
| `kimi-k2` | Kimi-K2 | Moonshot Kimi K2 |
| `kimi-k2.5` | Kimi-K2.5 | Moonshot Kimi K2.5 |
| `minimax-m2.5` | MiniMax-M2.5 | MiniMax M2.5 |

### 使用方式

1. 部署并启动 Docker 容器
2. 访问 `/admin` 完成 API Key、OAuth 或 Cookie 登录
3. 配置客户端使用 `http://localhost:28000/v1` 作为 API 端点
""",
version=get_version(),
lifespan=lifespan,
    redirect_slashes=True,  # 自动处理末尾斜杠
    docs_url="/docs",  # Swagger UI
    redoc_url="/redoc",  # ReDoc
    openapi_url="/openapi.json",  # OpenAPI schema
)

# 添加 CORS 中间件（H-05 修复：不再同时使用通配符 origin + credentials）
# 默认允许所有来源但不携带凭据；如需限制来源请在此列举
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,  # RFC 禁止 allow_origins=["*"] + allow_credentials=True
    allow_methods=["*"],
    allow_headers=["*"],
)


# ============ 请求体大小限制中间件 ============

_MAX_REQUEST_BODY_SIZE = 10 * 1024 * 1024  # 10 MB（H-07 修复）

@app.middleware("http")
async def limit_request_body(request: Request, call_next):
    """拒绝超大请求体，防止内存耗尽 DoS（H-07 修复）"""
    content_length = request.headers.get("content-length")
    if content_length and int(content_length) > _MAX_REQUEST_BODY_SIZE:
        return JSONResponse(
            status_code=413,
            content={"error": {"message": "Request body too large", "type": "invalid_request_error"}},
        )
    return await call_next(request)


# ============ 自定义 API 鉴权中间件 ============

# 简单内存缓存，减少每次请求读磁盘（H-08 修复）
_settings_cache: dict = {"data": None, "ts": 0.0}
_SETTINGS_CACHE_TTL = 5.0  # 5 秒内复用缓存


def _get_cached_settings():
    """获取缓存的设置，超过 TTL 才重新读盘"""
    import time as _time
    from .settings import load_settings
    now = _time.monotonic()
    if _settings_cache["data"] is None or now - _settings_cache["ts"] > _SETTINGS_CACHE_TTL:
        _settings_cache["data"] = load_settings()
        _settings_cache["ts"] = now
    return _settings_cache["data"]


@app.middleware("http")
async def custom_auth_middleware(request: Request, call_next):
    """自定义 API 鉴权中间件

    如果配置了 custom_api_key，则验证请求头中的授权信息
    支持 "Bearer {key}" 和 "{key}" 两种格式
    """
    # 跳过健康检查、文档等路由
    skip_paths = ["/health", "/docs", "/redoc", "/openapi.json", "/admin"]
    if any(request.url.path.startswith(path) for path in skip_paths):
        return await call_next(request)

    # 使用缓存的设置，避免每次请求读磁盘（H-08 修复）
    settings = _get_cached_settings()
    
    # 如果未设置 custom_api_key，则跳过验证
    if not settings.custom_api_key:
        return await call_next(request)
    
    # 获取授权标头
    auth_header_name = settings.custom_auth_header or "Authorization"
    auth_value = request.headers.get(auth_header_name)
    
    # 验证授权信息
    if not auth_value:
        return JSONResponse(
            status_code=401,
            content={
                "error": {
                    "message": f"Missing {auth_header_name} header",
                    "type": "authentication_error",
                    "code": "missing_api_key",
                }
            },
        )
    
    # 提取实际的 key（支持 "Bearer {key}" 和 "{key}" 格式）
    actual_key = auth_value
    if auth_value.startswith("Bearer "):
        actual_key = auth_value[7:]  # 移除 "Bearer " 前缀
    
    # 验证 key（使用常数时间比较防止时序攻击）
    import hmac as _hmac
    if not _hmac.compare_digest(actual_key, settings.custom_api_key):
        return JSONResponse(
            status_code=401,
            content={
                "error": {
                    "message": "Invalid API key",
                    "type": "authentication_error",
                    "code": "invalid_api_key",
                }
            },
        )
    
    # 验证通过，继续处理请求
    return await call_next(request)


# ============ 管理界面 ============

# 挂载静态文件目录
_admin_static_dir = _os.path.join(_os.path.dirname(__file__), "admin", "static")
if _os.path.exists(_admin_static_dir):
    app.mount("/admin/static", StaticFiles(directory=_admin_static_dir), name="admin_static")


@app.get("/admin", response_class=HTMLResponse, tags=["Admin"])
@app.get("/admin/", response_class=HTMLResponse, tags=["Admin"])
async def admin_page():
    """管理界面入口"""
    index_path = _os.path.join(_admin_static_dir, "index.html")
    if _os.path.exists(index_path):
        with open(index_path, "r", encoding="utf-8") as f:
            return f.read()
    return HTMLResponse(content="<h1>管理界面未找到</h1>", status_code=404)


# 注册管理界面路由
try:
    from .admin.routes import admin_router
    app.include_router(admin_router)
except ImportError as e:
    logger.warning("无法加载管理界面路由: %s", e)


@app.middleware("http")
async def log_requests(request: Request, call_next):
    """记录请求信息，包括请求体大小和响应时间"""
    start_time = time.time()
    
    # 获取请求体大小（仅对 POST/PUT/PATCH 请求）
    body_size = 0
    if request.method in ("POST", "PUT", "PATCH"):
        content_length = request.headers.get("content-length")
        if content_length:
            body_size = int(content_length)
    
    # 格式化请求体大小
    def format_size(size: int) -> str:
        if size < 1024:
            return f"{size}B"
        elif size < 1024 * 1024:
            return f"{size/1024:.1f}KB"
        else:
            return f"{size/1024/1024:.1f}MB"
    
    logger.info("Request: %s %s%s", request.method, request.url.path,
                 f" ({format_size(body_size)})" if body_size > 0 else "")
    
    if request.method == "OPTIONS":
        # 显式处理 OPTIONS 请求以确保 CORS 正常
        response = await call_next(request)
        return response
    
    response = await call_next(request)
    
    # 计算响应时间
    elapsed_ms = (time.time() - start_time) * 1000
    logger.info("Response: %d (%.0fms)", response.status_code, elapsed_ms)
    
    # 如果返回 405，打印更多调试信息
    if response.status_code == 405:
        logger.debug("路径 %s 不支持 %s 方法", request.url.path, request.method)
        logger.debug("当前已注册的 POST 路由包括: /v1/chat/completions, /v1/messages, / 等")
        
    return response


# ============ 请求/响应模型 ============

class ChatMessage(BaseModel):
    """聊天消息"""
    role: str
    content: Any  # 可以是字符串或内容块列表


class ChatCompletionRequest(BaseModel):
    """Chat Completions API 请求体"""
    model: str
    messages: list[ChatMessage]
    temperature: Optional[float] = None
    top_p: Optional[float] = None
    max_tokens: Optional[int] = None
    stream: Optional[bool] = False
    stop: Optional[list[str] | str] = None
    presence_penalty: Optional[float] = None
    frequency_penalty: Optional[float] = None
    user: Optional[str] = None

    model_config = ConfigDict(extra="allow")


# ============ 示例请求 ============

OPENAI_CHAT_EXAMPLE = {
    "model": "glm-5",
    "messages": [
        {"role": "system", "content": "你是一个有帮助的助手。"},
        {"role": "user", "content": "你好，请介绍一下你自己。"}
    ],
    "temperature": 0.7,
    "max_tokens": 1024,
    "stream": False
}

OPENAI_CHAT_STREAM_EXAMPLE = {
    "model": "glm-5",
    "messages": [
        {"role": "user", "content": "写一首关于春天的诗。"}
    ],
    "stream": True
}

ANTHROPIC_MESSAGES_EXAMPLE = {
    "model": "claude-sonnet-4-5-20250929",
    "max_tokens": 1024,
    "system": "你是一个有帮助的助手。",
    "messages": [
        {"role": "user", "content": "你好，请介绍一下你自己。"}
    ]
}


# ============ API 端点 ============

@app.get(
    "/",
    summary="根路径",
    description="返回服务基本信息和可用端点列表",
    response_description="服务信息",
    tags=["基本信息"],
)
async def root():
    """根路径"""
    return {
        "service": "iflow2api",
        "version": get_version(),
        "description": "iFlow AI 服务 → OpenAI 兼容 API",
        "endpoints": {
            "models": "/v1/models",
            "chat_completions": "/v1/chat/completions",
            "messages": "/v1/messages",
            "health": "/health",
            "docs": "/docs",
            "redoc": "/redoc",
        },
    }


@app.get(
    "/health",
    summary="健康检查",
    description="检查服务健康状态和 iFlow 登录状态",
    response_description="健康状态",
    tags=["基本信息"],
)
async def health():
    """健康检查"""
    is_logged_in = check_iflow_login()
    diagnostic = get_diagnostic_info()
    return {
        "status": "healthy" if is_logged_in else "degraded",
        "iflow_logged_in": is_logged_in,
        "version": diagnostic["version"],
        "os": diagnostic["os"],
        "platform": diagnostic["platform"]["system"],
        "architecture": diagnostic["platform"]["architecture"],
        "python": diagnostic["platform"]["python_version"],
        "runtime": diagnostic["runtime"],
        "docker": diagnostic["docker"],
        "kubernetes": diagnostic["kubernetes"],
        "wsl": diagnostic["wsl"],
    }


@app.get(
    "/v1/models",
    summary="获取模型列表",
    description="获取所有可用的 AI 模型列表",
    response_description="模型列表",
    tags=["模型"],
)
async def list_models():
    """获取可用模型列表"""
    try:
        proxy = get_proxy()
        return await proxy.get_models()
    except IFlowNotConfiguredError:
        logger.info("iFlow 未配置，/v1/models 回退返回静态模型列表")
        return IFlowProxy.build_models_response()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get(
    "/v1/vision-models",
    summary="获取视觉模型列表",
    description="获取所有支持图像输入的视觉模型列表",
    response_description="视觉模型列表",
    tags=["模型"],
)
async def list_vision_models():
    """获取支持视觉功能的模型列表"""
    vision_models = get_vision_models_list()
    return {
        "object": "list",
        "data": [
            {
                "id": model["id"],
                "object": "model",
                "owned_by": model["provider"],
                "supports_vision": True,
                "max_images": model["max_images"],
            }
            for model in vision_models
        ],
    }


def create_error_response(status_code: int, message: str, error_type: str = "api_error") -> JSONResponse:
    """创建 OpenAI 兼容的错误响应"""
    normalized_status_code = _normalize_error_status_code(status_code)
    return JSONResponse(
        status_code=normalized_status_code,
        content={
            "error": {
                "message": message,
                "type": error_type,
                "param": None,
                "code": str(normalized_status_code)
            }
        }
    )


@app.post(
    "/v1/chat/completions",
    summary="Chat Completions API (OpenAI 格式)",
    description="""
OpenAI 兼容的 Chat Completions API 端点。

支持流式和非流式响应。使用 `stream: true` 启用流式输出。

**支持的模型**: glm-4.6, glm-4.7, glm-5, deepseek-v3.2-chat, qwen3-coder-plus, kimi-k2, kimi-k2.5, minimax-m2.5
""",
    response_description="Chat completion 响应",
    tags=["Chat"],
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {
                "application/json": {
                    "schema": {
                        "type": "object",
                        "required": ["model", "messages"],
                        "properties": {
                            "model": {
                                "type": "string",
                                "description": "模型 ID",
                                "example": "glm-5",
                            },
                            "messages": {
                                "type": "array",
                                "description": "对话消息列表",
                                "items": {
                                    "type": "object",
                                    "required": ["role", "content"],
                                    "properties": {
                                        "role": {
                                            "type": "string",
                                            "enum": ["system", "user", "assistant"],
                                            "description": "消息角色",
                                        },
                                        "content": {
                                            "type": "string",
                                            "description": "消息内容",
                                        },
                                    },
                                },
                            },
                            "temperature": {
                                "type": "number",
                                "minimum": 0,
                                "maximum": 2,
                                "description": "采样温度",
                            },
                            "top_p": {
                                "type": "number",
                                "minimum": 0,
                                "maximum": 1,
                                "description": "核采样参数",
                            },
                            "max_tokens": {
                                "type": "integer",
                                "minimum": 1,
                                "description": "最大生成 token 数",
                            },
                            "stream": {
                                "type": "boolean",
                                "description": "是否启用流式输出",
                                "default": False,
                            },
                            "stop": {
                                "oneOf": [
                                    {"type": "string"},
                                    {"type": "array", "items": {"type": "string"}},
                                ],
                                "description": "停止序列",
                            },
                        },
                    },
                    "examples": {
                        "basic": {
                            "summary": "基本对话",
                            "value": OPENAI_CHAT_EXAMPLE,
                        },
                        "streaming": {
                            "summary": "流式输出",
                            "value": OPENAI_CHAT_STREAM_EXAMPLE,
                        },
                    },
                }
            },
        }
    },
)
@app.post("/v1/chat/completions/")
@app.post("/chat/completions")
@app.post("/chat/completions/")
@app.post("/api/v1/chat/completions")
@app.post("/api/v1/chat/completions/")
async def chat_completions_openai(request: Request):
    """Chat Completions API - OpenAI 格式"""
    try:
        body_bytes = await request.body()
        body = json.loads(body_bytes.decode("utf-8"))
        if "messages" not in body:
            return create_error_response(422, "Field 'messages' is required", "invalid_request_error")
        stream = body.get("stream", False)
        model = body.get("model", "unknown")
        msg_count = len(body.get("messages", []))
        has_tools = "tools" in body
        logger.info("Chat请求: model=%s, stream=%s, messages=%d, has_tools=%s",
                     model, stream, msg_count, has_tools)

        if stream:
            try:
                lease = await _acquire_upstream_lease()
            except IFlowNotConfiguredError as e:
                return create_error_response(503, str(e), "iflow_not_configured")
            except (UpstreamQueueFullError, asyncio.TimeoutError) as e:
                return _create_openai_busy_response(e)

            log_upstream_request_context(
                lease,
                body,
                endpoint="openai.chat_completions",
                stream=True,
            )

            try:
                stream_gen = await lease.proxy.chat_completions(
                    body,
                    stream=True,
                    apply_concurrency_limit=False,
                )
            except Exception as e:
                await lease.close()
                return _map_upstream_exception(
                    e,
                    lease=lease,
                    request_body=body,
                    endpoint="openai.chat_completions",
                    stream=True,
                )

            async def generate_with_lease():
                """持有账号租约直到整个流式响应结束。"""
                chunk_count = 0
                cancelled = False
                try:
                    async for chunk in stream_gen:
                        chunk_count += 1
                        if chunk_count <= 3:
                            logger.debug("流式chunk[%d]: %s", chunk_count, chunk[:200])
                        yield chunk
                except asyncio.CancelledError:
                    cancelled = True
                    logger.info("客户端取消流式请求，立即释放账号令牌: %s", lease.account.id)
                    raise
                except Exception as e:
                    logger.error("Streaming error after %d chunks: %s", chunk_count, e)
                else:
                    if chunk_count > 0:
                        yield b"data: [DONE]\n\n"
                finally:
                    logger.debug("流式完成: account=%s, chunks=%d", lease.account.id, chunk_count)
                    if not cancelled and chunk_count == 0:
                        logger.warning(
                            "生成错误回退响应 (0 chunks from upstream): account=%s, label=%s, model=%s",
                            lease.account.id,
                            lease.account.label,
                            model,
                        )
                        import time as _time

                        fallback = {
                            "id": f"fallback-{int(_time.time())}",
                            "object": "chat.completion.chunk",
                            "created": int(_time.time()),
                            "model": model,
                            "choices": [{
                                "index": 0,
                                "delta": {
                                    "role": "assistant",
                                    "content": "上游流式响应为空（0 chunks）。这通常是上游未返回任何 SSE 数据，常见于上下文超限、模型侧异常或上游代理吞流，请查看服务端日志。"
                                },
                                "finish_reason": "stop"
                            }]
                        }
                        yield ("data: " + json.dumps(fallback, ensure_ascii=False) + "\n\n").encode("utf-8")
                        yield b"data: [DONE]\n\n"
                    await lease.close()

            return StreamingResponse(
                generate_with_lease(),
                media_type="text/event-stream",
                headers={
                    "Cache-Control": "no-cache",
                    "Connection": "keep-alive",
                    "X-Accel-Buffering": "no",
                },
            )
        else:
            try:
                lease = await _acquire_upstream_lease()
            except IFlowNotConfiguredError as e:
                return create_error_response(503, str(e), "iflow_not_configured")
            except (UpstreamQueueFullError, asyncio.TimeoutError) as e:
                return _create_openai_busy_response(e)

            try:
                log_upstream_request_context(
                    lease,
                    body,
                    endpoint="openai.chat_completions",
                    stream=False,
                )
                result = await _run_nonstream_with_lease(lease, body)
            except Exception as e:
                return _map_upstream_exception(
                    e,
                    lease=lease,
                    request_body=body,
                    endpoint="openai.chat_completions",
                    stream=False,
                )
            finally:
                await lease.close()
            # 验证响应包含有效的 choices
            if not result.get("choices"):
                logger.error("API 响应缺少 choices 数组: %s", json.dumps(result, ensure_ascii=False)[:500])
                return create_error_response(500, "API 响应格式错误: 缺少 choices 数组")
            
            # 日志输出关键信息
            msg = result["choices"][0].get("message", {})
            content = msg.get("content")
            reasoning = msg.get("reasoning_content")
            tool_calls = msg.get("tool_calls")
            logger.debug("非流式响应: content=%r, reasoning=%s, tool_calls=%s",
                         content[:80] if content else None,
                         '有' if reasoning else '无',
                         '有' if tool_calls else '无')
            
            return JSONResponse(content=result)

    except json.JSONDecodeError as e:
        return create_error_response(400, f"Invalid JSON: {e}", "invalid_request_error")
    except Exception as e:
        return _map_upstream_exception(
            e,
            request_body=body if "body" in locals() and isinstance(body, dict) else None,
            endpoint="openai.chat_completions",
            stream=bool(body.get("stream", False)) if "body" in locals() and isinstance(body, dict) else False,
        )


@app.post(
    "/v1/messages",
    summary="Messages API (Anthropic 格式)",
    description="""
Anthropic 兼容的 Messages API 端点，支持 Claude Code 等客户端。

请求格式与 Anthropic API 兼容，会自动转换为 OpenAI 格式并映射到 iFlow 模型。

**模型映射**: Claude 系列模型会自动映射到 glm-5，也可直接指定 iFlow 模型 ID。
""",
    response_description="Messages 响应",
    tags=["Chat"],
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {
                "application/json": {
                    "schema": {
                        "type": "object",
                        "required": ["model", "max_tokens", "messages"],
                        "properties": {
                            "model": {
                                "type": "string",
                                "description": "模型 ID (Claude 系列会自动映射到 glm-5)",
                                "example": "claude-sonnet-4-5-20250929",
                            },
                            "max_tokens": {
                                "type": "integer",
                                "minimum": 1,
                                "description": "最大生成 token 数",
                            },
                            "system": {
                                "oneOf": [
                                    {"type": "string"},
                                    {
                                        "type": "array",
                                        "items": {
                                            "type": "object",
                                            "properties": {
                                                "type": {"type": "string", "enum": ["text"]},
                                                "text": {"type": "string"},
                                            },
                                        },
                                    },
                                ],
                                "description": "系统提示词",
                            },
                            "messages": {
                                "type": "array",
                                "description": "对话消息列表",
                                "items": {
                                    "type": "object",
                                    "required": ["role", "content"],
                                    "properties": {
                                        "role": {
                                            "type": "string",
                                            "enum": ["user", "assistant"],
                                            "description": "消息角色",
                                        },
                                        "content": {
                                            "oneOf": [
                                                {"type": "string"},
                                                {
                                                    "type": "array",
                                                    "items": {
                                                        "type": "object",
                                                        "properties": {
                                                            "type": {
                                                                "type": "string",
                                                                "enum": ["text", "image"],
                                                            },
                                                            "text": {"type": "string"},
                                                        },
                                                    },
                                                },
                                            ],
                                            "description": "消息内容",
                                        },
                                    },
                                },
                            },
                            "temperature": {
                                "type": "number",
                                "minimum": 0,
                                "maximum": 1,
                                "description": "采样温度",
                            },
                            "top_p": {
                                "type": "number",
                                "minimum": 0,
                                "maximum": 1,
                                "description": "核采样参数",
                            },
                            "stream": {
                                "type": "boolean",
                                "description": "是否启用流式输出",
                                "default": False,
                            },
                            "stop_sequences": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "停止序列",
                            },
                        },
                    },
                    "examples": {
                        "basic": {
                            "summary": "基本对话",
                            "value": ANTHROPIC_MESSAGES_EXAMPLE,
                        },
                    },
                }
            },
        }
    },
)
@app.post("/v1/messages/")
@app.post("/messages")
@app.post("/messages/")
@app.post("/api/v1/messages")
@app.post("/api/v1/messages/")
async def messages_anthropic(request: Request):
    """Messages API - Anthropic 格式（Claude Code 兼容）"""
    try:
        body_bytes = await request.body()
        body = json.loads(body_bytes.decode("utf-8"))
        if "messages" not in body:
            return JSONResponse(
                status_code=422,
                content={"type": "error", "error": {"type": "invalid_request_error", "message": "Field 'messages' is required"}}
            )
        stream = body.get("stream", False)
        original_model = body.get("model", "unknown")
        
        # 将 Anthropic 请求体转换为 OpenAI 格式
        openai_body = anthropic_to_openai_request(body)
        mapped_model = openai_body["model"]
        
        logger.info("Anthropic 格式请求: model=%s → %s, stream=%s",
                     original_model, mapped_model, stream)

        if stream:
            # 加载配置以获取思考链设置
            from .settings import load_settings
            settings = load_settings()
            preserve_reasoning = settings.preserve_reasoning_content
            try:
                lease = await _acquire_upstream_lease()
            except IFlowNotConfiguredError as e:
                return JSONResponse(
                    status_code=503,
                    content={"type": "error", "error": {"type": "iflow_not_configured", "message": str(e)}}
                )
            except (UpstreamQueueFullError, asyncio.TimeoutError) as e:
                return _create_anthropic_busy_response(e)

            log_upstream_request_context(
                lease,
                openai_body,
                endpoint="anthropic.messages",
                stream=True,
            )
            try:
                stream_gen = await lease.proxy.chat_completions(
                    openai_body,
                    stream=True,
                    apply_concurrency_limit=False,
                )
            except Exception as e:
                await lease.close()
                status_code, error_msg, error_type = _extract_upstream_error(e)
                log_upstream_failure(
                    e,
                    status_code=status_code,
                    error_msg=error_msg,
                    error_type=error_type,
                    lease=lease,
                    request_body=openai_body,
                    endpoint="anthropic.messages",
                    stream=True,
                )
                return JSONResponse(
                    status_code=status_code,
                    content={"type": "error", "error": {"type": error_type, "message": error_msg}},
                )
            async def close_lease() -> None:
                await lease.close()

            return create_anthropic_streaming_response(
                stream_gen,
                mapped_model=mapped_model,
                preserve_reasoning=preserve_reasoning,
                on_close=close_lease,
            )
        else:
            try:
                lease = await _acquire_upstream_lease()
            except IFlowNotConfiguredError as e:
                return JSONResponse(
                    status_code=503,
                    content={"type": "error", "error": {"type": "iflow_not_configured", "message": str(e)}}
                )
            except (UpstreamQueueFullError, asyncio.TimeoutError) as e:
                return _create_anthropic_busy_response(e)

            try:
                log_upstream_request_context(
                    lease,
                    openai_body,
                    endpoint="anthropic.messages",
                    stream=False,
                )
                openai_result = await _run_nonstream_with_lease(lease, openai_body)
            except Exception as e:
                status_code, error_msg, error_type = _extract_upstream_error(e)
                log_upstream_failure(
                    e,
                    status_code=status_code,
                    error_msg=error_msg,
                    error_type=error_type,
                    lease=lease,
                    request_body=openai_body,
                    endpoint="anthropic.messages",
                    stream=False,
                )
                return JSONResponse(
                    status_code=status_code,
                    content={"type": "error", "error": {"type": error_type, "message": error_msg}},
                )
            finally:
                await lease.close()
            logger.debug("收到 OpenAI 格式响应: %s", json.dumps(openai_result, ensure_ascii=False)[:300])
            anthropic_result = openai_to_anthropic_response(openai_result, mapped_model)
            first_block = anthropic_result['content'][0] if anthropic_result['content'] else {}
            first_preview = first_block.get('text') or first_block.get('name') or ''
            logger.debug("Anthropic 格式响应: id=%s, stop_reason=%s, preview=%s",
                         anthropic_result['id'], anthropic_result['stop_reason'], first_preview[:80])
            return JSONResponse(content=anthropic_result)

    except json.JSONDecodeError as e:
        raise HTTPException(status_code=400, detail=f"Invalid JSON: {e}")
    except Exception as e:
        error_msg = str(e)
        resp = getattr(e, "response", None)
        if resp is not None:
            try:
                error_data = resp.json()
                error_msg = error_data.get("msg", error_msg)
            except Exception:
                pass
        # Anthropic 格式的错误响应
        error_response = {
            "type": "error",
            "error": {
                "type": "api_error",
                "message": error_msg
            }
        }
        return JSONResponse(content=error_response, status_code=500)


@app.post("/")
@app.post("/v1/")
async def root_post(request: Request):
    """根路径 POST - 尝试自动检测格式"""
    try:
        body_bytes = await request.body()
        body = json.loads(body_bytes.decode("utf-8"))
        
        # 简单启发式：如果请求中没有 choices 相关字段，默认使用 Anthropic 格式
        # 因为 CCR 主要使用 Anthropic 格式
        # 但为了安全起见，默认使用 OpenAI 格式
        stream = body.get("stream", False)
        if stream:
            try:
                lease = await _acquire_upstream_lease()
            except IFlowNotConfiguredError as e:
                return create_error_response(503, str(e), "iflow_not_configured")
            except (UpstreamQueueFullError, asyncio.TimeoutError) as e:
                return _create_openai_busy_response(e)

            log_upstream_request_context(
                lease,
                body,
                endpoint="root_post",
                stream=True,
            )
            try:
                stream_gen = await lease.proxy.chat_completions(
                    body,
                    stream=True,
                    apply_concurrency_limit=False,
                )
            except Exception as e:
                await lease.close()
                return _map_upstream_exception(
                    e,
                    lease=lease,
                    request_body=body,
                    endpoint="root_post",
                    stream=True,
                )

            async def generate_with_lease():
                """持有账号租约直到整个流式传输结束。"""
                chunk_count = 0
                try:
                    async for chunk in stream_gen:
                        chunk_count += 1
                        yield chunk
                except asyncio.CancelledError:
                    logger.info("客户端取消 root 流式请求，立即释放账号令牌: %s", lease.account.id)
                    raise
                finally:
                    logger.debug("流式完成 (root_post): account=%s, chunks=%d", lease.account.id, chunk_count)
                    await lease.close()
            
            return StreamingResponse(
                generate_with_lease(),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
            )
        else:
            try:
                lease = await _acquire_upstream_lease()
            except IFlowNotConfiguredError as e:
                return create_error_response(503, str(e), "iflow_not_configured")
            except (UpstreamQueueFullError, asyncio.TimeoutError) as e:
                return _create_openai_busy_response(e)

            try:
                log_upstream_request_context(
                    lease,
                    body,
                    endpoint="root_post",
                    stream=False,
                )
                result = await _run_nonstream_with_lease(lease, body)
            except Exception as e:
                return _map_upstream_exception(
                    e,
                    lease=lease,
                    request_body=body,
                    endpoint="root_post",
                    stream=False,
                )
            finally:
                await lease.close()
            # 验证响应包含有效的 choices
            if not result.get("choices"):
                logger.error("API 响应缺少 choices 数组 (root_post): %s", json.dumps(result, ensure_ascii=False)[:500])
                raise HTTPException(status_code=500, detail="API 响应格式错误: 缺少 choices 数组")
            return JSONResponse(content=result)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/models")
async def list_models_compat():
    """Models API - 兼容不带 /v1 前缀的请求"""
    return await list_models()


# ============ Anthropic SDK 兼容端点 ============

@app.post(
    "/api/event_logging/batch",
    summary="事件日志批量上报 (Anthropic SDK 兼容)",
    description="处理 Anthropic SDK 的事件日志请求，直接返回成功响应",
    tags=["Anthropic SDK 兼容"],
)
async def event_logging_batch(request: Request):
    """处理 Anthropic SDK 事件日志请求
    
    Anthropic SDK 会发送事件日志到这个端点，
    我们直接返回成功响应，避免 404 错误。
    """
    # 可以选择记录日志或忽略
    # body = await request.body()
    # print(f"[iflow2api] 事件日志: {body[:200]}")
    return {"status": "ok", "logged": True}


@app.post(
    "/v1/messages/count_tokens",
    summary="Token 计数 (Anthropic SDK 兼容)",
    description="估算请求的 token 数量",
    tags=["Anthropic SDK 兼容"],
)
async def count_tokens(request: Request):
    """估算 token 数量
    
    Anthropic SDK 会调用此端点估算 token 消耗。
    由于我们无法精确计算上游模型的 token 数，
    返回一个估算值。
    """
    try:
        body_bytes = await request.body()
        body = json.loads(body_bytes.decode("utf-8"))
        
        # 简单估算：计算消息文本的字符数，除以 4 得到大致的 token 数
        messages = body.get("messages", [])
        system = body.get("system", "")
        
        total_chars = len(str(system))
        for msg in messages:
            content = msg.get("content", "")
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "text":
                        total_chars += len(block.get("text", ""))
            else:
                total_chars += len(str(content))
        
        # L-06: 语言感知 token 估算：中文约 1.5 字/token，英文约 4 字/token
        cjk_chars = sum(1 for c in str(system) + "".join(
            (block.get("text", "") if isinstance(block, dict) and block.get("type") == "text" else str(msg.get("content", "")) if not isinstance(msg.get("content"), list) else "")
            for msg in messages
            for block in (msg.get("content") if isinstance(msg.get("content"), list) else [msg])
        ) if '\u4e00' <= c <= '\u9fff')
        ascii_chars = total_chars - cjk_chars
        estimated_tokens = max(1, int(cjk_chars / 1.5 + ascii_chars / 4.0))
        
        return {
            "input_tokens": estimated_tokens
        }
    except Exception as e:
        # 出错时返回一个默认值
        logger.warning("count_tokens 错误: %s", e)
        return {"input_tokens": 100}


def main():
    """主入口"""
    import argparse
    import uvicorn
    from .settings import load_settings
    from .logging_setup import setup_file_logging

    # 初始化文件日志
    setup_file_logging()

    # 解析命令行参数
    parser = argparse.ArgumentParser(
        prog='iflow2api',
        description='iFlow AI 服务代理 - OpenAI 兼容 API',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog='''
示例:
  python -m iflow2api                    # 使用默认配置启动
  python -m iflow2api --port 28001       # 指定端口
  python -m iflow2api --host 0.0.0.0     # 监听所有网卡
  python -m iflow2api --version          # 显示版本信息

配置文件位置:
  ~/.iflow2api/config.json     # 应用配置

更多信息请访问: https://github.com/cacaview/iflow2api
        '''
    )
    parser.add_argument('--host', default=None, help='监听地址 (默认: 0.0.0.0)')
    parser.add_argument('--port', type=int, default=None, help='监听端口 (默认: 28000)')
    parser.add_argument('--version', action='store_true', help='显示版本信息')
    args = parser.parse_args()

    # --version 处理
    if args.version:
        print(f"iflow2api {get_version()}")
        sys.exit(0)

    # 加载配置（需要在检查登录状态之前加载，以便获取端口信息）
    settings = load_settings()

    # 未配置时允许继续启动，后续通过 WebUI 完成账号登录
    if not check_iflow_login():
        if is_docker():
            logger.warning("iFlow 未登录，请通过 WebUI 完成登录")
        else:
            logger.warning("尚未配置上游账号，请通过 WebUI 完成登录")
        logger.warning("访问 http://localhost:%d/admin 进入管理界面", settings.port)

    # 命令行参数优先于配置文件
    host = args.host if args.host else settings.host
    port = args.port if args.port else settings.port

    # 打印启动信息
    logger.info("%s", get_startup_info())
    logger.info("  监听地址: %s:%d", host, port)

    # 显示快速入门引导
    _show_quick_start_guide(port)

    # 启动服务 - 直接传入 app 对象而非字符串，避免打包后导入失败
    try:
        uvicorn.run(
            app,
            host=host,
            port=port,
            reload=False,
            log_config=None,  # 不覆盖我们在 setup_file_logging() 中配置的日志 handler
        )
    except OSError as e:
        # 端口冲突友好提示
        if "Address already in use" in str(e) or getattr(e, 'errno', None) in (48, 98, 10048):
            logger.error("端口 %d 已被占用", port)
            logger.error("请使用 --port 指定其他端口，例如: python -m iflow2api --port %d", port + 1)
            logger.error("或修改配置文件 ~/.iflow2api/config.json 中的 port 字段")
        raise


def _show_quick_start_guide(port: int):
    """显示快速入门引导"""
    logger.info("")
    logger.info("╔══════════════════════════════════════════════════════════╗")
    logger.info("║                    快速入门指南                           ║")
    logger.info("╠══════════════════════════════════════════════════════════╣")
    logger.info("║  API 端点: http://localhost:%-5d/v1                    ║", port)
    logger.info("║  模型列表: http://localhost:%-5d/v1/models             ║", port)
    logger.info("║  管理界面: http://localhost:%-5d/admin                 ║", port)
    logger.info("║  API 文档: http://localhost:%-5d/docs                  ║", port)
    logger.info("╠══════════════════════════════════════════════════════════╣")
    logger.info("║  使用示例:                                                ║")
    logger.info("║  curl http://localhost:%-5d/v1/models                  ║", port)
    logger.info("╚══════════════════════════════════════════════════════════╝")
    logger.info("")


if __name__ == "__main__":
    main()
