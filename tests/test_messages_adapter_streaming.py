from __future__ import annotations

import asyncio
import json

from iflow2api.messages_adapter import _iter_anthropic_stream_events


def _sse(payload: dict) -> bytes:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n".encode("utf-8")


async def _collect_events(chunks: list[bytes]) -> list[tuple[str, dict]]:
    async def gen():
        for chunk in chunks:
            yield chunk
        yield b"data: [DONE]\n\n"

    events: list[tuple[str, dict]] = []
    async for raw in _iter_anthropic_stream_events(
        gen(),
        mapped_model="glm-5",
        preserve_reasoning=False,
    ):
        text = raw.decode("utf-8")
        parts = [line for line in text.strip().splitlines() if line]
        event_name = next(line.split(": ", 1)[1] for line in parts if line.startswith("event:"))
        payload = json.loads(next(line.split("data: ", 1)[1] for line in parts if line.startswith("data:")))
        events.append((event_name, payload))
    return events


def test_interleaved_tool_calls_do_not_stop_first_block_prematurely() -> None:
    chunks = [
        _sse(
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call_0",
                                    "function": {"name": "tool_a", "arguments": '{"a":'},
                                }
                            ]
                        },
                        "finish_reason": None,
                    }
                ]
            }
        ),
        _sse(
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 1,
                                    "id": "call_1",
                                    "function": {"name": "tool_b", "arguments": '{"b":'},
                                }
                            ]
                        },
                        "finish_reason": None,
                    }
                ]
            }
        ),
        _sse(
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "function": {"arguments": '1}'},
                                }
                            ]
                        },
                        "finish_reason": "tool_calls",
                    }
                ]
            }
        ),
    ]

    events = asyncio.run(_collect_events(chunks))
    event_types = [(name, payload.get("index")) for name, payload in events]

    first_tool_start = event_types.index(("content_block_start", 0))
    second_tool_start = event_types.index(("content_block_start", 1))
    first_tool_second_delta = next(
        idx
        for idx, (name, payload) in enumerate(events)
        if name == "content_block_delta"
        and payload.get("index") == 0
        and payload["delta"].get("partial_json") == "1}"
    )
    first_tool_stop = next(
        idx
        for idx, (name, payload) in enumerate(events)
        if name == "content_block_stop" and payload.get("index") == 0
    )

    assert first_tool_start < second_tool_start < first_tool_second_delta < first_tool_stop


def test_text_after_tool_use_closes_tool_block_before_text_block() -> None:
    chunks = [
        _sse(
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call_0",
                                    "function": {"name": "tool_a", "arguments": '{"a":1}'},
                                }
                            ]
                        },
                        "finish_reason": None,
                    }
                ]
            }
        ),
        _sse(
            {
                "choices": [
                    {
                        "delta": {"content": "done"},
                        "finish_reason": "stop",
                    }
                ]
            }
        ),
    ]

    events = asyncio.run(_collect_events(chunks))
    tool_stop = next(
        idx
        for idx, (name, payload) in enumerate(events)
        if name == "content_block_stop" and payload.get("index") == 0
    )
    text_start = next(
        idx
        for idx, (name, payload) in enumerate(events)
        if name == "content_block_start"
        and payload.get("index") == 1
        and payload["content_block"].get("type") == "text"
    )

    assert tool_stop < text_start


def test_adapter_midstream_exception_still_emits_message_stop() -> None:
    async def gen():
        yield _sse(
            {
                "choices": [
                    {
                        "delta": {"content": "hello"},
                        "finish_reason": None,
                    }
                ]
            }
        )
        raise RuntimeError("stream aborted")
        yield b""  # pragma: no cover

    async def collect():
        collected: list[str] = []
        async for raw in _iter_anthropic_stream_events(
            gen(),
            mapped_model="glm-5",
            preserve_reasoning=False,
        ):
            collected.append(raw.decode("utf-8"))
        return collected

    events = asyncio.run(collect())
    merged = "".join(events)

    assert "event: message_delta" in merged
    assert "event: message_stop" in merged
