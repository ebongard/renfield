"""Unit tests for ``api.lifecycle._spawn_periodic_task`` (#678).

The daily schedulers (Skill Curator, Trajectory Cleanup, Skill Shadow-Log
Cleanup, Speaker Vocab rebuild) run on an 86400s interval. The plain
sleep-then-work loop fires its first tick a full interval after boot and
the timer resets on every pod restart, so a pod that recycles more often
than the interval never runs the work — observed as an empty
``skill_curator_runs`` table in production. The ``run_at_boot`` opt-in
runs one tick promptly after spawn to close that gap.
"""
from __future__ import annotations

import asyncio

import pytest

from api import lifecycle

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


@pytest.fixture(autouse=True)
async def _clean_startup_tasks():
    """Cancel + drain any task a test spawned, and restore the registry."""
    before = list(lifecycle._startup_tasks)
    yield
    added = [t for t in lifecycle._startup_tasks if t not in before]
    for t in added:
        t.cancel()
    for t in added:
        try:
            await t
        except BaseException:  # noqa: BLE001 — best-effort teardown
            pass
    lifecycle._startup_tasks[:] = before


def _spawn(**kwargs) -> asyncio.Task:
    """Spawn via the helper and return the Task it registered."""
    n = len(lifecycle._startup_tasks)
    lifecycle._spawn_periodic_task(**kwargs)
    assert len(lifecycle._startup_tasks) == n + 1
    return lifecycle._startup_tasks[-1]


async def test_run_at_boot_runs_work_promptly():
    """With run_at_boot=True the first tick must fire without waiting the
    (here 1h) interval."""
    ran = asyncio.Event()

    async def work():
        ran.set()

    _spawn(name="t", interval=3600, work=work,
           started_msg="x", run_at_boot=True)

    await asyncio.wait_for(ran.wait(), timeout=1.0)


async def test_default_does_not_run_at_boot():
    """Default (run_at_boot=False) preserves the legacy cadence: nothing
    runs until the first interval elapses."""
    calls = 0

    async def work():
        nonlocal calls
        calls += 1

    _spawn(name="t", interval=3600, work=work, started_msg="x")

    await asyncio.sleep(0.05)
    assert calls == 0


async def test_boot_run_exception_swallowed_and_loop_survives():
    """A boot-run failure must be logged and swallowed, with the task
    falling through into the interval loop (not crashing)."""
    boot_ran = asyncio.Event()
    calls = 0

    async def work():
        nonlocal calls
        calls += 1
        if calls == 1:
            boot_ran.set()
            raise RuntimeError("boom on boot")

    task = _spawn(name="t", interval=3600, work=work,
                  started_msg="x", run_at_boot=True)

    await asyncio.wait_for(boot_ran.wait(), timeout=1.0)
    await asyncio.sleep(0)     # let the swallowed error settle into sleep(3600)
    assert calls == 1          # boot tick ran
    assert not task.done()     # exception swallowed → now in the interval loop


async def test_cancel_during_boot_run_terminates_cleanly():
    """Cancelling while the boot tick is in-flight ends the task without
    leaking an unexpected exception (shutdown path)."""
    entered = asyncio.Event()

    async def work():
        entered.set()
        await asyncio.sleep(10)  # block inside the boot run

    task = _spawn(name="t", interval=3600, work=work,
                  started_msg="x", run_at_boot=True)

    await asyncio.wait_for(entered.wait(), timeout=1.0)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    assert task.done()


# ---------------------------------------------------------------------------
# Paperless metadata backfill reconciler — config gating (self-populating meta)
# ---------------------------------------------------------------------------

class _App:
    class state:  # noqa: N801 - mimic Starlette app.state
        mcp_manager = object()


def test_metadata_reconciler_scheduled_when_enabled(monkeypatch):
    calls = []
    monkeypatch.setattr(lifecycle, "_spawn_periodic_task", lambda **kw: calls.append(kw))
    monkeypatch.setattr(lifecycle.settings, "folder_ingest_to_paperless", True)
    monkeypatch.setattr(lifecycle.settings, "paperless_metadata_backfill_enabled", True)
    lifecycle._schedule_paperless_metadata_reconciler(_App())
    assert calls and calls[0]["name"] == "Paperless metadata backfill"
    assert calls[0]["run_at_boot"] is True


def test_metadata_reconciler_skipped_when_backfill_off(monkeypatch):
    calls = []
    monkeypatch.setattr(lifecycle, "_spawn_periodic_task", lambda **kw: calls.append(kw))
    monkeypatch.setattr(lifecycle.settings, "folder_ingest_to_paperless", True)
    monkeypatch.setattr(lifecycle.settings, "paperless_metadata_backfill_enabled", False)
    lifecycle._schedule_paperless_metadata_reconciler(_App())
    assert calls == []


def test_metadata_reconciler_skipped_when_paperless_off(monkeypatch):
    calls = []
    monkeypatch.setattr(lifecycle, "_spawn_periodic_task", lambda **kw: calls.append(kw))
    monkeypatch.setattr(lifecycle.settings, "folder_ingest_to_paperless", False)
    monkeypatch.setattr(lifecycle.settings, "email_ingest_to_paperless", False)
    monkeypatch.setattr(lifecycle.settings, "paperless_metadata_backfill_enabled", True)
    lifecycle._schedule_paperless_metadata_reconciler(_App())
    assert calls == []
