"""
Regression tests for the boot-time reconnect scheduling.

Bug (2026-06-14): a satellite whose FIRST connect failed at boot was stranded
in idle forever and never retried. Root cause: cold mDNS resolution of the
server host (renfield.local) raises "[Errno -2] Name or service not known" for
the first ~2-3s after boot until avahi's multicast cache warms; the startup
path scheduled the recovery loop with a BARE asyncio.create_task() whose result
was discarded. The event loop only holds a weak reference to such a task, so it
was garbage-collected before it ever ran — "Failed to connect - will retry" was
printed but the reconnect loop never executed (confirmed in the journal: no
"Reconnecting in Xs" line ever appeared across three boots).

The fix routes startup reconnection through `_start_reconnect_loop()`, which
keeps a strong reference on `self._reconnect_task` and honors the
`_reconnecting` guard (mirroring the disconnect path).
"""

import asyncio

import pytest

from renfield_satellite.satellite import Satellite


def _bare_satellite():
    """A Satellite with only the attributes the reconnect-scheduling seam needs,
    bypassing __init__ (which constructs real hardware controllers)."""
    sat = Satellite.__new__(Satellite)
    sat._reconnecting = False
    sat._reconnect_task = None
    sat._running = True
    return sat


@pytest.mark.asyncio
async def test_startup_reconnect_task_is_stored_and_runs():
    """The scheduled reconnect loop must be stored on self (so asyncio's weak
    task reference can't GC it before it runs) AND must actually execute.

    Regression guard: reverting to a bare, discarded asyncio.create_task() leaves
    _reconnect_task None — the isinstance assert fails. (A deterministic "GC
    reaps an unreferenced task" reproduction isn't possible in a unit test, so we
    assert the contract the fix establishes: the task is retained and the loop
    body runs to its first statement.)"""
    sat = _bare_satellite()

    ran = asyncio.Event()

    async def fake_loop():
        ran.set()

    sat._reconnect_with_discovery_wrapper = fake_loop

    sat._start_reconnect_loop()

    assert sat._reconnecting is True
    assert isinstance(sat._reconnect_task, asyncio.Task)

    # The scheduled coroutine actually runs (would never fire if it were dropped).
    await asyncio.wait_for(ran.wait(), timeout=1.0)
    await sat._reconnect_task


@pytest.mark.asyncio
async def test_start_reconnect_loop_guard_prevents_duplicate():
    """A second schedule while already reconnecting is a no-op (no duplicate loop
    racing a concurrent _on_disconnected)."""
    sat = _bare_satellite()

    calls = {"n": 0}

    async def fake_loop():
        calls["n"] += 1
        await asyncio.sleep(0.01)

    sat._reconnect_with_discovery_wrapper = fake_loop

    sat._start_reconnect_loop()
    first = sat._reconnect_task
    sat._start_reconnect_loop()  # guarded — must not spawn a second task

    assert sat._reconnect_task is first
    await first
    assert calls["n"] == 1


@pytest.mark.asyncio
async def test_reconnect_loop_survives_transient_exception():
    """A transient exception during an attempt (discovery/token/connect) must NOT
    escape the loop. If it did, the wrapper's finally would reset _reconnecting
    and the task would die with no live receive loop to re-trigger it — re-
    stranding the boot path and bypassing the disconnect watchdog. The loop must
    swallow it and retry the next attempt."""
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    sat = _bare_satellite()
    sat.config = SimpleNamespace(
        server=SimpleNamespace(
            max_disconnected_seconds=0,  # watchdog off for the test
            reconnect_interval=0,  # no real sleep
            auto_discover=False,
            url="wss://renfield.local/ws/satellite",
            auth_enabled=False,
        )
    )

    # First attempt raises (e.g. cold-mDNS getaddrinfo), second connects.
    sat.ws_client = SimpleNamespace(
        connect=AsyncMock(side_effect=[OSError("[Errno -2] Name or service not known"), True])
    )

    result = await asyncio.wait_for(sat._reconnect_with_discovery(), timeout=2.0)

    assert result is True
    assert sat.ws_client.connect.await_count == 2  # retried after the exception
