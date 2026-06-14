"""
Presence Analytics — persist events and provide heatmap/prediction queries.

Hook handlers run fire-and-forget with their own DB session.
PresenceAnalyticsService accepts a caller-provided session (for routes/tests).
"""

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from loguru import logger
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from models.database import PresenceEvent, Room
from utils.config import settings
from ha_glue.utils.config import ha_glue_settings
from utils.hooks import register_hook


def _analytics_tz() -> ZoneInfo:
    """The configured local timezone for analytics bucketing (UTC fallback)."""
    name = ha_glue_settings.presence_analytics_timezone or "UTC"
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        logger.warning(f"Invalid presence_analytics_timezone {name!r}; falling back to UTC")
        return ZoneInfo("UTC")


def _to_local(created_at: datetime, tz: ZoneInfo) -> datetime:
    """Convert a naive-UTC stored timestamp to local wall-clock time.

    Presence events are stored as naive UTC; the heatmap/forecast bucket by
    hour and day-of-week, which must be in the user's local time or they read
    hours off by the UTC offset.
    """
    return created_at.replace(tzinfo=UTC).astimezone(tz)


def _local_dow(local_dt: datetime) -> int:
    """Day of week as 0=Sunday..6=Saturday (matches Postgres `dow` and the
    frontend's So/Mo/.../Sa day selector)."""
    return (local_dt.weekday() + 1) % 7

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
                satellite_id=kwargs.get("satellite_id"),
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

        stmt = (
            select(
                PresenceEvent.room_id,
                Room.name.label("room_name"),
                PresenceEvent.created_at,
            )
            .join(Room, Room.id == PresenceEvent.room_id)
            .where(
                PresenceEvent.event_type == "enter",
                PresenceEvent.created_at >= cutoff,
            )
        )

        if user_id is not None:
            stmt = stmt.where(PresenceEvent.user_id == user_id)

        rows = (await self.db.execute(stmt)).all()

        # Bucket by LOCAL hour (events are stored UTC) — in Python so this works
        # on both Postgres and the sqlite test backend.
        tz = _analytics_tz()
        counts: dict[tuple[int, int], int] = {}
        room_names: dict[int, str] = {}
        for row in rows:
            if row.created_at is None:
                continue
            hour = _to_local(row.created_at, tz).hour
            room_names[row.room_id] = row.room_name
            key = (row.room_id, hour)
            counts[key] = counts.get(key, 0) + 1

        out = [
            {"room_id": rid, "room_name": room_names[rid], "hour": hour, "count": count}
            for (rid, hour), count in counts.items()
        ]
        out.sort(key=lambda d: (d["room_id"], d["hour"]))
        return out

    async def get_predictions(
        self, user_id: int, days: int = 60
    ) -> list[dict]:
        """
        Per-user probability of being in each room by day-of-week and hour.

        Returns list of {room_id, room_name, day_of_week (0=Sun), hour, probability}.
        Entries with probability < 0.10 are excluded.
        """
        cutoff = datetime.now(UTC).replace(tzinfo=None) - timedelta(days=days)

        # Total distinct weeks in the data range
        total_weeks = max(days / 7, 1)

        stmt = (
            select(
                PresenceEvent.room_id,
                Room.name.label("room_name"),
                PresenceEvent.created_at,
            )
            .join(Room, Room.id == PresenceEvent.room_id)
            .where(
                PresenceEvent.user_id == user_id,
                PresenceEvent.event_type == "enter",
                PresenceEvent.created_at >= cutoff,
            )
        )

        rows = (await self.db.execute(stmt)).all()

        # Group by (room, LOCAL day-of-week, LOCAL hour) → distinct LOCAL dates,
        # in Python so hour/dow are in the user's timezone (events stored UTC).
        tz = _analytics_tz()
        buckets: dict[tuple[int, int, int], set] = {}
        room_names: dict[int, str] = {}
        for row in rows:
            if row.created_at is None:
                continue
            local = _to_local(row.created_at, tz)
            key = (row.room_id, _local_dow(local), local.hour)
            buckets.setdefault(key, set()).add(local.date())
            room_names[row.room_id] = row.room_name

        predictions = []
        for (room_id, dow, hour), dates in buckets.items():
            probability = round(len(dates) / total_weeks, 2)
            if probability < 0.10:
                continue
            predictions.append({
                "room_id": room_id,
                "room_name": room_names[room_id],
                "day_of_week": dow,
                "hour": hour,
                "probability": probability,
            })

        return sorted(predictions, key=lambda p: (-p["probability"], p["hour"]))

    async def get_daily_summary(self, days: int = 7) -> list[dict]:
        """
        Daily enter/leave counts.

        Returns list of {date, enter_count, leave_count}.
        """
        cutoff = datetime.now(UTC).replace(tzinfo=None) - timedelta(days=days)

        stmt = (
            select(PresenceEvent.event_type, PresenceEvent.created_at)
            .where(PresenceEvent.created_at >= cutoff)
        )

        rows = (await self.db.execute(stmt)).all()

        # Bucket by LOCAL date (events stored UTC) so a late-evening event near
        # the UTC/local midnight boundary lands on the correct local day.
        tz = _analytics_tz()
        by_date: dict[str, list[int]] = {}
        for row in rows:
            if row.created_at is None:
                continue
            day = str(_to_local(row.created_at, tz).date())
            entry = by_date.setdefault(day, [0, 0])
            if row.event_type == "enter":
                entry[0] += 1
            elif row.event_type == "leave":
                entry[1] += 1

        return [
            {"date": day, "enter_count": enter, "leave_count": leave}
            for day, (enter, leave) in sorted(by_date.items())
        ]

    async def get_timeline(
        self,
        user_id: int,
        since: datetime | None = None,
        until: datetime | None = None,
        room_id: int | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict]:
        """
        Chronological presence-event timeline for a single user.

        Returns rows oldest-first as dicts with all event fields plus the joined
        room_name. ``since``/``until`` bound created_at (naive UTC); ``room_id``
        restricts to one room. ``limit``/``offset`` page the result.
        """
        stmt = (
            select(
                PresenceEvent.id,
                PresenceEvent.user_id,
                PresenceEvent.room_id,
                Room.name.label("room_name"),
                PresenceEvent.event_type,
                PresenceEvent.source,
                PresenceEvent.confidence,
                PresenceEvent.satellite_id,
                PresenceEvent.created_at,
            )
            .join(Room, Room.id == PresenceEvent.room_id)
            .where(PresenceEvent.user_id == user_id)
        )

        if since is not None:
            stmt = stmt.where(PresenceEvent.created_at >= since)
        if until is not None:
            stmt = stmt.where(PresenceEvent.created_at <= until)
        if room_id is not None:
            stmt = stmt.where(PresenceEvent.room_id == room_id)

        stmt = (
            stmt.order_by(PresenceEvent.created_at.asc(), PresenceEvent.id.asc())
            .limit(limit)
            .offset(offset)
        )

        rows = (await self.db.execute(stmt)).all()
        return [
            {
                "id": r.id,
                "user_id": r.user_id,
                "room_id": r.room_id,
                "room_name": r.room_name,
                "event_type": r.event_type,
                "source": r.source,
                "confidence": r.confidence,
                "satellite_id": r.satellite_id,
                "created_at": r.created_at,
            }
            for r in rows
        ]

    async def get_last_seen_by_room(self, user_id: int) -> list[dict]:
        """
        Most recent 'enter' time for the user in each room.

        Returns list of {room_id, room_name, last_seen} ordered most-recent
        first. 'leave' events are ignored. Empty list when the user has no
        enter events.
        """
        from sqlalchemy import func

        stmt = (
            select(
                PresenceEvent.room_id,
                Room.name.label("room_name"),
                func.max(PresenceEvent.created_at).label("last_seen"),
            )
            .join(Room, Room.id == PresenceEvent.room_id)
            .where(
                PresenceEvent.user_id == user_id,
                PresenceEvent.event_type == "enter",
            )
            .group_by(PresenceEvent.room_id, Room.name)
        )

        rows = (await self.db.execute(stmt)).all()
        out = [
            {
                "room_id": r.room_id,
                "room_name": r.room_name,
                "last_seen": r.last_seen,
            }
            for r in rows
            if r.last_seen is not None
        ]
        out.sort(key=lambda d: d["last_seen"], reverse=True)
        return out

    async def get_room_occupancy_window(
        self, room_id: int, since: datetime, until: datetime
    ) -> list[dict]:
        """
        All users' enter/leave events for one room within a time window.

        Returns rows chronological (oldest-first) so a caller can reconstruct
        who was in the room and when over the window.
        """
        stmt = (
            select(
                PresenceEvent.id,
                PresenceEvent.user_id,
                PresenceEvent.room_id,
                Room.name.label("room_name"),
                PresenceEvent.event_type,
                PresenceEvent.source,
                PresenceEvent.confidence,
                PresenceEvent.satellite_id,
                PresenceEvent.created_at,
            )
            .join(Room, Room.id == PresenceEvent.room_id)
            .where(
                PresenceEvent.room_id == room_id,
                PresenceEvent.created_at >= since,
                PresenceEvent.created_at <= until,
            )
            .order_by(PresenceEvent.created_at.asc(), PresenceEvent.id.asc())
        )

        rows = (await self.db.execute(stmt)).all()
        return [
            {
                "id": r.id,
                "user_id": r.user_id,
                "room_id": r.room_id,
                "room_name": r.room_name,
                "event_type": r.event_type,
                "source": r.source,
                "confidence": r.confidence,
                "satellite_id": r.satellite_id,
                "created_at": r.created_at,
            }
            for r in rows
        ]

    async def cleanup_old_events(self, retention_days: int | None = None) -> int:
        """Delete events older than retention_days. Returns count deleted."""
        retention = retention_days or ha_glue_settings.presence_analytics_retention_days
        cutoff = datetime.now(UTC).replace(tzinfo=None) - timedelta(days=retention)

        result = await self.db.execute(
            delete(PresenceEvent).where(PresenceEvent.created_at < cutoff)
        )
        await self.db.commit()
        count = result.rowcount
        if count > 0:
            logger.info(f"Presence analytics: cleaned up {count} events older than {retention}d")
        return count
