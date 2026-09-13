"""Failure-streak alerting for scheduled tasks (A2).

The failure this encodes: on 2026-09-11 the Paperless dedupe task failed 50 times
in a row over a day and a half. Every run was recorded faithfully in
``ScheduledTaskRun`` and ``last_status`` — and nobody was told, because
``last_status`` only ever shows the NEWEST run. These tests pin the streak
counter, the once-per-streak alert, the durable re-alert TTL, and the recovery
notice.

Same sqlite harness + AsyncSessionLocal rebinding as test_scheduled_tasks_engine.
"""
import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from models.database import (
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
    import services.database as db_mod

    smk = async_sessionmaker(db_session.bind, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(db_mod, "AsyncSessionLocal", smk)
    monkeypatch.setattr(db_mod, "engine", db_session.bind)
    return smk


@pytest.fixture(autouse=True)
def _reset_state(monkeypatch):
    from services import ops_alert
    from services.scheduled_tasks import engine, registry
    from utils.config import settings

    monkeypatch.setattr(settings, "scheduled_tasks_max_concurrent", 1)
    monkeypatch.setattr(settings, "scheduled_task_failure_alert_enabled", True)
    monkeypatch.setattr(settings, "scheduled_task_failure_alert_threshold", 3)
    ops_alert.reset_ledger()
    engine.reset_state()
    registry.clear_handlers()
    yield
    ops_alert.reset_ledger()
    engine.reset_state()
    registry.clear_handlers()


@pytest.fixture
def alerts(monkeypatch):
    """Capture every notify_admin call the engine makes."""
    from services.scheduled_tasks import engine as eng

    captured: list[dict] = []

    async def _fake(**kw):
        captured.append(kw)
        return True  # notify_admin reports whether it actually handed off

    monkeypatch.setattr(eng.ops_alert, "notify_admin", _fake)
    return captured


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


async def _run_n(smk, task_id: int, n: int, *, failing: bool) -> None:
    """Execute the task n times through the real engine path."""
    from types import SimpleNamespace

    from services.scheduled_tasks import engine, registry

    async def _ok(app, params):
        return "fine"

    async def _boom(app, params):
        raise RuntimeError("upstream is dead")

    registry.register_handler("h", _boom if failing else _ok)
    app = SimpleNamespace(state=SimpleNamespace())
    for _ in range(n):
        await engine._execute_task(app, task_id)


# ---------------------------------------------------------------------------


@pytest.mark.database
@pytest.mark.asyncio
class TestFailureStreak:
    async def test_counter_increments_on_each_failure(self, session_factory, alerts):
        task_id = await _mk(session_factory)
        await _run_n(session_factory, task_id, 2, failing=True)

        row = await _get(session_factory, task_id)
        assert row.last_status == SCHEDULED_TASK_STATUS_ERROR
        assert row.consecutive_error_count == 2

    async def test_below_threshold_stays_silent(self, session_factory, alerts):
        """A single transient failure must not wake anybody."""
        task_id = await _mk(session_factory)
        await _run_n(session_factory, task_id, 2, failing=True)

        assert alerts == []
        row = await _get(session_factory, task_id)
        assert row.error_alerted_at is None

    async def test_alerts_once_at_threshold_then_stays_quiet(self, session_factory, alerts):
        """The 50-failures-in-silence case: alert ONCE, not once per run."""
        task_id = await _mk(session_factory, name="Paperless-Duplikate aufräumen")
        await _run_n(session_factory, task_id, 10, failing=True)

        assert len(alerts) == 1, f"expected exactly one alert, got {len(alerts)}"
        alert = alerts[0]
        assert "Paperless-Duplikate aufräumen" in alert["title"]
        assert alert["data"]["consecutive_errors"] == 3
        assert "upstream is dead" in alert["message"]

        row = await _get(session_factory, task_id)
        assert row.consecutive_error_count == 10
        assert row.error_alerted_at is not None

    async def test_realert_after_ttl(self, session_factory, alerts, monkeypatch):
        from utils.config import settings

        monkeypatch.setattr(settings, "scheduled_task_failure_realert_seconds", 3600.0)
        task_id = await _mk(session_factory)
        await _run_n(session_factory, task_id, 3, failing=True)
        assert len(alerts) == 1

        # Backdate the marker past the TTL — the streak is still ongoing.
        async with session_factory() as db:
            row = await db.get(ScheduledTask, task_id)
            row.error_alerted_at = _naive_now() - timedelta(hours=2)
            await db.commit()

        await _run_n(session_factory, task_id, 1, failing=True)
        assert len(alerts) == 2

    async def test_success_resets_counter_and_notifies_recovery(self, session_factory, alerts):
        task_id = await _mk(session_factory)
        await _run_n(session_factory, task_id, 3, failing=True)
        assert len(alerts) == 1

        await _run_n(session_factory, task_id, 1, failing=False)

        row = await _get(session_factory, task_id)
        assert row.last_status == SCHEDULED_TASK_STATUS_OK
        assert row.consecutive_error_count == 0
        assert row.error_alerted_at is None
        assert len(alerts) == 2
        assert alerts[1]["data"]["recovered_after"] == 3

    async def test_recovery_is_silent_if_no_alert_was_sent(self, session_factory, alerts):
        """Two failures below the threshold then a success: nobody was told it was
        broken, so nobody gets told it recovered."""
        task_id = await _mk(session_factory)
        await _run_n(session_factory, task_id, 2, failing=True)
        await _run_n(session_factory, task_id, 1, failing=False)

        assert alerts == []
        row = await _get(session_factory, task_id)
        assert row.consecutive_error_count == 0

    async def test_flag_off_counts_but_never_alerts(self, session_factory, alerts, monkeypatch):
        """The counter is plain bookkeeping the admin UI shows; only DELIVERY is
        gated, so turning the alerts off does not blind the UI."""
        from utils.config import settings

        monkeypatch.setattr(settings, "scheduled_task_failure_alert_enabled", False)
        task_id = await _mk(session_factory)
        await _run_n(session_factory, task_id, 5, failing=True)

        assert alerts == []
        row = await _get(session_factory, task_id)
        assert row.consecutive_error_count == 5

    async def test_alerting_failure_never_costs_the_run_state(self, session_factory, monkeypatch):
        """An exploding notification pipeline must not stop the task from being
        rescheduled — that would turn a reporting bug into an outage."""
        from services.scheduled_tasks import engine as eng

        async def _explode(**kw):
            raise RuntimeError("notification pipeline down")

        monkeypatch.setattr(eng.ops_alert, "notify_admin", _explode)
        task_id = await _mk(session_factory)
        await _run_n(session_factory, task_id, 3, failing=True)

        row = await _get(session_factory, task_id)
        assert row.last_status == SCHEDULED_TASK_STATUS_ERROR
        assert row.next_run_at is not None

    async def test_unknown_handler_skip_counts_as_failure(self, session_factory, alerts):
        """A skip means the handler_key does not resolve — the task is not running
        AT ALL, which is worse than failing, not better. Treating it as a recovery
        would have told the admin a permanently dead task "runs again" and zeroed
        the counter that keeps it visible in internal.system_health."""
        from types import SimpleNamespace

        from services.scheduled_tasks import engine, registry

        task_id = await _mk(session_factory)
        await _run_n(session_factory, task_id, 3, failing=True)
        assert (await _get(session_factory, task_id)).consecutive_error_count == 3
        assert len(alerts) == 1

        registry.clear_handlers()  # handler_key "h" no longer resolves
        await engine._execute_task(SimpleNamespace(state=SimpleNamespace()), task_id)

        row = await _get(session_factory, task_id)
        assert row.consecutive_error_count == 4, "a skip must not zero the streak"
        # And no bogus "läuft wieder" notice.
        assert len(alerts) == 1
        assert all("läuft wieder" not in a["title"] for a in alerts)

    async def test_skip_alone_alerts_as_unrunnable(self, session_factory, alerts):
        """A task whose handler never resolves reaches the threshold on skips
        alone and says so — the mid-rollout ordering gap made durable."""
        from types import SimpleNamespace

        from services.scheduled_tasks import engine

        task_id = await _mk(session_factory, name="Verwaiste Aufgabe", handler_key="gone")
        app = SimpleNamespace(state=SimpleNamespace())
        for _ in range(3):
            await engine._execute_task(app, task_id)

        assert len(alerts) == 1
        assert "kann nicht ausgeführt werden" in alerts[0]["title"]
        assert alerts[0]["data"]["last_status"] == SCHEDULED_TASK_STATUS_SKIPPED
        assert "unknown handler_key" in alerts[0]["message"]

    async def test_marker_not_stamped_when_delivery_failed(self, session_factory, monkeypatch):
        """The whole point of this feature is not being silent: an alert attempted
        while the notification pipeline is down must NOT be recorded as sent, or
        the first genuine alert is suppressed for the full six-hour TTL."""
        from services.scheduled_tasks import engine as eng

        attempts: list[str] = []

        async def _undeliverable(**kw):
            attempts.append(kw["dedup_key"])
            return False

        monkeypatch.setattr(eng.ops_alert, "notify_admin", _undeliverable)
        task_id = await _mk(session_factory)
        await _run_n(session_factory, task_id, 3, failing=True)

        row = await _get(session_factory, task_id)
        assert row.error_alerted_at is None
        # …and it keeps trying on the next run rather than going quiet.
        await _run_n(session_factory, task_id, 1, failing=True)
        assert len(attempts) == 2
