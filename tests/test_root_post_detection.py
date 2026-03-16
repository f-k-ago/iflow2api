from __future__ import annotations

from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

import iflow2api.app as app_module


async def _openai_handler(request):
    body = await request.json()
    return JSONResponse({"handler": "openai", "body": body})


async def _anthropic_handler(request):
    body = await request.json()
    return JSONResponse({"handler": "anthropic", "body": body})


def test_root_post_routes_openai_body_to_chat_handler(monkeypatch):
    monkeypatch.setattr(app_module, "chat_completions_openai", _openai_handler)
    monkeypatch.setattr(app_module, "messages_anthropic", _anthropic_handler)

    with TestClient(app_module.app) as client:
        response = client.post(
            "/",
            json={
                "model": "glm-5",
                "messages": [{"role": "user", "content": "hi"}],
                "stream": False,
            },
        )

    assert response.status_code == 200
    assert response.json()["handler"] == "openai"


def test_root_post_routes_claude_body_to_anthropic_handler(monkeypatch):
    monkeypatch.setattr(app_module, "chat_completions_openai", _openai_handler)
    monkeypatch.setattr(app_module, "messages_anthropic", _anthropic_handler)

    with TestClient(app_module.app) as client:
        response = client.post(
            "/v1/",
            json={
                "model": "claude-3-5-sonnet",
                "max_tokens": 1024,
                "messages": [{"role": "user", "content": "hi"}],
                "stream": False,
            },
        )

    assert response.status_code == 200
    assert response.json()["handler"] == "anthropic"


def test_root_post_routes_anthropic_tools_shape_to_messages_handler(monkeypatch):
    monkeypatch.setattr(app_module, "chat_completions_openai", _openai_handler)
    monkeypatch.setattr(app_module, "messages_anthropic", _anthropic_handler)

    with TestClient(app_module.app) as client:
        response = client.post(
            "/",
            json={
                "model": "glm-5",
                "max_tokens": 1000,
                "messages": [{"role": "user", "content": "hi"}],
                "tools": [
                    {
                        "name": "lookup_weather",
                        "input_schema": {
                            "type": "object",
                            "properties": {"city": {"type": "string"}},
                        },
                    }
                ],
            },
        )

    assert response.status_code == 200
    assert response.json()["handler"] == "anthropic"


def test_root_post_invalid_json_returns_openai_style_400():
    with TestClient(app_module.app) as client:
        response = client.post(
            "/",
            content="{bad json",
            headers={"Content-Type": "application/json"},
        )

    assert response.status_code == 400
    payload = response.json()
    assert payload["error"]["type"] == "invalid_request_error"
    assert "Invalid JSON" in payload["error"]["message"]
