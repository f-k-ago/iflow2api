import json
from uuid import UUID, uuid4

from iflow2api.config import DEFAULT_BASE_URL, IFlowConfig
from iflow2api.proxy import IFlowProxy
from iflow2api.request_identity import ensure_request_ids, normalize_request_ids
from iflow2api.settings import load_settings


def test_normalize_request_ids_preserves_official_cli_shape() -> None:
    session_id = f"session-{uuid4()}"
    conversation_id = str(uuid4())

    normalized_session_id, normalized_conversation_id = normalize_request_ids(
        session_id,
        conversation_id,
    )

    assert normalized_session_id == session_id
    assert normalized_conversation_id == conversation_id


def test_ensure_request_ids_generates_missing_values() -> None:
    session_id, conversation_id, generated = ensure_request_ids("", None)

    assert generated is True
    assert session_id.startswith("session-")
    UUID(session_id.removeprefix("session-"))
    UUID(conversation_id)


def test_proxy_generates_request_ids_when_config_is_empty() -> None:
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

    assert payload["sessionId"].startswith("session-")
    UUID(payload["sessionId"].removeprefix("session-"))
    UUID(payload["conversationId"])


def test_load_settings_preserves_persisted_request_ids(tmp_path, monkeypatch) -> None:
    session_id = f"session-{uuid4()}"
    conversation_id = str(uuid4())
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
                        "session_id": session_id,
                        "conversation_id": conversation_id,
                    }
                ],
                "upstream_transport_backend": "node_fetch",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr("iflow2api.settings.get_config_dir", lambda: config_dir)

    settings = load_settings()

    assert settings.upstream_accounts[0].session_id == session_id
    assert settings.upstream_accounts[0].conversation_id == conversation_id

    saved_data = json.loads(config_path.read_text(encoding="utf-8"))
    assert saved_data["upstream_accounts"][0]["session_id"] == session_id
    assert saved_data["upstream_accounts"][0]["conversation_id"] == conversation_id
