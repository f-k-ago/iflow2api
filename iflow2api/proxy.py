"""API 代理服务 - 转发请求到 iFlow API"""

import hmac
import hashlib
import json
import logging
import re
import time
import uuid
import secrets
import platform
import urllib.parse

from typing import AsyncIterator, Literal, Optional, overload
from .config import IFlowConfig
from .transport import BaseUpstreamTransport, create_upstream_transport

logger = logging.getLogger("iflow2api")


# iFlow CLI 特殊 User-Agent，用于解锁更多模型
IFLOW_CLI_USER_AGENT = "iFlow-Cli"
IFLOW_CLI_VERSION = "0.5.17"  # 与官方反混淆版本保持一致

MMSTAT_GM_BASE = "https://gm.mmstat.com"
MMSTAT_VGIF_URL = "https://log.mmstat.com/v.gif"
IFLOW_USERINFO_URL = "https://iflow.cn/api/oauth/getUserInfo"
NODE_VERSION_EMULATED = "v22.22.0"


def generate_signature(user_agent: str, session_id: str, timestamp: int, api_key: str) -> str | None:
    """
    生成 iFlow API 签名 (HMAC-SHA256)
    
    签名算法来源于 iflow-cli 源码:
    - 算法: HMAC-SHA256
    - 密钥: apiKey
    - 签名内容: `{user_agent}:{session_id}:{timestamp}`
    - 输出: 十六进制字符串
    """
    if not api_key:
        return None
    message = f"{user_agent}:{session_id}:{timestamp}"
    try:
        return hmac.new(
            api_key.encode('utf-8'),
            message.encode('utf-8'),
            hashlib.sha256
        ).hexdigest()
    except Exception as e:
        logger.error("Failed to generate HMAC signature: %s", e)
        return None


class IFlowProxy:
    """iFlow API 代理"""

    def __init__(self, config: IFlowConfig):
        self.config = config
        self.base_url = config.base_url.rstrip("/")
        self._client: Optional[BaseUpstreamTransport] = None
        # 保持会话一致性
        # 与 iflow-cli 的观测结果保持一致：session-id 使用 session- 前缀
        self._session_id = f"session-{uuid.uuid4()}"
        self._conversation_id = str(uuid.uuid4())
        self._telemetry_user_id = str(uuid.uuid5(uuid.NAMESPACE_DNS, config.api_key or self._session_id))

    @staticmethod
    def _generate_traceparent() -> str:
        """生成 W3C Trace Context 的 traceparent。"""
        # 00-<32hex trace_id>-<16hex parent_id>-01
        trace_id = secrets.token_hex(16)
        parent_id = secrets.token_hex(8)
        return f"00-{trace_id}-{parent_id}-01"

    def _is_aone_endpoint(self) -> bool:
        """是否为 Aone 端点（iflow-cli 在此分支追加额外头）。"""
        return "ducky.code.alibaba-inc.com" in self.base_url.lower()

    def _get_headers(self, stream: bool = False, traceparent: Optional[str] = None) -> dict:
        """
        获取请求头（按 iflow-cli 0.5.13 关键特征对齐）。

        关键字段：
        - Content-Type
        - Authorization
        - user-agent
        - session-id
        - conversation-id
        - x-iflow-signature
        - x-iflow-timestamp
        - traceparent
        - Aone 分支: X-Client-Type / X-Client-Version

        Args:
            stream: 是否流式（保留参数以兼容现有调用）
            traceparent: 可选，显式指定 traceparent 以对齐埋点 trace_id
        """
        _ = stream

        timestamp = int(time.time() * 1000)  # 毫秒时间戳

        # 注意：按 iflow-cli 观测结果，user-agent 使用小写键名。
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.config.api_key}",
            "user-agent": IFLOW_CLI_USER_AGENT,
            "session-id": self._session_id,
            "conversation-id": self._conversation_id,
            "accept": "*/*",
            "accept-language": "*",
            "sec-fetch-mode": "cors",
            "accept-encoding": "br, gzip, deflate",
        }

        # HMAC 签名：`${userAgent}:${sessionId}:${timestamp}`
        signature = generate_signature(
            IFLOW_CLI_USER_AGENT,
            self._session_id,
            timestamp,
            self.config.api_key,
        )
        if signature:
            headers["x-iflow-signature"] = signature
            headers["x-iflow-timestamp"] = str(timestamp)

        # iflow-cli 中 traceparent 为“有则传”的可选头。
        # 同一轮请求链路中需复用同一个 traceparent（chat + telemetry）。
        headers["traceparent"] = traceparent or self._generate_traceparent()

        # Aone 分支专有头。
        if self._is_aone_endpoint():
            headers["X-Client-Type"] = "iflow-cli"
            headers["X-Client-Version"] = IFLOW_CLI_VERSION

        return headers

    @staticmethod
    def _extract_trace_id(traceparent: str) -> str:
        parts = traceparent.split("-")
        if len(parts) == 4 and len(parts[1]) == 32:
            return parts[1]
        return secrets.token_hex(16)

    @staticmethod
    def _rand_observation_id() -> str:
        return secrets.token_hex(8)

    async def _telemetry_post_gm(self, path: str, gokey: str) -> None:
        """发送 gm.mmstat 事件（run_started / run_error）。"""
        try:
            client = await self._get_client()
            await client.post(
                f"{MMSTAT_GM_BASE}{path}",
                headers={
                    "Content-Type": "application/json",
                    "accept": "*/*",
                    "accept-language": "*",
                    "sec-fetch-mode": "cors",
                    "user-agent": "node",
                    "accept-encoding": "br, gzip, deflate",
                },
                json_body={"gmkey": "AI", "gokey": gokey},
                timeout=10.0,
            )
        except Exception as e:
            logger.debug("mmstat gm event failed (%s): %s", path, e)

    async def _telemetry_post_vgif(self) -> None:
        """发送 log.mmstat.com/v.gif 事件（简化版）。"""
        try:
            client = await self._get_client()
            payload = {
                "logtype": "1",
                "title": "iFlow-CLI",
                "pre": "-",
                "platformType": "pc",
                "device_model": platform.system(),
                "os": platform.system(),
                "o": "win" if platform.system().lower().startswith("win") else platform.system().lower(),
                "node_version": NODE_VERSION_EMULATED,
                "language": "zh_CN.UTF-8",
                "interactive": "0",
                "iFlowEnv": "",
                "_g_encode": "utf-8",
                "pid": "iflow",
                "_user_id": self._telemetry_user_id,
            }
            await client.post(
                MMSTAT_VGIF_URL,
                headers={
                    "content-type": "text/plain;charset=UTF-8",
                    "cache-control": "no-cache",
                    "accept": "*/*",
                    "accept-language": "*",
                    "sec-fetch-mode": "cors",
                    "user-agent": "node",
                    "accept-encoding": "br, gzip, deflate",
                },
                data=urllib.parse.urlencode(payload),
                timeout=10.0,
            )
        except Exception as e:
            logger.debug("mmstat v.gif failed: %s", e)

    async def _emit_run_started(self, model: str, trace_id: str) -> str:
        observation_id = self._rand_observation_id()
        gokey = (
            f"pid=iflow"
            f"&sam=iflow.cli.{self._conversation_id}.{trace_id}"
            f"&trace_id={trace_id}"
            f"&session_id={self._session_id}"
            f"&conversation_id={self._conversation_id}"
            f"&observation_id={observation_id}"
            f"&model={urllib.parse.quote(model or '')}"
            f"&tool="
            f"&user_id={self._telemetry_user_id}"
        )
        await self._telemetry_post_gm("//aitrack.lifecycle.run_started", gokey)
        await self._telemetry_post_vgif()
        return observation_id

    async def _emit_run_error(self, model: str, trace_id: str, parent_observation_id: str, error_msg: str) -> None:
        observation_id = self._rand_observation_id()
        gokey = (
            f"pid=iflow"
            f"&sam=iflow.cli.{self._conversation_id}.{trace_id}"
            f"&trace_id={trace_id}"
            f"&observation_id={observation_id}"
            f"&parent_observation_id={parent_observation_id}"
            f"&session_id={self._session_id}"
            f"&conversation_id={self._conversation_id}"
            f"&user_id={self._telemetry_user_id}"
            f"&error_msg={urllib.parse.quote(error_msg)}"
            f"&model={urllib.parse.quote(model or '')}"
            f"&tool="
            f"&toolName="
            f"&toolArgs="
            f"&cliVer={IFLOW_CLI_VERSION}"
            f"&platform={urllib.parse.quote(platform.system().lower())}"
            f"&arch={urllib.parse.quote(platform.machine().lower())}"
            f"&nodeVersion={urllib.parse.quote(NODE_VERSION_EMULATED)}"
            f"&osVersion={urllib.parse.quote(platform.platform())}"
        )
        await self._telemetry_post_gm("//aitrack.lifecycle.run_error", gokey)

    async def _get_client(self) -> BaseUpstreamTransport:
        """获取或创建上游传输层客户端。"""
        if self._client is None:
            # 加载代理配置
            from .settings import load_settings
            settings = load_settings()

            proxy = settings.upstream_proxy if settings.upstream_proxy_enabled and settings.upstream_proxy else None
            self._client = create_upstream_transport(
                backend=settings.upstream_transport_backend,
                timeout=300.0,
                follow_redirects=True,
                proxy=proxy,
                trust_env=False,
                impersonate=settings.tls_impersonate,
            )

            if proxy:
                logger.info("使用上游代理: %s", proxy)
            logger.info(
                "上游传输层: backend=%s, tls_impersonate=%s",
                settings.upstream_transport_backend,
                settings.tls_impersonate,
            )

        return self._client

    @staticmethod
    def _normalize_response(result: dict, preserve_reasoning: bool = False) -> dict:
        """
        规范化 OpenAI 格式响应
        
        某些模型（如 GLM-5）使用 reasoning_content 而非 content 返回内容，
        导致 OpenAI 兼容客户端无法读取助手消息。
        
        Args:
            result: OpenAI 格式响应
            preserve_reasoning: 是否保留 reasoning_content 字段
                - False（默认）: 将 reasoning_content 合并到 content，确保兼容性
                - True: 保留 reasoning_content 字段，客户端可分别处理思考过程和最终回答
        """
        choices = result.get("choices", [])
        for choice in choices:
            message = choice.get("message", {})
            content = message.get("content")
            reasoning_content = message.get("reasoning_content")
            
            if not content and reasoning_content:
                # content 为空但 reasoning_content 有值
                if preserve_reasoning:
                    # 保留 reasoning_content，同时复制到 content 以确保兼容性
                    logger.debug("保留思考链: reasoning_content (len=%d) → content", len(reasoning_content))
                    message["content"] = reasoning_content
                else:
                    # 将 reasoning_content 移动到 content（删除原字段）
                    logger.debug("合并思考链: reasoning_content → content (len=%d)", len(reasoning_content))
                    message["content"] = reasoning_content
                    del message["reasoning_content"]
            elif content and reasoning_content:
                # 两者都有值
                if preserve_reasoning:
                    logger.debug("响应包含 content(len=%d) 和 reasoning_content(len=%d)", len(content), len(reasoning_content))
                else:
                    logger.debug("合并思考链: 删除 reasoning_content，保留 content(len=%d)", len(content))
                    del message["reasoning_content"]
            elif not content and not reasoning_content:
                logger.warning("message 中 content 和 reasoning_content 均为空，keys: %s", list(message.keys()))
        
        return result

    @staticmethod
    def _normalize_stream_chunk(chunk_data: dict, preserve_reasoning: bool = False) -> dict:
        """
        规范化流式响应中的 delta
        
        Args:
            chunk_data: OpenAI 流式响应 chunk
            preserve_reasoning: 是否保留 reasoning_content 字段
                - False（默认）: 将 reasoning_content 合并到 content，确保兼容性
                - True: 保留 reasoning_content 字段，客户端可分别处理思考过程和最终回答
        
        上游API行为（GLM-5）：
        - 流式响应：思考过程和回答分开返回
          - 大部分chunk只有 reasoning_content（思考过程）
          - 少部分chunk只有 content（最终回答）
          - 两者不会同时出现在同一个chunk中
        - 非流式响应：content 是最终回答，reasoning_content 是思考链（两者不同）
        """
        choices = chunk_data.get("choices", [])
        for choice in choices:
            delta = choice.get("delta", {})
            content = delta.get("content")
            reasoning_content = delta.get("reasoning_content")
            
            if not content and reasoning_content:
                # 只有 reasoning_content 有值（思考过程chunk）
                if preserve_reasoning:
                    # 保留 reasoning_content 字段，不复制到 content
                    # 支持思考链的客户端会读取 reasoning_content
                    # 不支持的客户端会跳过这个chunk，等回答chunk
                    pass
                else:
                    # 不保留思考链，将 reasoning_content 移动到 content
                    delta["content"] = reasoning_content
                    del delta["reasoning_content"]
            elif content and reasoning_content:
                # 两者都有值（理论上不应该发生，但做防御性处理）
                if content == reasoning_content:
                    # 内容相同，只保留 content，避免重复
                    del delta["reasoning_content"]
                elif not preserve_reasoning:
                    # 内容不同且不保留 reasoning_content
                    del delta["reasoning_content"]
                # 如果内容不同且 preserve_reasoning=True，保留两者
            # 如果只有 content 有值，不需要处理，直接保留
        
        return chunk_data

    @staticmethod
    def _align_official_body_defaults(request_body: dict, stream: bool = False) -> dict:
        """
        对齐 iflow-cli 的通用请求体默认参数。

        观测到官方 CLI 在 chat/completions 会补齐：
        - max_new_tokens
        - temperature
        - top_p
        - tools（即使为空数组）

        并且不会把 transport 层的 stream 字段透传给上游 body。
        """
        body = request_body.copy()

        # 官方行为：流式请求会携带 stream=true；非流式不携带该字段
        body.pop("stream", None)
        if stream:
            body["stream"] = True

        # 官方默认参数（来源于官方 CLI 抓包 + bundle 配置）
        body.setdefault("temperature", 0.7)
        body.setdefault("top_p", 0.95)
        body.setdefault("max_new_tokens", 8192)
        body.setdefault("tools", [])

        return body

    @staticmethod
    def _configure_model_request(request_body: dict, model: str) -> dict:
        """
        为特定模型配置必要的请求参数
        
        来源: iFlow CLI 源码中的模型配置 (iflow.js)
        
        模型配置规则:
        - deepseek: thinking_mode=True, reasoning=True
        - glm-4.7: chat_template_kwargs={enable_thinking: True}
        - glm-5: chat_template_kwargs={enable_thinking: True}, enable_thinking=True, thinking={type: "enabled"}
        - glm-* (其他): chat_template_kwargs={enable_thinking: True}
        - kimi-k2.5: thinking={type: "enabled"}
        - *thinking*: thinking_mode=True
        - mimo-*: thinking={type: "enabled"}
        - claude: chat_template_kwargs={enable_thinking: True}
        - sonnet-*: chat_template_kwargs={enable_thinking: True}
        - *reasoning*: reasoning=True
        - qwen*4b: 删除 thinking_mode, reasoning, chat_template_kwargs (不支持思考)
        
        Args:
            request_body: 原始请求体
            model: 模型 ID
            
        Returns:
            配置后的请求体（副本）
        """
        # 创建请求体副本
        body = request_body.copy()
        model_lower = model.lower()
        
        # 1. DeepSeek 模型
        # configureRequest:(e,r)=>{r.reasoningLevel!=="low"&&(e.reasoning=!0),e.thinking_mode=!0}
        if model_lower.startswith("deepseek"):
            if "thinking_mode" not in body:
                body["thinking_mode"] = True
            if "reasoning" not in body:
                body["reasoning"] = True
            logger.debug("为模型 %s 添加思考参数: thinking_mode=True, reasoning=True", model)
        
        # 2. GLM-5 模型 (特殊配置)
        # configureRequest:e=>{e.chat_template_kwargs={enable_thinking:!0},e.enable_thinking=!0,e.thinking={type:"enabled"}}
        elif model == "glm-5":
            if "chat_template_kwargs" not in body:
                body["chat_template_kwargs"] = {"enable_thinking": True}
            if "enable_thinking" not in body:
                body["enable_thinking"] = True
            if "thinking" not in body:
                body["thinking"] = {"type": "enabled"}
            logger.debug("为模型 %s 添加思考参数: chat_template_kwargs, enable_thinking, thinking", model)
        
        # 3. GLM-4.7 模型
        # configureRequest:e=>{e.chat_template_kwargs={enable_thinking:!0}}
        elif model == "glm-4.7":
            if "chat_template_kwargs" not in body:
                body["chat_template_kwargs"] = {"enable_thinking": True}
            logger.debug("为模型 %s 添加思考参数: chat_template_kwargs", model)
        
        # 4. 其他 GLM 模型
        # configureRequest:e=>{e.chat_template_kwargs={enable_thinking:!0}}
        elif model_lower.startswith("glm-"):
            if "chat_template_kwargs" not in body:
                body["chat_template_kwargs"] = {"enable_thinking": True}
            logger.debug("为模型 %s 添加思考参数: chat_template_kwargs", model)
        
        # 5. Kimi-K2.5 模型
        # configureRequest:(e,r)=>{e.thinking={type:"enabled"}}
        elif model_lower.startswith("kimi-k2.5"):
            if "thinking" not in body:
                body["thinking"] = {"type": "enabled"}
            logger.debug("为模型 %s 添加思考参数: thinking", model)
        
        # 6. 包含 "thinking" 的模型 (如 kimi-k2-thinking, gemini-2.0-flash-thinking)
        # configureRequest:e=>{e.thinking_mode=!0}
        elif "thinking" in model_lower:
            if "thinking_mode" not in body:
                body["thinking_mode"] = True
            logger.debug("为模型 %s 添加思考参数: thinking_mode", model)
        
        # 7. mimo- 模型
        # configureRequest:e=>{e.thinking={type:"enabled"}}
        elif model_lower.startswith("mimo-"):
            if "thinking" not in body:
                body["thinking"] = {"type": "enabled"}
            logger.debug("为模型 %s 添加思考参数: thinking", model)
        
        # 8. Claude 模型
        # configureRequest:e=>{e.chat_template_kwargs={enable_thinking:!0}}
        elif "claude" in model_lower:
            if "chat_template_kwargs" not in body:
                body["chat_template_kwargs"] = {"enable_thinking": True}
            logger.debug("为模型 %s 添加思考参数: chat_template_kwargs", model)
        
        # 9. sonnet- 模型
        # configureRequest:e=>{e.chat_template_kwargs={enable_thinking:!0}}
        elif "sonnet-" in model_lower:
            if "chat_template_kwargs" not in body:
                body["chat_template_kwargs"] = {"enable_thinking": True}
            logger.debug("为模型 %s 添加思考参数: chat_template_kwargs", model)
        
        # 10. 包含 "reasoning" 的模型
        # configureRequest:e=>{e.reasoning=!0}
        elif "reasoning" in model_lower:
            if "reasoning" not in body:
                body["reasoning"] = True
            logger.debug("为模型 %s 添加思考参数: reasoning", model)
        
        # 11. Qwen 4B 模型 (不支持思考，需要删除相关参数)
        # configureRequest:e=>{delete e.thinking_mode,delete e.reasoning,delete e.chat_template_kwargs}
        if re.match(r'qwen.*4b', model_lower, re.IGNORECASE):
            for key in ["thinking_mode", "reasoning", "chat_template_kwargs"]:
                if key in body:
                    del body[key]
            logger.debug("为模型 %s 移除思考参数 (不支持)", model)
        
        return body

    async def close(self):
        """关闭 HTTP 客户端"""
        if self._client:
            await self._client.close()
            self._client = None

    @staticmethod
    def _known_models() -> list[dict[str, str]]:
        """返回 iFlow 已知模型列表。"""
        return [
            {"id": "glm-4.7", "name": "GLM-4.7", "description": "智谱 GLM-4.7"},
            {
                "id": "iFlow-ROME-30BA3B",
                "name": "iFlow-ROME-30BA3B",
                "description": "iFlow ROME 30B (快速)",
            },
            {
                "id": "deepseek-v3.2-chat",
                "name": "DeepSeek-V3.2",
                "description": "DeepSeek V3.2 对话模型",
            },
            {"id": "glm-5", "name": "GLM-5", "description": "智谱 GLM-5 (推荐)"},
            {
                "id": "qwen3-coder-plus",
                "name": "Qwen3-Coder-Plus",
                "description": "通义千问 Qwen3 Coder Plus",
            },
            {
                "id": "kimi-k2-thinking",
                "name": "Kimi-K2-Thinking",
                "description": "Moonshot Kimi K2 思考模型",
            },
            {
                "id": "minimax-m2.5",
                "name": "MiniMax-M2.5",
                "description": "MiniMax M2.5",
            },
            {
                "id": "kimi-k2.5",
                "name": "Kimi-K2.5",
                "description": "Moonshot Kimi K2.5",
            },
            {
                "id": "kimi-k2-0905",
                "name": "Kimi-K2-0905",
                "description": "Moonshot Kimi K2 0905",
            },
        ]

    @classmethod
    def build_models_response(cls) -> dict:
        """构建 OpenAI 兼容模型列表响应。"""
        models = cls._known_models()
        current_time = int(time.time())
        return {
            "object": "list",
            "data": [
                {
                    "id": model["id"],
                    "object": "model",
                    "created": current_time,
                    "owned_by": "iflow",
                    "permission": [],
                    "root": model["id"],
                    "parent": None,
                }
                for model in models
            ],
        }

    async def get_models(self) -> dict:
        """
        获取可用模型列表

        iFlow API 没有公开的 /models 端点，因此返回已知的模型列表。
        模型列表来源于 iflow-cli 源码中的 SUPPORTED_MODELS。
        使用 iFlow-Cli User-Agent 可以解锁这些高级模型。
        
        注意：所有模型都支持图像输入，由上游 API 决定如何处理。
        """
        # iFlow CLI 支持的模型列表 (来源: uniflow_round2 反混淆代码)
        # 与官方客户端 0.5.17 版本完全一致，避免被检测
        # 2026.3.12 更新
        return self.build_models_response()

    @overload
    async def chat_completions(
        self,
        request_body: dict,
        stream: Literal[True],
    ) -> AsyncIterator[bytes]: ...

    @overload
    async def chat_completions(
        self,
        request_body: dict,
        stream: Literal[False] = ...,
    ) -> dict: ...

    async def chat_completions(
        self,
        request_body: dict,
        stream: bool = False,
    ) -> "dict | AsyncIterator[bytes]":
        """
        调用 chat completions API

        Args:
            request_body: 请求体
            stream: 是否流式响应

        Returns:
            非流式: 返回完整响应 dict
            流式: 返回字节流迭代器
        """
        # 并发限制：每个 API Key 最多 1 个并发请求（官方限制）
        from .concurrency_limiter import get_concurrency_limiter
        from .settings import load_settings

        settings = load_settings()

        # 如果启用了并发限制，则使用并发控制
        if settings.enable_concurrency_limit:
            limiter = get_concurrency_limiter(max_concurrent=settings.max_concurrent_requests)
            # 使用 API Key 作为并发限制的标识
            async with limiter.acquire(self.config.api_key, timeout=300.0):
                return await self._chat_completions_impl(request_body, stream)
        else:
            # 未启用并发限制，直接执行
            return await self._chat_completions_impl(request_body, stream)

    async def _chat_completions_impl(
        self,
        request_body: dict,
        stream: bool = False,
    ) -> "dict | AsyncIterator[bytes]":
        """
        chat completions 的实际实现（内部方法）
        """
        client = await self._get_client()
        
        # 加载配置以获取思考链设置
        from .settings import load_settings
        settings = load_settings()
        preserve_reasoning = settings.preserve_reasoning_content
        
        # 先对齐官方通用默认参数，再按模型补充特定参数
        request_body = self._align_official_body_defaults(request_body, stream=stream)

        # 为特定模型添加必要的请求参数
        # 来源: iFlow CLI 源码中的模型配置
        # GLM-5 等思考模型需要 enable_thinking 参数才能正常工作
        model = request_body.get("model", "")
        request_body = self._configure_model_request(request_body, model)

        # 统一 trace 链路：同一轮 run_started/chat/run_error 复用同一个 trace_id
        traceparent = self._generate_traceparent()
        trace_id = self._extract_trace_id(traceparent)
        parent_observation_id = ""
        try:
            parent_observation_id = await self._emit_run_started(model, trace_id)
        except Exception as e:
            logger.debug("emit run_started failed: %s", e)

        if stream:
            # 对于流式请求，使用 httpx 的 stream 方法实现真正的流式传输
            # 注意：不能使用 await client.post()，因为它会等待整个响应体下载完成
            async def stream_generator():
                buffer = b""
                chunk_count = 0
                headers = self._get_headers(stream=True, traceparent=traceparent)
                
                # 调试：打印请求详情
                logger.debug("流式请求 URL: %s/chat/completions", self.base_url)
                logger.debug("流式请求头: %s", json.dumps({k: v for k, v in headers.items() if k != 'Authorization'}, ensure_ascii=False))
                logger.debug("流式请求体: model=%s, messages=%d, tools=%d",
                             request_body.get('model'),
                             len(request_body.get('messages', [])),
                             len(request_body.get('tools', [])) if 'tools' in request_body else 0)
                
                try:
                    async with client.stream(
                        "POST",
                        f"{self.base_url}/chat/completions",
                        headers=headers,
                        json_body=request_body,
                        timeout=300.0,
                    ) as response:
                        # 检查状态码
                        response.raise_for_status()
                        
                        # 记录上游响应信息以便调试
                        content_type = response.headers.get("content-type", "unknown")
                        logger.debug("上游响应: status=%d, content-type=%s", response.status_code, content_type)
                        
                        # 如果上游没有返回 SSE 流（可能是 JSON 错误），读取并处理
                        if "text/event-stream" not in content_type and "application/octet-stream" not in content_type:
                            # 上游返回了非流式响应（可能是错误）
                            raw_body = await response.aread()
                            body_str = raw_body.decode("utf-8", errors="replace")
                            logger.debug("上游非流式响应体: %s", body_str[:500])
                            try:
                                error_data = json.loads(body_str)
                                error_msg = error_data.get("msg") or error_data.get("error", {}).get("message") or body_str[:200]
                            except json.JSONDecodeError:
                                error_msg = body_str[:200] or "上游返回空响应"

                            if parent_observation_id:
                                try:
                                    await self._emit_run_error(model, trace_id, parent_observation_id, error_msg)
                                except Exception as telemetry_err:
                                    logger.debug("emit run_error failed: %s", telemetry_err)

                            # 生成一个包含错误信息的 SSE chunk
                            error_chunk = {
                                "id": f"error-{int(time.time())}",
                                "object": "chat.completion.chunk",
                                "created": int(time.time()),
                                "model": request_body.get("model", "unknown"),
                                "choices": [{
                                    "index": 0,
                                    "delta": {"content": f"[API Error] {error_msg}"},
                                    "finish_reason": "stop"
                                }]
                            }
                            yield ("data: " + json.dumps(error_chunk, ensure_ascii=False) + "\n\n").encode("utf-8")
                            yield b"data: [DONE]\n\n"
                            return
                        
                        # 流式读取响应
                        async for chunk in response.aiter_bytes():
                            buffer += chunk
                            # 按行处理 SSE 数据
                            while b"\n" in buffer:
                                line, buffer = buffer.split(b"\n", 1)
                                line_str = line.decode("utf-8", errors="replace").strip()
                                if not line_str:
                                    yield b"\n"
                                    continue
                                chunk_count += 1
                                if line_str.startswith("data:"):
                                    data_str = line_str[5:].strip()
                                    if data_str == "[DONE]":
                                        yield b"data: [DONE]\n\n"
                                        continue
                                    try:
                                        chunk_data = json.loads(data_str)
                                        chunk_data = self._normalize_stream_chunk(chunk_data, preserve_reasoning)
                                        yield ("data: " + json.dumps(chunk_data, ensure_ascii=False) + "\n\n").encode("utf-8")
                                    except (json.JSONDecodeError, Exception):
                                        # 无法解析的 chunk 原样传递
                                        yield (line_str + "\n").encode("utf-8")
                                else:
                                    yield (line_str + "\n").encode("utf-8")
                        
                        # 处理 buffer 中剩余数据（不以 \n 结尾的最后部分）
                        if buffer:
                            line_str = buffer.decode("utf-8", errors="replace").strip()
                            if line_str.startswith("data:"):
                                data_str = line_str[5:].strip()
                                if data_str != "[DONE]":
                                    try:
                                        chunk_data = json.loads(data_str)
                                        chunk_data = self._normalize_stream_chunk(chunk_data, preserve_reasoning)
                                        yield ("data: " + json.dumps(chunk_data, ensure_ascii=False) + "\n\n").encode("utf-8")
                                    except (json.JSONDecodeError, Exception):
                                        yield (line_str + "\n").encode("utf-8")
                                else:
                                    yield b"data: [DONE]\n\n"
                            elif line_str:
                                yield (line_str + "\n").encode("utf-8")
                                
                except Exception as e:
                    logger.error("流式请求错误: %s", e)
                    if parent_observation_id:
                        try:
                            await self._emit_run_error(model, trace_id, parent_observation_id, str(e))
                        except Exception as telemetry_err:
                            logger.debug("emit run_error failed: %s", telemetry_err)
                    raise
                finally:
                    if chunk_count == 0:
                        logger.warning("上游流式响应为空 (0 chunks)")
                    else:
                        logger.debug("流式完成: 共 %d chunks", chunk_count)
            
            return stream_generator()
        else:
            try:
                response = await client.post(
                    f"{self.base_url}/chat/completions",
                    headers=self._get_headers(traceparent=traceparent),
                    json_body=request_body,
                    timeout=300.0,
                )
                response.raise_for_status()
                result = response.json()

                # 确保 usage 统计信息存在 (OpenAI 兼容)
                if "usage" not in result:
                    result["usage"] = {
                        "prompt_tokens": 0,
                        "completion_tokens": 0,
                        "total_tokens": 0,
                    }

                # 规范化响应: 确保 content 字段有效
                # GLM-5 等推理模型可能只返回 reasoning_content 而 content 为 null
                # OpenAI 兼容客户端（如 Kilo Code）只检查 content 字段
                result = self._normalize_response(result, preserve_reasoning)

                return result
            except Exception as e:
                if parent_observation_id:
                    try:
                        await self._emit_run_error(model, trace_id, parent_observation_id, str(e))
                    except Exception as telemetry_err:
                        logger.debug("emit run_error failed: %s", telemetry_err)
                raise

    async def proxy_request(
        self,
        method: str,
        path: str,
        body: Optional[dict] = None,
        stream: bool = False,
    ) -> dict | AsyncIterator[bytes]:
        """
        通用请求代理

        Args:
            method: HTTP 方法
            path: API 路径 (不含 base_url)
            body: 请求体
            stream: 是否流式响应

        Returns:
            响应数据
        """
        client = await self._get_client()
        url = f"{self.base_url}{path}"

        if stream and method.upper() == "POST":
            # 使用统一传输层的 stream 方法实现真正的流式传输
            async def stream_gen():
                try:
                    async with client.stream(
                        "POST",
                        url,
                        headers=self._get_headers(stream=True),
                        json_body=body,
                        timeout=300.0,
                    ) as response:
                        response.raise_for_status()
                        async for chunk in response.aiter_bytes():
                            yield chunk
                except Exception as e:
                    logger.error("proxy_request 流式错误: %s", e)
                    raise
            return stream_gen()

        if method.upper() == "GET":
            response = await client.get(url, headers=self._get_headers(), timeout=300.0)
        elif method.upper() in ("POST", "PUT", "PATCH"):
            response = await client.request(
                method.upper(),
                url,
                headers=self._get_headers(),
                json_body=body,
                timeout=300.0,
            )
        elif method.upper() == "DELETE":
            response = await client.request(
                "DELETE",
                url,
                headers=self._get_headers(),
                timeout=300.0,
            )
        else:
            raise ValueError(f"不支持的 HTTP 方法: {method}")

        response.raise_for_status()
        return response.json()
