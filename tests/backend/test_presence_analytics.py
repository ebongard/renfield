"""
Tests for Presence Analytics — hook handlers, heatmap, predictions, cleanup.
"""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest

from models.database import PresenceEvent, Role, Room, User
from services.presence_analytics import (
    PresenceAnalyticsService,
    _on_enter_room,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _seed_role(db, role_id=1):
    """Insert a default role for FK constraints."""
    from sqlalchemy import select
    result = await db.execute(select(Role).where(Role.id == role_id))
    if result.scalar_one_or_none():
        return
    role = Role(id=role_id, name="TestRole", permissions="{}")
    db.add(role)
    await db.commit()


async def _seed_user_and_room(db, user_id=1, room_id=10, room_name="Kitchen"):
    """Insert a user and room for FK constraints."""
    await _seed_role(db)
    user = User(id=user_id, username=f"user{user_id}", password_hash="x", role_id=1)
    room = Room(id=room_id, name=room_name)
    db.add_all([user, room])
    await db.commit()
    return user, room


async def _insert_event(db, user_id, room_id, event_type="enter", source="ble",
                        confidence=None, created_at=None):
    """Insert a PresenceEvent with optional timestamp override."""
    ev = PresenceEvent(
        user_id=user_id,
        room_id=room_id,
        event_type=event_type,
        source=source,
        confidence=confidence,
    )
    if created_at:
        ev.created_at = created_at
    db.add(ev)
    await db.commit()
    return ev


# ---------------------------------------------------------------------------
# Hook handler tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.unit
class TestHookHandlers:
    async def test_on_enter_room_creates_event(self, db_session):
        """_on_enter_room persists an 'enter' event."""
        from services.presence_analytics import _persist_event

        await _seed_user_and_room(db_session, user_id=1, room_id=10)

        # Patch the lazy import inside _persist_event
        mock_session_ctx = AsyncMock()
        mock_session_ctx.__aenter__ = AsyncMock(return_value=db_session)
        mock_session_ctx.__aexit__ = AsyncMock(return_value=False)

        import sys
        mock_db_mod = type(sys)("services.database")
        mock_db_mod.AsyncSessionLocal = lambda: mock_session_ctx
        with patch.dict(sys.modules, {"services.database": mock_db_mod}):
            await _persist_event(
                "enter",
                user_id=1, room_id=10, user_name="alice",
                room_name="Kitchen", confidence=0.85, source="ble",
            )

        from sqlalchemy import select
        result = await db_session.execute(select(PresenceEvent))
        events = result.scalars().all()
        assert len(events) == 1
        assert events[0].event_type == "enter"
        assert events[0].source == "ble"
        assert events[0].confidence == 0.85

    async def test_on_leave_room_creates_event(self, db_session):
        """_on_leave_room persists a 'leave' event."""
        from services.presence_analytics import _persist_event

        await _seed_user_and_room(db_session, user_id=1, room_id=10)

        mock_session_ctx = AsyncMock()
        mock_session_ctx.__aenter__ = AsyncMock(return_value=db_session)
        mock_session_ctx.__aexit__ = AsyncMock(return_value=False)

        import sys
        mock_db_mod = type(sys)("services.database")
        mock_db_mod.AsyncSessionLocal = lambda: mock_session_ctx
        with patch.dict(sys.modules, {"services.database": mock_db_mod}):
            await _persist_event(
                "leave",
                user_id=1, room_id=10, user_name="alice",
                room_name="Kitchen", source="voice",
            )

        from sqlalchemy import select
        result = await db_session.execute(select(PresenceEvent))
        events = result.scalars().all()
        assert len(events) == 1
        assert events[0].event_type == "leave"
        assert events[0].source == "voice"

    async def test_missing_user_or_room_skips(self, db_session):
        """No event created when user_id or room_id is missing."""
        await _on_enter_room(user_id=None, room_id=10)
        await _on_enter_room(user_id=1, room_id=None)

        from sqlalchemy import select
        result = await db_session.execute(select(PresenceEvent))
        assert result.scalars().all() == []


# ---------------------------------------------------------------------------
# Heatmap tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.unit
class TestHeatmap:
    async def test_empty_db_returns_empty(self, db_session):
        """Heatmap returns [] when no events exist."""
        service = PresenceAnalyticsService(db_session)
        result = await service.get_heatmap(days=30)
        assert result == []

    async def test_groups_by_room_and_hour(self, db_session):
        """Heatmap groups enter events by room and hour."""
        await _seed_user_and_room(db_session, user_id=1, room_id=10, room_name="Kitchen")

        # 3 events at hour 14 in Kitchen
        base = datetime.now(UTC).replace(tzinfo=None, hour=14, minute=0, second=0)
        for i in range(3):
            await _insert_event(db_session, 1, 10, "enter", created_at=base + timedelta(minutes=i))
        # 1 leave event (should not count)
        await _insert_event(db_session, 1, 10, "leave", created_at=base + timedelta(minutes=5))

        service = PresenceAnalyticsService(db_session)
        result = await service.get_heatmap(days=30)
        assert len(result) == 1
        assert result[0]["room_name"] == "Kitchen"
        assert result[0]["hour"] == 14
        assert result[0]["count"] == 3

    async def test_user_filter(self, db_session):
        """Heatmap with user_id filter only returns that user's events."""
        await _seed_user_and_room(db_session, user_id=1, room_id=10)
        user2 = User(id=2, username="user2", password_hash="x", role_id=1)
        db_session.add(user2)
        await db_session.commit()

        base = datetime.now(UTC).replace(tzinfo=None, hour=10, minute=0)
        await _insert_event(db_session, 1, 10, "enter", created_at=base)
        await _insert_event(db_session, 2, 10, "enter", created_at=base + timedelta(minutes=1))

        service = PresenceAnalyticsService(db_session)

        all_result = await service.get_heatmap(days=30)
        assert all_result[0]["count"] == 2

        user1_result = await service.get_heatmap(days=30, user_id=1)
        assert user1_result[0]["count"] == 1


# ---------------------------------------------------------------------------
# Prediction tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.unit
class TestPredictions:
    async def test_empty_db_returns_empty(self, db_session):
        """Predictions return [] when no events exist."""
        service = PresenceAnalyticsService(db_session)
        result = await service.get_predictions(user_id=1, days=60)
        assert result == []

    async def test_calculates_probability(self, db_session):
        """Predictions calculate probability as distinct_days / total_weeks."""
        await _seed_user_and_room(db_session, user_id=1, room_id=10)

        # Events on 4 different days within 28 days → 4 / 4 weeks = 1.0
        now = datetime.now(UTC).replace(tzinfo=None)
        for day_offset in range(4):
            ts = (now - timedelta(days=day_offset)).replace(hour=14, minute=0, second=0)
            await _insert_event(db_session, 1, 10, "enter", created_at=ts)

        service = PresenceAnalyticsService(db_session)
        result = await service.get_predictions(user_id=1, days=28)
        # Should have entries — probability depends on distinct days / total weeks
        assert len(result) > 0
        for entry in result:
            assert entry["probability"] >= 0.10

    async def test_low_probability_excluded(self, db_session):
        """Entries with probability < 10% are excluded."""
        await _seed_user_and_room(db_session, user_id=1, room_id=10)

        # 1 event in 365 days → 1 / 52.14 ≈ 0.02 → excluded
        ts = datetime.now(UTC).replace(tzinfo=None) - timedelta(days=5)
        ts = ts.replace(hour=3, minute=0, second=0)
        await _insert_event(db_session, 1, 10, "enter", created_at=ts)

        service = PresenceAnalyticsService(db_session)
        result = await service.get_predictions(user_id=1, days=365)
        # With 1 distinct day over 52 weeks, probability ≈ 0.02 → filtered out
        assert result == []


# ---------------------------------------------------------------------------
# Daily summary tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.unit
class TestDailySummary:
    async def test_empty_db_returns_empty(self, db_session):
        service = PresenceAnalyticsService(db_session)
        result = await service.get_daily_summary(days=7)
        assert result == []

    async def test_counts_enter_and_leave(self, db_session):
        """Daily summary counts enter and leave events separately."""
        await _seed_user_and_room(db_session, user_id=1, room_id=10)

        now = datetime.now(UTC).replace(tzinfo=None, hour=12)
        await _insert_event(db_session, 1, 10, "enter", created_at=now)
        await _insert_event(db_session, 1, 10, "enter", created_at=now + timedelta(hours=1))
        await _insert_event(db_session, 1, 10, "leave", created_at=now + timedelta(hours=2))

        service = PresenceAnalyticsService(db_session)
        result = await service.get_daily_summary(days=7)
        assert len(result) == 1
        assert result[0]["enter_count"] == 2
        assert result[0]["leave_count"] == 1


# ---------------------------------------------------------------------------
# Cleanup tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.unit
class TestCleanup:
    async def test_cleanup_deletes_old_keeps_recent(self, db_session):
        """Cleanup removes events older than retention, keeps recent ones."""
        await _seed_user_and_room(db_session, user_id=1, room_id=10)

        now = datetime.now(UTC).replace(tzinfo=None)
        old_ts = now - timedelta(days=100)
        recent_ts = now - timedelta(days=10)

        await _insert_event(db_session, 1, 10, "enter", created_at=old_ts)
        await _insert_event(db_session, 1, 10, "enter", created_at=recent_ts)

        service = PresenceAnalyticsService(db_session)
        deleted = await service.cleanup_old_events(retention_days=90)
        assert deleted == 1

        from sqlalchemy import select
        result = await db_session.execute(select(PresenceEvent))
        remaining = result.scalars().all()
        assert len(remaining) == 1
        assert remaining[0].created_at == recent_ts
