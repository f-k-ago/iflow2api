import asyncio

from iflow2api.anthropic_compat import anthropic_to_openai_request
from iflow2api.official_cli_bridge import OfficialIFlowCLITransport


async def _build_from_messages(anthropic_body: dict, **extra):
    openai_body = anthropic_to_openai_request(anthropic_body)
    client = OfficialIFlowCLITransport(timeout=30.0, proxy=None)
    try:
        return await client.build_chat_request(
            {
                "apiKey": "sk-test",
                "baseUrl": extra.pop("baseUrl", "https://apis.iflow.cn/v1"),
                "sessionId": extra.pop("sessionId", "session-123"),
                "conversationId": extra.pop("conversationId", "conversation-123"),
                "traceparent": extra.pop(
                    "traceparent",
                    "00-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa-bbbbbbbbbbbbbbbb-01",
                ),
                "stream": anthropic_body.get("stream", False),
                "timestampMs": extra.pop("timestampMs", 1710000000000),
                "requestBody": openai_body,
                **extra,
            }
        )
    finally:
        await client.close()


def build_from_messages(anthropic_body: dict, **extra):
    return asyncio.run(_build_from_messages(anthropic_body, **extra))


def test_messages_glm5_also_uses_official_builder_defaults():
    request = build_from_messages(
        {
            "model": "glm-5",
            "system": "You are a helpful assistant.",
            "max_tokens": 1234,
            "messages": [{"role": "user", "content": "hi"}],
        }
    )

    assert request.body == {
        "model": "glm-5",
        "messages": [
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "user", "content": "hi"},
        ],
        "chat_template_kwargs": {"enable_thinking": False},
        "enable_thinking": False,
        "thinking": {"type": "disabled"},
        "temperature": 1,
        "top_p": 0.95,
        "max_new_tokens": 1234,
    }


def test_messages_stream_request_gets_official_stream_shape():
    request = build_from_messages(
        {
            "model": "glm-4.7",
            "stream": True,
            "max_tokens": 2048,
            "messages": [{"role": "user", "content": "hi"}],
        }
    )

    assert request.body == {
        "model": "glm-4.7",
        "messages": [{"role": "user", "content": "hi"}],
        "stream": True,
        "stream_options": {"include_usage": True},
        "chat_template_kwargs": {"enable_thinking": False},
        "temperature": 1,
        "top_p": 0.95,
        "max_new_tokens": 2048,
    }


def test_messages_with_tools_still_flow_through_official_header_builder():
    request = build_from_messages(
        {
            "model": "glm-5",
            "messages": [{"role": "user", "content": "hi"}],
            "tools": [
                {
                    "name": "lookup_weather",
                    "description": "look up weather",
                    "input_schema": {
                        "type": "object",
                        "properties": {"city": {"type": "string"}},
                    },
                }
            ],
            "tool_choice": {"type": "any"},
            "max_tokens": 1000,
        }
    )

    assert request.headers["user-agent"] == "iFlow-Cli"
    assert request.headers["session-id"] == "session-123"
    assert request.headers["conversation-id"] == "conversation-123"
    assert request.body["tools"] == [
        {
            "type": "function",
            "function": {
                "name": "lookup_weather",
                "description": "look up weather",
                "parameters": {
                    "type": "object",
                    "properties": {"city": {"type": "string"}},
                    "additionalProperties": False,
                },
            },
        }
    ]
    assert request.body["tool_choice"] == "required"
    assert request.body["max_new_tokens"] == 1000


def test_messages_text_blocks_preserve_content_array_shape():
    request = build_from_messages(
        {
            "model": "glm-5",
            "max_tokens": 1000,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "hello"},
                        {"type": "text", "text": "world"},
                    ],
                }
            ],
        }
    )

    assert request.body["messages"] == [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "hello"},
                {"type": "text", "text": "world"},
            ],
        }
    ]


def test_messages_assistant_tool_use_matches_official_content_and_argument_shape():
    request = build_from_messages(
        {
            "model": "glm-5",
            "max_tokens": 1000,
            "messages": [
                {
                    "role": "assistant",
                    "content": [
                        {"type": "tool_use", "id": "toolu_1", "name": "lookup", "input": {"x": 1}},
                    ],
                }
            ],
        }
    )

    assert request.body["messages"] == [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "toolu_1",
                    "type": "function",
                    "function": {"name": "lookup", "arguments": '{"x":1}'},
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "toolu_1",
            "content": "tool not executed",
        },
    ]
