"""Notifications and reminders are scoped to their recipient.

Finding, 2026-09-14: on an auth-on instance every authenticated user could list,
acknowledge, dismiss and build suppression rules from EVERY notification —
including `privacy="personal"` ones addressed to someone else — and list or cancel
everyone's reminders. The presence gate only protected the live WS push; the REST
list was the leak. Auth-off installs (one household) and admins are unaffected.
"""
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from models.database import (
    NOTIFICATION_ACKNOWLEDGED,
    NOTIFICATION_DELIVERED,
    NOTIFICATION_DISMISSED,
    REMINDER_CANCELLED,
    REMINDER_PENDING,
    Notification,
    Reminder,
    Role,
    User,
)

pytestmark = [pytest.mark.database]


@pytest.fixture
async def two_users(db_session: AsyncSession) -> tuple[User, User]:
    role = Role(name="member", description="member", permissions=["kb.own"], is_system=False)
    db_session.add(role)
    await db_session.commit()
    await db_session.refresh(role)
    users = []
    for name in ("alice", "bob"):
        user = User(role_id=role.id, username=name, email=f"{name}@example.com",
                    password_hash="$2b$12$fakefakefakefakefakefake", is_active=True)
        db_session.add(user)
        users.append(user)
    await db_session.commit()
    for user in users:
        await db_session.refresh(user)
    return users[0], users[1]


@pytest.fixture
async def notifications(db_session: AsyncSession, two_users) -> dict[str, Notification]:
    alice, bob = two_users

    def make(title, privacy, target):
        return Notification(event_type="test", title=title, message=title, urgency="info",
                            source="test", status=NOTIFICATION_DELIVERED, privacy=privacy,
                            target_user_id=target, delivered_at=datetime.utcnow())

    rows = {
        "broadcast": make("Waschmaschine fertig", "public", None),
        "for_alice": make("Frist Steuerbescheid", "personal", alice.id),
        "for_bob": make("Arzttermin", "personal", bob.id),
        "personal_no_target": make("Kontostand", "personal", None),
    }
    db_session.add_all(rows.values())
    await db_session.commit()
    for row in rows.values():
        await db_session.refresh(row)
    return rows


@pytest.fixture
def service(db_session: AsyncSession):
    from services.notification_service import NotificationService

    return NotificationService(db_session)


# --- visibility rule -------------------------------------------------------------

async def test_a_user_lists_public_broadcasts_and_their_own(service, two_users, notifications):
    alice, _ = two_users
    listed = await service.list_notifications(viewer_id=alice.id, restrict_to_viewer=True)
    assert {n.title for n in listed} == {"Waschmaschine fertig", "Frist Steuerbescheid"}


async def test_a_personal_notification_without_a_recipient_stays_hidden(service, two_users, notifications):
    _, bob = two_users
    listed = await service.list_notifications(viewer_id=bob.id, restrict_to_viewer=True)
    assert "Kontostand" not in {n.title for n in listed}


async def test_unrestricted_listing_is_unchanged(service, notifications):
    """Auth-off households and admins see everything, exactly as before."""
    listed = await service.list_notifications()
    assert len(listed) == 4


# --- actions by id ----------------------------------------------------------------

async def test_someone_elses_notification_cannot_be_acknowledged(service, two_users, notifications, db_session):
    alice, _ = two_users
    target = notifications["for_bob"]

    assert await service.acknowledge(target.id, acknowledged_by="alice",
                                     viewer_id=alice.id, restrict_to_viewer=True) is False
    await db_session.refresh(target)
    assert target.status == NOTIFICATION_DELIVERED


async def test_someone_elses_notification_cannot_be_dismissed(service, two_users, notifications, db_session):
    alice, _ = two_users
    target = notifications["for_bob"]

    assert await service.dismiss(target.id, viewer_id=alice.id, restrict_to_viewer=True) is False
    await db_session.refresh(target)
    assert target.status == NOTIFICATION_DELIVERED


async def test_own_and_broadcast_notifications_can_be_acted_on(service, two_users, notifications, db_session):
    alice, _ = two_users
    own, broadcast = notifications["for_alice"], notifications["broadcast"]

    assert await service.acknowledge(own.id, viewer_id=alice.id, restrict_to_viewer=True)
    assert await service.dismiss(broadcast.id, viewer_id=alice.id, restrict_to_viewer=True)
    await db_session.refresh(own)
    await db_session.refresh(broadcast)
    assert own.status == NOTIFICATION_ACKNOWLEDGED
    assert broadcast.status == NOTIFICATION_DISMISSED


async def test_no_suppression_rule_from_someone_elses_notification(service, two_users, notifications):
    """The rule would carry the other person's notification into the caller's view."""
    alice, _ = two_users
    rule = await service.suppress_similar(notifications["for_bob"].id, user_id=alice.id,
                                          restrict_to_viewer=True)
    assert rule is None


async def test_hidden_and_missing_are_indistinguishable(service, two_users, notifications):
    alice, _ = two_users
    hidden = await service.get_notification(notifications["for_bob"].id, viewer_id=alice.id,
                                            restrict_to_viewer=True)
    missing = await service.get_notification(999_999, viewer_id=alice.id, restrict_to_viewer=True)
    assert hidden is None and missing is None


# --- reminders -------------------------------------------------------------------

@pytest.fixture
async def reminders(db_session: AsyncSession, two_users) -> dict[str, Reminder]:
    alice, bob = two_users
    soon = datetime.utcnow() + timedelta(hours=1)
    rows = {
        "alice": Reminder(message="Müll rausbringen", trigger_at=soon, user_id=alice.id,
                          status=REMINDER_PENDING),
        "bob": Reminder(message="Medikament nehmen", trigger_at=soon, user_id=bob.id,
                        status=REMINDER_PENDING),
    }
    db_session.add_all(rows.values())
    await db_session.commit()
    for row in rows.values():
        await db_session.refresh(row)
    return rows


async def test_a_user_lists_only_their_own_reminders(db_session, two_users, reminders):
    from services.reminder_service import ReminderService

    alice, _ = two_users
    listed = await ReminderService(db_session).list_pending(user_id=alice.id, restrict_to_user=True)
    assert [r.message for r in listed] == ["Müll rausbringen"]
    assert len(await ReminderService(db_session).list_pending()) == 2


async def test_someone_elses_reminder_cannot_be_cancelled(db_session, two_users, reminders):
    from services.reminder_service import ReminderService

    alice, _ = two_users
    service = ReminderService(db_session)
    bobs = reminders["bob"]

    assert await service.cancel(bobs.id, user_id=alice.id, restrict_to_user=True) is False
    await db_session.refresh(bobs)
    assert bobs.status == REMINDER_PENDING

    assert await service.cancel(reminders["alice"].id, user_id=alice.id, restrict_to_user=True)
    await db_session.refresh(reminders["alice"])
    assert reminders["alice"].status == REMINDER_CANCELLED


# --- route scope -------------------------------------------------------------------

def _user(user_id, *, manage=False):
    return SimpleNamespace(id=user_id, username=f"u{user_id}",
                           has_permission=lambda perm: manage and perm == "notifications.manage")


def test_scope_is_open_when_auth_is_off(monkeypatch):
    from api.routes import notifications as route

    monkeypatch.setattr(route.settings, "auth_enabled", False)
    assert route._viewer_scope(None) == (False, None)


def test_scope_refuses_anonymous_when_auth_is_on(monkeypatch):
    from api.routes import notifications as route

    monkeypatch.setattr(route.settings, "auth_enabled", True)
    with pytest.raises(HTTPException) as exc:
        route._viewer_scope(None)
    assert exc.value.status_code == 401


def test_scope_restricts_members_but_not_notification_managers(monkeypatch):
    from api.routes import notifications as route

    monkeypatch.setattr(route.settings, "auth_enabled", True)
    assert route._viewer_scope(_user(7)) == (True, 7)
    assert route._viewer_scope(_user(1, manage=True)) == (False, 1)
