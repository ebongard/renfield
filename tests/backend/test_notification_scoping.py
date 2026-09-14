"""Notifications, reminders and suppression rules are scoped to their owner.

Finding, 2026-09-14: on an auth-on instance every authenticated user could list,
acknowledge, dismiss and build suppression rules from EVERY notification —
including `privacy="personal"` ones addressed to someone else — list or cancel
everyone's reminders, and read or remove anyone's suppression rules. The presence
gate only protected the live WS push. The pre-merge review then found two more
doors: the browser toast acknowledges over the device WebSocket (not REST), and a
fired reminder became a PUBLIC notification. Auth-off installs and admins are
unaffected.
"""
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI, HTTPException
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from models.database import (
    NOTIFICATION_ACKNOWLEDGED,
    NOTIFICATION_DELIVERED,
    NOTIFICATION_DISMISSED,
    REMINDER_CANCELLED,
    REMINDER_PENDING,
    Notification,
    NotificationSuppression,
    Reminder,
    Role,
    User,
)

pytestmark = [pytest.mark.database]


async def _user(db: AsyncSession, name: str, permissions: list[str]) -> User:
    role = Role(name=f"role-{name}", description=name, permissions=permissions, is_system=False)
    db.add(role)
    await db.commit()
    await db.refresh(role)
    user = User(role_id=role.id, username=name, email=f"{name}@example.com",
                password_hash="$2b$12$fakefakefakefakefakefake", is_active=True)
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


@pytest.fixture
async def people(db_session: AsyncSession) -> dict[str, User]:
    return {
        "alice": await _user(db_session, "alice", ["notifications.view"]),
        "bob": await _user(db_session, "bob", ["notifications.view"]),
        "admin": await _user(db_session, "admin", ["admin"]),
        "manager": await _user(db_session, "manager", ["notifications.manage"]),
    }


@pytest.fixture
async def notifications(db_session: AsyncSession, people) -> dict[str, Notification]:
    def make(title, privacy, target):
        return Notification(event_type="test", title=title, message=title, urgency="info",
                            source="test", status=NOTIFICATION_DELIVERED, privacy=privacy,
                            target_user_id=target, delivered_at=datetime.utcnow())

    rows = {
        "broadcast": make("Waschmaschine fertig", "public", None),
        "legacy_null_privacy": make("Alte Meldung", None, None),
        "for_alice": make("Frist Steuerbescheid", "personal", people["alice"].id),
        "for_bob": make("Arzttermin", "personal", people["bob"].id),
        "personal_no_target": make("Kontostand", "personal", None),
        "confidential_no_target": make("Vertraulich", "confidential", None),
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


# --- visibility rule ---------------------------------------------------------------

async def test_a_member_lists_public_broadcasts_and_their_own(service, people, notifications):
    listed = await service.list_notifications(viewer_id=people["alice"].id, restrict_to_viewer=True)
    assert {n.title for n in listed} == {"Waschmaschine fertig", "Alte Meldung", "Frist Steuerbescheid"}


async def test_list_and_fetch_by_id_agree_for_every_row(service, people, notifications):
    """The SQL clause and the Python check are one rule — nothing the list hides
    may be reachable by id, and vice versa."""
    for viewer in (people["alice"].id, people["bob"].id, None):
        listed = {n.id for n in await service.list_notifications(
            viewer_id=viewer, restrict_to_viewer=True)}
        for row in notifications.values():
            fetched = await service.get_notification(row.id, viewer_id=viewer, restrict_to_viewer=True)
            assert (row.id in listed) == (fetched is not None), (viewer, row.title)


async def test_no_viewer_sees_only_public_untargeted(service, notifications):
    """`target_user_id == None` compiles to IS NULL — the clause must not hand out
    personal untargeted rows to a caller without a user."""
    listed = await service.list_notifications(viewer_id=None, restrict_to_viewer=True)
    assert {n.title for n in listed} == {"Waschmaschine fertig", "Alte Meldung"}


async def test_unrestricted_listing_is_unchanged(service, notifications):
    assert len(await service.list_notifications()) == len(notifications)


# --- actions by id -------------------------------------------------------------------

@pytest.mark.parametrize("row, allowed", [
    ("broadcast", True), ("for_alice", True), ("for_bob", False),
    ("personal_no_target", False), ("confidential_no_target", False),
])
async def test_acknowledge_follows_visibility(service, people, notifications, db_session, row, allowed):
    target = notifications[row]
    done = await service.acknowledge(target.id, acknowledged_by="alice",
                                     viewer_id=people["alice"].id, restrict_to_viewer=True)
    await db_session.refresh(target)
    assert done is allowed
    assert target.status == (NOTIFICATION_ACKNOWLEDGED if allowed else NOTIFICATION_DELIVERED)


@pytest.mark.parametrize("row, allowed", [("broadcast", True), ("for_bob", False)])
async def test_dismiss_follows_visibility(service, people, notifications, db_session, row, allowed):
    target = notifications[row]
    done = await service.dismiss(target.id, viewer_id=people["alice"].id, restrict_to_viewer=True)
    await db_session.refresh(target)
    assert done is allowed
    assert target.status == (NOTIFICATION_DISMISSED if allowed else NOTIFICATION_DELIVERED)


async def test_suppression_only_from_a_visible_notification(service, people, notifications):
    alice = people["alice"].id
    with patch.object(service, "_get_embedding", AsyncMock(return_value=None)):
        assert await service.suppress_similar(notifications["for_bob"].id, user_id=alice,
                                              restrict_to_viewer=True) is None
        own = await service.suppress_similar(notifications["for_alice"].id, user_id=alice,
                                             restrict_to_viewer=True)
    assert own is not None and own.user_id == alice


# --- suppression rules -----------------------------------------------------------------

@pytest.fixture
async def rules(db_session: AsyncSession, people) -> dict[str, NotificationSuppression]:
    rows = {
        "alice": NotificationSuppression(event_pattern="a", user_id=people["alice"].id,
                                         reason="nervt", is_active=True),
        "bob": NotificationSuppression(event_pattern="b", user_id=people["bob"].id,
                                       reason="privat", is_active=True),
        "global": NotificationSuppression(event_pattern="g", user_id=None, is_active=True),
    }
    db_session.add_all(rows.values())
    await db_session.commit()
    for row in rows.values():
        await db_session.refresh(row)
    return rows


async def test_a_member_lists_own_and_global_rules(service, people, rules):
    listed = await service.list_suppressions(viewer_id=people["alice"].id, restrict_to_viewer=True)
    assert {r.event_pattern for r in listed} == {"a", "g"}


async def test_a_member_removes_only_their_own_rule(service, people, rules):
    alice = people["alice"].id
    assert await service.delete_suppression(rules["bob"].id, viewer_id=alice, restrict_to_viewer=True) is False
    assert await service.delete_suppression(rules["global"].id, viewer_id=alice, restrict_to_viewer=True) is False
    assert await service.delete_suppression(rules["alice"].id, viewer_id=alice, restrict_to_viewer=True) is True


# --- reminders -----------------------------------------------------------------------

@pytest.fixture
async def reminders(db_session: AsyncSession, people) -> dict[str, Reminder]:
    soon = datetime.utcnow() + timedelta(hours=1)
    rows = {
        "alice": Reminder(message="Müll rausbringen", trigger_at=soon, user_id=people["alice"].id,
                          status=REMINDER_PENDING),
        "bob": Reminder(message="Medikament nehmen", trigger_at=soon, user_id=people["bob"].id,
                        status=REMINDER_PENDING),
    }
    db_session.add_all(rows.values())
    await db_session.commit()
    for row in rows.values():
        await db_session.refresh(row)
    return rows


async def test_a_member_lists_only_their_own_reminders(db_session, people, reminders):
    from services.reminder_service import ReminderService

    service = ReminderService(db_session)
    listed = await service.list_pending(user_id=people["alice"].id, restrict_to_user=True)
    assert [r.message for r in listed] == ["Müll rausbringen"]
    assert len(await service.list_pending()) == 2


async def test_someone_elses_reminder_cannot_be_cancelled(db_session, people, reminders):
    from services.reminder_service import ReminderService

    service = ReminderService(db_session)
    alice = people["alice"].id
    assert await service.cancel(reminders["bob"].id, user_id=alice, restrict_to_user=True) is False
    await db_session.refresh(reminders["bob"])
    assert reminders["bob"].status == REMINDER_PENDING
    assert await service.cancel(reminders["alice"].id, user_id=alice, restrict_to_user=True)
    await db_session.refresh(reminders["alice"])
    assert reminders["alice"].status == REMINDER_CANCELLED


async def test_a_fired_reminder_is_a_personal_notification_for_its_owner(db_session, people, monkeypatch):
    """Review finding: fired as public-without-recipient, the reminder text was
    listed to every user once it fired."""
    import services.reminder_service as rs

    due = Reminder(message="Medikament nehmen", trigger_at=datetime.utcnow() - timedelta(minutes=1),
                   user_id=people["bob"].id, status=REMINDER_PENDING)
    db_session.add(due)
    await db_session.commit()

    @asynccontextmanager
    async def session():
        yield db_session

    monkeypatch.setattr(rs.settings, "proactive_reminders_enabled", True)
    fire = AsyncMock(return_value={"notification_id": None})
    with patch("services.database.AsyncSessionLocal", session), \
         patch("services.notification_service.NotificationService.process_webhook", fire):
        await rs.check_due_reminders()

    kwargs = fire.await_args.kwargs
    assert kwargs["privacy"] == "personal" and kwargs["target_user_id"] == people["bob"].id


# --- admin rule (real roles, not mocks) ------------------------------------------------

async def test_admin_and_manager_roles_are_unrestricted_members_are_not(db_session, people):
    from services.notification_service import NotificationService

    reloaded = {}
    for name, user in people.items():
        from sqlalchemy import select
        from sqlalchemy.orm import selectinload

        reloaded[name] = (await db_session.execute(
            select(User).options(selectinload(User.role)).where(User.id == user.id))).scalar_one()
    assert NotificationService.viewer_is_unrestricted(reloaded["admin"]) is True
    assert NotificationService.viewer_is_unrestricted(reloaded["manager"]) is True
    assert NotificationService.viewer_is_unrestricted(reloaded["alice"]) is False


# --- device WebSocket ack ---------------------------------------------------------------

async def test_ws_scope_for_each_kind_of_connection(service, people):
    assert await service.resolve_ws_viewer_scope({"authenticated": True, "auth_skipped": True}) == (False, None)
    assert await service.resolve_ws_viewer_scope({"authenticated": True, "device_id": "tablet"}) == (True, None)
    alice = people["alice"].id
    assert await service.resolve_ws_viewer_scope({"user_id": alice, "auth_method": "jwt"}) == (True, alice)
    admin = people["admin"].id
    assert await service.resolve_ws_viewer_scope({"user_id": admin, "auth_method": "jwt"}) == (False, admin)


async def test_a_device_token_cannot_ack_a_personal_notification(service, notifications, db_session):
    restrict, viewer = await service.resolve_ws_viewer_scope({"device_id": "tablet"})
    target = notifications["for_bob"]
    assert await service.dismiss(target.id, viewer_id=viewer, restrict_to_viewer=restrict) is False
    await db_session.refresh(target)
    assert target.status == NOTIFICATION_DELIVERED


# --- REST routes ---------------------------------------------------------------------------

@pytest.fixture
async def client_as(db_session: AsyncSession, monkeypatch):
    from slowapi.errors import RateLimitExceeded

    from api.routes import notifications as route
    from services.api_rate_limiter import limiter, rate_limit_exceeded_handler
    from services.auth_service import get_current_user
    from services.database import get_db

    monkeypatch.setattr(route.settings, "auth_enabled", True)

    @asynccontextmanager
    async def make(user: User | None):
        app = FastAPI()
        app.state.limiter = limiter
        app.add_exception_handler(RateLimitExceeded, rate_limit_exceeded_handler)
        app.include_router(route.router, prefix="/api/notifications")

        async def _db():
            yield db_session

        loaded = None
        if user is not None:
            from sqlalchemy import select
            from sqlalchemy.orm import selectinload

            loaded = (await db_session.execute(
                select(User).options(selectinload(User.role)).where(User.id == user.id))).scalar_one()
        app.dependency_overrides[get_db] = _db
        app.dependency_overrides[get_current_user] = lambda: loaded
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            yield c

    return make


async def test_route_lists_only_what_the_member_may_see(client_as, people, notifications):
    async with client_as(people["alice"]) as client:
        resp = await client.get("/api/notifications")
    assert resp.status_code == 200
    titles = {n["title"] for n in resp.json()["notifications"]}
    assert "Arzttermin" not in titles and "Frist Steuerbescheid" in titles


async def test_route_admin_lists_everything(client_as, people, notifications):
    async with client_as(people["admin"]) as client:
        resp = await client.get("/api/notifications")
    assert len(resp.json()["notifications"]) == len(notifications)


async def test_route_someone_elses_notification_is_404(client_as, people, notifications):
    other = notifications["for_bob"].id
    async with client_as(people["alice"]) as client:
        ack = await client.patch(f"/api/notifications/{other}/acknowledge")
        dismiss = await client.delete(f"/api/notifications/{other}")
    assert ack.status_code == 404 and dismiss.status_code == 404


async def test_route_someone_elses_reminder_is_404(client_as, people, reminders):
    async with client_as(people["alice"]) as client:
        listed = await client.get("/api/notifications/reminders")
        cancel = await client.delete(f"/api/notifications/reminders/{reminders['bob'].id}")
    assert [r["message"] for r in listed.json()["reminders"]] == ["Müll rausbringen"]
    assert cancel.status_code == 404


async def test_route_refuses_anonymous_callers(client_as):
    async with client_as(None) as client:
        resp = await client.get("/api/notifications")
    assert resp.status_code == 401


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
