"""Routing traces (#1524): the post_routing consumer, the reads, the cleanup and
the two admin routes the /admin/routing page had been calling into a 404."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select, text

from models.database import RoutingTrace
from utils.config import settings


@pytest.fixture
def traces_on(monkeypatch):
    monkeypatch.setattr(settings, "routing_traces_enabled", True)


async def _count(db) -> int:
    return (await db.execute(select(func.count(RoutingTrace.id)))).scalar()


async def _add(db, *, domain="general", layer="llm", age_days=0, user_id=None, message="x"):
    row = RoutingTrace(
        message=message, domain=domain, layer=layer, user_id=user_id,
        created_at=datetime.now(UTC).replace(tzinfo=None) - timedelta(days=age_days),
    )
    db.add(row)
    await db.commit()
    return row


# ----------------------------------------------------------- hook consumer
@pytest.mark.database
class TestRecord:
    async def test_records_the_decision(self, db_session, own_session_to_test_db, traces_on):
        from services.routing_trace_service import record_routing_trace

        await record_routing_trace(
            message="Mach das Licht im Wohnzimmer an",
            domain="smart_home", user_id=None, layer="semantic", confidence=0.91,
            sub_intent=None, entity_matches=[{"id": "light.wohnzimmer"}],
            session_id="s-1",  # the hook passes it; the row does not keep it
        )
        row = (await db_session.execute(select(RoutingTrace))).scalar_one()
        assert row.message == "Mach das Licht im Wohnzimmer an"
        assert (row.domain, row.layer) == ("smart_home", "semantic")
        assert row.confidence == pytest.approx(0.91)
        assert row.entity_matches == [{"id": "light.wohnzimmer"}]

    async def test_flag_off_writes_nothing(self, db_session, own_session_to_test_db, monkeypatch):
        """H4: a handler left registered must not write once the switch is off."""
        from services.routing_trace_service import record_routing_trace

        monkeypatch.setattr(settings, "routing_traces_enabled", False)
        await record_routing_trace(message="hallo", domain="conversation", layer="llm")
        assert await _count(db_session) == 0

    async def test_unstamped_role_is_marked_unknown(self, db_session, own_session_to_test_db, traces_on):
        from services.routing_trace_service import record_routing_trace

        await record_routing_trace(message="hallo", domain="conversation", layer=None)
        row = (await db_session.execute(select(RoutingTrace))).scalar_one()
        assert row.layer == "unknown"
        assert row.confidence is None

    async def test_deleting_the_user_deletes_their_traces(self, db_session, test_user):
        """The row holds the user's full message; it must not outlive the account."""
        await _add(db_session, user_id=test_user.id)
        await _add(db_session, user_id=None)
        await db_session.execute(text("DELETE FROM users WHERE id = :id"), {"id": test_user.id})
        await db_session.commit()
        assert await _count(db_session) == 1


# ------------------------------------------------------------------- reads
@pytest.mark.database
class TestReads:
    async def test_newest_first_and_domain_filter(self, db_session):
        from services.routing_trace_service import list_traces

        await _add(db_session, domain="smart_home", age_days=2, message="alt")
        await _add(db_session, domain="research", age_days=1)
        await _add(db_session, domain="smart_home", age_days=0, message="neu")

        assert [r.domain for r in await list_traces(db_session)] == [
            "smart_home", "research", "smart_home",
        ]
        only = await list_traces(db_session, domain="smart_home")
        assert [r.message for r in only] == ["neu", "alt"]
        assert len(await list_traces(db_session, limit=1)) == 1

    async def test_stats_by_domain_and_layer(self, db_session):
        from services.routing_trace_service import trace_stats

        await _add(db_session, domain="smart_home", layer="semantic")
        await _add(db_session, domain="smart_home", layer="llm")
        await _add(db_session, domain="research", layer="llm")

        stats = await trace_stats(db_session)
        assert stats == {
            "total": 3,
            "by_domain": {"smart_home": 2, "research": 1},
            "by_layer": {"llm": 2, "semantic": 1},
        }

    async def test_purge_keeps_the_window(self, db_session, monkeypatch):
        from services.routing_trace_service import purge_expired

        monkeypatch.setattr(settings, "routing_traces_retention_days", 14)
        await _add(db_session, age_days=15)
        await _add(db_session, age_days=13)
        assert await purge_expired(db_session) == 1
        assert await _count(db_session) == 1


@pytest.mark.database
class TestCleanupTask:
    async def test_purges_with_the_flag_off(self, db_session, own_session_to_test_db, monkeypatch):
        """Ungated on purpose: switching capture off must not keep full
        messages forever."""
        from services.scheduled_tasks import builtins

        monkeypatch.setattr(settings, "routing_traces_enabled", False)
        await _add(db_session, age_days=settings.routing_traces_retention_days + 1)
        out = await builtins._routing_trace_cleanup_handler(SimpleNamespace(), {})
        assert out == "deleted=1"
        assert await _count(db_session) == 0


# ------------------------------------------------------------------ routes
def _as(app, *, admin: bool):
    from services.auth_service import get_current_user

    user = SimpleNamespace(
        id=7, username="u", has_permission=lambda p: admin and p == "admin",
    )
    app.dependency_overrides[get_current_user] = lambda: user


@pytest.mark.database
class TestRoutes:
    async def test_traces_and_stats_for_an_admin(
        self, async_client: AsyncClient, app_with_test_db, db_session, monkeypatch,
    ):
        monkeypatch.setattr(settings, "auth_enabled", True)
        monkeypatch.setattr(settings, "routing_traces_enabled", True)
        _as(app_with_test_db, admin=True)
        await _add(db_session, domain="smart_home", layer="semantic", message="Licht an")
        await _add(db_session, domain="research", layer="llm")

        resp = await async_client.get("/api/admin/routing-traces", params={"domain": "smart_home"})
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["enabled"] is True
        assert body["retention_days"] == settings.routing_traces_retention_days
        assert [t["message"] for t in body["traces"]] == ["Licht an"]
        assert body["traces"][0]["entity_matches"] == []

        stats = await async_client.get("/api/admin/routing-stats")
        assert stats.status_code == 200, stats.text
        assert stats.json()["by_domain"] == {"smart_home": 1, "research": 1}

    @pytest.mark.parametrize("path", ["/api/admin/routing-traces", "/api/admin/routing-stats"])
    async def test_non_admin_is_refused(
        self, async_client: AsyncClient, app_with_test_db, monkeypatch, path,
    ):
        """The rows carry other users' full messages."""
        monkeypatch.setattr(settings, "auth_enabled", True)
        _as(app_with_test_db, admin=False)
        resp = await async_client.get(path)
        assert resp.status_code == 403, resp.text

    async def test_flag_off_answers_with_enabled_false(
        self, async_client: AsyncClient, app_with_test_db, monkeypatch,
    ):
        """Not a 404: the page has to be able to say WHY it is empty."""
        monkeypatch.setattr(settings, "routing_traces_enabled", False)
        _as(app_with_test_db, admin=True)
        resp = await async_client.get("/api/admin/routing-traces")
        assert resp.status_code == 200, resp.text
        assert resp.json() == {"traces": [], "enabled": False, "retention_days": settings.routing_traces_retention_days}
