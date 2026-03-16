"""WebSocket 连接管理器 - 实时状态推送"""

import asyncio
from datetime import datetime
from typing import Any, Optional

from fastapi import WebSocket


class ConnectionManager:
    """WebSocket 连接管理器"""

    def __init__(self):
        self._connections: list[WebSocket] = []
        self._lock = asyncio.Lock()

    async def connect(self, websocket: WebSocket) -> None:
        """接受新连接"""
        await websocket.accept()
        await self.register(websocket)

    async def register(self, websocket: WebSocket) -> None:
        """登记一个已经完成 accept 的连接。"""
        async with self._lock:
            self._connections.append(websocket)

    async def disconnect(self, websocket: WebSocket) -> None:
        """断开连接"""
        async with self._lock:
            if websocket in self._connections:
                self._connections.remove(websocket)

    async def send_personal(self, websocket: WebSocket, message: dict[str, Any]) -> None:
        """发送个人消息"""
        try:
            await websocket.send_json(message)
        except Exception:
            await self.disconnect(websocket)

    async def broadcast(self, message: dict[str, Any]) -> None:
        """广播消息到所有连接"""
        async with self._lock:
            disconnected = []
            for connection in self._connections:
                try:
                    await connection.send_json(message)
                except Exception:
                    disconnected.append(connection)
            
            # 移除断开的连接
            for connection in disconnected:
                self._connections.remove(connection)

    async def broadcast_status(self, status: dict[str, Any]) -> None:
        """广播状态更新"""
        await self.broadcast({
            "type": "status",
            "timestamp": datetime.now().isoformat(),
            "data": status,
        })

    async def broadcast_log(self, log_level: str, message: str, details: Optional[dict] = None) -> None:
        """广播日志消息"""
        await self.broadcast({
            "type": "log",
            "timestamp": datetime.now().isoformat(),
            "data": {
                "level": log_level,
                "message": message,
                "details": details or {},
            },
        })

    async def broadcast_metrics(self, metrics: dict[str, Any]) -> None:
        """广播指标数据"""
        await self.broadcast({
            "type": "metrics",
            "timestamp": datetime.now().isoformat(),
            "data": metrics,
        })

    @property
    def connection_count(self) -> int:
        """当前连接数"""
        return len(self._connections)


# 全局连接管理器实例
_connection_manager: Optional[ConnectionManager] = None


def get_connection_manager() -> ConnectionManager:
    """获取全局连接管理器实例"""
    global _connection_manager
    if _connection_manager is None:
        _connection_manager = ConnectionManager()
    return _connection_manager
