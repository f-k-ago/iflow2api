import json
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

import iflow2api.admin.auth as admin_auth_module
import iflow2api.admin.websocket as admin_websocket_module
import iflow2api.app as app_module
import iflow2api.oauth as oauth_module
from iflow2api.admin.auth import ADMIN_SESSION_COOKIE_NAME
from iflow2api.settings import load_settings, save_settings


class _NoopRefresher:
    def __init__(self, *args, **kwargs):
        self._callback = None

    def set_refresh_callback(self, callback):
        self._callback = callback

    def start(self):
        return None

    def stop(self):
        return None


class _FakeOAuth:
    def get_auth_url(self, redirect_uri: str, state: str | None = None) -> str:
        return f"https://iflow.cn/oauth?redirect={redirect_uri}&state={state}"

    async def get_token(self, code: str, redirect_uri: str = "") -> dict:
        assert code == "ok-code"
        assert redirect_uri
        return {
            "access_token": "oauth-access-token",
            "refresh_token": "oauth-refresh-token",
            "expires_at": datetime(2026, 3, 20, 12, 0, 0),
        }

    async def get_user_info(self, access_token: str) -> dict:
        assert access_token == "oauth-access-token"
        return {
            "apiKey": "iflow-api-key",
            "email": "demo@example.com",
        }


@pytest.fixture()
def isolated_admin_state(tmp_path, monkeypatch):
    config_dir = tmp_path / ".iflow2api"
    config_dir.mkdir()

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr("iflow2api.settings.get_config_dir", lambda: config_dir)
    monkeypatch.setattr(app_module, "OAuthTokenRefresher", _NoopRefresher)

    app_module.invalidate_settings_cache()
    app_module._proxy = None
    app_module._config = None
    app_module._refresher = None
    admin_auth_module._auth_manager = None
    admin_websocket_module._connection_manager = None

    yield config_dir

    app_module.invalidate_settings_cache()
    app_module._proxy = None
    app_module._config = None
    app_module._refresher = None
    admin_auth_module._auth_manager = None
    admin_websocket_module._connection_manager = None


def _login_headers(client: TestClient) -> dict[str, str]:
    response = client.post(
        "/admin/login",
        json={"username": "admin", "password": "secret-pass"},
    )
    assert response.status_code == 200, response.text
    token = response.json()["token"]
    return {"Authorization": f"Bearer {token}"}


def test_login_sets_http_only_cookie_and_status_accepts_cookie(
    isolated_admin_state,
) -> None:
    with TestClient(app_module.app) as client:
        response = client.post(
            "/admin/login",
            json={"username": "admin", "password": "secret-pass"},
        )
        assert response.status_code == 200
        set_cookie = response.headers["set-cookie"]
        assert f"{ADMIN_SESSION_COOKIE_NAME}=" in set_cookie
        assert "HttpOnly" in set_cookie
        assert "SameSite=lax" in set_cookie

        status_response = client.get("/admin/status")

    assert status_response.status_code == 200


def test_logout_clears_session_cookie(isolated_admin_state) -> None:
    with TestClient(app_module.app) as client:
        login_response = client.post(
            "/admin/login",
            json={"username": "admin", "password": "secret-pass"},
        )
        assert login_response.status_code == 200

        logout_response = client.post("/admin/logout")

    assert logout_response.status_code == 200
    assert f"{ADMIN_SESSION_COOKIE_NAME}=\"\"" in logout_response.headers["set-cookie"]


def test_login_fails_closed_when_admin_user_store_is_invalid(isolated_admin_state) -> None:
    invalid_store = isolated_admin_state / "admin_users.json"
    invalid_store.write_text("{not-json", encoding="utf-8")
    admin_auth_module._auth_manager = None

    with TestClient(app_module.app) as client:
        setup_response = client.get("/admin/check-setup")
        assert setup_response.status_code == 200
        assert setup_response.json()["load_error"] is True

        response = client.post(
            "/admin/login",
            json={"username": "admin", "password": "secret-pass"},
        )

    assert response.status_code == 503
    assert "管理员用户配置加载失败" in response.json()["detail"]


def test_oauth_callback_page_escapes_reflected_values(isolated_admin_state) -> None:
    injected_code = "</script><script>alert(1)</script>"

    with TestClient(app_module.app) as client:
        response = client.get(
            "/admin/oauth/callback",
            params={"code": injected_code, "state": "demo-state"},
        )

    assert response.status_code == 200
    assert injected_code not in response.text
    assert "\\u003c/script\\u003e\\u003cscript\\u003ealert(1)\\u003c/script\\u003e" in response.text
    assert "postMessage(payload, window.location.origin)" in response.text
    assert "}, '*')" not in response.text


def test_oauth_state_is_required_and_single_use(isolated_admin_state, monkeypatch) -> None:
    monkeypatch.setattr(oauth_module, "IFlowOAuth", _FakeOAuth)

    with TestClient(app_module.app) as client:
        headers = _login_headers(client)
        url_response = client.get("/admin/oauth/url", headers=headers)
        assert url_response.status_code == 200
        state = url_response.json()["state"]

        missing_state = client.post(
            "/admin/oauth/callback",
            headers=headers,
            json={"code": "ok-code"},
        )
        assert missing_state.status_code == 400
        assert "缺少 state" in missing_state.json()["detail"]

        invalid_state = client.post(
            "/admin/oauth/callback",
            headers=headers,
            json={"code": "ok-code", "state": "bad-state"},
        )
        assert invalid_state.status_code == 400
        assert "state 无效或已过期" in invalid_state.json()["detail"]

        success = client.post(
            "/admin/oauth/callback",
            headers=headers,
            json={"code": "ok-code", "state": state},
        )
        assert success.status_code == 200
        assert success.json()["success"] is True

        replay = client.post(
            "/admin/oauth/callback",
            headers=headers,
            json={"code": "ok-code", "state": state},
        )
        assert replay.status_code == 400
        assert "state 无效或已过期" in replay.json()["detail"]

    settings = load_settings()
    assert settings.upstream_accounts[0].api_key == "iflow-api-key"
    assert settings.upstream_accounts[0].oauth_access_token == "oauth-access-token"


def test_admin_websocket_supports_first_message_auth(isolated_admin_state) -> None:
    with TestClient(app_module.app) as client:
        headers = _login_headers(client)
        token = headers["Authorization"].split(" ", 1)[1]

        with client.websocket_connect("/admin/ws") as websocket:
            websocket.send_json({"type": "auth", "token": token})
            response = websocket.receive_json()

    assert response == {"type": "auth_success", "username": "admin"}


def test_admin_websocket_accepts_cookie_session_without_auth_message(
    isolated_admin_state,
) -> None:
    with TestClient(app_module.app) as client:
        login_response = client.post(
            "/admin/login",
            json={"username": "admin", "password": "secret-pass"},
        )
        assert login_response.status_code == 200

        with client.websocket_connect("/admin/ws") as websocket:
            websocket.send_json({"type": "ping"})
            response = websocket.receive_json()

    assert response["type"] == "pong"


def test_settings_roundtrip_persists_concurrency_fields(isolated_admin_state) -> None:
    settings = load_settings()
    settings.enable_concurrency_limit = False
    settings.max_concurrent_requests = 3
    save_settings(settings)

    reloaded = load_settings()
    config_payload = json.loads((isolated_admin_state / "config.json").read_text(encoding="utf-8"))

    assert reloaded.enable_concurrency_limit is False
    assert reloaded.max_concurrent_requests == 3
    assert config_payload["enable_concurrency_limit"] is False
    assert config_payload["max_concurrent_requests"] == 3


def test_settings_update_validates_range_and_logs_tail(isolated_admin_state) -> None:
    log_dir = isolated_admin_state / "logs"
    log_dir.mkdir()
    (log_dir / "app.log").write_text("one\ntwo\nthree\nfour\n", encoding="utf-8")

    with TestClient(app_module.app) as client:
        headers = _login_headers(client)

        invalid = client.put(
            "/admin/settings",
            headers=headers,
            json={"max_queued_requests": -1},
        )
        assert invalid.status_code == 422

        logs_response = client.get("/admin/logs?lines=2", headers=headers)
        assert logs_response.status_code == 200
        assert logs_response.json()["logs"] == ["three", "four"]
        assert logs_response.json()["total_lines"] == 4

        too_many = client.get("/admin/logs?lines=5000", headers=headers)
        assert too_many.status_code == 422


def test_settings_response_masks_sensitive_values_and_preserves_on_masked_update(
    isolated_admin_state,
) -> None:
    settings = load_settings()
    settings.custom_api_key = "sk-secret-1234567890"
    settings.custom_auth_header = "X-API-Key"
    settings.upstream_proxy = "http://user:secret@proxy.example.com:8080"
    settings.upstream_proxy_enabled = True
    save_settings(settings)

    with TestClient(app_module.app) as client:
        headers = _login_headers(client)

        response = client.get("/admin/settings", headers=headers)
        assert response.status_code == 200
        payload = response.json()

        assert payload["custom_api_key"] != "sk-secret-1234567890"
        assert "*" in payload["custom_api_key"]
        assert payload["custom_api_key_configured"] is True
        assert payload["custom_auth_header"] == "X-API-Key"

        assert payload["upstream_proxy"] != "http://user:secret@proxy.example.com:8080"
        assert "***" in payload["upstream_proxy"]
        assert payload["upstream_proxy_configured"] is True

        update_response = client.put(
            "/admin/settings",
            headers=headers,
            json={
                "theme_mode": "dark",
                "custom_api_key": payload["custom_api_key"],
                "upstream_proxy": payload["upstream_proxy"],
            },
        )
        assert update_response.status_code == 200

    reloaded = load_settings()
    assert reloaded.theme_mode == "dark"
    assert reloaded.custom_api_key == "sk-secret-1234567890"
    assert reloaded.upstream_proxy == "http://user:secret@proxy.example.com:8080"


def test_settings_update_can_clear_sensitive_values(isolated_admin_state) -> None:
    settings = load_settings()
    settings.custom_api_key = "sk-secret-1234567890"
    settings.upstream_proxy = "http://user:secret@proxy.example.com:8080"
    settings.upstream_proxy_enabled = True
    save_settings(settings)

    with TestClient(app_module.app) as client:
        headers = _login_headers(client)
        response = client.put(
            "/admin/settings",
            headers=headers,
            json={
                "custom_api_key": "",
                "upstream_proxy": "",
                "upstream_proxy_enabled": False,
            },
        )

    assert response.status_code == 200
    reloaded = load_settings()
    assert reloaded.custom_api_key == ""
    assert reloaded.upstream_proxy == ""
    assert reloaded.upstream_proxy_enabled is False
