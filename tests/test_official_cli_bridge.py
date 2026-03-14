import asyncio

from iflow2api.official_cli_bridge import OfficialIFlowCLITransport


async def _build_via_bridge(request_body: dict, **extra):
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
                "stream": extra.pop("stream", False),
                "timestampMs": extra.pop("timestampMs", 1710000000000),
                "requestBody": request_body,
                **extra,
            }
        )
    finally:
        await client.close()


def build_request(request_body: dict, **extra):
    return asyncio.run(_build_via_bridge(request_body, **extra))


def test_glm5_non_thinking_matches_official_shape():
    request = build_request(
        {
            "model": "glm-5",
            "messages": [{"role": "user", "content": "hi"}],
            "max_tokens": 1234,
            "response_format": {"type": "json_object"},
        },
    )

    assert request.url == "https://apis.iflow.cn/v1/chat/completions"
    assert request.headers["user-agent"] == "iFlow-Cli"
    assert request.headers["session-id"] == "session-123"
    assert request.headers["conversation-id"] == "conversation-123"
    assert request.headers["x-iflow-timestamp"] == "1710000000000"
    assert request.body == {
        "model": "glm-5",
        "messages": [{"role": "user", "content": "hi"}],
        "chat_template_kwargs": {"enable_thinking": False},
        "enable_thinking": False,
        "thinking": {"type": "disabled"},
        "temperature": 1,
        "top_p": 0.95,
        "max_new_tokens": 1234,
    }


def test_deepseek_thinking_uses_reasoner_defaults():
    request = build_request(
        {
            "model": "deepseek-v3.2-chat",
            "messages": [{"role": "user", "content": "hi"}],
            "thinking": {"type": "enabled"},
        },
    )

    assert request.body == {
        "model": "deepseek-v3.2-reasoner",
        "messages": [{"role": "user", "content": "hi"}],
        "reasoning": True,
        "thinking_mode": True,
        "max_new_tokens": 64000,
    }


def test_kimi_k25_tools_get_official_defaults():
    request = build_request(
        {
            "model": "kimi-k2.5",
            "messages": [{"role": "user", "content": "hi"}],
            "thinking": {"type": "enabled"},
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": "foo",
                        "parameters": {"type": "object", "properties": {}},
                    },
                }
            ],
            "max_tokens": 2048,
        },
    )

    assert request.body == {
        "model": "kimi-k2.5",
        "messages": [{"role": "user", "content": "hi"}],
        "thinking": {"type": "enabled"},
        "top_p": 0.95,
        "n": 1,
        "presence_penalty": 0,
        "frequency_penalty": 0,
        "max_tokens": 2048,
        "max_new_tokens": 2048,
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": "foo",
                    "parameters": {"type": "object", "properties": {}},
                },
            }
        ],
        "tool_choice": "auto",
    }


def test_offline_hosts_forward_extend_fields_session_id():
    request = build_request(
        {
            "model": "glm-4.7",
            "messages": [{"role": "user", "content": "hi"}],
        },
        baseUrl="https://offline-whale-wave.alibaba-inc.com/v1/openai",
        sessionId="session-offline",
    )

    assert request.body["extend_fields"] == {"sessionId": "session-offline"}
    assert request.body["chat_template_kwargs"] == {"enable_thinking": False}
