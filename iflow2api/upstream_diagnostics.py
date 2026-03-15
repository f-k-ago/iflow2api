"""上游请求诊断日志辅助函数。"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Any

logger = logging.getLogger("iflow2api")


def api_key_fingerprint(api_key: Any) -> str:
    """返回 API Key 的安全指纹，避免在日志中暴露明文。"""
    normalized = str(api_key or "").strip()
    if not normalized:
        return "missing"
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:12]


def build_preview(value: Any, *, limit: int = 300) -> str:
    """把对象压缩为空白较少的短摘要，适合日志输出。"""
    if value in (None, ""):
        return ""

    if isinstance(value, (dict, list, tuple)):
        raw_text = json.dumps(value, ensure_ascii=False, default=str)
    else:
        raw_text = str(value)

    collapsed = " ".join(raw_text.split())
    return collapsed[:limit]


def extract_upstream_payload_preview(exc: Exception, *, limit: int = 300) -> str:
    """优先从业务异常 payload 中提取上游错误摘要。"""
    payload = getattr(exc, "payload", None)
    if payload not in (None, ""):
        return build_preview(payload, limit=limit)

    response = getattr(exc, "response", None)
    if response is None:
        return ""

    try:
        return build_preview(response.json(), limit=limit)
    except Exception:
        try:
            return build_preview(response.text, limit=limit)
        except Exception:
            return ""


def build_lease_debug_context(
    lease: Any,
    request_body: dict[str, Any] | None,
    *,
    endpoint: str,
    stream: bool,
) -> dict[str, Any]:
    """提取与一次上游调用相关的安全诊断上下文。"""
    account = getattr(lease, "account", None)
    proxy = getattr(lease, "proxy", None)
    proxy_config = getattr(proxy, "config", None)
    body = request_body if isinstance(request_body, dict) else {}
    messages = body.get("messages")
    tools = body.get("tools")
    runtime_session_id = str(
        getattr(proxy, "_session_id", "") or getattr(account, "session_id", "") or ""
    ).strip()
    runtime_conversation_id = str(
        getattr(proxy, "_conversation_id", "") or getattr(account, "conversation_id", "") or ""
    ).strip()
    auth_type = str(
        getattr(proxy_config, "auth_type", "") or getattr(account, "auth_type", "") or "unknown"
    ).strip() or "unknown"
    base_url = str(
        getattr(proxy_config, "base_url", "") or getattr(account, "base_url", "") or "-"
    ).strip() or "-"
    api_key = getattr(proxy_config, "api_key", "") or getattr(account, "api_key", "")

    return {
        "endpoint": endpoint,
        "stream": stream,
        "account_id": getattr(account, "id", "-"),
        "label": getattr(account, "label", "") or "-",
        "auth_type": auth_type,
        "base_url": base_url,
        "api_key_fp": api_key_fingerprint(api_key),
        "has_session_id": bool(runtime_session_id),
        "has_conversation_id": bool(runtime_conversation_id),
        "model": str(body.get("model") or "unknown"),
        "message_count": len(messages) if isinstance(messages, list) else 0,
        "tool_count": len(tools) if isinstance(tools, list) else 0,
    }


def log_upstream_request_context(
    lease: Any,
    request_body: dict[str, Any] | None,
    *,
    endpoint: str,
    stream: bool,
) -> None:
    """记录请求发往上游前的关键上下文。"""
    context = build_lease_debug_context(
        lease,
        request_body,
        endpoint=endpoint,
        stream=stream,
    )
    logger.debug(
        "上游请求上下文: endpoint=%s, stream=%s, account_id=%s, label=%s, auth_type=%s, "
        "base_url=%s, api_key_fp=%s, has_session_id=%s, has_conversation_id=%s, model=%s, "
        "messages=%s, tools=%s",
        context["endpoint"],
        context["stream"],
        context["account_id"],
        context["label"],
        context["auth_type"],
        context["base_url"],
        context["api_key_fp"],
        context["has_session_id"],
        context["has_conversation_id"],
        context["model"],
        context["message_count"],
        context["tool_count"],
    )


def log_upstream_failure(
    exc: Exception,
    *,
    status_code: int,
    error_msg: str,
    error_type: str,
    lease: Any = None,
    request_body: dict[str, Any] | None = None,
    endpoint: str,
    stream: bool,
) -> None:
    """记录上游失败时的安全诊断信息。"""
    context = build_lease_debug_context(
        lease,
        request_body,
        endpoint=endpoint,
        stream=stream,
    )
    payload_preview = extract_upstream_payload_preview(exc)
    logger.warning(
        "上游请求失败: endpoint=%s, stream=%s, account_id=%s, label=%s, auth_type=%s, "
        "base_url=%s, api_key_fp=%s, has_session_id=%s, has_conversation_id=%s, model=%s, "
        "messages=%s, tools=%s, status_code=%s, error_type=%s, message=%s, payload_preview=%s",
        context["endpoint"],
        context["stream"],
        context["account_id"],
        context["label"],
        context["auth_type"],
        context["base_url"],
        context["api_key_fp"],
        context["has_session_id"],
        context["has_conversation_id"],
        context["model"],
        context["message_count"],
        context["tool_count"],
        status_code,
        error_type,
        error_msg,
        payload_preview or "-",
    )
