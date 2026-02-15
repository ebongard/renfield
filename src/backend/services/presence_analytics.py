"""
Presence Analytics — persist events and provide heatmap/prediction queries.

Hook handlers run fire-and-forget with their own DB session.
PresenceAnalyticsService accepts a caller-provided session (for routes/tests).
"""

from datetime import UTC, datetime, timedelta

from loguru import logger
from sqlalchemy import delete, distinct, extract, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from models.database import PresenceEvent, Room
from utils.config import settings
from utils.hooks import register_hook

# ---------------------------------------------------------------------------
# Hook handlers (fire-and-forget, own session)
# ---------------------------------------------------------------------------

async def _on_enter_room(**kwargs):
    """Persist an 'enter' event when a user enters a room."""
    await _persist_event("enter", **kwargs)


async def _on_leave_room(**kwargs):
    """Persist a 'leave' event when a user leaves a room."""
    await _persist_event("leave", **kwargs)


async def _persist_event(event_type: str, **kwargs):
    """Write a PresenceEvent row using a fresh DB session."""
    user_id = kwargs.get("user_id")
    room_id = kwargs.get("room_id")
    if user_id is None or room_id is None:
        return

    try:
        from services.database import AsyncSessionLocal

        async with AsyncSessionLocal() as db:
            event = PresenceEvent(
                user_id=user_id,
                room_id=room_id,
                event_type=event_type,
                source=kwargs.get("source", "ble"),
                confidence=kwargs.get("confidence"),
            )
            db.add(event)
            await db.commit()
    except Exception:
        logger.opt(exception=True).warning(f"Failed to persist presence event ({event_type})")


def register_presence_analytics_hooks():
    """Register enter/leave hooks for analytics persistence."""
    register_hook("presence_enter_room", _on_enter_room)
    register_hook("presence_leave_room", _on_leave_room)
    logger.info("Presence analytics hooks registered")


# ---------------------------------------------------------------------------
# Query service (caller-provided session)
# ---------------------------------------------------------------------------

class PresenceAnalyticsService:
    """SQL-based analytics over the presence_events table."""

    def __init__(self, db: AsyncSession):
        self.db = db

    async def get_heatmap(
        self, days: int = 30, user_id: int | None = None
    ) -> list[dict]:
        """
        Room x hour heatmap.

        Returns list of {room_id, room_name, hour, count} for 'enter' events.
        """
        cutoff = datetime.now(UTC).replace(tzinfo=None) - timedelta(days=days)

        hour_col = extract("hour", PresenceEvent.created_at).label("hour")

        stmt = (
            select(
                PresenceEvent.room_id,
                Room.name.label("room_name"),
                hour_col,
                func.count().label("count"),
            )
            .join(Room, Room.id == PresenceEvent.room_id)
            .where(
                PresenceEvent.event_type == "enter",
                PresenceEvent.created_at >= cutoff,
            )
            .group_by(PresenceEvent.room_id, Room.name, hour_col)
            .order_by(PresenceEvent.room_id, hour_col)
        )

        if user_id is not None:
            stmt = stmt.where(PresenceEvent.user_id == user_id)

        result = await self.db.execute(stmt)
        return [
            {
                "room_id": row.room_id,
                "room_name": row.room_name,
                "hour": int(row.hour),
                "count": row.count,
            }
            for row in result.all()
        ]

    async def get_predictions(
        self, user_id: int, days: int = 60
    ) -> list[dict]:
        """
        Per-user probability of being in each room by day-of-week and hour.

        Returns list of {room_id, room_name, day_of_week (0=Sun), hour, probability}.
        Entries with probability < 0.10 are excluded.
        """
        cutoff = datetime.now(UTC).replace(tzinfo=None) - timedelta(days=days)

        dow_col = extract("dow", PresenceEvent.created_at).label("dow")
        hour_col = extract("hour", PresenceEvent.created_at).label("hour")
        date_col = func.date(PresenceEvent.created_at).label("event_date")

        # Total distinct weeks in the data range
        total_weeks = max(days / 7, 1)

        stmt = (
            select(
                PresenceEvent.room_id,
                Room.name.label("room_name"),
                dow_col,
                hour_col,
                func.count(distinct(date_col)).label("distinct_days"),
            )
            .join(Room, Room.id == PresenceEvent.room_id)
            .where(
                PresenceEvent.user_id == user_id,
                PresenceEvent.event_type == "enter",
                PresenceEvent.created_at >= cutoff,
            )
            .group_by(PresenceEvent.room_id, Room.name, dow_col, hour_col)
        )

        result = await self.db.execute(stmt)
        predictions = []
        for row in result.all():
            probability = round(row.distinct_days / total_weeks, 2)
            if probability < 0.10:
                continue
            predictions.append({
                "room_id": row.room_id,
                "room_name": row.room_name,
                "day_of_week": int(row.dow),
                "hour": int(row.hour),
                "probability": probability,
            })

        return sorted(predictions, key=lambda p: (-p["probability"], p["hour"]))

    async def get_daily_summary(self, days: int = 7) -> list[dict]:
        """
        Daily enter/leave counts.

        Returns list of {date, enter_count, leave_count}.
        """
        cutoff = datetime.now(UTC).replace(tzinfo=None) - timedelta(days=days)

        date_col = func.date(PresenceEvent.created_at).label("event_date")

        stmt = (
            select(
                date_col,
                func.count().filter(PresenceEvent.event_type == "enter").label("enter_count"),
                func.count().filter(PresenceEvent.event_type == "leave").label("leave_count"),
            )
            .where(PresenceEvent.created_at >= cutoff)
            .group_by(date_col)
            .order_by(date_col)
        )

        result = await self.db.execute(stmt)
        return [
            {
                "date": str(row.event_date),
                "enter_count": row.enter_count,
                "leave_count": row.leave_count,
            }
            for row in result.all()
        ]

    async def cleanup_old_events(self, retention_days: int | None = None) -> int:
        """Delete events older than retention_days. Returns count deleted."""
        retention = retention_days or settings.presence_analytics_retention_days
        cutoff = datetime.now(UTC).replace(tzinfo=None) - timedelta(days=retention)

        result = await self.db.execute(
            delete(PresenceEvent).where(PresenceEvent.created_at < cutoff)
        )
        await self.db.commit()
        count = result.rowcount
        if count > 0:
            logger.info(f"Presence analytics: cleaned up {count} events older than {retention}d")
        return count
