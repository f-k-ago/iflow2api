"""上游 request identity 规范化与补全辅助函数。"""

from __future__ import annotations

from typing import Any
from uuid import uuid4


def normalize_request_ids(
    session_id: Any,
    conversation_id: Any,
) -> tuple[str, str]:
    """把 request identity 统一成裁剪过的字符串。"""
    return str(session_id or "").strip(), str(conversation_id or "").strip()


def generate_session_id() -> str:
    """生成与官方 CLI 非交互路径兼容的 session id。"""
    return f"session-{uuid4()}"


def generate_conversation_id() -> str:
    """生成与官方 CLI 非交互路径兼容的 conversation id。"""
    return str(uuid4())


def ensure_request_ids(
    session_id: Any,
    conversation_id: Any,
) -> tuple[str, str, bool]:
    """补齐缺失的 request identity，避免上游因空值拒绝请求。"""
    normalized_session_id, normalized_conversation_id = normalize_request_ids(
        session_id,
        conversation_id,
    )
    generated = False

    if not normalized_session_id:
        normalized_session_id = generate_session_id()
        generated = True
    if not normalized_conversation_id:
        normalized_conversation_id = generate_conversation_id()
        generated = True

    return normalized_session_id, normalized_conversation_id, generated
