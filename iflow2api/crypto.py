"""配置加密模块 - 敏感配置加密存储"""

import base64
import json
import logging
import os
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("iflow2api")

# 尝试导入加密库
try:
    from cryptography.fernet import Fernet, InvalidToken
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
    HAS_CRYPTOGRAPHY = True
except ImportError:
    HAS_CRYPTOGRAPHY = False
    Fernet = None
    InvalidToken = Exception


class ConfigEncryption:
    """配置加密器"""

    def __init__(self, key: Optional[bytes] = None):
        """
        初始化加密器

        Args:
            key: 加密密钥，如果为 None 则自动生成或从文件加载
        """
        self._key = key
        self._fernet: Optional[Fernet] = None
        self._key_path = Path.home() / ".iflow2api" / ".key"

        if not HAS_CRYPTOGRAPHY:
            logger.warning("cryptography 库未安装，配置加密功能不可用")
            logger.warning("运行 'pip install iflow2api[full]' 或 'pip install cryptography' 启用加密")
            return

        if key:
            self._fernet = Fernet(key)
        else:
            self._load_or_generate_key()

    def _load_or_generate_key(self) -> None:
        """加载或生成密钥（M-06 修复：不再对 Fernet key 做额外 base64 编码）"""
        if self._key_path.exists():
            try:
                # Fernet.generate_key() 返回的本身就是 url-safe base64 bytes
                # 直接读取文件内容即为 Fernet key
                self._key = self._key_path.read_bytes().strip()
                self._fernet = Fernet(self._key)
                return
            except Exception as e:
                logger.warning("加载密鑙失败: %s", e)

        # 生成新密钥（已是 url-safe base64 bytes，直接存储）
        self._key = Fernet.generate_key()
        self._fernet = Fernet(self._key)

        # 保存密钥
        self._key_path.parent.mkdir(parents=True, exist_ok=True)
        self._key_path.write_bytes(self._key)

        # 设置权限（仅所有者可读写）
        try:
            os.chmod(self._key_path, 0o600)
        except Exception:
            pass

    def encrypt(self, data: str) -> str:
        """
        加密数据

        Args:
            data: 要加密的字符串

        Returns:
            加密后的字符串（Base64 编码）
        """
        if not self._fernet:
            return data

        encrypted = self._fernet.encrypt(data.encode('utf-8'))
        return base64.urlsafe_b64encode(encrypted).decode('utf-8')

    def decrypt(self, encrypted_data: str) -> str:
        """
        解密数据

        Args:
            encrypted_data: 加密的字符串（Base64 编码）

        Returns:
            解密后的原始字符串
        """
        if not self._fernet:
            return encrypted_data

        try:
            encrypted = base64.urlsafe_b64decode(encrypted_data.encode('utf-8'))
            decrypted = self._fernet.decrypt(encrypted)
            return decrypted.decode('utf-8')
        except InvalidToken:
            raise ValueError("解密失败: 无效的加密数据或密钥不匹配")

    def encrypt_dict(self, data: dict, sensitive_keys: Optional[list[str]] = None) -> dict:
        """
        加密字典中的敏感字段

        Args:
            data: 要加密的字典
            sensitive_keys: 敏感字段列表，默认为常见敏感字段

        Returns:
            加密后的字典
        """
        if sensitive_keys is None:
            sensitive_keys = [
                "api_key", "apiKey",
                "oauth_access_token", "oauth_refresh_token",
                "password", "secret", "token",
            ]

        result = {}
        for key, value in data.items():
            if key in sensitive_keys and isinstance(value, str) and value:
                # 检查是否已加密（以 enc: 开头）
                if not value.startswith("enc:"):
                    result[key] = f"enc:{self.encrypt(value)}"
                else:
                    result[key] = value
            else:
                result[key] = value

        return result

    def decrypt_dict(self, data: dict) -> dict:
        """
        解密字典中的加密字段

        Args:
            data: 包含加密字段的字典

        Returns:
            解密后的字典
        """
        result = {}
        for key, value in data.items():
            if isinstance(value, str) and value.startswith("enc:"):
                try:
                    result[key] = self.decrypt(value[4:])  # 去掉 "enc:" 前缀
                except ValueError:
                    result[key] = value  # 解密失败，保留原值
            else:
                result[key] = value

        return result

    @property
    def is_available(self) -> bool:
        """检查加密功能是否可用"""
        return self._fernet is not None

    def rotate_key(self) -> bool:
        """
        轮换密钥

        生成新密钥并重新加密所有数据

        Returns:
            是否成功
        """
        if not self._fernet:
            return False

        try:
            # 读取当前配置
            config_path = Path.home() / ".iflow2api" / "config.json"
            if config_path.exists():
                with open(config_path, "r", encoding="utf-8") as f:
                    data = json.load(f)

                # 解密当前数据
                decrypted = self.decrypt_dict(data)

                # 生成新密钥
                new_key = Fernet.generate_key()
                new_fernet = Fernet(new_key)

                # 用新密钥加密
                self._fernet = new_fernet
                encrypted = self.encrypt_dict(decrypted)

                # 保存
                with open(config_path, "w", encoding="utf-8") as f:
                    json.dump(encrypted, f, indent=2, ensure_ascii=False)

                # 更新密钥文件
                self._key_path.write_bytes(base64.urlsafe_b64encode(new_key))

                return True
        except Exception as e:
            logger.error("密鑙轮换失败: %s", e)

        return False


def derive_key_from_password(password: str, salt: Optional[bytes] = None) -> tuple[bytes, bytes]:
    """
    从密码派生加密密钥

    Args:
        password: 用户密码
        salt: 盐值，如果为 None 则生成新的

    Returns:
        (密钥, 盐值)
    """
    if not HAS_CRYPTOGRAPHY:
        raise RuntimeError("cryptography 库未安装")

    if salt is None:
        salt = os.urandom(16)

    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        iterations=480000,
    )

    key = base64.urlsafe_b64encode(kdf.derive(password.encode('utf-8')))
    return key, salt


class SecureConfig:
    """安全配置管理器"""

    def __init__(self, config_path: Optional[Path] = None):
        """
        初始化安全配置管理器

        Args:
            config_path: 配置文件路径
        """
        self._config_path = config_path or Path.home() / ".iflow2api" / "secure_config.json"
        self._encryption = ConfigEncryption()
        self._cache: dict = {}

    def load(self) -> dict:
        """
        加载配置

        Returns:
            配置字典
        """
        if not self._config_path.exists():
            return {}

        try:
            with open(self._config_path, "r", encoding="utf-8") as f:
                data = json.load(f)

            # 解密敏感字段
            if self._encryption.is_available:
                data = self._encryption.decrypt_dict(data)

            self._cache = data
            return data
        except Exception as e:
            logger.error("加载配置失败: %s", e)
            return {}

    def save(self, data: dict) -> bool:
        """
        保存配置

        Args:
            data: 配置字典

        Returns:
            是否成功
        """
        try:
            # 加密敏感字段
            if self._encryption.is_available:
                data = self._encryption.encrypt_dict(data)

            self._config_path.parent.mkdir(parents=True, exist_ok=True)

            with open(self._config_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)

            # 设置权限
            try:
                os.chmod(self._config_path, 0o600)
            except Exception:
                pass

            self._cache = data
            return True
        except Exception as e:
            logger.error("保存配置失败: %s", e)
            return False

    def get(self, key: str, default: Any = None) -> Any:
        """
        获取配置值

        Args:
            key: 配置键
            default: 默认值

        Returns:
            配置值
        """
        if not self._cache:
            self.load()
        return self._cache.get(key, default)

    def set(self, key: str, value: Any) -> bool:
        """
        设置配置值

        Args:
            key: 配置键
            value: 配置值

        Returns:
            是否成功
        """
        if not self._cache:
            self.load()

        self._cache[key] = value
        return self.save(self._cache)

    def delete(self, key: str) -> bool:
        """
        删除配置值

        Args:
            key: 配置键

        Returns:
            是否成功
        """
        if not self._cache:
            self.load()

        if key in self._cache:
            del self._cache[key]
            return self.save(self._cache)

        return True

    def clear(self) -> bool:
        """
        清空所有配置

        Returns:
            是否成功
        """
        self._cache = {}
        return self.save({})


# 全局实例
_secure_config: Optional[SecureConfig] = None


def get_secure_config() -> SecureConfig:
    """获取全局安全配置管理器实例"""
    global _secure_config
    if _secure_config is None:
        _secure_config = SecureConfig()
    return _secure_config
