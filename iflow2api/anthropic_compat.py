"""Anthropic Messages API 与 OpenAI Chat 之间的兼容转换工具。"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any, Optional

from .vision import detect_image_content

logger = logging.getLogger("iflow2api")


# ============ Anthropic 格式转换函数 ============

def openai_to_anthropic_response(openai_response: dict, model: str) -> dict:
    """
    将 OpenAI 格式响应转换为 Anthropic 格式
    
    OpenAI 格式:
    {
      "id": "chatcmpl-xxx",
      "object": "chat.completion",
      "choices": [{"message": {"role": "assistant", "content": "...", "tool_calls": [...]}, "finish_reason": "stop"}],
      "usage": {"prompt_tokens": N, "completion_tokens": N, "total_tokens": N}
    }
    
    Anthropic 格式:
    {
      "id": "msg_xxx",
      "type": "message",
      "role": "assistant",
      "content": [{"type": "text", "text": "..."}, {"type": "tool_use", "id": "...", "name": "...", "input": {...}}],
      "model": "...",
      "stop_reason": "end_turn" | "tool_use",
      "usage": {"input_tokens": N, "output_tokens": N}
    }
    """
    choices = openai_response.get("choices", [])
    content_blocks = []
    finish_reason = "end_turn"

    if not choices:
        logger.warning("OpenAI 响应中 choices 数组为空")
        logger.debug("完整响应: %s", json.dumps(openai_response, ensure_ascii=False)[:500])
        content_blocks = [{"type": "text", "text": "[错误: API 未返回有效内容]"}]
    else:
        choice = choices[0]
        message = choice.get("message", {})
        content_text = message.get("content") or message.get("reasoning_content", "")
        tool_calls = message.get("tool_calls") or []

        # 添加文本内容块
        if content_text:
            content_blocks.append({"type": "text", "text": content_text})

        # 将 OpenAI tool_calls 转换为 Anthropic tool_use 内容块
        for tc in tool_calls:
            func = tc.get("function", {})
            try:
                tool_input = json.loads(func.get("arguments", "{}") or "{}")
            except (json.JSONDecodeError, TypeError):
                tool_input = {"_raw": func.get("arguments", "")}
            content_blocks.append({
                "type": "tool_use",
                "id": tc.get("id") or f"toolu_{uuid.uuid4().hex[:24]}",
                "name": func.get("name", ""),
                "input": tool_input,
            })

        if not content_blocks:
            logger.warning("message.content 和 tool_calls 均为空: %s", json.dumps(message, ensure_ascii=False))
            content_blocks = [{"type": "text", "text": "[错误: API 返回空内容]"}]

        # 转换 finish_reason
        openai_finish = choice.get("finish_reason", "stop")
        if openai_finish == "stop":
            finish_reason = "end_turn"
        elif openai_finish == "length":
            finish_reason = "max_tokens"
        elif openai_finish == "tool_calls":
            finish_reason = "tool_use"
        else:
            finish_reason = "end_turn"

    # 提取 usage
    openai_usage = openai_response.get("usage", {})

    return {
        "id": f"msg_{uuid.uuid4().hex[:24]}",
        "type": "message",
        "role": "assistant",
        "content": content_blocks,
        "model": model,
        "stop_reason": finish_reason,
        "stop_sequence": None,
        "usage": {
            "input_tokens": openai_usage.get("prompt_tokens", 0),
            "output_tokens": openai_usage.get("completion_tokens", 0),
        }
    }


def create_anthropic_stream_message_start(model: str) -> str:
    """创建 Anthropic 流式响应的 message_start 事件"""
    msg_id = f"msg_{uuid.uuid4().hex[:24]}"
    data = {
        "type": "message_start",
        "message": {
            "id": msg_id,
            "type": "message",
            "role": "assistant",
            "content": [],
            "model": model,
            "stop_reason": None,
            "stop_sequence": None,
            "usage": {"input_tokens": 0, "output_tokens": 0}
        }
    }
    return f"event: message_start\ndata: {json.dumps(data)}\n\n"


def create_anthropic_content_block_start(index: int = 0, block_type: str = "text") -> str:
    """创建 Anthropic 流式响应的 content_block_start 事件
    
    Args:
        index: 内容块索引
        block_type: 内容块类型 ("text" 或 "thinking")
    """
    if block_type == "thinking":
        content_block = {"type": "thinking", "thinking": ""}
    else:
        content_block = {"type": "text", "text": ""}
    data = {
        "type": "content_block_start",
        "index": index,
        "content_block": content_block
    }
    return f"event: content_block_start\ndata: {json.dumps(data)}\n\n"


def create_anthropic_content_block_delta(text: str, index: int = 0, delta_type: str = "text_delta") -> str:
    """创建 Anthropic 流式响应的 content_block_delta 事件
    
    Args:
        text: 内容文本
        index: 内容块索引
        delta_type: delta 类型 ("text_delta" 或 "thinking_delta")
    """
    if delta_type == "thinking_delta":
        delta = {"type": "thinking_delta", "thinking": text}
    else:
        delta = {"type": "text_delta", "text": text}
    data = {
        "type": "content_block_delta",
        "index": index,
        "delta": delta
    }
    return f"event: content_block_delta\ndata: {json.dumps(data)}\n\n"


def create_anthropic_content_block_stop(index: int = 0) -> str:
    """创建 Anthropic 流式响应的 content_block_stop 事件
    
    Args:
        index: 内容块索引
    """
    data = {"type": "content_block_stop", "index": index}
    return f"event: content_block_stop\ndata: {json.dumps(data)}\n\n"


def create_anthropic_message_delta(stop_reason: str = "end_turn", output_tokens: int = 0) -> str:
    """创建 Anthropic 流式响应的 message_delta 事件"""
    data = {
        "type": "message_delta",
        "delta": {"stop_reason": stop_reason, "stop_sequence": None},
        "usage": {"output_tokens": output_tokens}
    }
    return f"event: message_delta\ndata: {json.dumps(data)}\n\n"


def create_anthropic_message_stop() -> str:
    """创建 Anthropic 流式响应的 message_stop 事件"""
    data = {"type": "message_stop"}
    return f"event: message_stop\ndata: {json.dumps(data)}\n\n"


def create_anthropic_tool_use_block_start(index: int, tool_use_id: str, name: str) -> str:
    """创建 Anthropic 流式响应的 tool_use content_block_start 事件"""
    data = {
        "type": "content_block_start",
        "index": index,
        "content_block": {"type": "tool_use", "id": tool_use_id, "name": name, "input": {}}
    }
    return f"event: content_block_start\ndata: {json.dumps(data)}\n\n"


def create_anthropic_input_json_delta(partial_json: str, index: int) -> str:
    """创建 Anthropic 流式响应的 input_json_delta content_block_delta 事件"""
    data = {
        "type": "content_block_delta",
        "index": index,
        "delta": {"type": "input_json_delta", "partial_json": partial_json}
    }
    return f"event: content_block_delta\ndata: {json.dumps(data)}\n\n"


def parse_openai_sse_chunk(line: str) -> Optional[dict]:
    """解析 OpenAI SSE 流式数据块"""
    line = line.strip()
    if not line or line == "data: [DONE]" or line == "data:[DONE]":
        return None
    # iFlow 使用 "data:" 没有空格，标准SSE使用 "data: "
    if line.startswith("data:"):
        data_str = line[5:].strip()  # 去掉 "data:" 前缀
        if not data_str or data_str == "[DONE]":
            return None
        try:
            return json.loads(data_str)
        except json.JSONDecodeError:
            return None
    return None


def extract_content_from_delta(delta: dict, preserve_reasoning: bool = False) -> tuple[str, str]:
    """从 OpenAI delta 中提取内容
    
    Args:
        delta: OpenAI 流式响应的 delta 对象
        preserve_reasoning: 是否区分思考内容和回答内容
            - False（默认）: 将 reasoning_content 也作为普通文本输出（兼容模式）
            - True: 区分思考内容和回答内容，返回 (内容, 类型) 元组
    
    Returns:
        (内容, 类型) 元组，类型为 "text"、"thinking" 或 ""
    
    上游API行为（GLM-5）：
    - 流式响应：思考过程和回答分开返回
      - 大部分chunk只有 reasoning_content（思考过程）
      - 少部分chunk只有 content（最终回答）
      - 两者不会同时出现在同一个chunk中
    """
    content = delta.get("content", "")
    reasoning_content = delta.get("reasoning_content", "")
    
    if content:
        # 有 content，直接返回（这是最终回答）
        return (content, "text")
    elif reasoning_content:
        if preserve_reasoning:
            # 区分思考内容，标记为 thinking 类型
            return (reasoning_content, "thinking")
        else:
            # 兼容模式，将思考内容作为普通文本输出
            return (reasoning_content, "text")
    else:
        # 两者都为空
        return ("", "")


# ============ Anthropic → OpenAI 请求转换 ============

# Claude Code 发送的模型名 → iFlow 实际模型名映射
# 用户可通过 ANTHROPIC_MODEL 环境变量指定默认模型
DEFAULT_IFLOW_MODEL = "glm-5"


def get_mapped_model(anthropic_model: str, has_images: bool = False) -> str:
    """
    将 Anthropic/Claude 模型名映射为 iFlow 模型名。
    如果是已知的 iFlow 模型则原样返回，否则回退到默认模型。
    
    注意：所有模型都支持图像输入，由上游 API 决定如何处理。
    
    Args:
        anthropic_model: 原始模型名
        has_images: 请求是否包含图像（保留参数，用于日志）
    
    Returns:
        映射后的模型名
    """
    # iFlow 已知模型 ID（与 proxy.py get_models() 保持一致）
    known_iflow_models = {
        # 文本模型
        "glm-4.6", "glm-4.7", "glm-5",
        "iFlow-ROME-30BA3B", "deepseek-v3.2-chat",
        "qwen3-coder-plus", "kimi-k2", "kimi-k2-thinking", "kimi-k2.5",
        "kimi-k2-0905",  # L-02 修复：补充缺失模型
        "minimax-m2.5",
        # 视觉模型
        "glm-4v", "glm-4v-plus", "glm-4v-flash", "glm-4.5v", "glm-4.6v",
        "moonshot-v1-8k-vision", "moonshot-v1-32k-vision", "moonshot-v1-128k-vision",
        "qwen-vl-plus", "qwen-vl-max", "qwen-vl-max-latest", "Qwen-VL",
        "qwen2.5-vl", "qwen2.5-vl-72b-instruct", "qwen3-vl", "qwen3-vl-plus",
        "nova-lite-v1", "nova-pro-v1",
    }
    
    if anthropic_model in known_iflow_models:
        return anthropic_model
    
    # Claude 系列模型名回退到默认模型
    logger.info("模型映射: %s → %s", anthropic_model, DEFAULT_IFLOW_MODEL)
    return DEFAULT_IFLOW_MODEL


def _extract_text_parts(blocks: list[Any]) -> list[str]:
    """从 Anthropic content block 列表中提取文本片段。"""
    text_parts: list[str] = []
    for block in blocks:
        if isinstance(block, dict) and block.get("type") == "text":
            text = block.get("text", "")
            if text:
                text_parts.append(str(text))
        elif isinstance(block, str) and block:
            text_parts.append(block)
    return text_parts


def _build_openai_text_blocks(blocks: list[Any]) -> list[dict[str, str]]:
    """把文本片段保留成 OpenAI content array 形状。"""
    return [{"type": "text", "text": text} for text in _extract_text_parts(blocks)]


def _dump_tool_arguments(arguments: Any) -> str:
    """按官方 JS JSON.stringify 风格序列化工具参数。"""
    return json.dumps(arguments or {}, ensure_ascii=False, separators=(",", ":"))


def anthropic_to_openai_request(body: dict) -> dict:
    """
    将 Anthropic Messages API 请求体转换为 OpenAI Chat Completions 格式。
    
    Anthropic 格式:
    {
      "model": "claude-sonnet-4-5-20250929",
      "max_tokens": 8096,
      "system": "You are...",           # 或 [{"type":"text","text":"..."}]
      "messages": [
        {"role": "user", "content": "hello"}  # content 可以是 str 或 [{"type":"text","text":"..."}]
      ],
      "stream": true
    }
    
    OpenAI 格式:
    {
      "model": "glm-5",
      "max_tokens": 8096,
      "messages": [
        {"role": "system", "content": "You are..."},
        {"role": "user", "content": "hello"}
      ],
      "stream": true
    }
    
    支持图像输入（Vision）:
    - Anthropic 格式: {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "..."}}
    - OpenAI 格式: {"type": "image_url", "image_url": {"url": "data:image/png;base64,..."}}
    """
    openai_body = {}
    
    # 检测是否有图像内容
    has_images = False
    for msg in body.get("messages", []):
        content = msg.get("content", "")
        images = detect_image_content(content)
        if images:
            has_images = True
            break
    
    # 1. 模型映射（考虑图像支持）
    openai_body["model"] = get_mapped_model(body.get("model", DEFAULT_IFLOW_MODEL), has_images)
    
    # 2. 构建 messages（先处理 system）
    messages = []
    system = body.get("system")
    if system:
        if isinstance(system, list):
            # Anthropic 格式: [{"type": "text", "text": "..."}]
            system_text = " ".join(
                block.get("text", "") for block in system if block.get("type") == "text"
            )
        else:
            system_text = str(system)
        if system_text:
            messages.append({"role": "system", "content": system_text})
    
    # 3. 转换 messages 中的 content（支持图像、工具调用）
    for msg in body.get("messages", []):
        role = msg.get("role", "user")
        content = msg.get("content", "")

        if isinstance(content, list):
            if role == "assistant":
                # 提取文本块和 tool_use 块
                tool_use_blocks = [b for b in content if isinstance(b, dict) and b.get("type") == "tool_use"]
                text_parts = _extract_text_parts(content)
                text_content = "".join(text_parts)
                if not text_content and not tool_use_blocks:
                    continue

                openai_msg: dict = {"role": "assistant"}
                openai_msg["content"] = text_content if text_content else ""

                if tool_use_blocks:
                    openai_msg["tool_calls"] = [
                        {
                            "id": b.get("id") or f"call_{uuid.uuid4().hex[:24]}",
                            "type": "function",
                            "function": {
                                "name": b.get("name", ""),
                                "arguments": _dump_tool_arguments(b.get("input", {})),
                            },
                        }
                        for b in tool_use_blocks
                    ]
                messages.append(openai_msg)

            else:  # role == "user"
                # 先处理 tool_result 块 → 转成 role=tool 消息
                tool_result_blocks = [b for b in content if isinstance(b, dict) and b.get("type") == "tool_result"]
                for tr in tool_result_blocks:
                    tr_content = tr.get("content", "")
                    if isinstance(tr_content, list):
                        tr_text = "".join(_extract_text_parts(tr_content))
                    else:
                        tr_text = str(tr_content) if tr_content else ""
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tr.get("tool_use_id", ""),
                        "content": tr_text,
                    })

                # 处理剩余内容（文本 / 图像）
                remaining = [b for b in content if not (isinstance(b, dict) and b.get("type") == "tool_result")]
                if remaining:
                    images = detect_image_content(remaining)
                    if images:
                        # 有图像，使用 OpenAI 多模态格式
                        multimodal_content = []
                        multimodal_content.extend(_build_openai_text_blocks(remaining))
                        from .vision import convert_to_openai_format
                        multimodal_content.extend(convert_to_openai_format(images))
                        messages.append({"role": "user", "content": multimodal_content})
                    else:
                        # 无图像时，尽量保留官方 content array 形状
                        text_blocks = _build_openai_text_blocks(remaining)
                        if text_blocks:
                            messages.append({"role": "user", "content": text_blocks})
                # 如果只有 tool_result 没有额外文本/图像，则不追加 user 消息
        else:
            messages.append({"role": role, "content": content})

    openai_body["messages"] = messages

    # 4. 透传兼容参数
    if "max_tokens" in body:
        openai_body["max_tokens"] = body["max_tokens"]
    if "temperature" in body:
        openai_body["temperature"] = body["temperature"]
    if "top_p" in body:
        openai_body["top_p"] = body["top_p"]
    if "stop_sequences" in body:
        openai_body["stop"] = body["stop_sequences"]
    if "stream" in body:
        openai_body["stream"] = body["stream"]

    # 5. 转换 tools（Anthropic input_schema → OpenAI parameters）
    if "tools" in body:
        openai_body["tools"] = [
            {
                "type": "function",
                "function": {
                    "name": t.get("name", ""),
                    "description": t.get("description", ""),
                    "parameters": t.get("input_schema", {}),
                },
            }
            for t in body["tools"]
        ]

    # 6. 转换 tool_choice
    if "tool_choice" in body:
        tc = body["tool_choice"]
        if isinstance(tc, dict):
            tc_type = tc.get("type", "auto")
            if tc_type == "auto":
                openai_body["tool_choice"] = "auto"
            elif tc_type == "any":
                openai_body["tool_choice"] = "required"
            elif tc_type == "tool":
                openai_body["tool_choice"] = {
                    "type": "function",
                    "function": {"name": tc.get("name", "")},
                }
            else:
                openai_body["tool_choice"] = "auto"
        elif isinstance(tc, str):
            openai_body["tool_choice"] = tc

    return openai_body

