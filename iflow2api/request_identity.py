"""上游 request identity 规范化辅助函数。"""

from __future__ import annotations

import re
from typing import Any

_CANONICAL_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-"
    r"[0-9a-f]{4}-"
    r"[1-5][0-9a-f]{3}-"
    r"[89ab][0-9a-f]{3}-"
    r"[0-9a-f]{12}$",
    re.IGNORECASE,
)
_LEGACY_SESSION_ID_RE = re.compile(
    r"^session-"
    r"([0-9a-f]{8}-"
    r"[0-9a-f]{4}-"
    r"[1-5][0-9a-f]{3}-"
    r"[89ab][0-9a-f]{3}-"
    r"[0-9a-f]{12})$",
    re.IGNORECASE,
)


def normalize_request_ids(
    session_id: Any,
    conversation_id: Any,
) -> tuple[str, str]:
    """把 request identity 统一成裁剪过的字符串。"""
    return str(session_id or "").strip(), str(conversation_id or "").strip()


def is_legacy_generated_request_ids(
    session_id: Any,
    conversation_id: Any,
) -> bool:
    """判断是否命中旧版 iflow2api 自动生成的 request identity 形状。"""
    normalized_session_id, normalized_conversation_id = normalize_request_ids(
        session_id,
        conversation_id,
    )
    return bool(
        normalized_session_id
        and normalized_conversation_id
        and _LEGACY_SESSION_ID_RE.fullmatch(normalized_session_id)
        and _CANONICAL_UUID_RE.fullmatch(normalized_conversation_id)
    )


def strip_legacy_generated_request_ids(
    session_id: Any,
    conversation_id: Any,
) -> tuple[str, str, bool]:
    """清理旧版 iflow2api 自动生成的 request identity。"""
    normalized_session_id, normalized_conversation_id = normalize_request_ids(
        session_id,
        conversation_id,
    )
    if is_legacy_generated_request_ids(
        normalized_session_id,
        normalized_conversation_id,
    ):
        return "", "", True
    return normalized_session_id, normalized_conversation_id, False
