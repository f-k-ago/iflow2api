"""独立的 Anthropic Messages -> OpenAI Chat Completions 适配服务。"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

import httpx
import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from iflow2api.anthropic_compat import (
    anthropic_to_openai_request,
    openai_to_anthropic_response,
)
from iflow2api.messages_adapter import (
    create_anthropic_error_response,
    create_anthropic_streaming_response,
    extract_error_payload,
)

logger = logging.getLogger("anthropic_messages_adapter")

UPSTREAM_CHAT_COMPLETIONS_URL = os.getenv(
    "ANTHROPIC_ADAPTER_UPSTREAM_CHAT_COMPLETIONS_URL",
    "http://127.0.0.1:28000/v1/chat/completions",
)
HOST = os.getenv("ANTHROPIC_ADAPTER_HOST", "0.0.0.0")
PORT = int(os.getenv("ANTHROPIC_ADAPTER_PORT", "28001"))
REQUEST_TIMEOUT_SECONDS = float(os.getenv("ANTHROPIC_ADAPTER_REQUEST_TIMEOUT_SECONDS", "300"))
PRESERVE_REASONING = os.getenv("ANTHROPIC_ADAPTER_PRESERVE_REASONING", "false").strip().lower() in {
    "1",
    "true",
    "yes",
}

app = FastAPI(
    title="Standalone Anthropic Messages Adapter",
    version="0.1.0",
    description="将 Anthropic /v1/messages 独立转换并转发到上游 /v1/chat/completions。",
)


def _extract_access_token(request: Request) -> str:
    authorization = request.headers.get("authorization", "").strip()
    if authorization.lower().startswith("bearer "):
        token = authorization[7:].strip()
        if token:
            return token
    return request.headers.get("x-api-key", "").strip()


def _build_upstream_headers(request: Request) -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    token = _extract_access_token(request)
    if token:
        headers["Authorization"] = f"Bearer {token}"
    traceparent = request.headers.get("traceparent", "").strip()
    if traceparent:
        headers["traceparent"] = traceparent
    return headers


def _build_timeout() -> httpx.Timeout:
    return httpx.Timeout(REQUEST_TIMEOUT_SECONDS, connect=min(30.0, REQUEST_TIMEOUT_SECONDS))


async def _create_streaming_response(
    response: httpx.Response,
    client: httpx.AsyncClient,
    *,
    mapped_model: str,
):
    async def close_resources() -> None:
        await response.aclose()
        await client.aclose()

    return create_anthropic_streaming_response(
        response.aiter_text(),
        mapped_model=mapped_model,
        preserve_reasoning=PRESERVE_REASONING,
        on_close=close_resources,
    )


@app.get("/healthz")
async def healthz() -> dict[str, Any]:
    return {
        "ok": True,
        "upstream_chat_completions_url": UPSTREAM_CHAT_COMPLETIONS_URL,
        "preserve_reasoning": PRESERVE_REASONING,
    }


@app.post("/v1/messages")
@app.post("/v1/messages/")
@app.post("/messages")
@app.post("/messages/")
@app.post("/api/v1/messages")
@app.post("/api/v1/messages/")
async def messages(request: Request):
    try:
        body = await request.json()
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail=f"Invalid JSON: {exc}") from exc

    if "messages" not in body:
        return create_anthropic_error_response(422, "Field 'messages' is required", "invalid_request_error")

    openai_body = anthropic_to_openai_request(body)
    mapped_model = openai_body["model"]
    upstream_headers = _build_upstream_headers(request)

    if body.get("stream", False):
        client = httpx.AsyncClient(timeout=_build_timeout(), follow_redirects=True)
        try:
            upstream_request = client.build_request(
                "POST",
                UPSTREAM_CHAT_COMPLETIONS_URL,
                headers=upstream_headers,
                json=openai_body,
            )
            response = await client.send(upstream_request, stream=True)
        except httpx.HTTPError as exc:
            await client.aclose()
            return create_anthropic_error_response(502, str(exc), "api_error")

        if response.status_code >= 400:
            try:
                payload = response.json()
            except Exception:
                payload = await response.aread()
            await response.aclose()
            await client.aclose()
            if isinstance(payload, (bytes, bytearray)):
                message = payload.decode("utf-8", errors="replace")[:500] or "上游返回了错误响应"
                return create_anthropic_error_response(response.status_code, message)
            error_type, message = extract_error_payload(
                payload,
                fallback_message="上游返回了错误响应",
            )
            return create_anthropic_error_response(response.status_code, message, error_type)

        return await _create_streaming_response(response, client, mapped_model=mapped_model)

    try:
        async with httpx.AsyncClient(timeout=_build_timeout(), follow_redirects=True) as client:
            response = await client.post(
                UPSTREAM_CHAT_COMPLETIONS_URL,
                headers=upstream_headers,
                json=openai_body,
            )
    except httpx.HTTPError as exc:
        return create_anthropic_error_response(502, str(exc), "api_error")

    if response.status_code >= 400:
        try:
            payload = response.json()
        except Exception:
            payload = response.text
        if isinstance(payload, dict):
            error_type, message = extract_error_payload(
                payload,
                fallback_message="上游返回了错误响应",
            )
            return create_anthropic_error_response(response.status_code, message, error_type)
        return create_anthropic_error_response(response.status_code, str(payload)[:500] or "上游返回了错误响应")

    openai_result = response.json()
    if isinstance(openai_result, dict) and openai_result.get("error"):
        error_type, message = extract_error_payload(
            openai_result,
            fallback_message="上游返回了错误响应",
        )
        return create_anthropic_error_response(500, message, error_type)

    return JSONResponse(content=openai_to_anthropic_response(openai_result, mapped_model))


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    uvicorn.run(
        "anthropic_messages_adapter:app",
        host=HOST,
        port=PORT,
        reload=False,
        factory=False,
    )
