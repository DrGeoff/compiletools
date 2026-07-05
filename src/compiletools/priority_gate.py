"""Priority-ordered async concurrency gate.

Drop-in replacement for ``asyncio.Semaphore(n)`` in ShakeBackend that, when
all N slots are busy, hands the next freed slot to the highest-priority parked
waiter instead of the oldest. Priority is the rule's critical time (the longest
remaining path to the build target); starting the longest pole first keeps
cores busy at the tail rather than idling on a late-dispatched heavyweight.
Equal priorities fall back to FIFO so no waiter starves.

Not thread-safe by design: every acquire/release happens on the single event
loop thread that drives the Shake scheduler.
"""

from __future__ import annotations

import asyncio
import heapq
import itertools


class PriorityGate:
    """N-slot concurrency gate; a freed slot goes to the highest-priority waiter.

    ``async with`` is intentionally NOT supported because the acquire needs a
    per-call priority argument; callers use ``await gate.acquire(priority)`` and
    ``gate.release()`` in a ``try/finally`` so an exception cannot leak a slot.
    """

    def __init__(self, n: int) -> None:
        self._free = max(1, n)
        # Min-heap of (-priority, seq, future): highest priority first, with a
        # monotonic seq as a FIFO tiebreak among equal priorities.
        self._waiters: list[tuple[float, int, asyncio.Future]] = []
        self._seq = itertools.count()

    async def acquire(self, priority: float = 0.0) -> None:
        """Take a slot immediately if one is free, else park until one is
        handed to us. Higher ``priority`` wakes sooner."""
        if self._free > 0:
            self._free -= 1
            return
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        heapq.heappush(self._waiters, (-priority, next(self._seq), fut))
        try:
            await fut
        except asyncio.CancelledError:
            # If a slot was already handed to us (future resolved) but we were
            # cancelled before resuming, pass the slot on so it is not lost.
            # Otherwise our heap entry is now stale; release() skips done
            # futures, so nothing else is required.
            if fut.done() and not fut.cancelled():
                self.release()
            raise

    def release(self) -> None:
        """Hand a slot to the highest-priority parked waiter, or return it to
        the free pool if none are waiting."""
        while self._waiters:
            _, _, fut = heapq.heappop(self._waiters)
            if not fut.done():
                fut.set_result(None)  # direct hand-off: slot count unchanged
                return
        self._free += 1
