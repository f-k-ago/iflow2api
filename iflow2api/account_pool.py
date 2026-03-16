"""上游账号池调度与租约管理。"""

import asyncio
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
import time
from typing import Awaitable, Callable, Optional

from .concurrency_limiter import ConcurrencyLimiter, get_concurrency_limiter
from .config import IFlowConfig
from .proxy import IFlowProxy
from .settings import DEFAULT_BASE_URL, UpstreamAccount, get_enabled_upstream_accounts, load_settings

import logging

logger = logging.getLogger("iflow2api")


class NoUpstreamAccountError(Exception):
    """没有可用的上游账号。"""


class UpstreamQueueFullError(asyncio.TimeoutError):
    """上游账号排队队列已满。"""


QUEUE_RECHECK_INTERVAL_SECONDS = 1.0
AUTH_FAILURE_COOLDOWN_SECONDS = 5 * 60


def _parse_datetime(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def build_iflow_config_from_account(account: UpstreamAccount) -> IFlowConfig:
    """把账号池条目转换成代理使用的 iFlow 配置。"""
    return IFlowConfig(
        api_key=account.api_key,
        base_url=(account.base_url or DEFAULT_BASE_URL),
        auth_type=(account.auth_type or None),
        oauth_access_token=(account.oauth_access_token or None),
        oauth_refresh_token=(account.oauth_refresh_token or None),
        oauth_expires_at=_parse_datetime(account.oauth_expires_at),
        api_key_expires_at=_parse_datetime(account.oauth_expires_at),
        cookie=(account.cookie or None),
        cookie_email=(account.cookie_email or account.email or None),
        cookie_expires_at=account.cookie_expires_at,
        session_id=(account.session_id or None),
        conversation_id=(account.conversation_id or None),
    )


@dataclass
class AccountLease:
    """一次上游请求使用的账号租约。"""

    account: UpstreamAccount
    proxy: IFlowProxy
    limiter: Optional[ConcurrencyLimiter] = None
    limiter_key: str = ""
    on_release: Optional[Callable[[], Awaitable[None]]] = None
    _closed: bool = False

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            await self.proxy.close()
        finally:
            if self.limiter and self.limiter_key:
                self.limiter.release(self.limiter_key)
            if self.on_release is not None:
                await self.on_release()


@dataclass(slots=True)
class _LeaseWaiter:
    """等待账号租约的排队请求。"""

    sequence: int
    event: asyncio.Event = field(default_factory=asyncio.Event)


_scheduler_lock = asyncio.Lock()
_wait_queue: deque[_LeaseWaiter] = deque()
_waiter_sequence = 0
_round_robin_index = 0
_account_cooldowns: dict[str, float] = {}


def _next_waiter_sequence() -> int:
    """生成单调递增的等待序号。"""
    global _waiter_sequence
    _waiter_sequence += 1
    return _waiter_sequence


def _rotate_accounts_locked(accounts: list[UpstreamAccount]) -> list[UpstreamAccount]:
    """轮转账号顺序，避免总是命中第一个账号。调用方需持有调度锁。"""
    global _round_robin_index

    if not accounts:
        return []

    start = _round_robin_index % len(accounts)
    _round_robin_index = (_round_robin_index + 1) % len(accounts)
    return accounts[start:] + accounts[:start]


def _prune_expired_cooldowns_locked(now: Optional[float] = None) -> None:
    """清理已过期的账号冷却条目。调用方需持有调度锁。"""
    current = time.monotonic() if now is None else now
    expired_ids = [
        account_id
        for account_id, expires_at in _account_cooldowns.items()
        if expires_at <= current
    ]
    for account_id in expired_ids:
        _account_cooldowns.pop(account_id, None)


def _filter_available_accounts_locked(accounts: list[UpstreamAccount]) -> list[UpstreamAccount]:
    """过滤掉处于冷却期的账号。调用方需持有调度锁。"""
    now = time.monotonic()
    _prune_expired_cooldowns_locked(now)
    return [
        account
        for account in accounts
        if _account_cooldowns.get(account.id, 0.0) <= now
    ]


def _wake_next_waiter_locked() -> None:
    """唤醒队首等待者。调用方需持有调度锁。"""
    if _wait_queue:
        _wait_queue[0].event.set()


def _remove_waiter_locked(waiter: _LeaseWaiter) -> None:
    """从队列中移除等待者并在必要时唤醒下一位。调用方需持有调度锁。"""
    was_head = bool(_wait_queue) and _wait_queue[0] is waiter
    try:
        _wait_queue.remove(waiter)
    except ValueError:
        return
    if was_head:
        _wake_next_waiter_locked()


async def _notify_wait_queue() -> None:
    """在租约释放后唤醒队首等待者。"""
    async with _scheduler_lock:
        _wake_next_waiter_locked()


def _build_account_lease(
    account: UpstreamAccount,
    limiter: Optional[ConcurrencyLimiter] = None,
    limiter_key: str = "",
) -> AccountLease:
    """创建账号租约对象。"""
    return AccountLease(
        account=account,
        proxy=IFlowProxy(build_iflow_config_from_account(account)),
        limiter=limiter,
        limiter_key=limiter_key,
        on_release=_notify_wait_queue,
    )


def _try_allocate_lease_locked(settings, accounts: list[UpstreamAccount]) -> Optional[AccountLease]:
    """尝试立即分配一个租约。调用方需持有调度锁。"""
    available_accounts = _filter_available_accounts_locked(accounts)
    ordered_accounts = _rotate_accounts_locked(available_accounts)
    if not settings.enable_concurrency_limit:
        if not ordered_accounts:
            return None
        return _build_account_lease(ordered_accounts[0])

    limiter = get_concurrency_limiter(max_concurrent=settings.max_concurrent_requests)
    for account in ordered_accounts:
        if limiter.try_acquire_nowait(account.id):
            return _build_account_lease(
                account=account,
                limiter=limiter,
                limiter_key=account.id,
            )
    return None


async def mark_account_cooldown(
    account_id: str,
    *,
    cooldown_seconds: float = AUTH_FAILURE_COOLDOWN_SECONDS,
    reason: str = "auth_failure",
) -> None:
    """将指定账号标记为短暂冷却，避免被立即重复调度。"""
    normalized_account_id = (account_id or "").strip()
    if not normalized_account_id:
        return

    cooldown = max(float(cooldown_seconds), 0.0)
    expires_at = time.monotonic() + cooldown
    async with _scheduler_lock:
        _account_cooldowns[normalized_account_id] = expires_at
        _wake_next_waiter_locked()
    logger.warning(
        "账号进入冷却期: account_id=%s, cooldown_seconds=%.1f, reason=%s",
        normalized_account_id,
        cooldown,
        reason,
    )


async def clear_account_cooldown(account_id: str, *, reason: str = "manual") -> bool:
    """清除账号冷却标记。"""
    normalized_account_id = (account_id or "").strip()
    if not normalized_account_id:
        return False

    async with _scheduler_lock:
        removed = _account_cooldowns.pop(normalized_account_id, None) is not None
        if removed:
            _wake_next_waiter_locked()
    if removed:
        logger.info(
            "账号冷却已解除: account_id=%s, reason=%s",
            normalized_account_id,
            reason,
        )
    return removed


async def acquire_account_lease(timeout: float = 300.0) -> AccountLease:
    """获取一个可用账号租约。"""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    waiter: Optional[_LeaseWaiter] = None

    settings = load_settings()
    accounts = get_enabled_upstream_accounts(settings)
    if not accounts:
        raise NoUpstreamAccountError("没有可用的上游账号，请先在管理界面添加并启用账号")

    async with _scheduler_lock:
        if not settings.enable_concurrency_limit:
            lease = _try_allocate_lease_locked(settings, accounts)
            if lease is None:
                raise NoUpstreamAccountError("没有可用的上游账号，请先在管理界面添加并启用账号")
            return lease

        if not _wait_queue:
            lease = _try_allocate_lease_locked(settings, accounts)
            if lease is not None:
                return lease

        if len(_wait_queue) >= settings.max_queued_requests:
            raise UpstreamQueueFullError("当前排队请求过多，请稍后重试")

        waiter = _LeaseWaiter(sequence=_next_waiter_sequence())
        _wait_queue.append(waiter)
        if _wait_queue[0] is waiter:
            waiter.event.set()

    promoted = False
    try:
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise asyncio.TimeoutError("等待可用上游账号超时")

            if not promoted:
                await asyncio.wait_for(waiter.event.wait(), timeout=remaining)
                waiter.event.clear()
                promoted = True

            settings = load_settings()
            accounts = get_enabled_upstream_accounts(settings)
            if not accounts:
                raise NoUpstreamAccountError("没有可用的上游账号，请先在管理界面添加并启用账号")

            async with _scheduler_lock:
                if not _wait_queue or _wait_queue[0] is not waiter:
                    promoted = False
                    continue

                lease = _try_allocate_lease_locked(settings, accounts)
                if lease is not None:
                    _wait_queue.popleft()
                    _wake_next_waiter_locked()
                    return lease

            remaining = deadline - loop.time()
            if remaining <= 0:
                raise asyncio.TimeoutError("等待可用上游账号超时")

            try:
                await asyncio.wait_for(
                    waiter.event.wait(),
                    timeout=min(remaining, QUEUE_RECHECK_INTERVAL_SECONDS),
                )
            except asyncio.TimeoutError:
                continue
            else:
                waiter.event.clear()
    finally:
        if waiter is not None:
            async with _scheduler_lock:
                _remove_waiter_locked(waiter)


def get_account_pool_stats() -> dict[str, int]:
    """返回账号池排队概况。"""
    now = time.monotonic()
    active_cooldowns = sum(1 for expires_at in _account_cooldowns.values() if expires_at > now)
    return {
        "queued_requests": len(_wait_queue),
        "cooldown_accounts": active_cooldowns,
    }
