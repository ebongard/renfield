"""
Tests for Persistent Presence History — satellite_id persistence, timeline /
last-seen / room-window query methods, and the internal.presence_history tool.

Mirrors the fixture + seeding pattern in test_presence_analytics.py.
"""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest

from models.database import PresenceEvent, Role, Room, User
from ha_glue.services.presence_analytics import PresenceAnalyticsService

# ---------------------------------------------------------------------------
# Helpers (same shape as test_presence_analytics.py)
# ---------------------------------------------------------------------------

async def _seed_role(db, role_id=1):
    from sqlalchemy import select
    result = await db.execute(select(Role).where(Role.id == role_id))
    if result.scalar_one_or_none():
        return
    role = Role(id=role_id, name="TestRole", permissions="{}")
    db.add(role)
    await db.commit()


async def _seed_user(db, user_id=1, username=None):
    await _seed_role(db)
    user = User(
        id=user_id,
        username=username or f"user{user_id}",
        password_hash="x",
        role_id=1,
    )
    db.add(user)
    await db.commit()
    return user


async def _seed_room(db, room_id=10, room_name="Kitchen"):
    room = Room(id=room_id, name=room_name)
    db.add(room)
    await db.commit()
    return room


async def _insert_event(db, user_id, room_id, event_type="enter", source="ble",
                        confidence=None, satellite_id=None, created_at=None):
    ev = PresenceEvent(
        user_id=user_id,
        room_id=room_id,
        event_type=event_type,
        source=source,
        confidence=confidence,
        satellite_id=satellite_id,
    )
    if created_at:
        ev.created_at = created_at
    db.add(ev)
    await db.commit()
    return ev


@pytest.fixture(autouse=True)
def _utc_analytics_tz(monkeypatch):
    """Pin analytics bucketing/formatting to UTC for deterministic assertions."""
    monkeypatch.setattr(
        "ha_glue.services.presence_analytics.ha_glue_settings.presence_analytics_timezone",
        "UTC",
        raising=False,
    )


# ---------------------------------------------------------------------------
# satellite_id persistence
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.database
class TestSatelliteIdPersistence:
    async def test_enter_stores_satellite_id(self, db_session):
        """_persist_event writes satellite_id onto an enter row."""
        from ha_glue.services.presence_analytics import _persist_event

        await _seed_user(db_session, user_id=1)
        await _seed_room(db_session, room_id=10)

        mock_ctx = AsyncMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=db_session)
        mock_ctx.__aexit__ = AsyncMock(return_value=False)

        import sys
        mock_db_mod = type(sys)("services.database")
        mock_db_mod.AsyncSessionLocal = lambda: mock_ctx
        with patch.dict(sys.modules, {"services.database": mock_db_mod}):
            await _persist_event(
                "enter",
                user_id=1, room_id=10, source="ble", confidence=0.9,
                satellite_id="arbeitszimmer",
            )

        from sqlalchemy import select
        events = (await db_session.execute(select(PresenceEvent))).scalars().all()
        assert len(events) == 1
        assert events[0].event_type == "enter"
        assert events[0].satellite_id == "arbeitszimmer"

    async def test_leave_has_null_satellite_id(self, db_session):
        """A leave event (no satellite_id kwarg) stores NULL."""
        from ha_glue.services.presence_analytics import _persist_event

        await _seed_user(db_session, user_id=1)
        await _seed_room(db_session, room_id=10)

        mock_ctx = AsyncMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=db_session)
        mock_ctx.__aexit__ = AsyncMock(return_value=False)

        import sys
        mock_db_mod = type(sys)("services.database")
        mock_db_mod.AsyncSessionLocal = lambda: mock_ctx
        with patch.dict(sys.modules, {"services.database": mock_db_mod}):
            await _persist_event("leave", user_id=1, room_id=10, source="ble")

        from sqlalchemy import select
        events = (await db_session.execute(select(PresenceEvent))).scalars().all()
        assert len(events) == 1
        assert events[0].event_type == "leave"
        assert events[0].satellite_id is None


# ---------------------------------------------------------------------------
# get_timeline
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.database
class TestTimeline:
    async def test_empty_returns_empty(self, db_session):
        service = PresenceAnalyticsService(db_session)
        assert await service.get_timeline(user_id=1) == []

    async def test_chronological_order_and_fields(self, db_session):
        await _seed_user(db_session, user_id=1)
        await _seed_room(db_session, room_id=10, room_name="Kitchen")

        base = datetime.now(UTC).replace(tzinfo=None, microsecond=0)
        # Insert out of order; expect oldest-first out.
        await _insert_event(db_session, 1, 10, "leave", created_at=base + timedelta(minutes=10))
        await _insert_event(db_session, 1, 10, "enter", satellite_id="sat-a",
                            created_at=base)

        service = PresenceAnalyticsService(db_session)
        rows = await service.get_timeline(user_id=1)
        assert [r["event_type"] for r in rows] == ["enter", "leave"]
        assert rows[0]["room_name"] == "Kitchen"
        assert rows[0]["satellite_id"] == "sat-a"
        assert rows[1]["satellite_id"] is None

    async def test_since_filter(self, db_session):
        await _seed_user(db_session, user_id=1)
        await _seed_room(db_session, room_id=10)

        now = datetime.now(UTC).replace(tzinfo=None, microsecond=0)
        await _insert_event(db_session, 1, 10, "enter", created_at=now - timedelta(days=5))
        await _insert_event(db_session, 1, 10, "enter", created_at=now - timedelta(hours=1))

        service = PresenceAnalyticsService(db_session)
        rows = await service.get_timeline(user_id=1, since=now - timedelta(hours=2))
        assert len(rows) == 1

    async def test_room_id_filter(self, db_session):
        await _seed_user(db_session, user_id=1)
        await _seed_room(db_session, room_id=10, room_name="Kitchen")
        await _seed_room(db_session, room_id=11, room_name="Office")

        now = datetime.now(UTC).replace(tzinfo=None, microsecond=0)
        await _insert_event(db_session, 1, 10, "enter", created_at=now)
        await _insert_event(db_session, 1, 11, "enter", created_at=now + timedelta(minutes=1))

        service = PresenceAnalyticsService(db_session)
        rows = await service.get_timeline(user_id=1, room_id=11)
        assert len(rows) == 1
        assert rows[0]["room_name"] == "Office"

    async def test_pagination(self, db_session):
        await _seed_user(db_session, user_id=1)
        await _seed_room(db_session, room_id=10)

        now = datetime.now(UTC).replace(tzinfo=None, microsecond=0)
        for i in range(5):
            await _insert_event(db_session, 1, 10, "enter",
                                created_at=now + timedelta(minutes=i))

        service = PresenceAnalyticsService(db_session)
        page1 = await service.get_timeline(user_id=1, limit=2, offset=0)
        page2 = await service.get_timeline(user_id=1, limit=2, offset=2)
        assert len(page1) == 2
        assert len(page2) == 2
        assert page1[0]["id"] != page2[0]["id"]
        # Continues chronologically across pages.
        assert page1[-1]["created_at"] <= page2[0]["created_at"]


# ---------------------------------------------------------------------------
# get_last_seen_by_room
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.database
class TestLastSeenByRoom:
    async def test_empty_returns_empty(self, db_session):
        service = PresenceAnalyticsService(db_session)
        assert await service.get_last_seen_by_room(user_id=1) == []

    async def test_max_enter_per_room_ignores_leave(self, db_session):
        await _seed_user(db_session, user_id=1)
        await _seed_room(db_session, room_id=10, room_name="Kitchen")
        await _seed_room(db_session, room_id=11, room_name="Office")

        now = datetime.now(UTC).replace(tzinfo=None, microsecond=0)
        # Kitchen: two enters, latest wins.
        await _insert_event(db_session, 1, 10, "enter", created_at=now - timedelta(hours=3))
        latest_kitchen = now - timedelta(hours=1)
        await _insert_event(db_session, 1, 10, "enter", created_at=latest_kitchen)
        # A later LEAVE must not be reported as last-seen.
        await _insert_event(db_session, 1, 10, "leave", created_at=now)
        # Office: one enter.
        await _insert_event(db_session, 1, 11, "enter", created_at=now - timedelta(hours=2))

        service = PresenceAnalyticsService(db_session)
        rows = await service.get_last_seen_by_room(user_id=1)
        by_room = {r["room_name"]: r["last_seen"] for r in rows}
        assert by_room["Kitchen"] == latest_kitchen
        assert "Office" in by_room
        # Ordered most-recent first.
        assert rows[0]["room_name"] == "Kitchen"


# ---------------------------------------------------------------------------
# get_room_occupancy_window
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.database
class TestRoomOccupancyWindow:
    async def test_only_target_room_and_time_bounds(self, db_session):
        await _seed_user(db_session, user_id=1)
        await _seed_user(db_session, user_id=2)
        await _seed_room(db_session, room_id=10, room_name="Kitchen")
        await _seed_room(db_session, room_id=11, room_name="Office")

        now = datetime.now(UTC).replace(tzinfo=None, microsecond=0)
        since = now - timedelta(hours=2)
        until = now

        # In window, target room, two users.
        await _insert_event(db_session, 1, 10, "enter", created_at=now - timedelta(hours=1))
        await _insert_event(db_session, 2, 10, "enter", created_at=now - timedelta(minutes=30))
        # Other room — excluded.
        await _insert_event(db_session, 1, 11, "enter", created_at=now - timedelta(hours=1))
        # Out of window — excluded.
        await _insert_event(db_session, 1, 10, "enter", created_at=now - timedelta(hours=5))

        service = PresenceAnalyticsService(db_session)
        rows = await service.get_room_occupancy_window(room_id=10, since=since, until=until)
        assert len(rows) == 2
        assert {r["user_id"] for r in rows} == {1, 2}
        assert all(r["room_id"] == 10 for r in rows)
        # Chronological.
        assert rows[0]["created_at"] <= rows[1]["created_at"]


# ---------------------------------------------------------------------------
# internal.presence_history tool
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.database
class TestPresenceHistoryTool:
    def _patch_session(self, db_session):
        """Return a patch.dict context binding services.database.AsyncSessionLocal
        to the test session so the tool's lazy imports use it."""
        import sys

        mock_ctx = AsyncMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=db_session)
        mock_ctx.__aexit__ = AsyncMock(return_value=False)
        mock_db_mod = type(sys)("services.database")
        mock_db_mod.AsyncSessionLocal = lambda: mock_ctx
        return patch.dict(sys.modules, {"services.database": mock_db_mod})

    async def test_missing_user_name_timeline(self, db_session):
        from ha_glue.services.internal_tools import InternalToolService

        svc = InternalToolService()
        res = await svc._presence_history({"query_type": "timeline"})
        assert res["success"] is False
        assert "user_name" in res["message"]

    async def test_unknown_user_informative(self, db_session):
        from ha_glue.services.internal_tools import InternalToolService
        from ha_glue.services.presence_service import get_presence_service

        with patch.object(get_presence_service(), "find_user_by_name", return_value=None):
            svc = InternalToolService()
            res = await svc._presence_history(
                {"query_type": "timeline", "user_name": "Nobody"}
            )
        assert res["success"] is False
        assert "not found" in res["message"].lower()

    async def test_timeline_returns_summary(self, db_session):
        from ha_glue.services.internal_tools import InternalToolService
        from ha_glue.services.presence_service import get_presence_service

        await _seed_user(db_session, user_id=1, username="alice")
        await _seed_room(db_session, room_id=10, room_name="Kitchen")
        base = datetime.now(UTC).replace(tzinfo=None, microsecond=0)
        await _insert_event(db_session, 1, 10, "enter", satellite_id="sat-a", created_at=base)

        ps = get_presence_service()
        with patch.object(ps, "find_user_by_name", return_value=1), \
             patch.object(ps, "get_display_name", return_value="Alice"), \
             self._patch_session(db_session):
            svc = InternalToolService()
            res = await svc._presence_history({
                "query_type": "timeline",
                "user_name": "Alice",
                "since": (base - timedelta(hours=1)).isoformat(),
                "until": (base + timedelta(hours=1)).isoformat(),
            })

        assert res["success"] is True
        assert res["data"]["events"]
        assert "Alice" in res["summary"]
        assert "Kitchen" in res["summary"]

    async def test_gate_off_returns_disabled(self, db_session, monkeypatch):
        from ha_glue.services.internal_tools import InternalToolService

        monkeypatch.setattr(
            "ha_glue.utils.config.ha_glue_settings.presence_history_enabled",
            False,
            raising=False,
        )
        svc = InternalToolService()
        res = await svc._presence_history({"query_type": "timeline", "user_name": "Alice"})
        assert res["success"] is False
        assert "disabled" in res["message"].lower()
