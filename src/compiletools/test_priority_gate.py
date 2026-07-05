"""Unit tests for PriorityGate (no compiler required)."""

from __future__ import annotations

import asyncio

import pytest

from compiletools.priority_gate import PriorityGate


def test_free_slots_acquire_immediately():
    async def main():
        g = PriorityGate(2)
        await g.acquire(0.0)
        await g.acquire(0.0)  # both slots free -> neither blocks

    asyncio.run(asyncio.wait_for(main(), 1.0))


def test_high_priority_waiter_wakes_first():
    async def main():
        g = PriorityGate(1)
        order = []
        await g.acquire(0.0)  # occupy the only slot

        async def worker(pri, tag):
            await g.acquire(pri)
            order.append(tag)
            g.release()

        low = asyncio.ensure_future(worker(1.0, "low"))
        high = asyncio.ensure_future(worker(9.0, "high"))
        await asyncio.sleep(0.05)  # let both park on the heap
        g.release()  # wake the highest-priority waiter first
        await asyncio.gather(low, high)
        return order

    assert asyncio.run(asyncio.wait_for(main(), 2.0)) == ["high", "low"]


def test_equal_priority_is_fifo():
    async def main():
        g = PriorityGate(1)
        order = []
        await g.acquire(5.0)

        async def worker(tag):
            await g.acquire(5.0)
            order.append(tag)
            g.release()

        a = asyncio.ensure_future(worker("a"))
        await asyncio.sleep(0.01)
        b = asyncio.ensure_future(worker("b"))
        await asyncio.sleep(0.01)
        g.release()
        await asyncio.gather(a, b)
        return order

    assert asyncio.run(asyncio.wait_for(main(), 2.0)) == ["a", "b"]


def test_release_on_exception_frees_slot():
    async def main():
        g = PriorityGate(1)
        with pytest.raises(ValueError):
            await g.acquire(0.0)
            try:
                raise ValueError("boom")
            finally:
                g.release()
        # The slot released in the finally must be reusable.
        await asyncio.wait_for(g.acquire(0.0), 0.5)

    asyncio.run(asyncio.wait_for(main(), 2.0))


def test_equiv_to_semaphore_when_all_priorities_equal():
    """With uniform priority, N slots admit exactly N concurrent holders."""

    async def main():
        g = PriorityGate(3)
        live = 0
        peak = 0
        release_events = [asyncio.Event() for _ in range(6)]

        async def worker(i):
            nonlocal live, peak
            await g.acquire(0.0)
            live += 1
            peak = max(peak, live)
            await release_events[i].wait()
            live -= 1
            g.release()

        tasks = [asyncio.ensure_future(worker(i)) for i in range(6)]
        await asyncio.sleep(0.05)
        assert live == 3  # only N run at once
        for ev in release_events:
            ev.set()
        await asyncio.gather(*tasks)
        return peak

    assert asyncio.run(asyncio.wait_for(main(), 2.0)) == 3


def test_no_starvation_among_equal_priorities():
    """A stream of equal-priority waiters all eventually run (FIFO order)."""

    async def main():
        g = PriorityGate(1)
        order = []
        await g.acquire(0.0)

        async def worker(tag):
            await g.acquire(0.0)
            order.append(tag)
            g.release()

        tasks = []
        for i in range(10):
            tasks.append(asyncio.ensure_future(worker(i)))
            await asyncio.sleep(0)  # establish FIFO arrival order
        g.release()
        await asyncio.gather(*tasks)
        return order

    assert asyncio.run(asyncio.wait_for(main(), 2.0)) == list(range(10))
