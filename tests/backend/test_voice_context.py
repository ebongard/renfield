"""Tests for the ``voice_originated`` ContextVar.

The flag is set at WS message receipt by ``api/websocket/chat_handler.py``
based on whether the inbound ``WSChatMessage.speaker_embedding`` is
truthy. Downstream plugin hooks (notably Reva's voice-2FA destructive-tool
gate) read it without threading the signal through every method signature.

Critical properties tested here:

1. **Default is False** — text-channel callers and background workers
   that haven't crossed a voice WS message see ``False``.
2. **Set propagates to ``asyncio.create_task`` children** — Python 3.7+
   context propagation; we don't need explicit ``copy_context``.
3. **Set propagates to ``asyncio.gather`` children** — same mechanism.
4. **Set is request-scoped, not process-scoped** — concurrent requests
   with different voice-origin values don't bleed into each other.
"""
from __future__ import annotations

import asyncio
from contextvars import copy_context

import pytest

from utils.voice_context import voice_originated


@pytest.mark.unit
def test_default_is_false():
    """Fresh context: voice_originated.get() returns False."""
    # Use a fresh context to avoid leakage from earlier tests in the same
    # process. ``copy_context().run`` gives us isolation.
    ctx = copy_context()

    def _read() -> bool:
        return voice_originated.get()

    assert ctx.run(_read) is False


@pytest.mark.unit
@pytest.mark.asyncio
async def test_set_visible_in_same_task():
    """set(True) in a task is visible to subsequent reads in the same task."""

    async def _reader_after_set() -> bool:
        voice_originated.set(True)
        return voice_originated.get()

    result = await _reader_after_set()
    assert result is True


@pytest.mark.unit
@pytest.mark.asyncio
async def test_set_propagates_to_create_task_child():
    """asyncio.create_task inherits the parent's ContextVar value.

    This is Python 3.7+ default behavior; the test pins it as a contract
    so a future regression (e.g. someone wrapping create_task in a
    non-propagating helper) is caught immediately.
    """

    async def _child() -> bool:
        return voice_originated.get()

    voice_originated.set(True)
    task = asyncio.create_task(_child())
    result = await task
    assert result is True


@pytest.mark.unit
@pytest.mark.asyncio
async def test_set_propagates_to_gather_children():
    """asyncio.gather with bare coroutines: each child sees parent value.

    Renfield's orchestrator uses ``asyncio.gather`` for parallel sub-agent
    fan-out. Each sub-agent must see voice_originated=True if the parent
    was a voice message.
    """

    async def _child() -> bool:
        return voice_originated.get()

    voice_originated.set(True)
    results = await asyncio.gather(_child(), _child(), _child())
    assert results == [True, True, True]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_isolation_between_independent_tasks():
    """Two independently-spawned tasks have independent ContextVar values.

    This pins the per-task isolation property: setting voice_originated=True
    in one task does NOT bleed into a sibling task that started from a
    different parent context.
    """

    async def _set_true_and_yield() -> bool:
        voice_originated.set(True)
        await asyncio.sleep(0)  # yield so the other task can interleave
        return voice_originated.get()

    async def _read_default() -> bool:
        await asyncio.sleep(0)  # yield to let the other task run first
        return voice_originated.get()

    # Two top-level tasks via gather. Each starts from the parent context
    # (default False). One sets True, one reads. The reader must see False.
    result_true_setter, result_reader = await asyncio.gather(
        copy_context().run(asyncio.ensure_future, _set_true_and_yield()),
        copy_context().run(asyncio.ensure_future, _read_default()),
    )
    assert result_true_setter is True
    assert result_reader is False


@pytest.mark.unit
@pytest.mark.asyncio
async def test_set_does_not_leak_back_to_parent():
    """A child task setting True does NOT mutate the parent's value.

    contextvars semantics: child sees a snapshot of parent context;
    child's own .set() does not propagate back. This pins the property.
    """

    async def _child_sets_true() -> None:
        voice_originated.set(True)

    voice_originated.set(False)  # parent baseline
    await asyncio.create_task(_child_sets_true())
    assert voice_originated.get() is False
