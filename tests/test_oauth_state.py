from __future__ import annotations

from pathlib import Path

from iflow2api.admin.oauth_state import OAuthStateManager


def _make_manager(tmp_path: Path, *, ttl_seconds: int = 600) -> OAuthStateManager:
    return OAuthStateManager(
        "secret-key",
        store_path=tmp_path / "oauth_state_replay.json",
        ttl_seconds=ttl_seconds,
    )


def test_oauth_state_issue_and_consume_once(tmp_path) -> None:
    manager = _make_manager(tmp_path)

    state = manager.issue("admin")

    assert manager.consume("admin", state) is True
    assert manager.consume("admin", state) is False


def test_oauth_state_rejects_tampered_payload(tmp_path) -> None:
    manager = _make_manager(tmp_path)
    state = manager.issue("admin")
    version, payload, signature = state.split(".", 2)
    tampered_payload = payload[:-1] + ("A" if payload[-1] != "A" else "B")

    assert manager.consume("admin", f"{version}.{tampered_payload}.{signature}") is False


def test_oauth_state_rejects_wrong_user(tmp_path) -> None:
    manager = _make_manager(tmp_path)
    state = manager.issue("admin")

    assert manager.consume("other-user", state) is False


def test_oauth_state_persists_replay_protection_across_instances(tmp_path) -> None:
    state = _make_manager(tmp_path).issue("admin")

    assert _make_manager(tmp_path).consume("admin", state) is True
    assert _make_manager(tmp_path).consume("admin", state) is False
