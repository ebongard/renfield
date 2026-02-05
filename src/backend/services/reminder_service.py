"""
Reminder Service — Timer-basierte Erinnerungen

Parst relative Zeitangaben ("in 30 Minuten", "in 2 Stunden", "um 18:00")
und erstellt Reminder-Einträge. Background-Loop prüft periodisch auf
fällige Reminders und liefert sie als Notifications aus.
"""

import re
from datetime import datetime, timedelta

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.database import REMINDER_CANCELLED, REMINDER_FIRED, REMINDER_PENDING, Reminder
from utils.config import settings


class ReminderService:
    """CRUD + duration parsing for reminders."""

    def __init__(self, db: AsyncSession):
        self.db = db

    # ------------------------------------------------------------------
    # Duration Parsing
    # ------------------------------------------------------------------

    @staticmethod
    def parse_duration(text: str) -> timedelta | datetime | None:
        """
        Parse a relative duration or absolute time from text.

        Supported patterns:
        - "in 30 Minuten" / "in 30 minutes"
        - "in 2 Stunden" / "in 2 hours"
        - "in 1 Stunde" / "in 1 hour"
        - "um 18:00" / "at 18:00"
        - ISO datetime strings

        Returns timedelta for relative, datetime for absolute, None if unparseable.
        """
        text = text.strip()

        # Try ISO datetime first
        try:
            return datetime.fromisoformat(text)
        except ValueError:
            pass

        # Relative: "in X Minuten/minutes"
        m = re.match(
            r"in\s+(\d+)\s+(Minuten?|minutes?|min)",
            text,
            re.IGNORECASE,
        )
        if m:
            return timedelta(minutes=int(m.group(1)))

        # Relative: "in X Stunden/hours"
        m = re.match(
            r"in\s+(\d+)\s+(Stunden?|hours?|hrs?|h)",
            text,
            re.IGNORECASE,
        )
        if m:
            return timedelta(hours=int(m.group(1)))

        # Relative: "in X Sekunden/seconds"
        m = re.match(
            r"in\s+(\d+)\s+(Sekunden?|seconds?|sec|s)",
            text,
            re.IGNORECASE,
        )
        if m:
            return timedelta(seconds=int(m.group(1)))

        # Absolute: "um HH:MM" / "at HH:MM"
        m = re.match(
            r"(?:um|at)\s+(\d{1,2}):(\d{2})",
            text,
            re.IGNORECASE,
        )
        if m:
            hour, minute = int(m.group(1)), int(m.group(2))
            now = datetime.utcnow()
            target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            # If time already passed today, schedule for tomorrow
            if target <= now:
                target += timedelta(days=1)
            return target

        return None

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------

    async def create_reminder(
        self,
        message: str,
        trigger_at_str: str,
        room: str | None = None,
        user_id: int | None = None,
        session_id: str | None = None,
    ) -> Reminder:
        """Create a new reminder. Parses trigger_at_str as relative or absolute."""
        parsed = self.parse_duration(trigger_at_str)
        if parsed is None:
            raise ValueError(f"Could not parse trigger time: '{trigger_at_str}'")

        if isinstance(parsed, timedelta):
            trigger_at = datetime.utcnow() + parsed
        else:
            trigger_at = parsed

        if trigger_at <= datetime.utcnow():
            raise ValueError("Trigger time must be in the future")

        # Resolve room name
        room_id = None
        room_name = room
        if room:
            from models.database import Room
            result = await self.db.execute(
                select(Room).where(Room.name == room)
            )
            room_obj = result.scalar_one_or_none()
            if room_obj:
                room_id = room_obj.id
                room_name = room_obj.name

        reminder = Reminder(
            message=message,
            trigger_at=trigger_at,
            room_id=room_id,
            room_name=room_name,
            user_id=user_id,
            session_id=session_id,
            status=REMINDER_PENDING,
        )
        self.db.add(reminder)
        await self.db.commit()
        await self.db.refresh(reminder)

        logger.info(f"⏰ Reminder #{reminder.id} erstellt: '{message}' trigger={trigger_at}")
        return reminder

    async def list_pending(self) -> list[Reminder]:
        """List all pending reminders."""
        result = await self.db.execute(
            select(Reminder)
            .where(Reminder.status == REMINDER_PENDING)
            .order_by(Reminder.trigger_at)
        )
        return list(result.scalars().all())

    async def cancel(self, reminder_id: int) -> bool:
        """Cancel a pending reminder."""
        result = await self.db.execute(
            select(Reminder).where(
                Reminder.id == reminder_id,
                Reminder.status == REMINDER_PENDING,
            )
        )
        reminder = result.scalar_one_or_none()
        if not reminder:
            return False

        reminder.status = REMINDER_CANCELLED
        await self.db.commit()
        return True

    async def get_due_reminders(self) -> list[Reminder]:
        """Get all reminders that are past their trigger time and still pending."""
        now = datetime.utcnow()
        result = await self.db.execute(
            select(Reminder).where(
                Reminder.status == REMINDER_PENDING,
                Reminder.trigger_at <= now,
            )
        )
        return list(result.scalars().all())

    async def mark_fired(self, reminder_id: int, notification_id: int | None = None) -> None:
        """Mark a reminder as fired."""
        result = await self.db.execute(
            select(Reminder).where(Reminder.id == reminder_id)
        )
        reminder = result.scalar_one_or_none()
        if reminder:
            reminder.status = REMINDER_FIRED
            reminder.fired_at = datetime.utcnow()
            if notification_id:
                reminder.notification_id = notification_id
            await self.db.commit()


async def check_due_reminders():
    """Check for and fire due reminders. Called periodically by lifecycle."""
    if not settings.proactive_reminders_enabled:
        return

    from services.database import AsyncSessionLocal
    from services.notification_service import NotificationService

    try:
        async with AsyncSessionLocal() as db:
            service = ReminderService(db)
            due = await service.get_due_reminders()

            for reminder in due:
                try:
                    notification_service = NotificationService(db)
                    result = await notification_service.process_webhook(
                        event_type="reminder.fired",
                        title="Erinnerung",
                        message=reminder.message,
                        urgency="info",
                        room=reminder.room_name,
                        tts=True,
                    )
                    await service.mark_fired(
                        reminder.id,
                        notification_id=result.get("notification_id"),
                    )
                    logger.info(f"⏰ Reminder #{reminder.id} fired")
                except Exception as e:
                    logger.warning(f"⚠️ Failed to fire reminder #{reminder.id}: {e}")
    except Exception as e:
        logger.warning(f"⚠️ Reminder check failed: {e}")
