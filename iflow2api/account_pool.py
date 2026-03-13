"""上游账号池调度与租约管理。"""

import asyncio
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from .concurrency_limiter import ConcurrencyLimiter, get_concurrency_limiter
from .config import IFlowConfig
from .proxy import IFlowProxy
from .settings import DEFAULT_BASE_URL, UpstreamAccount, get_enabled_upstream_accounts, load_settings


class NoUpstreamAccountError(Exception):
    """没有可用上游账号。"""


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


_round_robin_lock = asyncio.Lock()
_round_robin_index = 0


async def _rotate_accounts(accounts: list[UpstreamAccount]) -> list[UpstreamAccount]:
    """轮转账号顺序，避免总是命中第一个账号。"""
    global _round_robin_index

    if not accounts:
        return []

    async with _round_robin_lock:
        start = _round_robin_index % len(accounts)
        _round_robin_index = (_round_robin_index + 1) % len(accounts)

    return accounts[start:] + accounts[:start]


async def acquire_account_lease(timeout: float = 300.0) -> AccountLease:
    """获取一个可用账号租约。"""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout

    while True:
        settings = load_settings()
        accounts = get_enabled_upstream_accounts(settings)
        if not accounts:
            raise NoUpstreamAccountError("没有可用的上游账号，请先在管理界面添加并启用账号")

        ordered_accounts = await _rotate_accounts(accounts)
        if not settings.enable_concurrency_limit:
            account = ordered_accounts[0]
            return AccountLease(
                account=account,
                proxy=IFlowProxy(build_iflow_config_from_account(account)),
            )

        limiter = get_concurrency_limiter(max_concurrent=settings.max_concurrent_requests)
        for account in ordered_accounts:
            if limiter.try_acquire_nowait(account.id):
                return AccountLease(
                    account=account,
                    proxy=IFlowProxy(build_iflow_config_from_account(account)),
                    limiter=limiter,
                    limiter_key=account.id,
                )

        if loop.time() >= deadline:
            raise asyncio.TimeoutError("等待可用上游账号超时")

        await asyncio.sleep(0.1)
