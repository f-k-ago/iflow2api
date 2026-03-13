"""上游传输层：支持 httpx、curl_cffi 与 Node fetch bridge。"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import shutil
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator, Literal, Optional

import httpx

logger = logging.getLogger("iflow2api")


TransportBackend = Literal["httpx", "curl_cffi", "node_fetch"]


_NODE_BRIDGE_META_PREFIX = "__IFLOW_NODE_META__="
_NODE_BRIDGE_ERROR_PREFIX = "__IFLOW_NODE_ERROR__="
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
        process: asyncio.subprocess.Process,
        status_code: int,
        headers: dict[str, str],
        stderr_task: "asyncio.Task[str]",
    ):
        self._process = process
        self.status_code = status_code
        self.headers = headers
        self._stderr_task = stderr_task
        self._cached_content: bytes | None = None
        self._stream_consumed = False

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

        stdout = self._process.stdout
        if stdout is None:
            self._cached_content = b""
            return self._cached_content

        content = await stdout.read()
        await self._wait_for_exit()
        self._cached_content = bytes(content)
        self._stream_consumed = True
        return self._cached_content

    async def aiter_bytes(self) -> AsyncIterator[bytes]:
        if self._cached_content is not None:
            if self._cached_content:
                yield self._cached_content
            return

        if self._stream_consumed:
            return

        stdout = self._process.stdout
        if stdout is None:
            self._cached_content = b""
            self._stream_consumed = True
            return

        chunks: list[bytes] = []
        try:
            while True:
                chunk = await stdout.read(65536)
                if not chunk:
                    break
                chunks.append(chunk)
                yield chunk
        finally:
            self._cached_content = b"".join(chunks)
            self._stream_consumed = True
            await self._wait_for_exit()

    async def _wait_for_exit(self) -> None:
        return_code = await self._process.wait()
        stderr_text = await self._stderr_task
        if return_code == 0:
            return
        details = stderr_text.strip()
        raise NodeFetchBridgeError(
            f"Node fetch bridge 进程异常退出 (code={return_code})"
            + (f": {details}" if details else "")
        )


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
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "method": method,
        "url": url,
        "headers": headers or {},
        "params": params or {},
        "timeout_ms": int((timeout or 300.0) * 1000),
        "stream": stream,
        "follow_redirects": follow_redirects,
    }
    if json_body is not None:
        payload["json_body"] = json_body
    elif data is not None:
        if isinstance(data, bytes):
            payload["data_base64"] = base64.b64encode(data).decode("ascii")
        else:
            payload["data_text"] = str(data)
    return payload


def _node_bridge_env(proxy: Optional[str]) -> dict[str, str]:
    env = os.environ.copy()
    for key in _PROXY_ENV_KEYS:
        env.pop(key, None)
    if proxy:
        env["NODE_USE_ENV_PROXY"] = "1"
        env["HTTP_PROXY"] = proxy
        env["HTTPS_PROXY"] = proxy
        env["ALL_PROXY"] = proxy
        env["http_proxy"] = proxy
        env["https_proxy"] = proxy
        env["all_proxy"] = proxy
    return env


def _parse_node_bridge_meta(stderr_text: str) -> tuple[int, dict[str, str]]:
    for line in stderr_text.splitlines():
        line = line.strip()
        if not line.startswith(_NODE_BRIDGE_META_PREFIX):
            continue
        payload = json.loads(line[len(_NODE_BRIDGE_META_PREFIX):])
        status_code = int(payload.get("status_code", 0))
        headers = payload.get("headers")
        return status_code, headers if isinstance(headers, dict) else {}
    raise NodeFetchBridgeError(
        "Node fetch bridge 未返回响应元数据"
        + (f": {stderr_text.strip()}" if stderr_text.strip() else "")
    )


def _parse_node_bridge_error(stderr_text: str) -> str:
    for line in stderr_text.splitlines():
        line = line.strip()
        if not line.startswith(_NODE_BRIDGE_ERROR_PREFIX):
            continue
        payload = json.loads(line[len(_NODE_BRIDGE_ERROR_PREFIX):])
        message = str(payload.get("message") or "").strip()
        if message:
            return message
        return json.dumps(payload, ensure_ascii=False)
    return stderr_text.strip()


async def _drain_stream_text(stream: asyncio.StreamReader | None) -> str:
    if stream is None:
        return ""
    data = await stream.read()
    return data.decode("utf-8", errors="replace")


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
    """Node fetch 桥接实现。"""

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
        )
        process = await asyncio.create_subprocess_exec(
            self._node_path,
            str(_NODE_BRIDGE_SCRIPT),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=_node_bridge_env(self._proxy),
        )
        stdout, stderr = await process.communicate(
            json.dumps(payload, ensure_ascii=False).encode("utf-8")
        )
        stderr_text = stderr.decode("utf-8", errors="replace")

        if process.returncode != 0:
            raise NodeFetchBridgeError(
                "Node fetch bridge 请求失败"
                + (f": {_parse_node_bridge_error(stderr_text)}" if stderr_text.strip() else "")
            )

        status_code, response_headers = _parse_node_bridge_meta(stderr_text)
        return UpstreamResponse(
            _BufferedNodeFetchRawResponse(
                status_code=status_code,
                headers=response_headers,
                content=stdout,
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
        )
        process = await asyncio.create_subprocess_exec(
            self._node_path,
            str(_NODE_BRIDGE_SCRIPT),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=_node_bridge_env(self._proxy),
        )

        stdin = process.stdin
        if stdin is None:
            process.kill()
            await process.wait()
            raise NodeFetchBridgeError("Node fetch bridge stdin 不可用")
        stdin.write(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
        await stdin.drain()
        stdin.close()

        stderr = process.stderr
        if stderr is None:
            process.kill()
            await process.wait()
            raise NodeFetchBridgeError("Node fetch bridge stderr 不可用")

        handshake_lines: list[str] = []
        while True:
            line = await stderr.readline()
            if not line:
                break
            text = line.decode("utf-8", errors="replace").rstrip("\n")
            handshake_lines.append(text)
            if text.startswith(_NODE_BRIDGE_META_PREFIX):
                break
            if text.startswith(_NODE_BRIDGE_ERROR_PREFIX):
                break

        stderr_text = "\n".join(handshake_lines).strip()
        if not stderr_text:
            process.kill()
            await process.wait()
            raise NodeFetchBridgeError("Node fetch bridge 未返回握手元数据")

        if any(line.startswith(_NODE_BRIDGE_ERROR_PREFIX) for line in handshake_lines):
            process.kill()
            await process.wait()
            raise NodeFetchBridgeError(_parse_node_bridge_error(stderr_text))

        status_code, response_headers = _parse_node_bridge_meta(stderr_text)
        stderr_task = asyncio.create_task(_drain_stream_text(stderr))

        try:
            yield UpstreamResponse(
                _StreamingNodeFetchRawResponse(
                    process=process,
                    status_code=status_code,
                    headers=response_headers,
                    stderr_task=stderr_task,
                )
            )
        finally:
            if process.returncode is None:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), timeout=2.0)
                except asyncio.TimeoutError:
                    process.kill()
                    await process.wait()
            if not stderr_task.done():
                stderr_task.cancel()
                try:
                    await stderr_task
                except asyncio.CancelledError:
                    pass

    async def close(self) -> None:
        return None


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
