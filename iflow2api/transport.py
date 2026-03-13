"""上游传输层：支持 httpx、curl_cffi 与 Node fetch bridge。"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import shutil
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator, Literal, Optional

import httpx

logger = logging.getLogger("iflow2api")


TransportBackend = Literal["httpx", "curl_cffi", "node_fetch"]


_NODE_BRIDGE_SCRIPT = Path(__file__).with_name("node_fetch_bridge.mjs")
_PROXY_ENV_KEYS = (
    "NODE_USE_ENV_PROXY",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "NO_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
    "no_proxy",
)


class NodeFetchBridgeError(RuntimeError):
    """Node fetch bridge 调用失败。"""


class NodeFetchHTTPStatusError(RuntimeError):
    """Node fetch 上游返回 HTTP 错误。"""

    def __init__(self, response: "_BufferedNodeFetchRawResponse | _StreamingNodeFetchRawResponse"):
        self.response = response
        super().__init__(f"{response.status_code} Server Error: upstream response")


class _BufferedNodeFetchRawResponse:
    """Node bridge 的缓冲响应包装。"""

    def __init__(self, *, status_code: int, headers: dict[str, str], content: bytes):
        self.status_code = status_code
        self.headers = headers
        self._content = content

    @property
    def text(self) -> str:
        return self._content.decode("utf-8", errors="replace")

    @property
    def content(self) -> bytes:
        return self._content

    def json(self) -> Any:
        return json.loads(self.text or "null")

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise NodeFetchHTTPStatusError(self)

    async def aread(self) -> bytes:
        return self._content

    async def aiter_bytes(self) -> AsyncIterator[bytes]:
        if self._content:
            yield self._content


class _StreamingNodeFetchRawResponse:
    """Node bridge 的流式响应包装。"""

    def __init__(
        self,
        *,
        transport: "NodeFetchTransport",
        request_id: str,
        status_code: int,
        headers: dict[str, str],
    ):
        self._transport = transport
        self._request_id = request_id
        self.status_code = status_code
        self.headers = headers
        self._cached_content: bytes | None = None
        self._stream_consumed = False
        self._ended = False
        self._released = False

    @property
    def text(self) -> str:
        if self._cached_content is None:
            return ""
        return self._cached_content.decode("utf-8", errors="replace")

    @property
    def content(self) -> bytes:
        return self._cached_content or b""

    def json(self) -> Any:
        if self._cached_content is None:
            raise RuntimeError("流式响应尚未读取完成，无法直接解析 JSON")
        return json.loads(self.text or "null")

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise NodeFetchHTTPStatusError(self)

    async def aread(self) -> bytes:
        if self._cached_content is not None:
            return self._cached_content

        chunks = []
        async for chunk in self.aiter_bytes():
            chunks.append(chunk)
        self._cached_content = b"".join(chunks)
        return self._cached_content

    async def aiter_bytes(self) -> AsyncIterator[bytes]:
        if self._cached_content is not None:
            if self._cached_content:
                yield self._cached_content
            return

        if self._stream_consumed:
            return

        chunks: list[bytes] = []
        try:
            while True:
                event = await self._transport._read_bridge_event(self._request_id)
                event_type = event.get("type")
                if event_type == "chunk":
                    chunk = base64.b64decode(event.get("data_base64") or "")
                    chunks.append(chunk)
                    yield chunk
                    continue
                if event_type == "end":
                    self._ended = True
                    break
                if event_type == "aborted":
                    raise NodeFetchBridgeError("Node fetch bridge 请求已取消")
                if event_type == "error":
                    raise NodeFetchBridgeError(str(event.get("message") or "Node fetch bridge 请求失败"))
                raise NodeFetchBridgeError(f"Node fetch bridge 返回了未知事件: {event_type!r}")
        finally:
            self._cached_content = b"".join(chunks)
            self._stream_consumed = True
            await self.aclose()

    async def aclose(self) -> None:
        if self._released:
            return
        self._released = True

        if not self._ended:
            try:
                await self._transport._cancel_request(self._request_id)
                await self._transport._drain_terminal_event(self._request_id)
            except Exception:
                await self._transport._reset_worker()

        self._transport._release_request_lock()


def _node_bridge_payload(
    *,
    method: str,
    url: str,
    headers: Optional[dict[str, str]],
    params: Optional[dict[str, Any]],
    data: Any,
    json_body: Any,
    timeout: Optional[float],
    stream: bool,
    follow_redirects: bool,
    proxy: Optional[str],
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "request_id": uuid.uuid4().hex,
        "method": method,
        "url": url,
        "headers": headers or {},
        "params": params or {},
        "timeout_ms": int((timeout or 300.0) * 1000),
        "stream": stream,
        "follow_redirects": follow_redirects,
        "proxy": proxy or "",
    }
    if json_body is not None:
        payload["json_body"] = json_body
    elif data is not None:
        if isinstance(data, bytes):
            payload["data_base64"] = base64.b64encode(data).decode("ascii")
        else:
            payload["data_text"] = str(data)
    return payload


def _node_bridge_cancel_payload(request_id: str) -> dict[str, Any]:
    return {
        "action": "cancel",
        "request_id": request_id,
    }


def _node_bridge_env(proxy: Optional[str]) -> dict[str, str]:
    env = os.environ.copy()
    for key in _PROXY_ENV_KEYS:
        env.pop(key, None)
    return env


class UpstreamResponse:
    """统一响应包装，屏蔽不同 HTTP 客户端差异。"""

    def __init__(self, raw: Any):
        self.raw = raw

    @property
    def status_code(self) -> int:
        return int(getattr(self.raw, "status_code", 0))

    @property
    def headers(self) -> dict[str, str]:
        headers = getattr(self.raw, "headers", {})
        try:
            return dict(headers)
        except Exception:
            return {}

    @property
    def text(self) -> str:
        value = getattr(self.raw, "text", "")
        return value() if callable(value) else str(value)

    @property
    def content(self) -> bytes:
        value = getattr(self.raw, "content", b"")
        return value() if callable(value) else bytes(value)

    def json(self) -> Any:
        return self.raw.json()

    def raise_for_status(self) -> None:
        self.raw.raise_for_status()

    async def aread(self) -> bytes:
        if hasattr(self.raw, "aread"):
            return await self.raw.aread()
        return self.content

    async def aiter_bytes(self) -> AsyncIterator[bytes]:
        """统一流式字节迭代。"""
        if hasattr(self.raw, "aiter_bytes"):
            async for chunk in self.raw.aiter_bytes():
                yield chunk
            return

        if hasattr(self.raw, "aiter_content"):
            async for chunk in self.raw.aiter_content():
                yield chunk
            return

        if hasattr(self.raw, "iter_bytes"):
            for chunk in self.raw.iter_bytes():
                yield chunk
            return

        if hasattr(self.raw, "iter_content"):
            for chunk in self.raw.iter_content():
                yield chunk
            return

        content = await self.aread()
        if content:
            yield content


class BaseUpstreamTransport:
    """统一传输层接口。"""

    async def request(
        self,
        method: str,
        url: str,
        *,
        headers: Optional[dict[str, str]] = None,
        params: Optional[dict[str, Any]] = None,
        data: Any = None,
        json_body: Any = None,
        timeout: Optional[float] = None,
    ) -> UpstreamResponse:
        raise NotImplementedError

    async def get(
        self,
        url: str,
        *,
        headers: Optional[dict[str, str]] = None,
        params: Optional[dict[str, Any]] = None,
        timeout: Optional[float] = None,
    ) -> UpstreamResponse:
        return await self.request(
            "GET",
            url,
            headers=headers,
            params=params,
            timeout=timeout,
        )

    async def post(
        self,
        url: str,
        *,
        headers: Optional[dict[str, str]] = None,
        data: Any = None,
        json_body: Any = None,
        timeout: Optional[float] = None,
    ) -> UpstreamResponse:
        return await self.request(
            "POST",
            url,
            headers=headers,
            data=data,
            json_body=json_body,
            timeout=timeout,
        )

    @asynccontextmanager
    async def stream(
        self,
        method: str,
        url: str,
        *,
        headers: Optional[dict[str, str]] = None,
        params: Optional[dict[str, Any]] = None,
        data: Any = None,
        json_body: Any = None,
        timeout: Optional[float] = None,
    ) -> AsyncIterator[UpstreamResponse]:
        raise NotImplementedError

    async def close(self) -> None:
        raise NotImplementedError


class HttpxTransport(BaseUpstreamTransport):
    """httpx 传输实现。"""

    def __init__(
        self,
        *,
        timeout: float,
        follow_redirects: bool,
        proxy: Optional[str],
        trust_env: bool,
    ):
        kwargs: dict[str, Any] = {
            "timeout": httpx.Timeout(timeout, connect=min(10.0, timeout)),
            "follow_redirects": follow_redirects,
            "trust_env": trust_env,
        }
        if proxy:
            kwargs["proxy"] = proxy
        self._client = httpx.AsyncClient(**kwargs)

    async def request(
        self,
        method: str,
        url: str,
        *,
        headers: Optional[dict[str, str]] = None,
        params: Optional[dict[str, Any]] = None,
        data: Any = None,
        json_body: Any = None,
        timeout: Optional[float] = None,
    ) -> UpstreamResponse:
        response = await self._client.request(
            method,
            url,
            headers=headers,
            params=params,
            data=data,
            json=json_body,
            timeout=timeout,
        )
        return UpstreamResponse(response)

    @asynccontextmanager
    async def stream(
        self,
        method: str,
        url: str,
        *,
        headers: Optional[dict[str, str]] = None,
        params: Optional[dict[str, Any]] = None,
        data: Any = None,
        json_body: Any = None,
        timeout: Optional[float] = None,
    ) -> AsyncIterator[UpstreamResponse]:
        async with self._client.stream(
            method,
            url,
            headers=headers,
            params=params,
            data=data,
            json=json_body,
            timeout=timeout,
        ) as response:
            yield UpstreamResponse(response)

    async def close(self) -> None:
        await self._client.aclose()


class CurlCffiTransport(BaseUpstreamTransport):
    """curl_cffi 传输实现（支持 impersonate）。"""

    def __init__(
        self,
        *,
        timeout: float,
        follow_redirects: bool,
        proxy: Optional[str],
        impersonate: str,
    ):
        from curl_cffi import requests as curl_requests

        self._session = curl_requests.AsyncSession(
            timeout=timeout,
            allow_redirects=follow_redirects,
        )
        self._proxy = proxy
        self._impersonate = impersonate

    def _build_kwargs(
        self,
        *,
        headers: Optional[dict[str, str]],
        params: Optional[dict[str, Any]],
        data: Any,
        json_body: Any,
        timeout: Optional[float],
        stream: bool = False,
    ) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "headers": headers,
            "params": params,
            "data": data,
            "json": json_body,
            "timeout": timeout,
            "impersonate": self._impersonate,
        }
        if stream:
            kwargs["stream"] = True
        if self._proxy:
            kwargs["proxy"] = self._proxy
        return kwargs

    async def _request_with_proxy_fallback(
        self,
        method: str,
        url: str,
        kwargs: dict[str, Any],
    ) -> Any:
        """兼容 curl_cffi 不同版本的 proxy/proxies 参数。"""
        try:
            return await self._session.request(method, url, **kwargs)
        except TypeError:
            if "proxy" in kwargs and kwargs["proxy"]:
                proxy = kwargs.pop("proxy")
                kwargs["proxies"] = {"http": proxy, "https": proxy}
                return await self._session.request(method, url, **kwargs)
            raise

    async def request(
        self,
        method: str,
        url: str,
        *,
        headers: Optional[dict[str, str]] = None,
        params: Optional[dict[str, Any]] = None,
        data: Any = None,
        json_body: Any = None,
        timeout: Optional[float] = None,
    ) -> UpstreamResponse:
        kwargs = self._build_kwargs(
            headers=headers,
            params=params,
            data=data,
            json_body=json_body,
            timeout=timeout,
            stream=False,
        )
        response = await self._request_with_proxy_fallback(method, url, kwargs)
        return UpstreamResponse(response)

    @asynccontextmanager
    async def stream(
        self,
        method: str,
        url: str,
        *,
        headers: Optional[dict[str, str]] = None,
        params: Optional[dict[str, Any]] = None,
        data: Any = None,
        json_body: Any = None,
        timeout: Optional[float] = None,
    ) -> AsyncIterator[UpstreamResponse]:
        kwargs = self._build_kwargs(
            headers=headers,
            params=params,
            data=data,
            json_body=json_body,
            timeout=timeout,
            stream=True,
        )

        # 新版接口：AsyncSession.stream(...) 是 async context manager
        if hasattr(self._session, "stream"):
            try:
                async with self._session.stream(method, url, **kwargs) as response:
                    yield UpstreamResponse(response)
                    return
            except TypeError:
                if kwargs.get("proxy"):
                    proxy = kwargs.pop("proxy")
                    kwargs["proxies"] = {"http": proxy, "https": proxy}
                    async with self._session.stream(method, url, **kwargs) as response:
                        yield UpstreamResponse(response)
                        return

        # 兼容旧接口：request(stream=True)
        response = await self._request_with_proxy_fallback(method, url, kwargs)
        try:
            yield UpstreamResponse(response)
        finally:
            if hasattr(response, "aclose"):
                await response.aclose()

    async def close(self) -> None:
        await self._session.close()


class NodeFetchTransport(BaseUpstreamTransport):
    """Node fetch 长驻 worker 桥接实现。"""

    def __init__(
        self,
        *,
        timeout: float,
        follow_redirects: bool,
        proxy: Optional[str],
    ):
        self._timeout = timeout
        self._follow_redirects = follow_redirects
        self._proxy = proxy
        self._node_path = shutil.which("node")
        if not self._node_path:
            raise RuntimeError("未找到 node 可执行文件，无法启用 node_fetch 传输层")
        if not _NODE_BRIDGE_SCRIPT.exists():
            raise RuntimeError(f"Node fetch bridge 脚本不存在: {_NODE_BRIDGE_SCRIPT}")
        self._worker: asyncio.subprocess.Process | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._request_lock = asyncio.Lock()

    async def _ensure_worker(self) -> asyncio.subprocess.Process:
        process = self._worker
        if process is not None and process.returncode is None:
            return process

        process = await asyncio.create_subprocess_exec(
            self._node_path,
            str(_NODE_BRIDGE_SCRIPT),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=_node_bridge_env(self._proxy),
        )
        self._worker = process
        self._stderr_task = asyncio.create_task(self._drain_worker_stderr(process))
        return process

    async def _drain_worker_stderr(self, process: asyncio.subprocess.Process) -> None:
        stream = process.stderr
        if stream is None:
            return
        while True:
            line = await stream.readline()
            if not line:
                break
            text = line.decode("utf-8", errors="replace").rstrip()
            if text:
                logger.debug("node_fetch worker stderr[%s]: %s", process.pid, text)

    async def _reset_worker(self) -> None:
        process = self._worker
        self._worker = None
        stderr_task = self._stderr_task
        self._stderr_task = None

        if process is not None and process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=2.0)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()

        if stderr_task is not None:
            try:
                await stderr_task
            except asyncio.CancelledError:
                pass

    async def _send_bridge_request(self, payload: dict[str, Any]) -> None:
        process = await self._ensure_worker()
        stdin = process.stdin
        if stdin is None:
            await self._reset_worker()
            raise NodeFetchBridgeError("Node fetch worker stdin 不可用")

        stdin.write((json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8"))
        await stdin.drain()

    async def _cancel_request(self, request_id: str) -> None:
        await self._send_bridge_request(_node_bridge_cancel_payload(request_id))

    async def _read_bridge_event(self, request_id: str) -> dict[str, Any]:
        process = await self._ensure_worker()
        stdout = process.stdout
        if stdout is None:
            await self._reset_worker()
            raise NodeFetchBridgeError("Node fetch worker stdout 不可用")

        while True:
            line = await stdout.readline()
            if not line:
                return_code = await process.wait()
                await self._reset_worker()
                raise NodeFetchBridgeError(
                    f"Node fetch worker 意外退出，未返回完整响应 (code={return_code})"
                )

            try:
                event = json.loads(line.decode("utf-8"))
            except json.JSONDecodeError as exc:
                await self._reset_worker()
                raise NodeFetchBridgeError("Node fetch worker 返回了非法 JSON 事件") from exc

            if event.get("request_id") != request_id:
                await self._reset_worker()
                raise NodeFetchBridgeError("Node fetch worker 请求序列错乱，request_id 不匹配")
            return event

    async def _drain_terminal_event(self, request_id: str) -> None:
        while True:
            event = await self._read_bridge_event(request_id)
            event_type = event.get("type")
            if event_type == "chunk":
                continue
            if event_type in {"end", "aborted"}:
                return
            if event_type == "error":
                raise NodeFetchBridgeError(str(event.get("message") or "Node fetch bridge 请求失败"))
            raise NodeFetchBridgeError(f"Node fetch worker 返回了未知终止事件: {event_type!r}")

    async def _await_meta(self, request_id: str) -> tuple[int, dict[str, str]]:
        event = await self._read_bridge_event(request_id)
        event_type = event.get("type")
        if event_type == "meta":
            status_code = int(event.get("status_code") or 0)
            headers = event.get("headers")
            return status_code, headers if isinstance(headers, dict) else {}
        if event_type == "error":
            raise NodeFetchBridgeError(str(event.get("message") or "Node fetch bridge 请求失败"))
        raise NodeFetchBridgeError(f"Node fetch worker 返回了未知握手事件: {event_type!r}")

    def _release_request_lock(self) -> None:
        if self._request_lock.locked():
            self._request_lock.release()

    async def request(
        self,
        method: str,
        url: str,
        *,
        headers: Optional[dict[str, str]] = None,
        params: Optional[dict[str, Any]] = None,
        data: Any = None,
        json_body: Any = None,
        timeout: Optional[float] = None,
    ) -> UpstreamResponse:
        payload = _node_bridge_payload(
            method=method,
            url=url,
            headers=headers,
            params=params,
            data=data,
            json_body=json_body,
            timeout=timeout or self._timeout,
            stream=False,
            follow_redirects=self._follow_redirects,
            proxy=self._proxy,
        )
        request_id = str(payload["request_id"])

        await self._request_lock.acquire()
        try:
            await self._send_bridge_request(payload)
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
                    raise NodeFetchBridgeError("Node fetch bridge 请求已取消")
                if event_type == "error":
                    raise NodeFetchBridgeError(str(event.get("message") or "Node fetch bridge 请求失败"))
                raise NodeFetchBridgeError(f"Node fetch worker 返回了未知事件: {event_type!r}")
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
    async def stream(
        self,
        method: str,
        url: str,
        *,
        headers: Optional[dict[str, str]] = None,
        params: Optional[dict[str, Any]] = None,
        data: Any = None,
        json_body: Any = None,
        timeout: Optional[float] = None,
    ) -> AsyncIterator[UpstreamResponse]:
        payload = _node_bridge_payload(
            method=method,
            url=url,
            headers=headers,
            params=params,
            data=data,
            json_body=json_body,
            timeout=timeout or self._timeout,
            stream=True,
            follow_redirects=self._follow_redirects,
            proxy=self._proxy,
        )
        request_id = str(payload["request_id"])

        await self._request_lock.acquire()
        response: UpstreamResponse | None = None

        try:
            await self._send_bridge_request(payload)
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

    async def close(self) -> None:
        await self._reset_worker()


def create_upstream_transport(
    *,
    backend: TransportBackend,
    timeout: float,
    follow_redirects: bool,
    proxy: Optional[str],
    trust_env: bool = False,
    impersonate: str = "chrome124",
) -> BaseUpstreamTransport:
    """创建上游传输层实例。"""
    if backend == "node_fetch":
        return NodeFetchTransport(
            timeout=timeout,
            follow_redirects=follow_redirects,
            proxy=proxy,
        )

    if backend == "curl_cffi":
        try:
            return CurlCffiTransport(
                timeout=timeout,
                follow_redirects=follow_redirects,
                proxy=proxy,
                impersonate=impersonate,
            )
        except Exception as e:
            logger.warning("curl_cffi 不可用，回退 httpx: %s", e)

    return HttpxTransport(
        timeout=timeout,
        follow_redirects=follow_redirects,
        proxy=proxy,
        trust_env=trust_env,
    )
