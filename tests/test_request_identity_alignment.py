import json
from uuid import uuid4

from iflow2api.config import DEFAULT_BASE_URL, IFlowConfig
from iflow2api.proxy import IFlowProxy
from iflow2api.request_identity import strip_legacy_generated_request_ids
from iflow2api.settings import load_settings


def test_strip_legacy_generated_request_ids_clears_old_auto_values():
    session_id = f"session-{uuid4()}"
    conversation_id = str(uuid4())

    normalized_session_id, normalized_conversation_id, cleared = (
        strip_legacy_generated_request_ids(
            session_id,
            conversation_id,
        )
    )

    assert cleared is True
    assert normalized_session_id == ""
    assert normalized_conversation_id == ""


def test_proxy_no_longer_generates_request_ids_when_config_is_empty():
    proxy = IFlowProxy(
        IFlowConfig(
            api_key="sk-test",
            base_url=DEFAULT_BASE_URL,
            session_id=None,
            conversation_id=None,
        )
    )

    payload = proxy._build_official_chat_request_payload(
        {"model": "glm-5", "messages": [{"role": "user", "content": "hi"}]},
        stream=False,
        traceparent="",
    )

    assert payload["sessionId"] == ""
    assert payload["conversationId"] == ""


def test_load_settings_clears_persisted_legacy_generated_request_ids(tmp_path, monkeypatch):
    config_dir = tmp_path / ".iflow2api"
    config_dir.mkdir()
    config_path = config_dir / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "upstream_accounts": [
                    {
                        "id": "acc-1",
                        "label": "test",
                        "enabled": True,
                        "auth_type": "oauth-iflow",
                        "api_key": "sk-test",
                        "base_url": DEFAULT_BASE_URL,
                        "session_id": f"session-{uuid4()}",
                        "conversation_id": str(uuid4()),
                    }
                ],
                "upstream_transport_backend": "node_fetch",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr("iflow2api.settings.get_config_dir", lambda: config_dir)

    settings = load_settings()

    assert settings.upstream_accounts[0].session_id == ""
    assert settings.upstream_accounts[0].conversation_id == ""

    saved_data = json.loads(config_path.read_text(encoding="utf-8"))
    assert saved_data["upstream_accounts"][0]["session_id"] == ""
    assert saved_data["upstream_accounts"][0]["conversation_id"] == ""
