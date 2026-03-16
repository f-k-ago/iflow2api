"""Web 管理界面认证模块"""

import hashlib
import hmac
import json
import logging
import os
import secrets
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from pydantic import BaseModel

logger = logging.getLogger("iflow2api")

# PBKDF2 哈希参数
_PBKDF2_ITERATIONS = 260000  # OWASP 2023 推荐值
_PBKDF2_HASH = "sha256"
_HASH_PREFIX = "pbkdf2:"  # 用于区分新旧格式
ADMIN_SESSION_COOKIE_NAME = "iflow2api_admin_session"
ADMIN_SESSION_MAX_AGE_SECONDS = 24 * 60 * 60


class AdminUser(BaseModel):
    """管理员用户"""
    username: str
    password_hash: str
    created_at: datetime
    last_login: Optional[datetime] = None


class TokenData(BaseModel):
    """Token 数据"""
    username: str
    exp: datetime
    iat: datetime


class AuthManager:
    """认证管理器"""

    def __init__(self):
        self._users: dict[str, AdminUser] = {}
        self._active_tokens: dict[str, TokenData] = {}
        self._config_path = Path.home() / ".iflow2api" / "admin_users.json"
        self._load_failed = False
        self._load_error = ""
        # JWT secret 单独存储，与用户数据分离
        self._jwt_secret_path = Path.home() / ".iflow2api" / ".jwt_secret"
        self._jwt_secret = self._load_or_create_jwt_secret()
        self._load_users()

    def _load_or_create_jwt_secret(self) -> str:
        """加载或创建 JWT 签名密钥，存储在独立的权限严格文件中"""
        if self._jwt_secret_path.exists():
            try:
                secret = self._jwt_secret_path.read_text(encoding="utf-8").strip()
                if len(secret) >= 32:
                    return secret
            except Exception:
                pass
        # 生成新密钥
        secret = secrets.token_hex(32)
        self._jwt_secret_path.parent.mkdir(parents=True, exist_ok=True)
        self._jwt_secret_path.write_text(secret, encoding="utf-8")
        try:
            os.chmod(self._jwt_secret_path, 0o600)
        except Exception:
            pass
        return secret

    def _load_users(self) -> None:
        """加载用户数据"""
        self._load_failed = False
        self._load_error = ""
        if self._config_path.exists():
            try:
                with open(self._config_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    for username, user_data in data.get("users", {}).items():
                        self._users[username] = AdminUser(
                            username=username,
                            password_hash=user_data["password_hash"],
                            created_at=datetime.fromisoformat(user_data["created_at"]),
                            last_login=datetime.fromisoformat(user_data["last_login"])
                            if user_data.get("last_login") else None,
                        )
                    # 兼容旧版本：旧版 jwt_secret 存在 users 文件中，迁移到独立文件
                    if "jwt_secret" in data and not self._jwt_secret_path.exists():
                        secret = data["jwt_secret"]
                        self._jwt_secret_path.parent.mkdir(parents=True, exist_ok=True)
                        self._jwt_secret_path.write_text(secret, encoding="utf-8")
                        try:
                            os.chmod(self._jwt_secret_path, 0o600)
                        except Exception:
                            pass
                        self._jwt_secret = secret
            except Exception as exc:
                self._users.clear()
                self._load_failed = True
                self._load_error = str(exc)
                logger.error("管理员用户文件加载失败: %s", exc)

    def _save_users(self) -> None:
        """保存用户数据（不包含 JWT secret，避免敏感信息共存）"""
        self._config_path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "users": {
                username: {
                    "password_hash": user.password_hash,
                    "created_at": user.created_at.isoformat(),
                    "last_login": user.last_login.isoformat() if user.last_login else None,
                }
                for username, user in self._users.items()
            }
            # jwt_secret 不再保存在此文件中
        }
        with open(self._config_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)

    @staticmethod
    def _hash_password(password: str) -> str:
        """哈希密码 - 使用 PBKDF2-HMAC-SHA256 加随机 salt（C-03 修复）

        格式: pbkdf2:{salt_hex}:{hash_hex}
        """
        salt = secrets.token_bytes(32)
        dk = hashlib.pbkdf2_hmac(
            _PBKDF2_HASH,
            password.encode("utf-8"),
            salt,
            _PBKDF2_ITERATIONS,
        )
        return f"{_HASH_PREFIX}{salt.hex()}:{dk.hex()}"

    @staticmethod
    def _verify_password(password: str, stored_hash: str) -> bool:
        """验证密码，兼容旧版 SHA-256 格式（无 salt）和新版 PBKDF2 格式

        Args:
            password: 明文密码
            stored_hash: 存储的哈希值

        Returns:
            密码是否匹配
        """
        if stored_hash.startswith(_HASH_PREFIX):
            # 新格式：pbkdf2:{salt_hex}:{hash_hex}
            try:
                _, salt_hex, hash_hex = stored_hash.split(":")
                salt = bytes.fromhex(salt_hex)
                expected = bytes.fromhex(hash_hex)
                dk = hashlib.pbkdf2_hmac(
                    _PBKDF2_HASH,
                    password.encode("utf-8"),
                    salt,
                    _PBKDF2_ITERATIONS,
                )
                # 使用常数时间比较，防止时序攻击（C-04 修复）
                return hmac.compare_digest(dk, expected)
            except Exception:
                return False
        else:
            # 旧格式：裸 SHA-256（向后兼容，登录成功后自动升级）
            old_hash = hashlib.sha256(password.encode()).hexdigest()
            return hmac.compare_digest(stored_hash, old_hash)

    def create_user(self, username: str, password: str) -> bool:
        """创建用户"""
        if username in self._users:
            return False
        
        user = AdminUser(
            username=username,
            password_hash=self._hash_password(password),
            created_at=datetime.now(),
        )
        self._users[username] = user
        self._save_users()
        return True

    def delete_user(self, username: str) -> bool:
        """删除用户"""
        if username not in self._users:
            return False
        
        del self._users[username]
        # 清除该用户的所有 token
        tokens_to_remove = [
            token for token, data in self._active_tokens.items()
            if data.username == username
        ]
        for token in tokens_to_remove:
            del self._active_tokens[token]
        
        self._save_users()
        return True

    def change_password(self, username: str, old_password: str, new_password: str) -> bool:
        """修改密码"""
        if username not in self._users:
            return False

        user = self._users[username]
        if not self._verify_password(old_password, user.password_hash):
            return False

        user.password_hash = self._hash_password(new_password)
        # 清除该用户的所有 token，强制重新登录
        tokens_to_remove = [
            token for token, data in self._active_tokens.items()
            if data.username == username
        ]
        for token in tokens_to_remove:
            del self._active_tokens[token]
        
        self._save_users()
        return True

    def authenticate(self, username: str, password: str) -> Optional[str]:
        """验证用户并返回 token，登录成功后自动升级旧密码哈希格式"""
        if username not in self._users:
            return None

        user = self._users[username]
        if not self._verify_password(password, user.password_hash):
            return None

        # 旧格式哈希自动升级为 PBKDF2（C-03 修复）
        if not user.password_hash.startswith(_HASH_PREFIX):
            user.password_hash = self._hash_password(password)

        # 更新最后登录时间
        user.last_login = datetime.now()
        self._save_users()
        
        # 创建 token
        token = create_access_token(username, self._jwt_secret)
        self._active_tokens[token] = TokenData(
            username=username,
            exp=datetime.now() + timedelta(hours=24),
            iat=datetime.now(),
        )
        return token

    def verify_token(self, token: str) -> Optional[str]:
        """验证 token 并返回用户名"""
        if token not in self._active_tokens:
            return None
        
        token_data = self._active_tokens[token]
        if datetime.now() > token_data.exp:
            del self._active_tokens[token]
            return None
        
        return token_data.username

    def logout(self, token: str) -> bool:
        """登出"""
        if token in self._active_tokens:
            del self._active_tokens[token]
            return True
        return False

    def get_users(self) -> list[dict]:
        """获取所有用户列表"""
        return [
            {
                "username": user.username,
                "created_at": user.created_at.isoformat(),
                "last_login": user.last_login.isoformat() if user.last_login else None,
            }
            for user in self._users.values()
        ]

    def has_users(self) -> bool:
        """检查是否有用户"""
        return len(self._users) > 0

    def has_load_error(self) -> bool:
        """用户配置是否加载失败。"""
        return self._load_failed

    def get_load_error(self) -> str:
        """返回加载失败原因。"""
        return self._load_error

    def get_signing_secret(self) -> str:
        """返回当前认证体系使用的签名密钥。"""
        return self._jwt_secret


def create_access_token(username: str, secret: str) -> str:
    """创建访问令牌"""
    timestamp = str(int(time.time() * 1000))
    random_part = secrets.token_hex(16)
    data = f"{username}:{timestamp}:{random_part}"
    signature = hashlib.sha256(f"{data}:{secret}".encode()).hexdigest()[:32]
    return f"{data}:{signature}"


def verify_token(token: str, secret: str) -> Optional[str]:
    """验证令牌并返回用户名"""
    try:
        parts = token.split(":")
        if len(parts) != 4:
            return None

        username, timestamp, random_part, signature = parts
        data = f"{username}:{timestamp}:{random_part}"
        expected_signature = hashlib.sha256(f"{data}:{secret}".encode()).hexdigest()[:32]

        # 使用常数时间比较，防止时序攻击（C-04 修复）
        if not hmac.compare_digest(signature, expected_signature):
            return None

        return username
    except Exception:
        return None


# 全局认证管理器实例
_auth_manager: Optional[AuthManager] = None


def get_auth_manager() -> AuthManager:
    """获取全局认证管理器实例"""
    global _auth_manager
    if _auth_manager is None:
        _auth_manager = AuthManager()
    return _auth_manager
