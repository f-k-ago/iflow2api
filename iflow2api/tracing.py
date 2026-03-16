"""轻量级 trace/span 上下文管理。"""

from __future__ import annotations

import contextvars
import secrets
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Iterator, Optional


@dataclass(slots=True)
class TraceSpan:
    """当前协程上下文中的轻量 span。"""

    name: str
    trace_id: str
    span_id: str
    parent_span_id: str
    trace_flags: str = "01"
    attributes: dict[str, Any] = field(default_factory=dict)
    start_time_ms: int = field(default_factory=lambda: int(time.time() * 1000))
    end_time_ms: int | None = None
    status: str = "running"

    @property
    def traceparent(self) -> str:
        return f"00-{self.trace_id}-{self.span_id}-{self.trace_flags}"


_current_span: contextvars.ContextVar[Optional[TraceSpan]] = contextvars.ContextVar(
    "iflow_current_trace_span",
    default=None,
)
_current_session_traceparent: contextvars.ContextVar[str] = contextvars.ContextVar(
    "iflow_current_session_traceparent",
    default="",
)


def _generate_trace_id() -> str:
    return secrets.token_hex(16)


def _generate_span_id() -> str:
    return secrets.token_hex(8)


def parse_traceparent(traceparent: str | None) -> tuple[str, str, str]:
    """解析 W3C traceparent。"""
    if not traceparent:
        return "", "", "01"

    parts = traceparent.strip().split("-")
    if len(parts) != 4:
        return "", "", "01"

    _, trace_id, parent_span_id, trace_flags = parts
    if len(trace_id) != 32 or len(parent_span_id) != 16 or len(trace_flags) != 2:
        return "", "", "01"
    return trace_id, parent_span_id, trace_flags


def get_current_span() -> TraceSpan | None:
    """返回当前协程上下文中的 span。"""
    return _current_span.get()


def get_current_traceparent() -> str:
    """返回当前协程上下文中的 traceparent。"""
    span = get_current_span()
    return span.traceparent if span else _current_session_traceparent.get()


@contextmanager
def session_trace_context(traceparent: str | None) -> Iterator[None]:
    """设置当前协程上下文中的 session 级 traceparent 回退值。"""
    previous = _current_session_traceparent.get()
    token = _current_session_traceparent.set((traceparent or "").strip())
    try:
        yield
    finally:
        try:
            _current_session_traceparent.reset(token)
        except ValueError:
            _current_session_traceparent.set(previous)


@contextmanager
def span_context(
    name: str,
    *,
    attributes: Optional[dict[str, Any]] = None,
    traceparent: Optional[str] = None,
) -> Iterator[TraceSpan]:
    """进入一个轻量 span 上下文。"""
    parent = get_current_span()
    if parent is not None:
        trace_id = parent.trace_id
        parent_span_id = parent.span_id
        trace_flags = parent.trace_flags
    else:
        trace_id, parent_span_id, trace_flags = parse_traceparent(
            traceparent or _current_session_traceparent.get()
        )
        if not trace_id:
            trace_id = _generate_trace_id()
            parent_span_id = ""

    span = TraceSpan(
        name=name,
        trace_id=trace_id,
        span_id=_generate_span_id(),
        parent_span_id=parent_span_id,
        trace_flags=trace_flags or "01",
        attributes=dict(attributes or {}),
    )
    token = _current_span.set(span)
    try:
        yield span
    except Exception:
        span.status = "error"
        raise
    else:
        span.status = "ok"
    finally:
        span.end_time_ms = int(time.time() * 1000)
        try:
            _current_span.reset(token)
        except ValueError:
            _current_span.set(parent)
