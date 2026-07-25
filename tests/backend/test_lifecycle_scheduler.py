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



# --- Platform-core Paperless audit mount (decoupled from the ha_glue plugin) ---
# The audit REST router is mounted here, not inside the HA-only ha_glue plugin, so
# it reaches HA-less deploys (business/xidra) instead of 404'ing.

from unittest.mock import MagicMock  # noqa: E402


async def test_init_paperless_audit_mounts_when_enabled(monkeypatch):
    """Flag on + Paperless MCP present → router mounted + service started."""
    from ha_glue.utils.config import ha_glue_settings

    monkeypatch.setattr(ha_glue_settings, "paperless_audit_enabled", True)

    started = {}

    class _FakeService:
        def __init__(self, **_kw):
            started["init"] = True

        async def start(self):
            started["start"] = True

    monkeypatch.setattr(
        "ha_glue.services.paperless_audit_service.PaperlessAuditService", _FakeService
    )

    app = MagicMock()
    app.state.mcp_manager = MagicMock()
    app.state.mcp_manager.has_server = MagicMock(return_value=True)

    await lifecycle._init_paperless_audit(app)

    app.include_router.assert_called_once()
    assert isinstance(app.state.paperless_audit, _FakeService)
    assert started == {"init": True, "start": True}


async def test_init_paperless_audit_noop_when_disabled(monkeypatch):
    from ha_glue.utils.config import ha_glue_settings

    monkeypatch.setattr(ha_glue_settings, "paperless_audit_enabled", False)
    app = MagicMock()

    await lifecycle._init_paperless_audit(app)

    app.include_router.assert_not_called()


async def test_init_paperless_audit_noop_without_paperless_mcp(monkeypatch):
    from ha_glue.utils.config import ha_glue_settings

    monkeypatch.setattr(ha_glue_settings, "paperless_audit_enabled", True)
    app = MagicMock()
    app.state.mcp_manager = MagicMock()
    app.state.mcp_manager.has_server = MagicMock(return_value=False)

    await lifecycle._init_paperless_audit(app)

    app.include_router.assert_not_called()
