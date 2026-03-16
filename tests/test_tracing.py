from __future__ import annotations

import contextvars

from iflow2api.tracing import (
    get_current_span,
    get_current_traceparent,
    session_trace_context,
    span_context,
)


def test_span_context_can_exit_from_copied_context_without_error() -> None:
    manager = span_context("stream-span")
    captured_contexts: list[contextvars.Context] = []

    def enter_in_context():
        span = manager.__enter__()
        captured_contexts.append(contextvars.copy_context())
        return span

    enter_context = contextvars.copy_context()
    span = enter_context.run(enter_in_context)

    assert enter_context.run(get_current_span) is span

    exit_context = captured_contexts[0]
    exit_context.run(manager.__exit__, None, None, None)

    assert exit_context.run(get_current_span) is None


def test_session_trace_context_can_exit_from_copied_context_without_error() -> None:
    manager = session_trace_context("00-0123456789abcdef0123456789abcdef-0123456789abcdef-01")
    captured_contexts: list[contextvars.Context] = []

    def enter_in_context():
        manager.__enter__()
        captured_contexts.append(contextvars.copy_context())
        return get_current_traceparent()

    enter_context = contextvars.copy_context()
    traceparent = enter_context.run(enter_in_context)

    assert traceparent.startswith("00-0123456789abcdef")

    exit_context = captured_contexts[0]
    exit_context.run(manager.__exit__, None, None, None)

    assert exit_context.run(get_current_traceparent) == ""
