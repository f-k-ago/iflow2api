import asyncio

import pytest

from iflow2api.official_cli_bridge import (
    OfficialBuilderValidationError,
    OfficialBundleRequiredError,
    OfficialIFlowCLITransport,
)


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
                        "parameters": {"type": "object", "additionalProperties": False},
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
                    "parameters": {"type": "object", "additionalProperties": False},
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


def test_aone_request_uses_current_official_client_version():
    request = build_request(
        {
            "model": "glm-5",
            "messages": [{"role": "user", "content": "hi"}],
        },
        baseUrl="https://ducky.code.alibaba-inc.com/v1/openai",
    )

    assert "X-Client-Type" not in request.headers
    assert "X-Client-Version" not in request.headers


def test_chat_developer_role_collapses_to_official_system_message():
    request = build_request(
        {
            "model": "glm-5",
            "messages": [
                {"role": "developer", "content": "You are strict."},
                {"role": "user", "content": "hi"},
            ],
        },
    )

    assert request.body["messages"] == [
        {"role": "system", "content": "You are strict."},
        {"role": "user", "content": "hi"},
    ]


def test_chat_tool_call_and_tool_result_roundtrip_via_official_converter():
    request = build_request(
        {
            "model": "glm-5",
            "messages": [
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "call_weather",
                            "type": "function",
                            "function": {
                                "name": "lookup_weather",
                                "arguments": '{"city":"Hangzhou"}',
                            },
                        }
                    ],
                },
                {
                    "role": "tool",
                    "tool_call_id": "call_weather",
                    "content": '{"temp":26}',
                },
            ],
        },
    )

    assert request.body["messages"] == [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call_weather",
                    "type": "function",
                    "function": {
                        "name": "lookup_weather",
                        "arguments": '{"city":"Hangzhou"}',
                    },
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "call_weather",
            "content": '{"temp":26}',
        },
    ]


def test_strict_official_mode_rejects_non_data_url_images():
    with pytest.raises(OfficialBuilderValidationError) as exc_info:
        build_request(
            {
                "model": "glm-5",
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "image_url",
                                "image_url": {"url": "https://example.com/demo.png"},
                            }
                        ],
                    }
                ],
            },
        )

    assert "无法严格翻译" in str(exc_info.value)


def test_qwen4b_scrubs_thinking_fields_into_extend_fields():
    request = build_request(
        {
            "model": "qwen-4b-chat",
            "messages": [{"role": "user", "content": "hi"}],
            "thinking": {"type": "enabled"},
            "thinking_mode": True,
            "reasoning": True,
        },
    )

    assert request.body == {
        "model": "qwen-4b-chat",
        "messages": [{"role": "user", "content": "hi"}],
        "max_new_tokens": 8000,
    }


def test_mock_official_bundle_shim_can_override_roundtrip_and_thinking(tmp_path, monkeypatch):
    bundle_path = tmp_path / "iflow.js"
    bundle_path.write_text(
        """
class gH {
  constructor(options = {}) {
    this.apiKey = options.apiKey;
    this.baseUrl = options.baseUrl;
    this.config = options.config;
  }
  async convertToOpenAIMessages() {
    return [{ role: "user", content: "shim-user" }];
  }
  convertToOpenAITools() {
    return [];
  }
  async calculateInputTokens() {
    return 17;
  }
  async generateContentInternal(e, r, n) {
    const p = {
      model: this.config?.getModel?.() || "glm-5",
      messages: [{ role: "user", content: "shim-user" }],
      bundle_nonthinking: true,
      max_new_tokens: 4242,
    };
    n && n(p);
    await fetch(`${this.baseUrl}/chat/completions`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Authorization: `Bearer ${this.apiKey}`,
        "user-agent": "iFlow-Cli",
        "session-id": this.config?.getSessionId?.() || "",
        "conversation-id": this.config?.getConversationId?.() || "",
        "x-iflow-timestamp": String(Date.now()),
      },
      body: JSON.stringify(p),
    });
    return { text: "ok" };
  }
}

const h2 = {
  configureThinkingRequest(model, body, cfg) {
    body.bundle_thinking = cfg.maxTokens;
    return true;
  },
  configureNonThinkingRequest(model, body) {
    body.bundle_nonthinking = true;
    return true;
  },
};

function MOt(model, inputTokens = 0, explicitLimit) {
  return explicitLimit || 4242;
}

async function Eao() {}
Eao().catch(t=>{console.error("An unexpected critical error occurred:"),t instanceof Error?console.error(t.stack):console.error(String(t)),process.exit(1)});
""".strip(),
        encoding="utf-8",
    )

    monkeypatch.setenv("IFLOW_OFFICIAL_BUNDLE_PATH", str(bundle_path))
    monkeypatch.delenv("IFLOW_DISABLE_OFFICIAL_BUNDLE_SHIM", raising=False)

    request = build_request(
        {
            "model": "glm-5",
            "messages": [{"role": "user", "content": "hi"}],
        },
    )

    assert request.body["messages"] == [{"role": "user", "content": "shim-user"}]
    assert request.body["bundle_nonthinking"] is True
    assert request.body["max_new_tokens"] == 4242
    assert request.meta["normalizationSource"] == "official_bundle"


def test_official_bundle_is_now_required(monkeypatch):
    monkeypatch.delenv("IFLOW_OFFICIAL_BUNDLE_PATH", raising=False)
    monkeypatch.setenv("IFLOW_DISABLE_OFFICIAL_BUNDLE_SHIM", "1")

    with pytest.raises(OfficialBundleRequiredError) as exc_info:
        build_request(
            {
                "model": "glm-5",
                "messages": [{"role": "user", "content": "hi"}],
            },
        )

    assert "强制要求 patched 官方 bundle" in str(exc_info.value)
