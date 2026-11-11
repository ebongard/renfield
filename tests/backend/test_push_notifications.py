"""Web Push (#79): preconditions, routes, recipient selection and the sender.

No test here talks to a real push service. ``pywebpush`` is not installed in
the test image; the sender is exercised against a stub module placed in
``sys.modules``, so the code under test is the real ``_send_blocking`` with its
lazy import.
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest
from httpx import AsyncClient
from loguru import logger
from pydantic import SecretStr
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from models.database import (
    NOTIFICATION_DELIVERED,
    Notification,
    PushSubscription,
    Role,
    User,
)
from services import push_preconditions, push_service
from services.push_service import PushTarget
from utils.config import settings

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _load_key_script():
    path = _REPO_ROOT / "bin" / "generate-vapid-keys.py"
    spec = importlib.util.spec_from_file_location("generate_vapid_keys_under_test", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


_KEY_SCRIPT = _load_key_script()
PRIVATE_KEY, PUBLIC_KEY = _KEY_SCRIPT.generate()
OTHER_PRIVATE_KEY, OTHER_PUBLIC_KEY = _KEY_SCRIPT.generate()

# Shaped like real browser values: a 65-byte point and a 16-byte secret.
P256DH = PUBLIC_KEY
AUTH = "AAECAwQFBgcICQoLDA0ODw"
ENDPOINT = "https://fcm.googleapis.com/fcm/send/device-{}"

ROUTES = [
    ("GET", "/api/push/vapid-key"),
    ("GET", "/api/push/subscriptions"),
    ("POST", "/api/push/subscribe"),
    ("DELETE", "/api/push/subscribe"),
]


def _body(n: int = 1) -> dict:
    return {"endpoint": ENDPOINT.format(n), "keys": {"p256dh": P256DH, "auth": AUTH}}


@pytest.fixture
def push_on(monkeypatch):
    """Every precondition met."""
    monkeypatch.setattr(settings, "push_notifications_enabled", True)
    monkeypatch.setattr(settings, "auth_enabled", True)
    monkeypatch.setattr(settings, "push_vapid_private_key", SecretStr(PRIVATE_KEY))
    monkeypatch.setattr(settings, "push_vapid_public_key", PUBLIC_KEY)
    monkeypatch.setattr(settings, "push_vapid_claims_email", "ops@example.com")


@pytest.fixture
def warnings_seen():
    seen: list[str] = []
    sink = logger.add(lambda message: seen.append(str(message)), level="WARNING")
    yield seen
    logger.remove(sink)


# =========================================================== preconditions
@pytest.mark.unit
class TestPreconditions:
    def test_default_is_off(self):
        assert settings.push_notifications_enabled is False
        assert push_preconditions.push_blocker() == push_preconditions.FLAG_OFF
        assert push_preconditions.push_active() is False

    def test_all_preconditions_met(self, push_on):
        assert push_preconditions.push_blocker() is None
        assert push_preconditions.push_active() is True

    def test_flag_alone_is_not_enough(self, monkeypatch):
        monkeypatch.setattr(settings, "auth_enabled", True)
        monkeypatch.setattr(settings, "push_notifications_enabled", True)
        assert push_preconditions.push_blocker() == push_preconditions.KEY_MISSING

    def test_auth_off_blocks(self, push_on, monkeypatch):
        """A subscription belongs to the logged-in user; without a login there
        is nobody to bind it to."""
        monkeypatch.setattr(settings, "auth_enabled", False)
        assert push_preconditions.push_blocker() == push_preconditions.AUTH_OFF

    @pytest.mark.parametrize("missing", ["private", "public"])
    def test_missing_key(self, push_on, monkeypatch, missing):
        if missing == "private":
            monkeypatch.setattr(settings, "push_vapid_private_key", None)
        else:
            monkeypatch.setattr(settings, "push_vapid_public_key", "  ")
        assert push_preconditions.push_blocker() == push_preconditions.KEY_MISSING

    @pytest.mark.parametrize(
        "private_key, public_key",
        [
            ("not base64 !!", PUBLIC_KEY),
            (PRIVATE_KEY[:-4], PUBLIC_KEY),          # too short
            (PRIVATE_KEY, PUBLIC_KEY[:-4]),          # too short
            (PRIVATE_KEY, "A" + PUBLIC_KEY[1:]),     # not an uncompressed point
            ("A" * 43, PUBLIC_KEY),                  # zero scalar: not on the curve
        ],
    )
    def test_malformed_key(self, push_on, monkeypatch, private_key, public_key):
        monkeypatch.setattr(settings, "push_vapid_private_key", SecretStr(private_key))
        monkeypatch.setattr(settings, "push_vapid_public_key", public_key)
        assert push_preconditions.push_blocker() == push_preconditions.KEY_MALFORMED

    def test_halves_of_two_different_pairs(self, push_on, monkeypatch):
        """Browsers would subscribe with a key the push service then refuses
        every send for."""
        monkeypatch.setattr(settings, "push_vapid_public_key", OTHER_PUBLIC_KEY)
        assert push_preconditions.push_blocker() == push_preconditions.KEY_MISMATCH

    @pytest.mark.parametrize("address", ["", "   ", "not-an-address"])
    def test_contact_address_required(self, push_on, monkeypatch, address):
        monkeypatch.setattr(settings, "push_vapid_claims_email", address)
        assert push_preconditions.push_blocker() == push_preconditions.CLAIMS_MISSING

    @pytest.mark.parametrize(
        "address, subject",
        [("ops@example.com", "mailto:ops@example.com"), ("mailto:a@b.c", "mailto:a@b.c")],
    )
    def test_subject_is_a_mailto_uri(self, push_on, monkeypatch, address, subject):
        monkeypatch.setattr(settings, "push_vapid_claims_email", address)
        assert push_preconditions.vapid_subject() == subject

    def test_flag_off_is_silent_at_startup(self, warnings_seen):
        push_preconditions.warn_if_push_misconfigured()
        assert warnings_seen == []

    def test_active_is_silent_at_startup(self, push_on, warnings_seen):
        push_preconditions.warn_if_push_misconfigured()
        assert warnings_seen == []

    def test_one_warning_when_switched_on_without_a_key(self, push_on, monkeypatch, warnings_seen):
        monkeypatch.setattr(settings, "push_vapid_private_key", None)
        push_preconditions.warn_if_push_misconfigured()
        assert len(warnings_seen) == 1
        assert push_preconditions.KEY_MISSING in warnings_seen[0]
        assert "stays OFF" in warnings_seen[0]

    def test_the_warning_never_prints_the_key(self, push_on, monkeypatch, warnings_seen):
        monkeypatch.setattr(settings, "push_vapid_public_key", OTHER_PUBLIC_KEY)
        push_preconditions.warn_if_push_misconfigured()
        assert len(warnings_seen) == 1
        assert PRIVATE_KEY not in warnings_seen[0]


@pytest.mark.unit
class TestKeyScript:
    def test_generated_pair_passes_the_precondition(self):
        private_key, public_key = _KEY_SCRIPT.generate()
        assert push_preconditions._key_pair_blocker(private_key, public_key) is None
        assert (private_key, public_key) != (PRIVATE_KEY, PUBLIC_KEY)

    def test_prints_both_lines_and_writes_nothing(self, capsys, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        _KEY_SCRIPT.main()
        lines = capsys.readouterr().out.strip().splitlines()
        values = dict(line.split("=", 1) for line in lines if "=" in line)
        assert set(values) == {"PUSH_VAPID_PRIVATE_KEY", "PUSH_VAPID_PUBLIC_KEY"}
        assert push_preconditions._key_pair_blocker(*values.values()) is None
        assert list(tmp_path.iterdir()) == []


@pytest.mark.unit
class TestRequirement:
    def test_pywebpush_is_declared_with_a_capped_major(self):
        """The harness cannot find a missing package (it runs in the old
        image), so the DECLARATION is what is checked."""
        lines = (_REPO_ROOT / "src" / "backend" / "requirements.txt").read_text().splitlines()
        declared = [line.strip() for line in lines if line.strip().startswith("pywebpush")]
        assert declared == ["pywebpush>=2.5.0,<3"]

    def test_the_sender_module_imports_without_the_package(self):
        source = (_REPO_ROOT / "src" / "backend" / "services" / "push_service.py").read_text()
        top_level = [line for line in source.splitlines() if line.startswith(("import ", "from "))]
        assert not any("pywebpush" in line for line in top_level)


# =============================================================== allowlist
@pytest.mark.unit
class TestEndpointAllowlist:
    @pytest.mark.parametrize(
        "endpoint",
        [
            "https://fcm.googleapis.com/fcm/send/abc",
            "https://updates.push.services.mozilla.com/wpush/v2/abc",
            "https://web.push.apple.com/abc",
            "https://db5p.notify.windows.com/w/?token=abc",
            "https://FCM.googleapis.com:443/fcm/send/abc",
        ],
    )
    def test_known_push_services(self, endpoint):
        assert push_service.endpoint_allowed(endpoint) is True

    @pytest.mark.parametrize(
        "endpoint",
        [
            "http://fcm.googleapis.com/fcm/send/abc",           # not https
            "https://backend.renfield.svc:8000/api/internal",    # inside the network
            "https://127.0.0.1/x",
            "https://fcm.googleapis.com.evil.example/x",         # suffix trick
            "https://evilpush.apple.com/x",                      # not a subdomain match
            "https://user:pw@fcm.googleapis.com/x",              # credentials
            "https://fcm.googleapis.com:8443/x",                 # odd port
            "https://fcm.googleapis.com:notaport/x",
            "fcm.googleapis.com/x",
            "",
        ],
    )
    def test_everything_else_is_refused(self, endpoint):
        assert push_service.endpoint_allowed(endpoint) is False

    def test_the_list_is_configurable(self, monkeypatch):
        monkeypatch.setattr(settings, "push_allowed_endpoint_hosts", "push.example.org")
        assert push_service.endpoint_allowed("https://push.example.org/x") is True
        assert push_service.endpoint_allowed("https://fcm.googleapis.com/x") is False


# ================================================================== people
async def _user(db: AsyncSession, name: str, permissions: list[str], *, active: bool = True) -> User:
    role = Role(name=f"role-{name}", description=name, permissions=permissions, is_system=False)
    db.add(role)
    await db.commit()
    await db.refresh(role)
    user = User(role_id=role.id, username=name, email=f"{name}@example.com",
                password_hash="$2b$12$fakefakefakefakefakefake", is_active=active)
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
        "guest": await _user(db_session, "guest", ["chat.own"]),
    }


async def _subscribe(db: AsyncSession, user: User, n: int) -> PushSubscription:
    return await push_service.save_subscription(
        db, user_id=user.id, endpoint=ENDPOINT.format(n), p256dh=P256DH, auth=AUTH,
        device_label="test",
    )


async def _rows(db: AsyncSession) -> list[tuple[int, str]]:
    result = await db.execute(
        select(PushSubscription.user_id, PushSubscription.endpoint).order_by(PushSubscription.id)
    )
    return [tuple(r) for r in result.all()]


# ================================================================== routes
@pytest.fixture
def push_routes(app_with_test_db):
    """Mount the router the way ``main.py`` does when the flag is set, and
    take it off again -- the shared app must not keep it for other tests."""
    from api.routes import push

    app = app_with_test_db
    before = list(app.router.routes)
    app.include_router(push.router, prefix="/api/push", tags=["Push"])
    yield app
    app.router.routes[:] = before


def _as(app, user: User, permissions: list[str]) -> None:
    from services.auth_service import get_current_user

    principal = SimpleNamespace(
        id=user.id, username=user.username, has_permission=lambda p: p in permissions,
    )
    app.dependency_overrides[get_current_user] = lambda: principal


async def _call(client: AsyncClient, method: str, path: str, body: dict | None = None):
    return await client.request(method, path, json=body)


@pytest.mark.database
class TestRoutesDisabled:
    async def test_flag_off_registers_no_route(self, async_client: AsyncClient, app_with_test_db):
        """Byte-identical to before: the paths do not exist."""
        assert settings.push_notifications_enabled is False
        paths = [getattr(r, "path", "") for r in app_with_test_db.router.routes]
        assert not [p for p in paths if p.startswith("/api/push")]
        for method, path in ROUTES:
            resp = await _call(async_client, method, path, _body())
            assert resp.status_code == 404, (method, path, resp.text)

    @pytest.mark.parametrize("method, path", ROUTES)
    async def test_mounted_but_flag_off_answers_as_absent(
        self, async_client: AsyncClient, push_routes, monkeypatch, method, path,
    ):
        """A flag flipped off at runtime: 404 before auth and before validation."""
        monkeypatch.setattr(settings, "auth_enabled", True)
        resp = await _call(async_client, method, path, {"nonsense": True})
        assert resp.status_code == 404, resp.text
        assert resp.json() == {"detail": "Not Found"}

    @pytest.mark.parametrize("method, path", ROUTES)
    @pytest.mark.parametrize("broken", ["no_key", "mismatch", "no_contact", "auth_off"])
    async def test_flag_on_with_a_missing_precondition_answers_as_absent(
        self, async_client: AsyncClient, push_routes, push_on, monkeypatch, people,
        method, path, broken,
    ):
        if broken == "no_key":
            monkeypatch.setattr(settings, "push_vapid_private_key", None)
        elif broken == "mismatch":
            monkeypatch.setattr(settings, "push_vapid_public_key", OTHER_PUBLIC_KEY)
        elif broken == "no_contact":
            monkeypatch.setattr(settings, "push_vapid_claims_email", "")
        else:
            monkeypatch.setattr(settings, "auth_enabled", False)
        _as(push_routes, people["alice"], ["notifications.view"])
        resp = await _call(async_client, method, path, _body())
        assert resp.status_code == 404, resp.text
        assert resp.json() == {"detail": "Not Found"}

    async def test_feature_list_follows_the_same_function(
        self, async_client: AsyncClient, app_with_test_db, push_on, monkeypatch,
    ):
        from services.auth_service import get_user_or_default

        app_with_test_db.dependency_overrides[get_user_or_default] = lambda: SimpleNamespace(id=1)
        on = await async_client.get("/api/config/features")
        assert on.json()["push_notifications_enabled"] is True

        monkeypatch.setattr(settings, "push_vapid_private_key", None)
        off = await async_client.get("/api/config/features")
        assert off.json()["push_notifications_enabled"] is False


@pytest.mark.database
class TestRoutes:
    @pytest.mark.parametrize("method, path", ROUTES)
    async def test_anonymous_is_refused(
        self, async_client: AsyncClient, push_routes, push_on, method, path,
    ):
        resp = await _call(async_client, method, path, _body())
        assert resp.status_code == 401, resp.text

    @pytest.mark.parametrize("method, path", ROUTES)
    async def test_without_the_permission_is_refused(
        self, async_client: AsyncClient, push_routes, push_on, people, db_session, method, path,
    ):
        _as(push_routes, people["guest"], ["chat.own"])
        resp = await _call(async_client, method, path, _body())
        assert resp.status_code == 403, resp.text
        assert await _rows(db_session) == []

    async def test_vapid_key(self, async_client: AsyncClient, push_routes, push_on, people):
        _as(push_routes, people["alice"], ["notifications.view"])
        resp = await async_client.get("/api/push/vapid-key")
        assert resp.status_code == 200, resp.text
        assert resp.json() == {"public_key": PUBLIC_KEY}
        assert PRIVATE_KEY not in resp.text

    async def test_subscribe_binds_the_row_to_the_session_user(
        self, async_client: AsyncClient, push_routes, push_on, people, db_session,
    ):
        """A ``user_id`` in the body is ignored -- the owner is the caller."""
        _as(push_routes, people["alice"], ["notifications.view"])
        body = {**_body(1), "user_id": people["bob"].id}
        resp = await async_client.post(
            "/api/push/subscribe", json=body, headers={"User-Agent": "Firefox/140 (Test)"},
        )
        assert resp.status_code == 201, resp.text
        assert resp.json()["endpoint"] == ENDPOINT.format(1)
        assert resp.json()["device_label"] == "Firefox/140 (Test)"
        assert await _rows(db_session) == [(people["alice"].id, ENDPOINT.format(1))]

    async def test_subscribing_twice_keeps_one_row(
        self, async_client: AsyncClient, push_routes, push_on, people, db_session,
    ):
        _as(push_routes, people["alice"], ["notifications.view"])
        first = await async_client.post("/api/push/subscribe", json=_body(1))
        second = await async_client.post("/api/push/subscribe", json=_body(1))
        assert (first.status_code, second.status_code) == (201, 201)
        assert first.json()["id"] == second.json()["id"]
        assert len(await _rows(db_session)) == 1

    @pytest.mark.parametrize(
        "endpoint",
        ["http://fcm.googleapis.com/x", "https://backend.renfield.svc/api/internal/x",
         "https://127.0.0.1/x"],
    )
    async def test_endpoint_outside_the_allowlist_is_refused(
        self, async_client: AsyncClient, push_routes, push_on, people, db_session, endpoint,
    ):
        _as(push_routes, people["alice"], ["notifications.view"])
        resp = await async_client.post(
            "/api/push/subscribe", json={**_body(), "endpoint": endpoint},
        )
        assert resp.status_code == 422, resp.text
        assert resp.json() == {"detail": "endpoint_not_allowed"}
        assert await _rows(db_session) == []

    @pytest.mark.parametrize(
        "keys", [{"p256dh": "AAAA", "auth": AUTH}, {"p256dh": P256DH, "auth": "AAAA"}, {}],
    )
    async def test_malformed_keys_are_refused(
        self, async_client: AsyncClient, push_routes, push_on, people, db_session, keys,
    ):
        _as(push_routes, people["alice"], ["notifications.view"])
        resp = await async_client.post(
            "/api/push/subscribe", json={"endpoint": ENDPOINT.format(1), "keys": keys},
        )
        assert resp.status_code == 422, resp.text
        assert await _rows(db_session) == []

    async def test_list_shows_only_ones_own(
        self, async_client: AsyncClient, push_routes, push_on, people, db_session,
    ):
        await _subscribe(db_session, people["alice"], 1)
        await _subscribe(db_session, people["bob"], 2)

        _as(push_routes, people["bob"], ["notifications.view"])
        resp = await async_client.get("/api/push/subscriptions")
        assert resp.status_code == 200, resp.text
        assert [s["endpoint"] for s in resp.json()["subscriptions"]] == [ENDPOINT.format(2)]

    async def test_an_admin_does_not_see_other_peoples_devices_either(
        self, async_client: AsyncClient, push_routes, push_on, people, db_session,
    ):
        await _subscribe(db_session, people["alice"], 1)
        _as(push_routes, people["admin"], ["admin", "notifications.manage", "notifications.view"])
        resp = await async_client.get("/api/push/subscriptions")
        assert resp.json() == {"subscriptions": []}

    async def test_cannot_remove_someone_elses_subscription(
        self, async_client: AsyncClient, push_routes, push_on, people, db_session,
    ):
        await _subscribe(db_session, people["alice"], 1)
        _as(push_routes, people["bob"], ["notifications.view"])
        resp = await async_client.request(
            "DELETE", "/api/push/subscribe", json={"endpoint": ENDPOINT.format(1)},
        )
        assert resp.status_code == 404, resp.text
        assert await _rows(db_session) == [(people["alice"].id, ENDPOINT.format(1))]

    async def test_remove_ones_own(
        self, async_client: AsyncClient, push_routes, push_on, people, db_session,
    ):
        await _subscribe(db_session, people["alice"], 1)
        await _subscribe(db_session, people["alice"], 2)
        _as(push_routes, people["alice"], ["notifications.view"])
        resp = await async_client.request(
            "DELETE", "/api/push/subscribe", json={"endpoint": ENDPOINT.format(1)},
        )
        assert resp.status_code == 204, resp.text
        assert await _rows(db_session) == [(people["alice"].id, ENDPOINT.format(2))]

        again = await async_client.request(
            "DELETE", "/api/push/subscribe", json={"endpoint": ENDPOINT.format(1)},
        )
        assert again.status_code == 404

    async def test_another_account_on_the_same_browser_takes_the_device_over(
        self, async_client: AsyncClient, push_routes, push_on, people, db_session,
    ):
        """Otherwise the device kept receiving the previous account's pushes."""
        await _subscribe(db_session, people["alice"], 1)
        _as(push_routes, people["bob"], ["notifications.view"])
        resp = await async_client.post("/api/push/subscribe", json=_body(1))
        assert resp.status_code == 201, resp.text
        assert await _rows(db_session) == [(people["bob"].id, ENDPOINT.format(1))]

    async def test_device_limit(
        self, async_client: AsyncClient, push_routes, push_on, people, db_session, monkeypatch,
    ):
        monkeypatch.setattr(push_service, "MAX_SUBSCRIPTIONS_PER_USER", 2)
        _as(push_routes, people["alice"], ["notifications.view"])
        assert (await async_client.post("/api/push/subscribe", json=_body(1))).status_code == 201
        assert (await async_client.post("/api/push/subscribe", json=_body(2))).status_code == 201
        third = await async_client.post("/api/push/subscribe", json=_body(3))
        assert third.status_code == 409, third.text
        assert third.json() == {"detail": "subscription_limit_reached"}
        # Refreshing a device that is already counted still works.
        assert (await async_client.post("/api/push/subscribe", json=_body(2))).status_code == 201
        assert len(await _rows(db_session)) == 2


# ============================================================== recipients
def _notification(title: str, privacy: str | None, target: int | None) -> Notification:
    return Notification(event_type="test", title=title, message=title, urgency="info",
                        source="test", status=NOTIFICATION_DELIVERED, privacy=privacy,
                        target_user_id=target, delivered_at=datetime.utcnow())


@pytest.fixture
async def notifications(db_session: AsyncSession, people) -> dict[str, Notification]:
    rows = {
        "broadcast": _notification("Waschmaschine fertig", "public", None),
        "legacy_null_privacy": _notification("Alte Meldung", None, None),
        "for_alice": _notification("Frist Steuerbescheid", "personal", people["alice"].id),
        "public_for_bob": _notification("Paket da", "public", people["bob"].id),
        "for_admin": _notification("MCP down", "personal", people["admin"].id),
        "personal_no_target": _notification("Kontostand", "personal", None),
        "confidential_no_target": _notification("Vertraulich", "confidential", None),
        "unknown_privacy": _notification("Seltsam", "", None),
    }
    db_session.add_all(rows.values())
    await db_session.commit()
    for row in rows.values():
        await db_session.refresh(row)
    return rows


@pytest.mark.database
class TestRecipients:
    async def test_push_reaches_exactly_whom_the_in_app_list_reaches(
        self, db_session, people, notifications,
    ):
        """Side by side: for every notification, the owners of the selected
        subscriptions are the users whose OWN (non-oversight) notification
        list contains it. One rule, read through two doors."""
        from services.notification_service import NotificationService

        for n, user in enumerate(people.values(), start=1):
            await _subscribe(db_session, user, n)
        service = NotificationService(db_session)

        compared = 0
        for name, notification in notifications.items():
            in_app = set()
            for user in people.values():
                listed = await service.list_notifications(
                    viewer_id=user.id, restrict_to_viewer=True, limit=200,
                )
                if notification.id in {row.id for row in listed}:
                    in_app.add(user.id)
            pushed = {
                row.user_id
                for row in await push_service.select_push_recipients(db_session, notification)
            }
            assert pushed == in_app, name
            compared += 1
        assert compared == len(notifications) == 8

    async def test_expected_recipients_spelled_out(self, db_session, people, notifications):
        for n, user in enumerate(people.values(), start=1):
            await _subscribe(db_session, user, n)
        everyone = {user.id for user in people.values()}
        expected = {
            "broadcast": everyone,
            "legacy_null_privacy": everyone,
            "for_alice": {people["alice"].id},
            "public_for_bob": {people["bob"].id},
            "for_admin": {people["admin"].id},
            "personal_no_target": set(),
            "confidential_no_target": set(),
            "unknown_privacy": set(),
        }
        for name, notification in notifications.items():
            rows = await push_service.select_push_recipients(db_session, notification)
            assert {row.user_id for row in rows} == expected[name], name

    async def test_admin_oversight_is_not_a_delivery(self, db_session, people, notifications):
        """An admin may LIST everyone's notifications; a personal one addressed
        to someone else is still not pushed to the admin's phone."""
        await _subscribe(db_session, people["admin"], 1)
        await _subscribe(db_session, people["manager"], 2)
        rows = await push_service.select_push_recipients(db_session, notifications["for_alice"])
        assert rows == []

    async def test_every_device_of_the_addressee(self, db_session, people, notifications):
        await _subscribe(db_session, people["alice"], 1)
        await _subscribe(db_session, people["alice"], 2)
        await _subscribe(db_session, people["bob"], 3)
        rows = await push_service.select_push_recipients(db_session, notifications["for_alice"])
        assert [row.endpoint for row in rows] == [ENDPOINT.format(1), ENDPOINT.format(2)]

    async def test_a_deactivated_account_gets_nothing(self, db_session, people, notifications):
        gone = await _user(db_session, "carl", ["notifications.view"], active=False)
        await _subscribe(db_session, gone, 9)
        targeted = _notification("Fuer Carl", "personal", gone.id)
        assert await push_service.select_push_recipients(db_session, targeted) == []
        assert await push_service.select_push_recipients(db_session, notifications["broadcast"]) == []

    async def test_a_deleted_account_takes_its_devices_along(self, db_session, people):
        await _subscribe(db_session, people["alice"], 1)
        await _subscribe(db_session, people["bob"], 2)
        await db_session.execute(text("DELETE FROM users WHERE id = :id"), {"id": people["alice"].id})
        await db_session.commit()
        assert await _rows(db_session) == [(people["bob"].id, ENDPOINT.format(2))]


# ================================================================== sender
class _PushError(Exception):
    """Shaped like ``pywebpush.WebPushException``: carries the HTTP response."""

    def __init__(self, status_code: int):
        super().__init__(f"Push failed: {status_code} secret-response-body")
        self.response = SimpleNamespace(status_code=status_code)


@pytest.fixture
def push_stub(monkeypatch):
    """A stand-in ``pywebpush`` module. ``behaviour`` maps an endpoint to an
    exception to raise or a callable to run; calls are recorded."""
    calls: list[dict] = []
    behaviour: dict[str, object] = {}
    lock = threading.Lock()

    def webpush(**kwargs):
        with lock:
            calls.append(kwargs)
        action = behaviour.get(kwargs["subscription_info"]["endpoint"])
        if isinstance(action, Exception):
            raise action
        if callable(action):
            action()

    module = ModuleType("pywebpush")
    module.webpush = webpush
    monkeypatch.setitem(sys.modules, "pywebpush", module)
    return SimpleNamespace(calls=calls, behaviour=behaviour)


def _target(notification: Notification) -> PushTarget:
    return PushTarget(
        notification_id=notification.id, target_user_id=notification.target_user_id,
        privacy=notification.privacy, urgency=notification.urgency,
    )


async def _drain() -> None:
    while push_service._tasks:
        await asyncio.gather(*list(push_service._tasks), return_exceptions=True)


@pytest.mark.database
class TestSender:
    async def test_one_content_free_push_per_recipient_device(
        self, db_session, own_session_to_test_db, push_on, push_stub, people, notifications,
    ):
        await _subscribe(db_session, people["alice"], 1)
        await _subscribe(db_session, people["alice"], 2)
        await _subscribe(db_session, people["bob"], 3)

        counts = await push_service.deliver_push(_target(notifications["for_alice"]))

        assert counts == {"sent": 2, "gone": 0, "failed": 0}
        assert sorted(c["subscription_info"]["endpoint"] for c in push_stub.calls) == [
            ENDPOINT.format(1), ENDPOINT.format(2),
        ]
        call = push_stub.calls[0]
        assert call["subscription_info"]["keys"] == {"p256dh": P256DH, "auth": AUTH}
        assert call["vapid_private_key"] == PRIVATE_KEY
        assert call["vapid_claims"] == {"sub": "mailto:ops@example.com"}
        assert call["timeout"] == settings.push_send_timeout_seconds
        # The payload: an id and the urgency -- nothing a push service could read
        # even if the encryption were not there.
        assert json.loads(call["data"]) == {
            "v": 1, "kind": "notification",
            "notification_id": notifications["for_alice"].id, "urgency": "info",
        }
        assert "Frist" not in call["data"] and "Steuerbescheid" not in call["data"]

    async def test_each_call_gets_its_own_claims_dict(
        self, db_session, own_session_to_test_db, push_on, push_stub, people, notifications,
    ):
        """pywebpush writes ``aud``/``exp`` into the dict it is given."""
        await _subscribe(db_session, people["alice"], 1)
        await _subscribe(db_session, people["alice"], 2)
        await push_service.deliver_push(_target(notifications["for_alice"]))
        first, second = (c["vapid_claims"] for c in push_stub.calls)
        assert first is not second

    def test_payload_never_carries_content(self):
        payload = push_service.build_payload(
            PushTarget(notification_id=7, target_user_id=1, privacy="personal", urgency="critical")
        )
        assert set(payload) == {"v", "kind", "notification_id", "urgency"}
        assert payload["urgency"] == "critical"
        odd = push_service.build_payload(
            PushTarget(notification_id=7, target_user_id=1, privacy=None, urgency="free text")
        )
        assert odd["urgency"] == "info"

    @pytest.mark.parametrize("status", [404, 410])
    async def test_a_gone_subscription_is_removed(
        self, db_session, own_session_to_test_db, push_on, push_stub, people, notifications, status,
    ):
        await _subscribe(db_session, people["alice"], 1)
        await _subscribe(db_session, people["alice"], 2)
        push_stub.behaviour[ENDPOINT.format(1)] = _PushError(status)

        counts = await push_service.deliver_push(_target(notifications["for_alice"]))

        assert counts == {"sent": 1, "gone": 1, "failed": 0}
        assert await _rows(db_session) == [(people["alice"].id, ENDPOINT.format(2))]

    @pytest.mark.parametrize("error", [_PushError(500), _PushError(429), RuntimeError("dns")])
    async def test_any_other_failure_keeps_the_subscription(
        self, db_session, own_session_to_test_db, push_on, push_stub, people, notifications,
        error, warnings_seen,
    ):
        await _subscribe(db_session, people["alice"], 1)
        push_stub.behaviour[ENDPOINT.format(1)] = error

        counts = await push_service.deliver_push(_target(notifications["for_alice"]))

        assert counts == {"sent": 0, "gone": 0, "failed": 1}
        assert len(await _rows(db_session)) == 1
        # Neither the capability URL nor the push service's answer in the log.
        joined = "\n".join(warnings_seen)
        assert "failed" in joined
        assert ENDPOINT.format(1) not in joined and "secret-response-body" not in joined

    async def test_missing_package_only_disables_the_send(
        self, db_session, own_session_to_test_db, push_on, people, notifications, monkeypatch,
    ):
        monkeypatch.setitem(sys.modules, "pywebpush", None)  # import raises ImportError
        await _subscribe(db_session, people["alice"], 1)
        counts = await push_service.deliver_push(_target(notifications["for_alice"]))
        assert counts == {"sent": 0, "gone": 0, "failed": 1}
        assert len(await _rows(db_session)) == 1

    async def test_inactive_feature_sends_nothing(
        self, db_session, own_session_to_test_db, push_on, push_stub, people, notifications,
        monkeypatch,
    ):
        await _subscribe(db_session, people["alice"], 1)
        monkeypatch.setattr(settings, "push_vapid_private_key", None)
        counts = await push_service.deliver_push(_target(notifications["for_alice"]))
        assert counts == {"sent": 0, "gone": 0, "failed": 0}
        assert push_stub.calls == []

    async def test_an_endpoint_no_longer_on_the_allowlist_is_not_requested(
        self, db_session, own_session_to_test_db, push_on, push_stub, people, notifications,
        monkeypatch,
    ):
        await _subscribe(db_session, people["alice"], 1)
        monkeypatch.setattr(settings, "push_allowed_endpoint_hosts", "push.example.org")
        await push_service.deliver_push(_target(notifications["for_alice"]))
        assert push_stub.calls == []

    async def test_a_hanging_send_is_cut_off(
        self, db_session, own_session_to_test_db, push_on, push_stub, people, notifications,
        monkeypatch,
    ):
        release = threading.Event()
        push_stub.behaviour[ENDPOINT.format(1)] = lambda: release.wait(20)
        monkeypatch.setattr(settings, "push_send_timeout_seconds", 0.05)
        monkeypatch.setattr(push_service, "_OUTER_TIMEOUT_MARGIN_SECONDS", 0.2)
        await _subscribe(db_session, people["alice"], 1)
        try:
            started = time.monotonic()
            counts = await push_service.deliver_push(_target(notifications["for_alice"]))
            assert time.monotonic() - started < 5
            assert counts == {"sent": 0, "gone": 0, "failed": 1}
        finally:
            release.set()


# ============================================= the in-app delivery is untouched
@pytest.fixture
def device_delivery(monkeypatch):
    """Stand in for ha_glue's device broadcast: the in-app half of ``_deliver``."""
    from services.notification_service import NotificationService

    delivered: list[int] = []

    async def _to_devices(self, notification, tts=True):
        delivered.append(notification.id)
        return ["device-kitchen"]

    monkeypatch.setattr(NotificationService, "_deliver_to_devices", _to_devices)
    return delivered


async def _fire(db_session, target_user_id: int, title: str = "Frist Steuerbescheid") -> dict:
    from services.notification_service import NotificationService

    return await NotificationService(db_session).process_webhook(
        event_type="test.push", title=title, message="morgen faellig", urgency="info",
        tts=False, privacy="personal", target_user_id=target_user_id,
    )


@pytest.mark.database
class TestInAppDeliveryUnaffected:
    async def test_delivery_schedules_a_push_for_the_addressee(
        self, db_session, own_session_to_test_db, push_on, push_stub, people, device_delivery,
    ):
        await _subscribe(db_session, people["alice"], 1)
        await _subscribe(db_session, people["bob"], 2)

        result = await _fire(db_session, people["alice"].id)
        await _drain()

        assert result["delivered_to"] == ["device-kitchen"]
        assert [c["subscription_info"]["endpoint"] for c in push_stub.calls] == [ENDPOINT.format(1)]
        assert json.loads(push_stub.calls[0]["data"])["notification_id"] == result["notification_id"]

    async def test_flag_off_starts_no_task_and_sends_nothing(
        self, db_session, own_session_to_test_db, push_stub, people, device_delivery,
        monkeypatch,
    ):
        # `_tasks` alone proves nothing: a task that finds the feature off is
        # finished and gone again before `_fire` returns. Count the starts.
        started: list[PushTarget] = []

        async def _record(target):
            started.append(target)

        monkeypatch.setattr(push_service, "_deliver_push_guarded", _record)
        await _subscribe(db_session, people["alice"], 1)
        result = await _fire(db_session, people["alice"].id)
        await _drain()
        assert result["status"] == NOTIFICATION_DELIVERED
        assert started == []
        assert push_service._tasks == set()
        assert push_stub.calls == []

    async def test_a_raising_push_does_not_break_the_delivery(
        self, db_session, own_session_to_test_db, push_on, push_stub, people, device_delivery,
    ):
        await _subscribe(db_session, people["alice"], 1)
        push_stub.behaviour[ENDPOINT.format(1)] = RuntimeError("push service down")

        result = await _fire(db_session, people["alice"].id)
        await _drain()

        assert result["status"] == NOTIFICATION_DELIVERED
        assert result["delivered_to"] == ["device-kitchen"]
        row = (await db_session.execute(
            select(Notification).where(Notification.id == result["notification_id"])
        )).scalar_one()
        assert row.status == NOTIFICATION_DELIVERED
        assert len(push_stub.calls) == 1

    async def test_a_failing_recipient_lookup_does_not_break_the_delivery(
        self, db_session, own_session_to_test_db, push_on, push_stub, people, device_delivery,
        monkeypatch,
    ):
        async def _boom(db, notification):
            raise RuntimeError("database gone")

        monkeypatch.setattr(push_service, "select_push_recipients", _boom)
        result = await _fire(db_session, people["alice"].id)
        await _drain()
        assert result["status"] == NOTIFICATION_DELIVERED

    async def test_scheduling_itself_cannot_raise(self, push_on, monkeypatch):
        """Outside a running loop, or with a broken notification object."""
        push_service.schedule_push(SimpleNamespace())  # no attributes at all
        assert push_service._tasks == set()

    async def test_a_hanging_push_does_not_delay_the_delivery(
        self, db_session, own_session_to_test_db, push_on, push_stub, people, device_delivery,
    ):
        """The push service never answers. The notification is delivered and
        the caller has its result while the send is still stuck."""
        release = threading.Event()
        entered = threading.Event()

        def _hang():
            entered.set()
            release.wait(20)

        push_stub.behaviour[ENDPOINT.format(1)] = _hang
        await _subscribe(db_session, people["alice"], 1)
        try:
            started = time.monotonic()
            result = await _fire(db_session, people["alice"].id)
            elapsed = time.monotonic() - started

            assert result["status"] == NOTIFICATION_DELIVERED
            assert result["delivered_to"] == ["device-kitchen"]
            assert elapsed < 3, elapsed
            # ...and the send really was in flight, not merely never started:
            for _ in range(100):
                if entered.is_set():
                    break
                await asyncio.sleep(0.05)
            assert entered.is_set()
            assert len(push_service._tasks) == 1
            # The event loop stays responsive while the thread is stuck.
            count = (await db_session.execute(select(func.count(Notification.id)))).scalar()
            assert count == 1
        finally:
            release.set()
            await _drain()
