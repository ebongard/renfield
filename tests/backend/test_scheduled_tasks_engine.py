"""Scheduled Tasks engine (#1137, docs/design/scheduled-tasks.md).

Covers the engine's correctness core: due-selection, interval + cron next-run,
start/end bounds, boot-force run_at_boot (#678), handler error → last_status,
unknown-handler_key skip+backoff (Review D3/D4), interval floor (M6),
ensure_builtin_tasks ON-CONFLICT idempotency + no-clobber (M8), spawn
independence (C1), the registry, and the paperless-dedupe handler self-gate.

Runs on the sqlite test harness — the Postgres advisory lock is skipped there
(the in-process _inflight guard covers same-process single-flight, which is all
one test engine needs). AsyncSessionLocal is monkeypatched onto the per-test
engine, mirroring test_paperless_finalize_reconciler.
"""
import asyncio
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from models.database import (
    SCHEDULE_KIND_CRON,
    SCHEDULE_KIND_INTERVAL,
    SCHEDULED_TASK_STATUS_ERROR,
    SCHEDULED_TASK_STATUS_OK,
    SCHEDULED_TASK_STATUS_SKIPPED,
    ScheduledTask,
)


def _naive_now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


@pytest.fixture
def session_factory(monkeypatch, db_session):
    """Bind AsyncSessionLocal AND the global engine to the per-test sqlite engine
    so the engine's own sessions hit the same fresh in-memory DB (function-scoped
    → no cross-test bleed) and _run_one sees the sqlite dialect (→ skips the
    Postgres advisory-lock path, which the in-process _inflight guard covers)."""
    import services.database as db_mod

    smk = async_sessionmaker(db_session.bind, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(db_mod, "AsyncSessionLocal", smk)
    monkeypatch.setattr(db_mod, "engine", db_session.bind)
    return smk


@pytest.fixture(autouse=True)
def _reset_engine_state(monkeypatch):
    """Clear the module-level in-flight set + Semaphore + registry between tests,
    and serialize execution to one concurrent task. The sqlite test engine uses a
    single shared StaticPool connection, so overlapping engine sessions would
    contend on it; concurrency=1 keeps one _execute_task session open at a time.
    Spawn-not-await (C1) is independent of the Semaphore size — run_engine_tick
    still returns without awaiting the spawned runs (see test_tick_spawns_...)."""
    from services.scheduled_tasks import engine, registry
    from utils.config import settings

    monkeypatch.setattr(settings, "scheduled_tasks_max_concurrent", 1)
    engine.reset_state()
    registry.clear_handlers()
    yield
    engine.reset_state()
    registry.clear_handlers()


async def _mk(smk, **kw) -> int:
    defaults = dict(
        name="task", handler_key="h", schedule_kind=SCHEDULE_KIND_INTERVAL,
        interval_seconds=300, cron_expr=None, params={}, enabled=True,
        run_at_boot=False, start_at=None, end_at=None, next_run_at=None,
        is_builtin=False,
    )
    defaults.update(kw)
    async with smk() as db:
        row = ScheduledTask(**defaults)
        db.add(row)
        await db.commit()
        await db.refresh(row)
        return row.id


async def _get(smk, task_id: int) -> ScheduledTask:
    async with smk() as db:
        return await db.get(ScheduledTask, task_id)


async def _drain(timeout: float = 2.0):
    """Wait until every spawned run has finished (the _inflight set empties)."""
    from services.scheduled_tasks import engine

    deadline = asyncio.get_event_loop().time() + timeout
    while engine._inflight and asyncio.get_event_loop().time() < deadline:
        await asyncio.sleep(0.01)


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestPureHelpers:
    def test_compute_next_run_interval(self):
        from services.scheduled_tasks import engine

        t = ScheduledTask(schedule_kind=SCHEDULE_KIND_INTERVAL, interval_seconds=60)
        after = datetime(2026, 1, 1, 12, 0, 0)
        assert engine.compute_next_run(t, after=after) == after + timedelta(seconds=60)

    def test_compute_next_run_zero_interval_unschedulable(self):
        from services.scheduled_tasks import engine

        t = ScheduledTask(schedule_kind=SCHEDULE_KIND_INTERVAL, interval_seconds=0)
        assert engine.compute_next_run(t, after=_naive_now()) is None

    def test_compute_next_run_cron(self):
        pytest.importorskip("croniter")
        from services.scheduled_tasks import engine

        # every day at 08:00 — next run is strictly after `after`.
        t = ScheduledTask(schedule_kind=SCHEDULE_KIND_CRON, cron_expr="0 8 * * *")
        after = datetime(2026, 1, 1, 9, 0, 0)
        nxt = engine.compute_next_run(t, after=after)
        assert nxt is not None and nxt > after

    def test_compute_next_run_bad_cron_is_none(self):
        from services.scheduled_tasks import engine

        t = ScheduledTask(schedule_kind=SCHEDULE_KIND_CRON, cron_expr="not a cron")
        assert engine.compute_next_run(t, after=_naive_now()) is None

    def test_interval_floor_rejects_sub_tick(self):
        from services.scheduled_tasks import engine
        from utils.config import settings

        with pytest.raises(ValueError):
            engine._validate_interval_floor(settings.scheduled_tasks_engine_tick_seconds - 1)
        # at/above the tick is fine
        engine._validate_interval_floor(settings.scheduled_tasks_engine_tick_seconds)
        engine._validate_interval_floor(None)


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestRegistry:
    def test_register_get_and_keys(self):
        from services.scheduled_tasks import registry

        async def _h(app, params):
            return None

        registry.register_handler("k1", _h)
        assert registry.get_handler("k1").handler is _h
        assert "k1" in registry.all_handler_keys()
        assert registry.get_handler("nope") is None

    def test_validate_params_unknown_raises(self):
        from services.scheduled_tasks import registry

        with pytest.raises(ValueError):
            registry.validate_params("missing", {})

    def test_validate_params_uses_handler_validator(self):
        from services.scheduled_tasks import registry

        def _v(params):
            if "n" not in params:
                raise ValueError("n required")

        async def _h(app, params):
            return None

        registry.register_handler("k2", _h, validate_params=_v)
        registry.validate_params("k2", {"n": 1})  # ok
        with pytest.raises(ValueError):
            registry.validate_params("k2", {})


# ---------------------------------------------------------------------------
# Seeding + boot pass
# ---------------------------------------------------------------------------

@pytest.mark.database
class TestSeeding:
    async def test_ensure_builtin_tasks_idempotent_and_no_clobber(self, session_factory, monkeypatch):
        from services.scheduled_tasks import builtins, engine

        # Deterministic seed set (avoid depending on the real built-ins list).
        seed = builtins.TaskSeed(name="Seed A", handler_key="a", interval_seconds=300)
        monkeypatch.setattr(builtins, "builtin_task_seeds", lambda: [seed])

        n1 = await engine.ensure_builtin_tasks()
        assert n1 == 1

        # Admin edits the row (disable + change interval).
        async with session_factory() as db:
            row = (await db.execute(select(ScheduledTask).where(ScheduledTask.name == "Seed A"))).scalar_one()
            row.enabled = False
            row.interval_seconds = 999
            await db.commit()

        # Second seed run must be a no-op AND must not clobber the edit.
        n2 = await engine.ensure_builtin_tasks()
        assert n2 == 0
        async with session_factory() as db:
            rows = (await db.execute(select(ScheduledTask).where(ScheduledTask.name == "Seed A"))).scalars().all()
        assert len(rows) == 1
        assert rows[0].enabled is False
        assert rows[0].interval_seconds == 999

    async def test_boot_force_run_at_boot(self, session_factory):
        from services.scheduled_tasks import engine

        far = _naive_now() + timedelta(hours=20)
        boot_id = await _mk(session_factory, name="boot", handler_key="a", run_at_boot=True, next_run_at=far)
        normal_id = await _mk(session_factory, name="normal", handler_key="a", run_at_boot=False, next_run_at=far)
        disabled_id = await _mk(session_factory, name="off", handler_key="a", run_at_boot=True, enabled=False, next_run_at=far)

        forced = await engine.force_run_at_boot_tasks()
        assert forced == 1

        assert (await _get(session_factory, boot_id)).next_run_at <= _naive_now()
        assert (await _get(session_factory, normal_id)).next_run_at == far      # untouched
        assert (await _get(session_factory, disabled_id)).next_run_at == far    # disabled → untouched


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------

@pytest.mark.database
class TestExecution:
    async def test_success_records_ok_and_reschedules(self, session_factory):
        from services.scheduled_tasks import engine, registry

        calls = []

        async def _h(app, params):
            calls.append(params)
            return "done"

        registry.register_handler("ok", _h)
        tid = await _mk(session_factory, name="t", handler_key="ok", interval_seconds=300, params={"x": 1})

        await engine._execute_task(SimpleNamespace(state=SimpleNamespace()), tid)

        assert calls == [{"x": 1}]
        row = await _get(session_factory, tid)
        assert row.last_status == SCHEDULED_TASK_STATUS_OK
        assert row.last_error is None
        assert row.last_run_at is not None
        assert row.last_duration_ms is not None
        assert row.next_run_at > _naive_now()

    async def test_handler_error_records_error_but_reschedules(self, session_factory):
        from services.scheduled_tasks import engine, registry

        async def _boom(app, params):
            raise RuntimeError("kaboom")

        registry.register_handler("boom", _boom)
        tid = await _mk(session_factory, name="t", handler_key="boom", interval_seconds=300)

        await engine._execute_task(SimpleNamespace(state=SimpleNamespace()), tid)

        row = await _get(session_factory, tid)
        assert row.last_status == SCHEDULED_TASK_STATUS_ERROR
        assert "kaboom" in (row.last_error or "")
        assert row.next_run_at > _naive_now()  # still scheduled — a failure isn't terminal

    async def test_unknown_handler_key_skips_and_backs_off(self, session_factory):
        from services.scheduled_tasks import engine

        # No handler registered for "ghost".
        tid = await _mk(session_factory, name="t", handler_key="ghost", interval_seconds=300)

        await engine._execute_task(SimpleNamespace(state=SimpleNamespace()), tid)

        row = await _get(session_factory, tid)
        assert row.last_status == SCHEDULED_TASK_STATUS_SKIPPED
        assert "unknown handler_key" in (row.last_error or "")
        # backed off, not left due → won't error-spin every tick
        assert row.next_run_at > _naive_now()

    async def test_disabled_task_is_not_run(self, session_factory):
        from services.scheduled_tasks import engine, registry

        calls = []

        async def _h(app, params):
            calls.append(1)

        registry.register_handler("d", _h)
        tid = await _mk(session_factory, name="t", handler_key="d", enabled=False)

        await engine._execute_task(SimpleNamespace(state=SimpleNamespace()), tid)
        assert calls == []


# ---------------------------------------------------------------------------
# Tick / selection / independence
# ---------------------------------------------------------------------------

@pytest.mark.database
class TestTick:
    async def test_due_selection_runs_due_only(self, session_factory):
        from services.scheduled_tasks import engine, registry

        ran = set()

        async def _h(app, params):
            ran.add(params["id"])

        registry.register_handler("h", _h)
        now = _naive_now()
        due = await _mk(session_factory, name="due", handler_key="h", next_run_at=now - timedelta(seconds=1), params={"id": "due"})
        future = await _mk(session_factory, name="future", handler_key="h", next_run_at=now + timedelta(hours=1), params={"id": "future"})
        null_next = await _mk(session_factory, name="null", handler_key="h", next_run_at=None, params={"id": "null"})

        await engine.run_engine_tick(SimpleNamespace(state=SimpleNamespace()))
        await _drain()

        assert "due" in ran
        assert "null" in ran        # NULL next_run_at is treated as due
        assert "future" not in ran

    async def test_start_end_bounds_exclude_out_of_window(self, session_factory):
        from services.scheduled_tasks import engine, registry

        ran = set()

        async def _h(app, params):
            ran.add(params["id"])

        registry.register_handler("h", _h)
        now = _naive_now()
        # not started yet
        await _mk(session_factory, name="notyet", handler_key="h", next_run_at=now - timedelta(seconds=1),
                  start_at=now + timedelta(hours=1), params={"id": "notyet"})
        # already ended
        await _mk(session_factory, name="ended", handler_key="h", next_run_at=now - timedelta(seconds=1),
                  end_at=now - timedelta(hours=1), params={"id": "ended"})
        # inside window
        await _mk(session_factory, name="active", handler_key="h", next_run_at=now - timedelta(seconds=1),
                  start_at=now - timedelta(hours=1), end_at=now + timedelta(hours=1), params={"id": "active"})

        await engine.run_engine_tick(SimpleNamespace(state=SimpleNamespace()))
        await _drain()

        assert ran == {"active"}

    async def test_tick_spawns_without_awaiting(self, session_factory):
        """run_engine_tick must SPAWN each due run and return, NOT inline-await the
        handler (C1). A 0.3s handler must not delay the tick's return."""
        from services.scheduled_tasks import engine, registry

        started = asyncio.Event()

        async def _slow(app, params):
            started.set()
            await asyncio.sleep(0.3)

        registry.register_handler("slow", _slow)
        now = _naive_now()
        await _mk(session_factory, name="slow", handler_key="slow", next_run_at=now - timedelta(seconds=1))

        t0 = asyncio.get_event_loop().time()
        await engine.run_engine_tick(SimpleNamespace(state=SimpleNamespace()))
        elapsed = asyncio.get_event_loop().time() - t0

        assert elapsed < 0.2          # returned well before the 0.3s handler finished
        assert engine._inflight       # the run is still in flight (spawned, not awaited)
        await _drain(timeout=2.0)
        assert not engine._inflight   # and it does finish


# ---------------------------------------------------------------------------
# Built-in paperless-dedupe handler self-gate (M7)
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestPaperlessDedupeHandler:
    async def test_self_gate_off_makes_no_mcp_call(self, monkeypatch):
        from services.scheduled_tasks import builtins
        from utils.config import settings

        monkeypatch.setattr(settings, "paperless_dedupe_reconciler_enabled", False)

        called = {"n": 0}

        class _MCP:
            async def execute_tool(self, *a, **k):
                called["n"] += 1
                return {}

        app = SimpleNamespace(state=SimpleNamespace(mcp_manager=_MCP()))
        out = await builtins._paperless_dedupe_handler(app, {})
        assert "skipped" in out
        assert called["n"] == 0

    async def test_enabled_calls_mcp_and_reports(self, monkeypatch):
        from services.scheduled_tasks import builtins
        from utils.config import settings

        monkeypatch.setattr(settings, "paperless_dedupe_reconciler_enabled", True)

        class _MCP:
            async def execute_tool(self, tool, params, **kw):
                assert tool == "mcp.paperless.dedupe_documents"
                assert params["dry_run"] is False
                assert kw.get("call_timeout") and kw["call_timeout"] > 30  # raised per-call timeout
                # MCPManager envelope: the tool's dict is JSON in "message". Include
                # the marker keys a real dedupe_documents response always carries, so
                # the contract-marker guard treats it as a legitimate result.
                return {
                    "success": True,
                    "message": json.dumps({
                        "scanned": 100, "duplicate_copies": 8,
                        "deleted": 5, "remaining": 3, "complete": False,
                    }),
                }

        app = SimpleNamespace(state=SimpleNamespace(mcp_manager=_MCP()))
        out = await builtins._paperless_dedupe_handler(app, {})
        assert "deleted=5" in out and "remaining=3" in out

    async def test_fuzzy_fallback_response_raises(self, monkeypatch):
        """An old MCP fuzzy-falls-back dedupe_documents to another tool → no marker
        keys. The handler must RAISE (→ last_status='error'), not report a green
        no-op that hides the never-draining backlog."""
        from services.scheduled_tasks import builtins
        from utils.config import settings

        monkeypatch.setattr(settings, "paperless_dedupe_reconciler_enabled", True)

        class _MCP:
            async def execute_tool(self, tool, params, **kw):
                # search_documents-shaped response: no scanned/complete/duplicate_copies
                return {"success": True, "message": json.dumps({"results": [], "total_matching": 0})}

        app = SimpleNamespace(state=SimpleNamespace(mcp_manager=_MCP()))
        with pytest.raises(RuntimeError):
            await builtins._paperless_dedupe_handler(app, {})


@pytest.mark.database
class TestUploadCleanupHandler:
    async def test_gate_off_skips(self, monkeypatch):
        from services.scheduled_tasks import builtins
        from utils.config import settings

        monkeypatch.setattr(settings, "chat_upload_cleanup_enabled", False)
        out = await builtins._upload_cleanup_handler(SimpleNamespace(state=SimpleNamespace()), {})
        assert "skipped" in out

    async def test_enabled_runs_without_error(self, session_factory, monkeypatch):
        """Regression: the handler used AsyncSessionLocal without importing it →
        NameError on every run. Assert the enabled path runs cleanly."""
        import api.routes.chat_upload as cu
        from services.scheduled_tasks import builtins
        from utils.config import settings

        monkeypatch.setattr(settings, "chat_upload_cleanup_enabled", True)
        monkeypatch.setattr(cu, "_cleanup_uploads", AsyncMock(return_value=(2, 3)))

        out = await builtins._upload_cleanup_handler(SimpleNamespace(state=SimpleNamespace()), {})
        assert out is not None and "deleted=2" in out


@pytest.mark.database
class TestShutdownDrain:
    async def test_drain_cancels_in_flight_runs(self, session_factory):
        """drain_running_tasks must cancel + clear a mid-flight spawned run so a
        pod recycle doesn't abandon it (no 'Task was destroyed but pending')."""
        from services.scheduled_tasks import engine, registry

        entered = asyncio.Event()

        async def _slow(app, params):
            entered.set()
            await asyncio.sleep(30)  # would outlive the drain

        registry.register_handler("slow", _slow)
        now = _naive_now()
        await _mk(session_factory, name="slow", handler_key="slow", next_run_at=now - timedelta(seconds=1))

        await engine.run_engine_tick(SimpleNamespace(state=SimpleNamespace()))
        await asyncio.wait_for(entered.wait(), timeout=2.0)  # the run has started
        assert engine._running_tasks  # tracked for shutdown

        await engine.drain_running_tasks(timeout=2.0)
        await asyncio.sleep(0)  # let done-callbacks fire

        assert not engine._running_tasks   # handles cleared
        assert not engine._inflight        # in-flight guard cleared


@pytest.mark.unit
class TestBatchAHandlers:
    """Phase 3 Batch A migrated handlers (daypart / paperless-finalize / mcp-health)."""

    def _app(self, **state):
        return SimpleNamespace(state=SimpleNamespace(**state))

    async def test_daypart_fires_hook_on_transition(self, monkeypatch):
        import services.daypart_service as dp
        import utils.hooks as hooks
        from services.scheduled_tasks import builtins

        builtins._daypart_watcher_state["last"] = "day"
        monkeypatch.setattr(dp, "get_daypart_info", lambda *a, **k: {"daypart": "night", "local_time": "22:00"})
        calls = []

        async def _run_hooks(name, **kw):
            calls.append((name, kw))

        monkeypatch.setattr(hooks, "run_hooks", _run_hooks)
        out = await builtins._daypart_watcher_handler(self._app(), {})
        assert calls and calls[0][0] == "daypart_changed"
        assert builtins._daypart_watcher_state["last"] == "night"
        assert "day -> night" in out

    async def test_daypart_noop_when_unchanged(self, monkeypatch):
        import services.daypart_service as dp
        from services.scheduled_tasks import builtins

        builtins._daypart_watcher_state["last"] = "night"
        monkeypatch.setattr(dp, "get_daypart_info", lambda *a, **k: {"daypart": "night", "local_time": "22:00"})
        out = await builtins._daypart_watcher_handler(self._app(), {})
        assert out is None

    async def test_finalize_calls_service_with_mcp_manager(self, monkeypatch):
        import services.paperless_finalize_reconciler as pf
        from services.scheduled_tasks import builtins

        seen = {}

        async def _reconcile(mcp_manager=None):
            seen["mcp"] = mcp_manager

        monkeypatch.setattr(pf, "reconcile_pending_finalizes", _reconcile)
        mcp = object()
        await builtins._paperless_finalize_reconciler_handler(self._app(mcp_manager=mcp), {})
        assert seen["mcp"] is mcp

    async def test_mcp_health_gate_off_skips(self, monkeypatch):
        import services.mcp_health_monitor as mh
        from services.scheduled_tasks import builtins
        from utils.config import settings

        monkeypatch.setattr(settings, "mcp_health_monitor_enabled", False)
        called = []

        async def _tick(app):
            called.append(1)

        monkeypatch.setattr(mh, "monitor_tick", _tick)
        out = await builtins._mcp_health_monitor_handler(self._app(), {})
        assert "skipped" in out
        assert not called

    async def test_mcp_health_gate_on_calls_monitor(self, monkeypatch):
        import services.mcp_health_monitor as mh
        from services.scheduled_tasks import builtins
        from utils.config import settings

        monkeypatch.setattr(settings, "mcp_health_monitor_enabled", True)
        called = []

        async def _tick(app):
            called.append(app)

        monkeypatch.setattr(mh, "monitor_tick", _tick)
        app = self._app()
        await builtins._mcp_health_monitor_handler(app, {})
        assert called == [app]


# --- Phase 3 Batch B + C: gate re-assertion (H4) -----------------------------

# Each gated handler must return "skipped" (NOT touch its service) when its
# runtime gate is off — the core H4 safety property. (handler, {flag: value}).
_GATE_OFF_CASES = [
    ("_notification_cleanup_handler", {"proactive_enabled": False}),
    ("_memory_cleanup_handler", {"memory_enabled": False}),
    ("_meeting_retention_handler", {"meeting_transcription_enabled": False}),
    ("_trajectory_cleanup_handler", {"trajectory_capture_enabled": False}),
    ("_kg_conflation_monitor_handler", {"kg_conflation_monitor_enabled": False}),
    ("_paperless_reconciler_handler", {"folder_ingest_to_paperless": False, "email_ingest_to_paperless": False}),
    ("_obligation_deadline_notifier_handler", {"obligation_notifier_enabled": False}),
    ("_obligation_digest_handler", {"obligation_digest_enabled": False}),
    ("_obligation_calendar_sync_handler", {"obligation_calendar_sync_enabled": False}),
    ("_speaker_vocab_rebuild_handler", {"speaker_vocab_capture_enabled": False}),
    ("_skill_curator_handler", {"skill_curator_enabled": False}),
    ("_kg_reconciler_handler", {"kg_reconciler_enabled": False}),
    ("_skill_shadow_log_cleanup_handler", {"skill_shadow_log_enabled": False}),
]


@pytest.mark.unit
class TestBatchBCHandlers:
    def _app(self, **state):
        return SimpleNamespace(state=SimpleNamespace(**state))

    @pytest.mark.parametrize("handler_name,flags", _GATE_OFF_CASES)
    async def test_gate_off_skips(self, monkeypatch, handler_name, flags):
        from services.scheduled_tasks import builtins
        from utils.config import settings

        for k, v in flags.items():
            monkeypatch.setattr(settings, k, v)
        out = await getattr(builtins, handler_name)(self._app(), {})
        assert out is not None and "skipped" in out

    async def test_obligation_notifier_gate_on_calls_scan(self, monkeypatch):
        """The compound-gate H4 hazard: with BOTH flags on, the handler DOES run
        scan_all_users (which consumes the ledger) — with either off it must not."""
        import services.obligation_deadline_notifier as notifier
        from services.scheduled_tasks import builtins
        from utils.config import settings

        monkeypatch.setattr(settings, "obligation_notifier_enabled", True)
        monkeypatch.setattr(settings, "proactive_enabled", True)
        called = []

        async def _scan():
            called.append(1)

        monkeypatch.setattr(notifier, "scan_all_users", _scan)
        await builtins._obligation_deadline_notifier_handler(self._app(), {})
        assert called == [1]

    async def test_obligation_notifier_proactive_off_skips(self, monkeypatch):
        import services.obligation_deadline_notifier as notifier
        from services.scheduled_tasks import builtins
        from utils.config import settings

        monkeypatch.setattr(settings, "obligation_notifier_enabled", True)
        monkeypatch.setattr(settings, "proactive_enabled", False)  # the ledger-consume trap
        called = []

        async def _scan():
            called.append(1)

        monkeypatch.setattr(notifier, "scan_all_users", _scan)
        out = await builtins._obligation_deadline_notifier_handler(self._app(), {})
        assert "skipped" in out
        assert called == []  # ledger NOT consumed

    async def test_obligation_digest_gate_on_calls_scan(self, monkeypatch):
        import services.obligation_digest as digest
        from services.scheduled_tasks import builtins
        from utils.config import settings

        monkeypatch.setattr(settings, "obligation_digest_enabled", True)
        monkeypatch.setattr(settings, "proactive_enabled", True)
        called = []

        async def _scan():
            called.append(1)

        monkeypatch.setattr(digest, "scan_all_users", _scan)
        await builtins._obligation_digest_handler(self._app(), {})
        assert called == [1]

    async def test_calendar_sync_no_mcp_skips(self, monkeypatch):
        from services.scheduled_tasks import builtins
        from utils.config import settings

        monkeypatch.setattr(settings, "obligation_calendar_sync_enabled", True)
        out = await builtins._obligation_calendar_sync_handler(self._app(mcp_manager=None), {})
        assert "skipped" in out

    async def test_paperless_ui_edit_sweep_no_mcp_skips(self, monkeypatch):
        from services.scheduled_tasks import builtins

        out = await builtins._paperless_ui_edit_sweep_handler(self._app(mcp_manager=None), {})
        assert "skipped" in out

    async def test_paperless_abandoned_confirm_calls_service(self, monkeypatch):
        import services.paperless_ui_edit_sweeper as sweeper
        from services.scheduled_tasks import builtins

        called = []

        async def _run():
            called.append(1)

        monkeypatch.setattr(sweeper, "run_abandoned_confirm_sweep", _run)
        await builtins._paperless_abandoned_confirm_sweep_handler(self._app(), {})
        assert called == [1]

    def test_all_builtins_registered_and_valid(self):
        """The real seed list resolves (every settings.*_interval exists), every
        seed's handler is registered, and no seed is below the engine-tick floor."""
        from services.scheduled_tasks import builtins, engine, registry
        from utils.config import settings

        registry.clear_handlers()
        builtins.register_builtin_handlers()
        seeds = builtins.builtin_task_seeds()

        assert len(seeds) == 23  # +1: Low-Coverage-Reindex (autonomous OCR-recovery sweep)
        names = [s.name for s in seeds]
        assert len(set(names)) == 23  # unique names
        for seed in seeds:
            assert registry.get_handler(seed.handler_key) is not None, seed.handler_key
            if seed.interval_seconds is not None:
                # must not be below the engine tick (interval floor)
                engine._validate_interval_floor(seed.interval_seconds)


@pytest.mark.database
class TestRunHistory:
    """Per-run history rows (the admin UI's 'log of each run')."""

    async def _runs(self, smk, task_id):
        from models.database import ScheduledTaskRun

        async with smk() as db:
            return (await db.execute(
                select(ScheduledTaskRun)
                .where(ScheduledTaskRun.task_id == task_id)
                .order_by(ScheduledTaskRun.started_at.desc(), ScheduledTaskRun.id.desc())
            )).scalars().all()

    async def test_records_ok_run_with_detail(self, session_factory):
        from services.scheduled_tasks import engine, registry

        async def _h(app, params):
            return "deleted=5 remaining=3"

        registry.register_handler("rh_ok", _h)
        tid = await _mk(session_factory, name="t", handler_key="rh_ok")
        await engine._execute_task(SimpleNamespace(state=SimpleNamespace()), tid)

        runs = await self._runs(session_factory, tid)
        assert len(runs) == 1
        assert runs[0].status == "ok"
        assert runs[0].detail == "deleted=5 remaining=3"
        assert runs[0].error is None
        assert runs[0].finished_at is not None
        assert runs[0].duration_ms is not None

    async def test_records_error_run(self, session_factory):
        from services.scheduled_tasks import engine, registry

        async def _boom(app, params):
            raise RuntimeError("kaboom")

        registry.register_handler("rh_err", _boom)
        tid = await _mk(session_factory, name="t", handler_key="rh_err")
        await engine._execute_task(SimpleNamespace(state=SimpleNamespace()), tid)

        runs = await self._runs(session_factory, tid)
        assert len(runs) == 1
        assert runs[0].status == "error"
        assert "kaboom" in (runs[0].error or "")

    async def test_records_unknown_handler_skip_run(self, session_factory):
        from services.scheduled_tasks import engine

        tid = await _mk(session_factory, name="t", handler_key="ghost")  # not registered
        await engine._execute_task(SimpleNamespace(state=SimpleNamespace()), tid)

        runs = await self._runs(session_factory, tid)
        assert len(runs) == 1
        assert runs[0].status == "skipped"
        assert "unknown handler_key" in (runs[0].detail or "")

    async def test_retention_prunes_to_limit(self, session_factory, monkeypatch):
        from utils.config import settings
        from services.scheduled_tasks import engine, registry

        monkeypatch.setattr(settings, "scheduled_tasks_run_history_limit", 3)

        async def _h(app, params):
            return "ok"

        registry.register_handler("rh_many", _h)
        tid = await _mk(session_factory, name="t", handler_key="rh_many")
        for _ in range(5):
            await engine._execute_task(SimpleNamespace(state=SimpleNamespace()), tid)

        runs = await self._runs(session_factory, tid)
        assert len(runs) == 3  # pruned to the newest N
