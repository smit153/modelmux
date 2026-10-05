"""Process concurrency limit with a bounded wait queue.

Limits are per process, which is why ModelMux runs a single Uvicorn worker.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from modelmux.errors import OverloadedError

log = logging.getLogger("modelmux.runtime.limits")

DEFAULT_RETRY_AFTER = 5.0


class ConcurrencyLimiter:
    """At most ``max_concurrent`` holders; at most ``max_queue`` waiters.

    A full queue or a wait longer than ``queue_timeout`` raises
    ``OverloadedError`` (503 with ``Retry-After``).
    """

    def __init__(
        self,
        max_concurrent: int,
        max_queue: int,
        queue_timeout: float,
        *,
        retry_after: float = DEFAULT_RETRY_AFTER,
    ) -> None:
        self.max_concurrent = max_concurrent
        self.max_queue = max_queue
        self.queue_timeout = queue_timeout
        self.retry_after = retry_after
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._active = 0
        self._waiting = 0

    @property
    def active(self) -> int:
        return self._active

    @property
    def waiting(self) -> int:
        return self._waiting

    def saturated(self) -> bool:
        """True when every slot is busy and the wait queue is full."""
        return self._active >= self.max_concurrent and self._waiting >= self.max_queue

    @asynccontextmanager
    async def slot(self) -> AsyncIterator[None]:
        await self._acquire()
        self._active += 1
        try:
            yield
        finally:
            self._active -= 1
            self._semaphore.release()

    async def _acquire(self) -> None:
        if not self._semaphore.locked():
            await self._semaphore.acquire()  # a slot is free: returns immediately
            return
        if self._waiting >= self.max_queue:
            log.warning("queue full", extra={"event": "overloaded", "reason": "queue_full"})
            raise OverloadedError("queue full", retry_after=self.retry_after)
        self._waiting += 1
        try:
            async with asyncio.timeout(self.queue_timeout):
                await self._semaphore.acquire()
        except TimeoutError:
            log.warning("queue timeout", extra={"event": "overloaded", "reason": "queue_timeout"})
            raise OverloadedError("queue wait timed out", retry_after=self.retry_after) from None
        finally:
            self._waiting -= 1
