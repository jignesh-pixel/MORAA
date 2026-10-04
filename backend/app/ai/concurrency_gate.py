"""A global limit on simultaneous paid provider calls, with priority (Q-6).

``MAX_CONCURRENT_PROVIDER_CALLS`` calls may run at once (0 = no limit). When all slots are busy, waiting calls are
served in priority order: a lower number goes first (a single Studio Shot, priority 0, is served before the seven calls of
a Catalog Pack, priority 1), then first-come-first-served. Waiting here costs nothing: the daily-cap slot was reserved
earlier, and the provider has not been called yet.

The gate lives per event loop (the web server has one). It limits calls inside one server process; with several
processes the limit applies per process (a shared limit needs Redis: see docs/QUESTIONS_FOR_MORNING.md).
"""

from __future__ import annotations

import asyncio
import contextvars
import heapq
import itertools
import weakref
from contextlib import asynccontextmanager
from typing import Dict, List, Tuple

from app.config import settings

PRIORITY_SINGLE = 0
PRIORITY_PACK = 1

# Set by a worker to say how urgent its provider calls are (inherited by the tasks it starts).
generation_priority: contextvars.ContextVar[int] = contextvars.ContextVar("generation_priority", default=PRIORITY_PACK)


class PriorityGate:
    def __init__(self) -> None:
        self._active = 0
        self._waiters: List[Tuple[int, int, asyncio.Future]] = []
        self._counter = itertools.count()

    @staticmethod
    def _limit() -> int:
        return max(int(getattr(settings, "MAX_CONCURRENT_PROVIDER_CALLS", 0) or 0), 0)

    @property
    def waiting(self) -> int:
        return sum(1 for _, _, f in self._waiters if not f.done())

    @property
    def active(self) -> int:
        return self._active

    async def acquire(self, priority: int) -> bool:
        """Wait for a slot. Returns True if one was taken (it must be released), False when there is no limit."""
        limit = self._limit()
        if limit <= 0:
            return False
        if self._active < limit and not self.waiting:
            self._active += 1
            return True
        future = asyncio.get_running_loop().create_future()
        heapq.heappush(self._waiters, (priority, next(self._counter), future))
        try:
            await future                    # the releaser hands its slot to us (self._active is not decremented)
        except BaseException:
            if future.done() and not future.cancelled():
                self._hand_over()           # we were given a slot but are leaving: pass it on
            raise
        return True

    def release(self) -> None:
        self._hand_over()

    def _hand_over(self) -> None:
        while self._waiters:
            _, _, future = heapq.heappop(self._waiters)
            if not future.done():
                future.set_result(None)
                return
        self._active = max(self._active - 1, 0)

    @asynccontextmanager
    async def slot(self, priority: int):
        taken = await self.acquire(priority)
        try:
            yield
        finally:
            if taken:
                self.release()


_gates: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, PriorityGate]" = weakref.WeakKeyDictionary()


def gate_for_current_loop() -> PriorityGate:
    loop = asyncio.get_running_loop()
    gate = _gates.get(loop)
    if gate is None:
        gate = _gates[loop] = PriorityGate()
    return gate


def snapshot() -> Dict[str, int]:
    """Active/waiting counts for the loop that is running (for metrics); zeros when called outside a loop."""
    try:
        gate = gate_for_current_loop()
    except RuntimeError:
        return {"active": 0, "waiting": 0}
    return {"active": gate.active, "waiting": gate.waiting}
