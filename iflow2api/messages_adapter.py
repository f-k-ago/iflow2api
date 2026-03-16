"""程序内/程序外共用的 Anthropic Messages 适配层。"""

from __future__ import annotations

import logging
from typing import Any, AsyncIterator, Awaitable, Callable

from fastapi.responses import JSONResponse, StreamingResponse

from .anthropic_compat import (
    create_anthropic_content_block_delta,
    create_anthropic_content_block_start,
    create_anthropic_content_block_stop,
    create_anthropic_input_json_delta,
    create_anthropic_message_delta,
    create_anthropic_message_stop,
    create_anthropic_stream_message_start,
    create_anthropic_tool_use_block_start,
    extract_content_from_delta,
    parse_openai_sse_chunk,
)


AsyncCloser = Callable[[], Awaitable[None]]
logger = logging.getLogger("iflow2api")


def create_anthropic_error_response(
    status_code: int,
    message: str,
    error_type: str = "api_error",
) -> JSONResponse:
    """返回 Anthropic 兼容错误响应。"""
    return JSONResponse(
        status_code=status_code,
        content={
            "type": "error",
            "error": {
                "type": error_type,
                "message": message,
            },
        },
    )


def extract_error_payload(
    data: Any,
    *,
    fallback_message: str,
) -> tuple[str, str]:
    """从 OpenAI/上游错误包提取 Anthropic 兼容错误类型和消息。"""
    if isinstance(data, dict):
        error = data.get("error")
        if isinstance(error, dict):
            error_type = str(error.get("type") or "api_error")
            error_message = str(error.get("message") or fallback_message)
            return error_type, error_message
        message = str(data.get("msg") or data.get("message") or fallback_message)
        return "api_error", message
    return "api_error", fallback_message


async def _iter_anthropic_stream_events(
    stream_gen: AsyncIterator[str | bytes],
    *,
    mapped_model: str,
    preserve_reasoning: bool,
    on_close: AsyncCloser | None = None,
) -> AsyncIterator[bytes]:
    """把 OpenAI SSE 字节流转换成 Anthropic SSE 字节流。"""
    output_tokens = 0
    buffer = ""
    block_index = 0
    stop_reason = "end_turn"
    current_text_block_type: str | None = None
    current_text_block_index = -1
    tool_call_block_map: dict[int, dict[str, str | int]] = {}

    async def close_open_tool_blocks() -> AsyncIterator[bytes]:
        """关闭当前打开的 tool_use block。"""
        for tool_state in sorted(
            tool_call_block_map.values(),
            key=lambda item: int(item["block_index"]),
        ):
            yield create_anthropic_content_block_stop(
                int(tool_state["block_index"])
            ).encode("utf-8")
        tool_call_block_map.clear()

    async def process_parsed_chunk(parsed: dict[str, Any]) -> AsyncIterator[bytes]:
        nonlocal block_index, output_tokens, stop_reason
        nonlocal current_text_block_type, current_text_block_index

        choices = parsed.get("choices", [])
        if not choices:
            return
        choice = choices[0]
        delta = choice.get("delta", {})
        finish_reason = choice.get("finish_reason")

        content, content_type = extract_content_from_delta(delta, preserve_reasoning)
        if content and content_type:
            if tool_call_block_map:
                async for event in close_open_tool_blocks():
                    yield event
            if current_text_block_type != content_type:
                if current_text_block_type is not None:
                    yield create_anthropic_content_block_stop(current_text_block_index).encode("utf-8")
                current_text_block_index = block_index
                block_index += 1
                yield create_anthropic_content_block_start(
                    current_text_block_index,
                    content_type,
                ).encode("utf-8")
                current_text_block_type = content_type
            output_tokens += len(content) // 4
            delta_type = "thinking_delta" if content_type == "thinking" else "text_delta"
            yield create_anthropic_content_block_delta(
                content,
                current_text_block_index,
                delta_type,
            ).encode("utf-8")

        for tool_call in delta.get("tool_calls", []):
            tool_index = tool_call.get("index", 0)
            tool_id = tool_call.get("id")
            tool_function = tool_call.get("function", {})
            tool_name = tool_function.get("name", "")
            tool_arguments = tool_function.get("arguments", "")

            if tool_index not in tool_call_block_map:
                if current_text_block_type is not None:
                    yield create_anthropic_content_block_stop(current_text_block_index).encode("utf-8")
                    current_text_block_type = None

                tool_block_index = block_index
                block_index += 1
                tool_call_block_map[tool_index] = {
                    "block_index": tool_block_index,
                    "id": tool_id or "",
                    "name": tool_name or "",
                }
                yield create_anthropic_tool_use_block_start(
                    tool_block_index,
                    str(tool_call_block_map[tool_index]["id"]),
                    str(tool_call_block_map[tool_index]["name"]),
                ).encode("utf-8")

            if tool_arguments:
                yield create_anthropic_input_json_delta(
                    tool_arguments,
                    int(tool_call_block_map[tool_index]["block_index"]),
                ).encode("utf-8")

        if finish_reason == "tool_calls":
            stop_reason = "tool_use"
        elif finish_reason == "length":
            stop_reason = "max_tokens"
        elif finish_reason == "stop":
            stop_reason = "end_turn"

    try:
        yield create_anthropic_stream_message_start(mapped_model).encode("utf-8")

        try:
            async for chunk in stream_gen:
                chunk_str = chunk if isinstance(chunk, str) else bytes(chunk).decode("utf-8", errors="replace")
                buffer += chunk_str

                while "\n" in buffer:
                    line, buffer = buffer.split("\n", 1)
                    parsed = parse_openai_sse_chunk(line)
                    if parsed:
                        async for event in process_parsed_chunk(parsed):
                            yield event
        except Exception as exc:
            logger.error("Anthropic 流式适配中途异常，输出规范终止事件: %s", exc)

        for line in buffer.split("\n"):
            if not line.strip():
                continue
            parsed = parse_openai_sse_chunk(line)
            if parsed:
                async for event in process_parsed_chunk(parsed):
                    yield event

        if current_text_block_type is not None:
            yield create_anthropic_content_block_stop(current_text_block_index).encode("utf-8")

        if tool_call_block_map:
            async for event in close_open_tool_blocks():
                yield event

        yield create_anthropic_message_delta(stop_reason, output_tokens).encode("utf-8")
        yield create_anthropic_message_stop().encode("utf-8")
    finally:
        if on_close is not None:
            await on_close()


def create_anthropic_streaming_response(
    stream_gen: AsyncIterator[str | bytes],
    *,
    mapped_model: str,
    preserve_reasoning: bool,
    on_close: AsyncCloser | None = None,
) -> StreamingResponse:
    """创建 Anthropic 兼容 SSE 响应。"""
    return StreamingResponse(
        _iter_anthropic_stream_events(
            stream_gen,
            mapped_model=mapped_model,
            preserve_reasoning=preserve_reasoning,
            on_close=on_close,
        ),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
