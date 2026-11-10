"""A reminder cancelled after the checker read it must not fire (#264 race).

`check_due_reminders` reads the due list, then delivers one by one, and delivery
commits in between. Before the claim, a cancel landing after the read was
overwritten by `mark_fired` and the cancelled text was spoken anyway.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select, update

from models.database import REMINDER_CANCELLED, REMINDER_FIRED, REMINDER_PENDING, Reminder

pytestmark = [pytest.mark.database]


async def _due(db) -> int:
    row = Reminder(
        message="Ofen aus",
        trigger_at=datetime.now(UTC).replace(tzinfo=None) - timedelta(minutes=1),
        status=REMINDER_PENDING,
    )
    db.add(row)
    await db.commit()
    return row.id


async def _status(db, rid: int) -> tuple[str, datetime | None]:
    db.expire_all()
    row = (await db.execute(select(Reminder).where(Reminder.id == rid))).scalar_one()
    return row.status, row.fired_at


@pytest.fixture
def checker(db_session, monkeypatch):
    """Run the real checker on the test session with delivery mocked."""
    import services.reminder_service as rs

    @asynccontextmanager
    async def session():
        yield db_session

    monkeypatch.setattr(rs.settings, "proactive_reminders_enabled", True)

    async def _run(fire):
        with patch("services.database.AsyncSessionLocal", session), \
             patch("services.notification_service.NotificationService.process_webhook", fire):
            await rs.check_due_reminders()

    return _run


async def test_cancelled_after_the_read_stays_silent(db_session, checker, monkeypatch):
    from services.reminder_service import ReminderService

    rid = await _due(db_session)
    original = ReminderService.get_due_reminders

    async def read_then_cancel(self):
        due = await original(self)
        # The user's cancel lands between the checker's read and its delivery.
        await self.db.execute(
            update(Reminder).where(Reminder.id == rid).values(status=REMINDER_CANCELLED)
        )
        await self.db.commit()
        return due

    monkeypatch.setattr(ReminderService, "get_due_reminders", read_then_cancel)
    fire = AsyncMock(return_value={"notification_id": None})
    await checker(fire)

    fire.assert_not_awaited()
    assert (await _status(db_session, rid))[0] == REMINDER_CANCELLED


async def test_a_due_reminder_fires_once(db_session, checker):
    rid = await _due(db_session)
    fire = AsyncMock(return_value={"notification_id": None})
    await checker(fire)
    await checker(fire)

    assert fire.await_count == 1
    status, fired_at = await _status(db_session, rid)
    assert status == REMINDER_FIRED and fired_at is not None


async def test_failed_delivery_is_retried(db_session, checker):
    rid = await _due(db_session)
    await checker(AsyncMock(side_effect=RuntimeError("TTS down")))
    assert await _status(db_session, rid) == (REMINDER_PENDING, None)

    fire = AsyncMock(return_value={"notification_id": None})
    await checker(fire)
    fire.assert_awaited_once()
    assert (await _status(db_session, rid))[0] == REMINDER_FIRED


async def test_cancel_after_the_claim_reports_not_cancelled(db_session):
    """The other half: once claimed, a cancel must not claim success."""
    from services.reminder_service import ReminderService

    rid = await _due(db_session)
    service = ReminderService(db_session)
    assert await service.claim_for_firing(rid) is True
    assert await service.claim_for_firing(rid) is False
    assert await service.cancel(rid) is False
    assert (await _status(db_session, rid))[0] == REMINDER_FIRED
