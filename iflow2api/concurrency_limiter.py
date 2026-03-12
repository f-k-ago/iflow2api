"""并发限制模块 - 实现官方的单并发限制"""

import asyncio
from threading import Lock
from typing import Optional, Dict
from contextlib import asynccontextmanager

from pydantic import BaseModel


class ConcurrencyLimitConfig(BaseModel):
    """并发限制配置"""
    enabled: bool = True
    max_concurrent_requests: int = 1  # 官方限制：每个用户最多1个并发


class ConcurrencyLimiter:
    """并发限制器 - 按 API Key 限制同时进行的请求数

    官方规则：
    - 每个用户（API Key）最多同时 1 个请求
    - 流式请求：取消后立即释放令牌
    - 非流式请求：取消后需等待完成才释放令牌
    """

    def __init__(self, max_concurrent: int = 1):
        """初始化并发限制器

        Args:
            max_concurrent: 每个 API Key 的最大并发数
        """
        self.max_concurrent = max_concurrent
        # {api_key: {"count": int, "lock": asyncio.Lock, "waiters": int}}
        self._api_keys: Dict[str, dict] = {}
        self._lock = Lock()

    def _get_or_create_key_state(self, api_key: str) -> dict:
        """获取或创建 API Key 的状态"""
        with self._lock:
            if api_key not in self._api_keys:
                self._api_keys[api_key] = {
                    "count": 0,
                    "lock": asyncio.Lock(),
                    "waiters": 0,
                }
            return self._api_keys[api_key]

    @asynccontextmanager
    async def acquire(self, api_key: str, timeout: Optional[float] = None):
        """获取并发令牌（异步上下文管理器）

        Args:
            api_key: API Key 标识
            timeout: 等待超时时间（秒），None 表示无限等待
        Yields:
            None
        Raises:
            asyncio.TimeoutError: 等待超时
        """
        state = self._get_or_create_key_state(api_key)

        # 增加等待计数
        with self._lock:
            state["waiters"] += 1

        try:
            # 等待获取令牌
            if timeout:
                await asyncio.wait_for(
                    self._wait_for_slot(state),
                    timeout=timeout
                )
            else:
                await self._wait_for_slot(state)

            # 增加并发计数
            with self._lock:
                state["count"] += 1

            try:
                yield
            finally:
                # 释放令牌
                with self._lock:
                    state["count"] -= 1
        finally:
            # 减少等待计数
            with self._lock:
                state["waiters"] -= 1

    async def _wait_for_slot(self, state: dict):
        """等待可用的并发槽位"""
        while True:
            with self._lock:
                if state["count"] < self.max_concurrent:
                    return
            # 短暂等待后重试
            await asyncio.sleep(0.1)

    def try_acquire_nowait(self, api_key: str) -> bool:
        """尝试立即获取并发令牌，不阻塞等待。"""
        state = self._get_or_create_key_state(api_key)
        with self._lock:
            if state["count"] >= self.max_concurrent:
                return False
            state["count"] += 1
            return True

    def release(self, api_key: str) -> None:
        """释放并发令牌。"""
        state = self._get_or_create_key_state(api_key)
        with self._lock:
            if state["count"] > 0:
                state["count"] -= 1

    def get_stats(self, api_key: str) -> dict:
        """获取 API Key 的并发统计

        Args:
            api_key: API Key 标识

        Returns:
            统计信息字典
        """
        state = self._get_or_create_key_state(api_key)
        with self._lock:
            return {
                "current_concurrent": state["count"],
                "max_concurrent": self.max_concurrent,
                "waiting": state["waiters"],
                "available": max(0, self.max_concurrent - state["count"]),
            }

    def get_all_stats(self) -> dict:
        """获取所有 API Key 的统计信息"""
        with self._lock:
            return {
                api_key: {
                    "current_concurrent": state["count"],
                    "waiting": state["waiters"],
                }
                for api_key, state in self._api_keys.items()
                if state["count"] > 0 or state["waiters"] > 0
            }


# 全局并发限制器实例
_concurrency_limiter: Optional[ConcurrencyLimiter] = None
_concurrency_limiter_lock = Lock()


def get_concurrency_limiter(
    max_concurrent: int = 1,
    force_new: bool = False,
) -> ConcurrencyLimiter:
    """获取全局并发限制器实例

    Args:
        max_concurrent: 最大并发数
        force_new: 是否强制创建新实例

    Returns:
        ConcurrencyLimiter 实例
    """
    global _concurrency_limiter

    with _concurrency_limiter_lock:
        if _concurrency_limiter is None or force_new:
            _concurrency_limiter = ConcurrencyLimiter(max_concurrent=max_concurrent)
        return _concurrency_limiter


def init_concurrency_limiter(config: ConcurrencyLimitConfig) -> None:
    """初始化并发限制器

    Args:
        config: 并发限制配置
    """
    if config.enabled:
        get_concurrency_limiter(
            max_concurrent=config.max_concurrent_requests,
            force_new=True,
        )
