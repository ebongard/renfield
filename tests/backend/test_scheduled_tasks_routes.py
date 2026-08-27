"""Scheduled Tasks REST API (#1137) — /api/scheduled-tasks CRUD.

Covers list/get/create/patch/run-now/delete, the schedule + params validation,
built-in edit-not-delete, and next_run_at recompute on a schedule change. Auth is
disabled in the test harness, so require_permission(ADMIN) short-circuits.
"""
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from models.database import SCHEDULE_KIND_CRON, SCHEDULE_KIND_INTERVAL, ScheduledTask

pytestmark = pytest.mark.database


@pytest.fixture(autouse=True)
def _registry(monkeypatch):
    """Fresh registry per test: a plain handler + one with a param validator."""
    from services.scheduled_tasks import registry

    registry.clear_handlers()

    async def _noop(app, params):
        return None

    def _needs_n(params):
        if "n" not in params:
            raise ValueError("param 'n' required")

    registry.register_handler("noop", _noop)
    registry.register_handler("validated", _noop, validate_params=_needs_n)
    yield
    registry.clear_handlers()


@pytest.fixture
def smk(db_session):
    return async_sessionmaker(db_session.bind, class_=AsyncSession, expire_on_commit=False)


async def _seed(smk, **kw) -> int:
    defaults = dict(
        name="seed", handler_key="noop", schedule_kind=SCHEDULE_KIND_INTERVAL,
        interval_seconds=300, params={}, enabled=True, is_builtin=False,
    )
    defaults.update(kw)
    async with smk() as db:
        row = ScheduledTask(**defaults)
        db.add(row)
        await db.commit()
        await db.refresh(row)
        return row.id


class TestListGet:
    async def test_list_empty_exposes_handlers_and_tick(self, async_client):
        r = await async_client.get("/api/scheduled-tasks")
        assert r.status_code == 200
        body = r.json()
        assert body["tasks"] == []
        assert set(body["available_handlers"]) == {"noop", "validated"}
        assert body["engine_tick_seconds"] == 10

    async def test_get_missing_404(self, async_client):
        r = await async_client.get("/api/scheduled-tasks/999999")
        assert r.status_code == 404


class TestCreate:
    async def test_create_interval_task(self, async_client):
        r = await async_client.post("/api/scheduled-tasks", json={
            "name": "My Task", "handler_key": "noop",
            "schedule_kind": "interval", "interval_seconds": 300,
        })
        assert r.status_code == 201, r.text
        body = r.json()
        assert body["name"] == "My Task"
        assert body["is_builtin"] is False
        assert body["next_run_at"] is not None

    async def test_unknown_handler_400(self, async_client):
        r = await async_client.post("/api/scheduled-tasks", json={
            "name": "Task Name", "handler_key": "ghost", "interval_seconds": 300,
        })
        assert r.status_code == 400
        assert "handler_key" in r.text

    async def test_sub_tick_interval_400(self, async_client):
        r = await async_client.post("/api/scheduled-tasks", json={
            "name": "Task Name", "handler_key": "noop", "interval_seconds": 5,
        })
        assert r.status_code == 400

    async def test_bad_cron_400(self, async_client):
        r = await async_client.post("/api/scheduled-tasks", json={
            "name": "Task Name", "handler_key": "noop", "schedule_kind": "cron", "cron_expr": "not a cron",
        })
        assert r.status_code == 400

    async def test_interval_missing_field_400(self, async_client):
        r = await async_client.post("/api/scheduled-tasks", json={
            "name": "Task Name", "handler_key": "noop", "schedule_kind": "interval",
        })
        assert r.status_code == 400

    async def test_invalid_params_400(self, async_client):
        r = await async_client.post("/api/scheduled-tasks", json={
            "name": "Task Name", "handler_key": "validated", "interval_seconds": 300, "params": {},
        })
        assert r.status_code == 400
        assert "n" in r.text

    async def test_duplicate_name_409(self, async_client, smk):
        await _seed(smk, name="Dup")
        r = await async_client.post("/api/scheduled-tasks", json={
            "name": "Dup", "handler_key": "noop", "interval_seconds": 300,
        })
        assert r.status_code == 409

    async def test_create_cron_nulls_interval(self, async_client):
        pytest.importorskip("croniter")
        r = await async_client.post("/api/scheduled-tasks", json={
            "name": "Cron Task", "handler_key": "noop",
            "schedule_kind": "cron", "cron_expr": "0 8 * * *", "interval_seconds": 999,
        })
        assert r.status_code == 201, r.text
        body = r.json()
        assert body["schedule_kind"] == "cron"
        assert body["cron_expr"] == "0 8 * * *"
        assert body["interval_seconds"] is None  # interval ignored for a cron task


class TestPatch:
    async def test_toggle_enabled(self, async_client, smk):
        tid = await _seed(smk, enabled=True)
        r = await async_client.patch(f"/api/scheduled-tasks/{tid}", json={"enabled": False})
        assert r.status_code == 200
        assert r.json()["enabled"] is False

    async def test_schedule_change_interval_to_cron_recomputes(self, async_client, smk):
        pytest.importorskip("croniter")
        tid = await _seed(smk, schedule_kind=SCHEDULE_KIND_INTERVAL, interval_seconds=300)
        r = await async_client.patch(f"/api/scheduled-tasks/{tid}", json={
            "schedule_kind": "cron", "cron_expr": "0 8 * * *",
        })
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["schedule_kind"] == "cron"
        assert body["cron_expr"] == "0 8 * * *"
        assert body["interval_seconds"] is None
        assert body["next_run_at"] is not None

    async def test_patch_sub_tick_interval_400(self, async_client, smk):
        tid = await _seed(smk)
        r = await async_client.patch(f"/api/scheduled-tasks/{tid}", json={"interval_seconds": 3})
        assert r.status_code == 400

    async def test_patch_invalid_params_400(self, async_client, smk):
        tid = await _seed(smk, handler_key="validated", params={"n": 1})
        r = await async_client.patch(f"/api/scheduled-tasks/{tid}", json={"params": {}})
        assert r.status_code == 400

    async def test_toggle_enabled_keeps_next_run(self, async_client, smk):
        """A non-schedule PATCH must NOT recompute next_run_at."""
        from datetime import UTC, datetime, timedelta

        nr = datetime.now(UTC).replace(tzinfo=None) + timedelta(hours=2)
        tid = await _seed(smk, next_run_at=nr)
        r = await async_client.patch(f"/api/scheduled-tasks/{tid}", json={"enabled": False})
        assert r.status_code == 200
        async with smk() as db:
            row = await db.get(ScheduledTask, tid)
        assert abs((row.next_run_at - nr).total_seconds()) < 1  # unchanged

    async def test_schedule_change_cron_to_interval(self, async_client, smk):
        tid = await _seed(smk, schedule_kind=SCHEDULE_KIND_CRON, cron_expr="0 8 * * *", interval_seconds=None)
        r = await async_client.patch(f"/api/scheduled-tasks/{tid}", json={
            "schedule_kind": "interval", "interval_seconds": 600,
        })
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["schedule_kind"] == "interval"
        assert body["interval_seconds"] == 600
        assert body["cron_expr"] is None  # unused field nulled

    async def test_patch_missing_404(self, async_client):
        r = await async_client.patch("/api/scheduled-tasks/999999", json={"enabled": False})
        assert r.status_code == 404


class TestRunNow:
    async def test_run_now_sets_next_run(self, async_client, smk):
        from datetime import UTC, datetime, timedelta

        future = datetime.now(UTC).replace(tzinfo=None) + timedelta(hours=5)
        tid = await _seed(smk, next_run_at=future)
        r = await async_client.post(f"/api/scheduled-tasks/{tid}/run-now")
        assert r.status_code == 200
        # next_run_at pulled back to ~now (well before the +5h)
        async with smk() as db:
            row = await db.get(ScheduledTask, tid)
        assert row.next_run_at < future

    async def test_run_now_disabled_409(self, async_client, smk):
        tid = await _seed(smk, enabled=False)
        r = await async_client.post(f"/api/scheduled-tasks/{tid}/run-now")
        assert r.status_code == 409

    async def test_run_now_before_start_409(self, async_client, smk):
        from datetime import UTC, datetime, timedelta

        future = datetime.now(UTC).replace(tzinfo=None) + timedelta(hours=1)
        tid = await _seed(smk, start_at=future)
        r = await async_client.post(f"/api/scheduled-tasks/{tid}/run-now")
        assert r.status_code == 409  # window not open → engine would never select it

    async def test_run_now_after_end_409(self, async_client, smk):
        from datetime import UTC, datetime, timedelta

        past = datetime.now(UTC).replace(tzinfo=None) - timedelta(hours=1)
        tid = await _seed(smk, end_at=past)
        r = await async_client.post(f"/api/scheduled-tasks/{tid}/run-now")
        assert r.status_code == 409


class TestDelete:
    async def test_delete_custom(self, async_client, smk):
        tid = await _seed(smk, is_builtin=False)
        r = await async_client.delete(f"/api/scheduled-tasks/{tid}")
        assert r.status_code == 200
        async with smk() as db:
            assert await db.get(ScheduledTask, tid) is None

    async def test_delete_builtin_409(self, async_client, smk):
        tid = await _seed(smk, is_builtin=True)
        r = await async_client.delete(f"/api/scheduled-tasks/{tid}")
        assert r.status_code == 409

    async def test_delete_missing_404(self, async_client):
        r = await async_client.delete("/api/scheduled-tasks/999999")
        assert r.status_code == 404
