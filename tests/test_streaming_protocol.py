from __future__ import annotations

from dataclasses import dataclass

import pytest
from fastapi.testclient import TestClient

import iflow2api.app as app_module
from iflow2api.proxy import UpstreamAPIError


class _NoopRefresher:
    def __init__(self, *args, **kwargs):
        self._callback = None

    def set_refresh_callback(self, callback):
        self._callback = callback

    def start(self):
        return None

    def stop(self):
        return None


@dataclass
class _FakeAccount:
    id: str = "acct-1"
    label: str = "demo"


class _FakeLease:
    def __init__(self, proxy):
        self.proxy = proxy
        self.account = _FakeAccount()
        self.closed = False

    async def close(self):
        self.closed = True


class _FakeProxy:
    def __init__(self, stream_factory):
        self._stream_factory = stream_factory

    async def chat_completions(self, request_body, stream=False, apply_concurrency_limit=False):
        assert stream is True
        return self._stream_factory()


@pytest.fixture()
def isolated_public_app(tmp_path, monkeypatch):
    config_dir = tmp_path / ".iflow2api"
    config_dir.mkdir()

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr("iflow2api.settings.get_config_dir", lambda: config_dir)
    monkeypatch.setattr(app_module, "OAuthTokenRefresher", _NoopRefresher)

    app_module.invalidate_settings_cache()
    app_module._proxy = None
    app_module._config = None
    app_module._refresher = None

    yield config_dir

    app_module.invalidate_settings_cache()
    app_module._proxy = None
    app_module._config = None
    app_module._refresher = None


def test_openai_stream_does_not_duplicate_done_marker(isolated_public_app, monkeypatch):
    async def stream_gen():
        yield (
            b'data: {"id":"chatcmpl-1","object":"chat.completion.chunk","created":1,'
            b'"model":"glm-5","choices":[{"index":0,"delta":{"content":"hi"},"finish_reason":null}]}'
            b"\n\n"
        )
        yield b"data: [DONE]\n\n"

    lease = _FakeLease(_FakeProxy(stream_gen))

    async def acquire_lease():
        return lease

    monkeypatch.setattr(app_module, "_acquire_upstream_lease", acquire_lease)

    with TestClient(app_module.app) as client:
        response = client.post(
            "/v1/chat/completions",
            json={
                "model": "glm-5",
                "stream": True,
                "messages": [{"role": "user", "content": "hi"}],
            },
        )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.text.count("data: [DONE]") == 1
    assert lease.closed is True


def test_openai_stream_business_error_returns_json_error_before_stream_start(
    isolated_public_app,
    monkeypatch,
):
    async def stream_gen():
        raise UpstreamAPIError(400, "Model not support", error_type="invalid_request_error")
        yield b""  # pragma: no cover

    lease = _FakeLease(_FakeProxy(stream_gen))

    async def acquire_lease():
        return lease

    monkeypatch.setattr(app_module, "_acquire_upstream_lease", acquire_lease)

    with TestClient(app_module.app) as client:
        response = client.post(
            "/v1/chat/completions",
            json={
                "model": "glm-5",
                "stream": True,
                "messages": [{"role": "user", "content": "hi"}],
            },
        )

    assert response.status_code == 400
    assert response.headers["content-type"].startswith("application/json")
    assert response.json()["error"]["message"] == "Model not support"
    assert response.json()["error"]["type"] == "invalid_request_error"
    assert lease.closed is True


def test_openai_stream_midstream_exception_still_emits_done(
    isolated_public_app,
    monkeypatch,
):
    async def stream_gen():
        yield (
            b'data: {"id":"chatcmpl-1","object":"chat.completion.chunk","created":1,'
            b'"model":"glm-5","choices":[{"index":0,"delta":{"content":"hi"},"finish_reason":null}]}'
            b"\n\n"
        )
        raise RuntimeError("upstream stream broke")
        yield b""  # pragma: no cover

    lease = _FakeLease(_FakeProxy(stream_gen))

    async def acquire_lease():
        return lease

    monkeypatch.setattr(app_module, "_acquire_upstream_lease", acquire_lease)

    with TestClient(app_module.app) as client:
        response = client.post(
            "/v1/chat/completions",
            json={
                "model": "glm-5",
                "stream": True,
                "messages": [{"role": "user", "content": "hi"}],
            },
        )

    assert response.status_code == 200
    assert response.text.count("data: [DONE]") == 1
    assert lease.closed is True


@pytest.mark.parametrize("endpoint", ["/v1/messages", "/messages"])
def test_anthropic_invalid_json_uses_anthropic_error_envelope(
    isolated_public_app,
    endpoint,
):
    with TestClient(app_module.app) as client:
        response = client.post(
            endpoint,
            content="{bad json",
            headers={"Content-Type": "application/json"},
        )

    assert response.status_code == 400
    payload = response.json()
    assert payload["type"] == "error"
    assert payload["error"]["type"] == "invalid_request_error"
    assert "Invalid JSON" in payload["error"]["message"]


def test_anthropic_stream_business_error_returns_anthropic_error_before_stream_start(
    isolated_public_app,
    monkeypatch,
):
    async def stream_gen():
        raise UpstreamAPIError(429, "Rate limit reached", error_type="rate_limit_exceeded")
        yield b""  # pragma: no cover

    lease = _FakeLease(_FakeProxy(stream_gen))

    async def acquire_lease():
        return lease

    monkeypatch.setattr(app_module, "_acquire_upstream_lease", acquire_lease)

    with TestClient(app_module.app) as client:
        response = client.post(
            "/v1/messages",
            json={
                "model": "claude-3-5-sonnet",
                "stream": True,
                "messages": [{"role": "user", "content": "hi"}],
            },
        )

    assert response.status_code == 429
    assert response.headers["content-type"].startswith("application/json")
    assert response.json() == {
        "type": "error",
        "error": {
            "type": "rate_limit_error",
            "message": "Rate limit reached",
        },
    }
    assert lease.closed is True


def test_anthropic_nonstream_business_error_uses_same_error_shape(
    isolated_public_app,
    monkeypatch,
):
    lease = _FakeLease(proxy=None)

    async def acquire_lease():
        return lease

    async def fail_nonstream(lease, request_body):
        raise UpstreamAPIError(429, "Rate limit reached", error_type="rate_limit_exceeded")

    monkeypatch.setattr(app_module, "_acquire_upstream_lease", acquire_lease)
    monkeypatch.setattr(app_module, "_run_nonstream_with_lease", fail_nonstream)

    with TestClient(app_module.app) as client:
        response = client.post(
            "/v1/messages",
            json={
                "model": "claude-3-5-sonnet",
                "stream": False,
                "messages": [{"role": "user", "content": "hi"}],
            },
        )

    assert response.status_code == 429
    assert response.json() == {
        "type": "error",
        "error": {
            "type": "rate_limit_error",
            "message": "Rate limit reached",
        },
    }
    assert lease.closed is True
