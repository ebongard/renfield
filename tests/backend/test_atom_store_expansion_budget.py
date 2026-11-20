"""Graph expansion's budget vs. a cancellation from outside (#1563).

`polymorphic_atom_store._within_budget` treats ONLY its own deadline as "the
budget ran out" -> an unexpanded result. A cancellation from outside
(shutdown, a cancelled federated query) must stop the task, also when it lands
while the expansion is still unwinding from that deadline.
"""

import asyncio

import pytest

from services.polymorphic_atom_store import _within_budget

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


async def test_work_that_finishes_in_time_returns_its_result():
    async def work():
        return ["hit"]

    assert await _within_budget(work(), 1.0) == (["hit"], False)


async def test_no_budget_is_no_deadline():
    async def work():
        await asyncio.sleep(0.05)
        return ["hit"]

    assert await _within_budget(work(), None) == (["hit"], False)


async def test_a_spent_budget_does_not_start_the_work():
    started = []

    async def work():
        started.append(1)
        return ["hit"]

    assert await _within_budget(work(), 0.0) == ([], True)
    assert started == []


async def test_our_own_deadline_is_an_unexpanded_result():
    async def work():
        await asyncio.sleep(10)

    assert await _within_budget(work(), 0.05) == ([], True)


async def test_a_timeout_raised_inside_the_work_propagates():
    async def work():
        raise TimeoutError("pool")

    with pytest.raises(TimeoutError):
        await _within_budget(work(), 5.0)


async def test_an_outside_cancel_before_the_deadline_propagates():
    async def work():
        await asyncio.sleep(10)

    task = asyncio.ensure_future(_within_budget(work(), 5.0))
    await asyncio.sleep(0.02)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_an_outside_cancel_during_the_unwind_after_our_deadline_propagates():
    """#1563: the budget expires, the expansion unwinds its statement, and an
    outside cancel lands in that window. The call must raise, not report an
    unexpanded result and run on."""
    unwinding = asyncio.Event()

    async def work():
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            # Like a driver cancelling its statement on the server.
            unwinding.set()
            await asyncio.sleep(0.5)
            raise

    task = asyncio.ensure_future(_within_budget(work(), 0.05))
    await asyncio.wait_for(unwinding.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 2)


async def test_an_outside_cancel_after_the_unwind_still_stops_the_caller():
    """Negative control for the window above: an outside cancel that arrives
    once the call has returned is the caller's next await, not ours."""
    async def work():
        await asyncio.sleep(10)

    async def caller():
        result = await _within_budget(work(), 0.05)
        await asyncio.sleep(10)
        return result

    task = asyncio.ensure_future(caller())
    await asyncio.sleep(0.2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
