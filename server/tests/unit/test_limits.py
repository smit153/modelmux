from __future__ import annotations

import asyncio

import pytest

from modelmux.errors import OverloadedError
from modelmux.runtime.limits import ConcurrencyLimiter


async def hold(limiter: ConcurrencyLimiter, release: asyncio.Event, entered: asyncio.Event) -> None:
    async with limiter.slot():
        entered.set()
        await release.wait()


async def test_runs_up_to_max_concurrently() -> None:
    limiter = ConcurrencyLimiter(2, 0, 1.0)
    release = asyncio.Event()
    events = [asyncio.Event(), asyncio.Event()]
    tasks = [asyncio.create_task(hold(limiter, release, e)) for e in events]
    await asyncio.gather(*(e.wait() for e in events))
    assert limiter.active == 2
    release.set()
    await asyncio.gather(*tasks)
    assert limiter.active == 0


async def test_queue_full_rejects_immediately() -> None:
    limiter = ConcurrencyLimiter(1, 1, 10.0, retry_after=7)
    release = asyncio.Event()
    entered = asyncio.Event()
    holder = asyncio.create_task(hold(limiter, release, entered))
    await entered.wait()
    waiter = asyncio.create_task(hold(limiter, release, asyncio.Event()))
    await asyncio.sleep(0.01)
    assert limiter.waiting == 1
    assert limiter.saturated()

    with pytest.raises(OverloadedError) as info:
        async with limiter.slot():
            pass  # pragma: no cover
    assert info.value.headers() == {"Retry-After": "7"}

    release.set()
    await asyncio.gather(holder, waiter)
    assert (limiter.active, limiter.waiting) == (0, 0)
    assert not limiter.saturated()


async def test_zero_queue_rejects_when_busy() -> None:
    limiter = ConcurrencyLimiter(1, 0, 10.0)
    release = asyncio.Event()
    entered = asyncio.Event()
    holder = asyncio.create_task(hold(limiter, release, entered))
    await entered.wait()
    assert limiter.saturated()
    with pytest.raises(OverloadedError):
        async with limiter.slot():
            pass  # pragma: no cover
    release.set()
    await holder


async def test_queue_timeout() -> None:
    limiter = ConcurrencyLimiter(1, 5, 0.05)
    release = asyncio.Event()
    entered = asyncio.Event()
    holder = asyncio.create_task(hold(limiter, release, entered))
    await entered.wait()
    with pytest.raises(OverloadedError) as info:
        async with limiter.slot():
            pass  # pragma: no cover
    assert "timed out" in str(info.value)
    assert limiter.waiting == 0
    release.set()
    await holder


async def test_waiter_gets_slot_when_released() -> None:
    limiter = ConcurrencyLimiter(1, 1, 5.0)
    release = asyncio.Event()
    entered = asyncio.Event()
    holder = asyncio.create_task(hold(limiter, release, entered))
    await entered.wait()
    second_entered = asyncio.Event()
    second = asyncio.create_task(hold(limiter, asyncio.Event(), second_entered))
    await asyncio.sleep(0.01)
    assert not second_entered.is_set()
    release.set()
    await asyncio.wait_for(second_entered.wait(), 1)
    second.cancel()
    await asyncio.gather(holder, second, return_exceptions=True)
    assert limiter.active == 0


async def test_slot_released_on_exception() -> None:
    limiter = ConcurrencyLimiter(1, 0, 1.0)
    with pytest.raises(RuntimeError):
        async with limiter.slot():
            raise RuntimeError
    async with limiter.slot():
        assert limiter.active == 1


async def test_cancelled_waiter_does_not_leak() -> None:
    limiter = ConcurrencyLimiter(1, 1, 10.0)
    release = asyncio.Event()
    entered = asyncio.Event()
    holder = asyncio.create_task(hold(limiter, release, entered))
    await entered.wait()
    waiter = asyncio.create_task(hold(limiter, release, asyncio.Event()))
    await asyncio.sleep(0.01)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    assert limiter.waiting == 0
    release.set()
    await holder
    # Both slots worth of capacity are still there.
    async with limiter.slot():
        assert limiter.active == 1
    assert limiter.active == 0
