"""OAuth state 签发与防重放管理。"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import tempfile
import threading
import time
from pathlib import Path

logger = logging.getLogger("iflow2api")


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64url_decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


class OAuthStateManager:
    """签发带签名的 OAuth state，并用持久化 nonce 防重放。"""

    def __init__(
        self,
        secret: str,
        *,
        store_path: Path | None = None,
        ttl_seconds: int = 10 * 60,
    ) -> None:
        self._secret = secret.encode("utf-8")
        self._ttl_seconds = ttl_seconds
        self._store_path = store_path or (Path.home() / ".iflow2api" / "oauth_state_replay.json")
        self._lock = threading.RLock()

    def issue(self, username: str) -> str:
        """签发 state。"""
        now = int(time.time())
        payload = {
            "v": 1,
            "u": username,
            "iat": now,
            "exp": now + self._ttl_seconds,
            "n": secrets.token_urlsafe(16),
        }
        payload_segment = _b64url_encode(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        )
        signature = hmac.new(
            self._secret,
            payload_segment.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        return f"v1.{payload_segment}.{signature}"

    def consume(self, username: str, state: str) -> bool:
        """校验并消费 state。"""
        payload = self._decode_and_verify(state)
        if payload is None:
            return False
        if str(payload.get("u") or "") != username:
            return False

        nonce = str(payload.get("n") or "").strip()
        expires_at = int(payload.get("exp") or 0)
        if not nonce or expires_at <= int(time.time()):
            return False

        with self._lock:
            used_nonces = self._load_used_nonces_unlocked()
            if used_nonces is None:
                return False

            now = int(time.time())
            used_nonces = {
                key: int(exp)
                for key, exp in used_nonces.items()
                if int(exp) > now
            }
            if nonce in used_nonces:
                return False

            used_nonces[nonce] = expires_at
            return self._save_used_nonces_unlocked(used_nonces)

    def _decode_and_verify(self, state: str) -> dict[str, object] | None:
        try:
            version, payload_segment, signature = str(state or "").split(".", 2)
        except ValueError:
            return None
        if version != "v1":
            return None

        expected_signature = hmac.new(
            self._secret,
            payload_segment.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        if not hmac.compare_digest(signature, expected_signature):
            return None

        try:
            payload = json.loads(_b64url_decode(payload_segment).decode("utf-8"))
        except Exception:
            return None
        if not isinstance(payload, dict):
            return None

        now = int(time.time())
        try:
            if int(payload.get("v") or 0) != 1:
                return None
            if int(payload.get("iat") or 0) > now + 60:
                return None
            if int(payload.get("exp") or 0) <= now:
                return None
        except (TypeError, ValueError):
            return None
        return payload

    def _load_used_nonces_unlocked(self) -> dict[str, int] | None:
        if not self._store_path.exists():
            return {}
        try:
            data = json.loads(self._store_path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.error("OAuth state 防重放存储加载失败: %s", exc)
            return None

        used = data.get("used", {})
        if not isinstance(used, dict):
            logger.error("OAuth state 防重放存储格式无效")
            return None
        normalized: dict[str, int] = {}
        for nonce, expires_at in used.items():
            try:
                normalized[str(nonce)] = int(expires_at)
            except (TypeError, ValueError):
                logger.error("OAuth state 防重放存储存在非法记录: nonce=%r", nonce)
                return None
        return normalized

    def _save_used_nonces_unlocked(self, used_nonces: dict[str, int]) -> bool:
        self._store_path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_path = tempfile.mkstemp(
            prefix=f"{self._store_path.name}.",
            suffix=".tmp",
            dir=str(self._store_path.parent),
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump({"used": used_nonces}, handle, ensure_ascii=False, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, self._store_path)
            try:
                os.chmod(self._store_path, 0o600)
            except OSError:
                pass
            return True
        except Exception as exc:
            logger.error("OAuth state 防重放存储写入失败: %s", exc)
            return False
        finally:
            if os.path.exists(temp_path):
                try:
                    os.unlink(temp_path)
                except OSError:
                    pass
