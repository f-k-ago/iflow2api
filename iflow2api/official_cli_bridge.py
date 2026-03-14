"""官方 iflow-cli request builder 桥接。"""

from __future__ import annotations

import asyncio
import base64
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncIterator

from .transport import (
    NodeFetchBridgeError,
    NodeFetchTransport,
    UpstreamResponse,
    _BufferedNodeFetchRawResponse,
    _StreamingNodeFetchRawResponse,
)

_OFFICIAL_IFLOW_CLI_BRIDGE = Path(__file__).with_name("iflow_cli_bridge.mjs")


@dataclass(slots=True)
class OfficialChatRequest:
    """官方 builder 生成的最终上游请求。"""

    url: str
    headers: dict[str, str]
    body: dict[str, Any]
    meta: dict[str, Any]


class OfficialIFlowCLITransport(NodeFetchTransport):
    """通过 Node bridge 复用官方 iflow-cli 请求构造与发送链。"""

    def __init__(self, *, timeout: float, proxy: str | None):
        super().__init__(
            timeout=timeout,
            follow_redirects=True,
            proxy=proxy,
            worker_script=_OFFICIAL_IFLOW_CLI_BRIDGE,
        )

    @staticmethod
    def _build_payload(
        *,
        action: str,
        payload: dict[str, Any],
        stream: bool,
        timeout: float,
    ) -> dict[str, Any]:
        return {
            "action": action,
            "request_id": uuid.uuid4().hex,
            "payload": payload,
            "stream": stream,
            "timeout_ms": int(timeout * 1000),
        }

    async def build_chat_request(
        self,
        payload: dict[str, Any],
        *,
        timeout: float | None = None,
    ) -> OfficialChatRequest:
        request_payload = self._build_payload(
            action="build_chat_request",
            payload=payload,
            stream=False,
            timeout=timeout or self._timeout,
        )
        request_id = str(request_payload["request_id"])

        await self._request_lock.acquire()
        try:
            await self._send_bridge_request(request_payload)
            result_payload: dict[str, Any] | None = None
            while True:
                event = await self._read_bridge_event(request_id)
                event_type = event.get("type")
                if event_type == "result":
                    payload_obj = event.get("payload")
                    if not isinstance(payload_obj, dict):
                        raise NodeFetchBridgeError("官方 builder 返回了非法 payload")
                    result_payload = payload_obj
                    continue
                if event_type == "end":
                    break
                if event_type == "error":
                    raise NodeFetchBridgeError(str(event.get("message") or "官方 builder 请求失败"))
                raise NodeFetchBridgeError(f"官方 builder 返回了未知事件: {event_type!r}")
        finally:
            self._release_request_lock()

        if result_payload is None:
            raise NodeFetchBridgeError("官方 builder 未返回请求结果")

        return OfficialChatRequest(
            url=str(result_payload.get("url") or ""),
            headers=dict(result_payload.get("headers") or {}),
            body=dict(result_payload.get("body") or {}),
            meta=dict(result_payload.get("meta") or {}),
        )

    async def request_chat_completions(
        self,
        payload: dict[str, Any],
        *,
        timeout: float | None = None,
    ) -> UpstreamResponse:
        request_payload = self._build_payload(
            action="chat_completions",
            payload=payload,
            stream=False,
            timeout=timeout or self._timeout,
        )
        request_id = str(request_payload["request_id"])

        await self._request_lock.acquire()
        try:
            await self._send_bridge_request(request_payload)
            status_code, response_headers = await self._await_meta(request_id)

            chunks: list[bytes] = []
            while True:
                event = await self._read_bridge_event(request_id)
                event_type = event.get("type")
                if event_type == "chunk":
                    chunks.append(base64.b64decode(event.get("data_base64") or ""))
                    continue
                if event_type == "end":
                    break
                if event_type == "aborted":
                    raise NodeFetchBridgeError("官方 bridge 请求已取消")
                if event_type == "error":
                    raise NodeFetchBridgeError(str(event.get("message") or "官方 bridge 请求失败"))
                raise NodeFetchBridgeError(f"官方 bridge 返回了未知事件: {event_type!r}")
        except asyncio.CancelledError:
            try:
                await self._cancel_request(request_id)
                await self._drain_terminal_event(request_id)
            except Exception:
                await self._reset_worker()
            raise
        finally:
            self._release_request_lock()

        return UpstreamResponse(
            _BufferedNodeFetchRawResponse(
                status_code=status_code,
                headers=response_headers,
                content=b"".join(chunks),
            )
        )

    @asynccontextmanager
    async def stream_chat_completions(
        self,
        payload: dict[str, Any],
        *,
        timeout: float | None = None,
    ) -> AsyncIterator[UpstreamResponse]:
        request_payload = self._build_payload(
            action="chat_completions",
            payload=payload,
            stream=True,
            timeout=timeout or self._timeout,
        )
        request_id = str(request_payload["request_id"])

        await self._request_lock.acquire()
        response: UpstreamResponse | None = None

        try:
            await self._send_bridge_request(request_payload)
            status_code, response_headers = await self._await_meta(request_id)
            response = UpstreamResponse(
                _StreamingNodeFetchRawResponse(
                    transport=self,
                    request_id=request_id,
                    status_code=status_code,
                    headers=response_headers,
                )
            )
            yield response
        finally:
            if response is not None:
                await response.raw.aclose()
            else:
                self._release_request_lock()
