import asyncio
from collections import deque

import iflow2api.account_pool as account_pool_module
from iflow2api.concurrency_limiter import get_concurrency_limiter
from iflow2api.settings import AppSettings, UpstreamAccount


class _FakeProxy:
    def __init__(self, config):
        self.config = config

    async def close(self):
        return None


def _make_settings(max_queued_requests: int = 10) -> AppSettings:
    return AppSettings(
        enable_concurrency_limit=True,
        max_concurrent_requests=1,
        max_queued_requests=max_queued_requests,
        upstream_accounts=[
            UpstreamAccount(
                id="acct-1",
                label="acct-1",
                enabled=True,
                auth_type="oauth-iflow",
                api_key="iflow-key-1",
                oauth_access_token="oauth-token-1",
                oauth_refresh_token="refresh-token-1",
            )
        ],
    )


def _reset_pool_state(monkeypatch, settings: AppSettings) -> None:
    monkeypatch.setattr(account_pool_module, "load_settings", lambda: settings)
    monkeypatch.setattr(account_pool_module, "IFlowProxy", _FakeProxy)
    monkeypatch.setattr(account_pool_module, "_scheduler_lock", asyncio.Lock())
    monkeypatch.setattr(account_pool_module, "_wait_queue", deque())
    monkeypatch.setattr(account_pool_module, "_waiter_sequence", 0)
    monkeypatch.setattr(account_pool_module, "_round_robin_index", 0)
    limiter = get_concurrency_limiter(max_concurrent=1, force_new=True)
    monkeypatch.setattr(
        account_pool_module,
        "get_concurrency_limiter",
        lambda max_concurrent: limiter,
    )


def test_queue_full_fails_fast(monkeypatch):
    settings = _make_settings(max_queued_requests=1)
    _reset_pool_state(monkeypatch, settings)

    async def scenario():
        first_lease = await account_pool_module.acquire_account_lease(timeout=1)
        second_task = asyncio.create_task(account_pool_module.acquire_account_lease(timeout=1))
        await asyncio.sleep(0.05)

        try:
            await account_pool_module.acquire_account_lease(timeout=1)
        except account_pool_module.UpstreamQueueFullError as exc:
            assert "排队请求过多" in str(exc)
        else:
            raise AssertionError("expected UpstreamQueueFullError")

        assert account_pool_module.get_account_pool_stats()["queued_requests"] == 1

        await first_lease.close()
        second_lease = await asyncio.wait_for(second_task, timeout=1)
        await second_lease.close()

    asyncio.run(scenario())


def test_waiters_acquire_in_fifo_order(monkeypatch):
    settings = _make_settings(max_queued_requests=5)
    _reset_pool_state(monkeypatch, settings)

    async def scenario():
        order: list[str] = []
        first_lease = await account_pool_module.acquire_account_lease(timeout=1)

        async def acquire_and_mark(name: str):
            lease = await account_pool_module.acquire_account_lease(timeout=2)
            order.append(name)
            return lease

        second_task = asyncio.create_task(acquire_and_mark("second"))
        third_task = asyncio.create_task(acquire_and_mark("third"))
        await asyncio.sleep(0.05)

        assert account_pool_module.get_account_pool_stats()["queued_requests"] == 2

        await first_lease.close()
        second_lease = await asyncio.wait_for(second_task, timeout=1)
        await asyncio.sleep(0.05)
        assert order == ["second"]
        assert not third_task.done()

        await second_lease.close()
        third_lease = await asyncio.wait_for(third_task, timeout=1)
        await third_lease.close()

        assert order == ["second", "third"]
        assert account_pool_module.get_account_pool_stats()["queued_requests"] == 0

    asyncio.run(scenario())
