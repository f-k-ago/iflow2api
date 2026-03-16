"""多实例管理模块 - 支持多个服务实例"""

import json
import logging
import socket
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Optional

from pydantic import BaseModel

logger = logging.getLogger("iflow2api")


class InstanceStatus(Enum):
    """实例状态"""
    STOPPED = "stopped"
    STARTING = "starting"
    RUNNING = "running"
    STOPPING = "stopping"
    ERROR = "error"


class InstanceConfig(BaseModel):
    """实例配置"""
    id: str
    name: str
    host: str = "0.0.0.0"
    port: int = 28000
    api_key: str = ""
    base_url: str = "https://apis.iflow.cn/v1"
    created_at: datetime = datetime.now()
    updated_at: datetime = datetime.now()

    class Config:
        json_encoders = {
            datetime: lambda v: v.isoformat()
        }


class InstanceInfo(BaseModel):
    """实例信息"""
    config: InstanceConfig
    status: InstanceStatus = InstanceStatus.STOPPED
    error_message: str = ""
    started_at: Optional[datetime] = None
    request_count: int = 0

    class Config:
        json_encoders = {
            datetime: lambda v: v.isoformat() if v else None
        }


class InstanceManager:
    """实例管理器"""

    def __init__(self, config_dir: Optional[Path] = None):
        """
        初始化实例管理器

        Args:
            config_dir: 配置目录路径
        """
        self._config_dir = config_dir or Path.home() / ".iflow2api" / "instances"
        self._instances: dict[str, InstanceInfo] = {}
        self._load_instances()

    def _load_instances(self) -> None:
        """加载所有实例配置"""
        if not self._config_dir.exists():
            return

        for instance_file in self._config_dir.glob("*.json"):
            try:
                with open(instance_file, "r", encoding="utf-8") as f:
                    data = json.load(f)

                config = InstanceConfig(
                    id=data["id"],
                    name=data["name"],
                    host=data.get("host", "0.0.0.0"),
                    port=data.get("port", 28000),
                    api_key=data.get("api_key", ""),
                    base_url=data.get("base_url", "https://apis.iflow.cn/v1"),
                    created_at=datetime.fromisoformat(data["created_at"]) if data.get("created_at") else datetime.now(),
                    updated_at=datetime.fromisoformat(data["updated_at"]) if data.get("updated_at") else datetime.now(),
                )

                self._instances[config.id] = InstanceInfo(config=config)
            except Exception as e:
                logger.warning("加载实例配置失败 %s: %s", instance_file, e)

    def _save_instance(self, instance_id: str) -> bool:
        """保存实例配置"""
        if instance_id not in self._instances:
            return False

        instance = self._instances[instance_id]
        self._config_dir.mkdir(parents=True, exist_ok=True)

        config_path = self._config_dir / f"{instance_id}.json"
        try:
            with open(config_path, "w", encoding="utf-8") as f:
                json.dump({
                    "id": instance.config.id,
                    "name": instance.config.name,
                    "host": instance.config.host,
                    "port": instance.config.port,
                    "api_key": instance.config.api_key,
                    "base_url": instance.config.base_url,
                    "created_at": instance.config.created_at.isoformat(),
                    "updated_at": instance.config.updated_at.isoformat(),
                }, f, indent=2, ensure_ascii=False)
            return True
        except Exception as e:
            logger.warning("保存实例配置失败: %s", e)
            return False

    def _delete_instance_file(self, instance_id: str) -> bool:
        """删除实例配置文件"""
        config_path = self._config_dir / f"{instance_id}.json"
        try:
            if config_path.exists():
                config_path.unlink()
            return True
        except Exception as e:
            logger.warning("删除实例配置失败: %s", e)
            return False

    @staticmethod
    def _generate_id() -> str:
        """生成实例 ID"""
        import uuid
        return uuid.uuid4().hex[:8]

    @staticmethod
    def is_port_available(host: str, port: int) -> bool:
        """检查端口是否可用"""
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.bind((host if host != "0.0.0.0" else "127.0.0.1", port))
                return True
        except OSError:
            return False

    def create_instance(
        self,
        name: str,
        host: str = "0.0.0.0",
        port: int = 28000,
        api_key: str = "",
        base_url: str = "https://apis.iflow.cn/v1",
    ) -> Optional[InstanceInfo]:
        """
        创建新实例

        Args:
            name: 实例名称
            host: 监听地址
            port: 监听端口
            api_key: API Key
            base_url: Base URL

        Returns:
            创建的实例信息，如果失败返回 None
        """
        # 检查端口是否已被其他实例使用
        for instance in self._instances.values():
            if instance.config.port == port and instance.status == InstanceStatus.RUNNING:
                return None

        instance_id = self._generate_id()
        config = InstanceConfig(
            id=instance_id,
            name=name,
            host=host,
            port=port,
            api_key=api_key,
            base_url=base_url,
        )

        instance = InstanceInfo(config=config)
        self._instances[instance_id] = instance

        if self._save_instance(instance_id):
            return instance
        return None

    def get_instance(self, instance_id: str) -> Optional[InstanceInfo]:
        """
        获取实例信息

        Args:
            instance_id: 实例 ID

        Returns:
            实例信息
        """
        return self._instances.get(instance_id)

    def list_instances(self) -> list[InstanceInfo]:
        """
        获取所有实例列表

        Returns:
            实例列表
        """
        return list(self._instances.values())

    def update_instance(
        self,
        instance_id: str,
        name: Optional[str] = None,
        host: Optional[str] = None,
        port: Optional[int] = None,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
    ) -> Optional[InstanceInfo]:
        """
        更新实例配置

        Args:
            instance_id: 实例 ID
            name: 新名称
            host: 新监听地址
            port: 新端口
            api_key: 新 API Key
            base_url: 新 Base URL

        Returns:
            更新后的实例信息
        """
        if instance_id not in self._instances:
            return None

        instance = self._instances[instance_id]

        # 不能修改运行中的实例
        if instance.status == InstanceStatus.RUNNING:
            return None

        if name is not None:
            instance.config.name = name
        if host is not None:
            instance.config.host = host
        if port is not None:
            instance.config.port = port
        if api_key is not None:
            instance.config.api_key = api_key
        if base_url is not None:
            instance.config.base_url = base_url

        instance.config.updated_at = datetime.now()

        if self._save_instance(instance_id):
            return instance
        return None

    def delete_instance(self, instance_id: str) -> bool:
        """
        删除实例

        Args:
            instance_id: 实例 ID

        Returns:
            是否成功
        """
        if instance_id not in self._instances:
            return False

        instance = self._instances[instance_id]

        # 不能删除运行中的实例
        if instance.status == InstanceStatus.RUNNING:
            return False

        del self._instances[instance_id]
        return self._delete_instance_file(instance_id)

    def set_instance_status(
        self,
        instance_id: str,
        status: InstanceStatus,
        error_message: str = "",
    ) -> bool:
        """
        设置实例状态

        Args:
            instance_id: 实例 ID
            status: 新状态
            error_message: 错误消息

        Returns:
            是否成功
        """
        if instance_id not in self._instances:
            return False

        instance = self._instances[instance_id]
        instance.status = status
        instance.error_message = error_message

        if status == InstanceStatus.RUNNING:
            instance.started_at = datetime.now()
        elif status == InstanceStatus.STOPPED:
            instance.started_at = None

        return True

    def increment_request_count(self, instance_id: str) -> bool:
        """
        增加请求计数

        Args:
            instance_id: 实例 ID

        Returns:
            是否成功
        """
        if instance_id not in self._instances:
            return False

        self._instances[instance_id].request_count += 1
        return True

    def get_running_instances(self) -> list[InstanceInfo]:
        """
        获取所有运行中的实例

        Returns:
            运行中的实例列表
        """
        return [
            inst for inst in self._instances.values()
            if inst.status == InstanceStatus.RUNNING
        ]

    def get_instances_by_port(self, port: int) -> list[InstanceInfo]:
        """
        获取使用指定端口的实例

        Args:
            port: 端口号

        Returns:
            实例列表
        """
        return [
            inst for inst in self._instances.values()
            if inst.config.port == port
        ]

    def find_available_port(self, start_port: int = 28000, max_attempts: int = 100) -> int:
        """
        查找可用端口

        Args:
            start_port: 起始端口
            max_attempts: 最大尝试次数

        Returns:
            可用端口号
        """
        for port in range(start_port, start_port + max_attempts):
            # 检查端口是否被系统占用
            if not self.is_port_available("0.0.0.0", port):
                continue

            # 检查端口是否被其他实例使用
            instances_on_port = self.get_instances_by_port(port)
            running_on_port = [
                inst for inst in instances_on_port
                if inst.status == InstanceStatus.RUNNING
            ]

            if not running_on_port:
                return port

        raise RuntimeError(f"无法找到可用端口 (尝试了 {max_attempts} 个端口)")

    def get_stats(self) -> dict[str, Any]:
        """
        获取实例统计信息

        Returns:
            统计信息
        """
        total = len(self._instances)
        running = len(self.get_running_instances())
        stopped = total - running

        total_requests = sum(
            inst.request_count for inst in self._instances.values()
        )

        return {
            "total_instances": total,
            "running_instances": running,
            "stopped_instances": stopped,
            "total_requests": total_requests,
        }


# 全局实例管理器
_instance_manager: Optional[InstanceManager] = None


def get_instance_manager() -> InstanceManager:
    """获取全局实例管理器"""
    global _instance_manager
    if _instance_manager is None:
        _instance_manager = InstanceManager()
    return _instance_manager
