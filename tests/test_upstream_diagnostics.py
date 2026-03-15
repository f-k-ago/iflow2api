import logging

from iflow2api.upstream_diagnostics import (
    api_key_fingerprint,
    build_lease_debug_context,
    extract_upstream_payload_preview,
    log_upstream_failure,
)
from iflow2api.proxy import UpstreamAPIError


class _DummyAccount:
    id = "acc-1"
    label = "primary"
    auth_type = "oauth-iflow"
    base_url = "https://apis.iflow.cn/v1"
    api_key = "sk-demo-secret"
    session_id = ""
    conversation_id = "conversation-1"


class _DummyProxyConfig:
    auth_type = "oauth-iflow"
    base_url = "https://apis.iflow.cn/v1"
    api_key = "sk-demo-secret"


class _DummyProxy:
    config = _DummyProxyConfig()
    _session_id = "session-runtime"
    _conversation_id = "conversation-1"


class _DummyLease:
    account = _DummyAccount()
    proxy = _DummyProxy()


def test_api_key_fingerprint_hides_plaintext() -> None:
    fingerprint = api_key_fingerprint("sk-demo-secret")

    assert fingerprint != "sk-demo-secret"
    assert len(fingerprint) == 12


def test_build_lease_debug_context_extracts_request_shape() -> None:
    context = build_lease_debug_context(
        _DummyLease(),
        {
            "model": "glm-5",
            "messages": [{"role": "user", "content": "hi"}],
            "tools": [{"type": "function"}],
        },
        endpoint="openai.chat_completions",
        stream=False,
    )

    assert context == {
        "endpoint": "openai.chat_completions",
        "stream": False,
        "account_id": "acc-1",
        "label": "primary",
        "auth_type": "oauth-iflow",
        "base_url": "https://apis.iflow.cn/v1",
        "api_key_fp": api_key_fingerprint("sk-demo-secret"),
        "has_session_id": True,
        "has_conversation_id": True,
        "model": "glm-5",
        "message_count": 1,
        "tool_count": 1,
    }


def test_extract_upstream_payload_preview_uses_business_payload() -> None:
    exc = UpstreamAPIError(
        400,
        "Model not support",
        error_type="invalid_request_error",
        payload={"status": "435", "msg": "Model not support", "details": {"model": "glm-5"}},
    )

    preview = extract_upstream_payload_preview(exc)

    assert '"status": "435"' in preview
    assert '"model": "glm-5"' in preview


def test_log_upstream_failure_records_safe_context(caplog) -> None:
    exc = UpstreamAPIError(
        400,
        "Model not support",
        error_type="invalid_request_error",
        payload={"status": "435", "msg": "Model not support"},
    )

    with caplog.at_level(logging.WARNING, logger="iflow2api"):
        log_upstream_failure(
            exc,
            status_code=400,
            error_msg="Model not support",
            error_type="invalid_request_error",
            lease=_DummyLease(),
            request_body={
                "model": "glm-5",
                "messages": [{"role": "user", "content": "hi"}],
                "tools": [{"type": "function"}],
            },
            endpoint="openai.chat_completions",
            stream=False,
        )

    assert "api_key_fp=" in caplog.text
    assert "sk-demo-secret" not in caplog.text
    assert "payload_preview=" in caplog.text
    assert '"status": "435"' in caplog.text
