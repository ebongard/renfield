"""Notification bell (#1653): unread count, "mark all read", `mine` filter and the
content-free `notifications_changed` user event.

The bell is ALWAYS viewer-scoped -- an admin's badge counts their own notifications
plus public ones, never everyone's personal calls.
"""
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from models.database import (
    NOTIFICATION_ACKNOWLEDGED,
    NOTIFICATION_DELIVERED,
    NOTIFICATION_DISMISSED,
    Notification,
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
    }


@pytest.fixture
async def rows(db_session: AsyncSession, people) -> dict[str, Notification]:
    now = datetime.utcnow()

    def make(title, privacy, target, status=NOTIFICATION_DELIVERED, expires_at=None):
        return Notification(event_type="test", title=title, message=title, urgency="info",
                            source="test", status=status, privacy=privacy,
                            target_user_id=target, delivered_at=now, expires_at=expires_at)

    alice = people["alice"].id
    data = {
        "broadcast": make("Waschmaschine fertig", "public", None),
        "legacy_null_privacy": make("Alte Meldung", None, None),
        "for_alice": make("Anruf", "personal", alice),
        "for_alice_read": make("Gelesen", "personal", alice, status=NOTIFICATION_ACKNOWLEDGED),
        "for_alice_dismissed": make("Weg", "personal", alice, status=NOTIFICATION_DISMISSED),
        "for_alice_expired": make("Abgelaufen", "personal", alice, expires_at=now - timedelta(hours=1)),
        "for_bob": make("Arzttermin", "personal", people["bob"].id),
        "personal_no_target": make("Kontostand", "personal", None),
    }
    db_session.add_all(data.values())
    await db_session.commit()
    for row in data.values():
        await db_session.refresh(row)
    return data


@pytest.fixture
def bell_on(monkeypatch):
    from utils.config import settings

    monkeypatch.setattr(settings, "notification_bell_enabled", True)
    monkeypatch.setattr(settings, "auth_enabled", True)
    return settings


@pytest.fixture
def published(monkeypatch):
    """Capture every publish_user_event call instead of touching Redis."""
    import services.redis_client as redis_client
    import services.user_events as user_events

    calls = AsyncMock()
    monkeypatch.setattr(user_events, "publish_user_event", calls)
    monkeypatch.setattr(redis_client, "get_redis", lambda: object())
    return calls


def _targets(published) -> list:
    return [c.args[1] for c in published.await_args_list]


# --- service ------------------------------------------------------------------------------

async def test_count_unread_is_scoped_and_ignores_read_dismissed_expired(db_session, people, rows):
    from services.notification_service import NotificationService

    service = NotificationService(db_session)
    # alice: her unread call + the two public ones. Not bob's, not the untargeted personal one.
    assert await service.count_unread(people["alice"].id, restrict_to_viewer=True) == 3
    assert await service.count_unread(people["bob"].id, restrict_to_viewer=True) == 3
    # Unrestricted (auth off): every unread, unexpired row.
    assert await service.count_unread(None, restrict_to_viewer=False) == 5


async def test_acknowledge_all_touches_only_what_the_viewer_sees(db_session, people, rows, bell_on, published):
    from services.notification_service import NotificationService

    service = NotificationService(db_session)
    done = await service.acknowledge_all("alice", people["alice"].id, restrict_to_viewer=True)
    assert done == 3

    for name in ("for_alice", "broadcast", "legacy_null_privacy"):
        await db_session.refresh(rows[name])
        assert rows[name].status == NOTIFICATION_ACKNOWLEDGED
        assert rows[name].acknowledged_by == "alice"
    for name in ("for_bob", "personal_no_target"):
        await db_session.refresh(rows[name])
        assert rows[name].status == NOTIFICATION_DELIVERED
    assert await service.count_unread(people["alice"].id, restrict_to_viewer=True) == 0

    from services.user_events import EVENT_NOTIFICATIONS_CHANGED, EVERYONE

    assert set(_targets(published)) == {people["alice"].id, EVERYONE}
    assert all(c.args[2] == EVENT_NOTIFICATIONS_CHANGED for c in published.await_args_list)


async def test_acknowledge_all_with_nothing_unread_publishes_nothing(db_session, people, bell_on, published):
    from services.notification_service import NotificationService

    assert await NotificationService(db_session).acknowledge_all("bob", people["bob"].id, True) == 0
    published.assert_not_awaited()


# --- event targeting -------------------------------------------------------------------------

async def test_event_targets(bell_on, published):
    from services.notification_service import NotificationService
    from services.user_events import EVERYONE

    await NotificationService._emit_changed(7, "personal", "created")
    await NotificationService._emit_changed(None, "public", "created")
    await NotificationService._emit_changed(None, None, "created")
    await NotificationService._emit_changed(None, "personal", "created")
    assert _targets(published) == [7, EVERYONE, EVERYONE, None]
    # Content-free: only (redis, target, type, reason).
    assert all(len(c.args) == 4 and not c.kwargs for c in published.await_args_list)


async def test_event_auth_off_goes_to_household_bucket(bell_on, published):
    from services.notification_service import NotificationService

    bell_on.auth_enabled = False
    await NotificationService._emit_changed(7, "personal", "created")
    assert _targets(published) == [None]


@pytest.mark.parametrize("flag", ["notification_bell_enabled", "user_events_enabled"])
async def test_no_event_when_bell_or_user_events_off(bell_on, published, monkeypatch, flag):
    from services.notification_service import NotificationService

    monkeypatch.setattr(bell_on, flag, False)
    await NotificationService._emit_changed(7, "personal", "created")
    published.assert_not_awaited()


async def test_publish_failure_never_breaks_the_caller(bell_on, monkeypatch):
    import services.redis_client as redis_client
    from services.notification_service import NotificationService

    def boom():
        raise RuntimeError("redis down")

    monkeypatch.setattr(redis_client, "get_redis", boom)
    await NotificationService._emit_changed(7, "personal", "created")  # must not raise


async def test_single_acknowledge_and_dismiss_publish(db_session, people, rows, bell_on, published):
    from services.notification_service import NotificationService

    service = NotificationService(db_session)
    alice = people["alice"].id
    assert await service.acknowledge(rows["for_alice"].id, "alice", viewer_id=alice, restrict_to_viewer=True)
    assert await service.dismiss(rows["broadcast"].id, viewer_id=alice, restrict_to_viewer=True)
    from services.user_events import EVERYONE

    assert _targets(published) == [alice, EVERYONE]


# --- routes ----------------------------------------------------------------------------------

@pytest.fixture
async def client_as(db_session: AsyncSession, bell_on):
    from slowapi.errors import RateLimitExceeded

    from api.routes import notifications as route
    from services.api_rate_limiter import limiter, rate_limit_exceeded_handler
    from services.auth_service import get_current_user
    from services.database import get_db

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
            loaded = (await db_session.execute(
                select(User).options(selectinload(User.role)).where(User.id == user.id))).scalar_one()
        app.dependency_overrides[get_db] = _db
        app.dependency_overrides[get_current_user] = lambda: loaded
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            yield c

    return make


async def test_route_unread_count_member(client_as, people, rows):
    async with client_as(people["alice"]) as client:
        resp = await client.get("/api/notifications/unread-count")
    assert resp.status_code == 200 and resp.json() == {"count": 3}


async def test_route_unread_count_admin_is_scoped_too(client_as, people, rows):
    async with client_as(people["admin"]) as client:
        resp = await client.get("/api/notifications/unread-count")
    # Only the two public ones -- not alice's, bob's or the untargeted personal one.
    assert resp.json() == {"count": 2}


async def test_route_mine_narrows_admin_list(client_as, people, rows):
    async with client_as(people["admin"]) as client:
        everything = await client.get("/api/notifications")
        mine = await client.get("/api/notifications", params={"mine": "true"})
    assert "Arzttermin" in {n["title"] for n in everything.json()["notifications"]}
    assert {n["title"] for n in mine.json()["notifications"]} == {"Waschmaschine fertig", "Alte Meldung"}


async def test_route_anonymous_refused(client_as, rows):
    async with client_as(None) as client:
        assert (await client.get("/api/notifications/unread-count")).status_code == 401
        assert (await client.post("/api/notifications/acknowledge-all")).status_code == 401


async def test_routes_absent_when_bell_off(client_as, people, rows, bell_on):
    bell_on.notification_bell_enabled = False
    async with client_as(people["alice"]) as client:
        assert (await client.get("/api/notifications/unread-count")).status_code == 404
        assert (await client.post("/api/notifications/acknowledge-all")).status_code == 404


async def test_route_acknowledge_all(client_as, people, rows, published):
    async with client_as(people["alice"]) as client:
        resp = await client.post("/api/notifications/acknowledge-all")
        after = await client.get("/api/notifications/unread-count")
    assert resp.json() == {"success": True, "acknowledged": 3}
    assert after.json() == {"count": 0}
