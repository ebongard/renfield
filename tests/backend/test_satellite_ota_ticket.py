"""Satellite OTA download ticket: the package route works on an auth-on instance.

🛑 The occasion (2026-11-02): `/api/satellites/update-package` became ADMIN-gated,
and the 1.4.12 fleet downloads the package with a bare `urllib` request -- no
header, the `package_url` fetched exactly as received. Since the household
auth-on cutover every OTA ended in `HTTP Error 401`. The fix travels inside the
URL: `initiate_update` mints a one-time ticket over the satellite's own
PSK-authenticated WebSocket.

What is checked here, one property at a time:
* the admin path is unchanged, and no credential at all still 401s;
* a ticket works exactly once, for its satellite and its package only;
* an expired ticket is refused (Redis expiry = the key is gone);
* auth off stays byte-identical: no ticket minted, no ticket read;
* the minted URL never reaches the access log in clear.
"""
from __future__ import annotations

import io
import logging
import logging.config
import tarfile
from unittest.mock import AsyncMock, MagicMock
from urllib.parse import parse_qs, urlsplit

import pytest
from httpx import AsyncClient

from ha_glue.services import satellite_ota_ticket as store
from ha_glue.services import satellite_update_service as update_mod
from ha_glue.services.satellite_manager import (
    SatelliteCapabilities,
    SatelliteInfo,
    SatelliteManager,
)

PACKAGE_PATH = "/api/satellites/update-package"


class _FakeRedis:
    """Only the two calls the store makes -- `set(nx=True, ex=…)` and `getdel`."""

    def __init__(self):
        self.data: dict[str, str] = {}
        self.set_calls: list[dict] = []
        self.fail = False

    async def set(self, key, value, ex=None, nx=False):
        if self.fail:
            raise RuntimeError("redis down")
        self.set_calls.append({"key": key, "ex": ex, "nx": nx})
        if nx and key in self.data:
            return None
        self.data[key] = value
        return True

    async def getdel(self, key):
        if self.fail:
            raise RuntimeError("redis down")
        return self.data.pop(key, None)


@pytest.fixture
def redis(monkeypatch):
    r = _FakeRedis()
    monkeypatch.setattr(store, "get_redis", lambda: r)
    return r


# =============================================================================
# The store
# =============================================================================

@pytest.mark.unit
class TestTicketStore:
    async def test_works_exactly_once(self, redis):
        t = await store.issue_ticket("sat-a", "1.4.13", "sha256:abc")
        assert t and len(t) >= 43  # 32 bytes urlsafe-base64

        grant = await store.consume_ticket(t, "sat-a", "1.4.13", "sha256:abc")
        assert grant == store.OtaTicketGrant("sat-a", "1.4.13", "sha256:abc")
        assert await store.consume_ticket(t, "sat-a", "1.4.13", "sha256:abc") is None

    async def test_stored_with_nx_and_ttl_and_only_as_hash(self, redis):
        t = await store.issue_ticket("sat-a", "1.4.13", "sha256:abc")
        call = redis.set_calls[-1]
        assert call["nx"] is True
        assert call["ex"] == store.TICKET_TTL_SECONDS == 600
        # The raw ticket is neither key nor value.
        (key, value), = redis.data.items()
        assert t not in key and t not in value
        assert key.startswith("sat_ota_ticket:") and len(key.split(":", 1)[1]) == 64

    async def test_expired_is_refused(self, redis):
        """Redis expiry removes the key; the store must read that as refusal."""
        t = await store.issue_ticket("sat-a", "1.4.13", "sha256:abc")
        redis.data.clear()
        assert await store.consume_ticket(t, "sat-a", "1.4.13", "sha256:abc") is None

    async def test_wrong_satellite_is_refused_and_burns_the_ticket(self, redis):
        t = await store.issue_ticket("sat-a", "1.4.13", "sha256:abc")
        assert await store.consume_ticket(t, "sat-b", "1.4.13", "sha256:abc") is None
        assert await store.consume_ticket(t, None, "1.4.13", "sha256:abc") is None
        # Leaked → worthless, also for the right satellite.
        assert await store.consume_ticket(t, "sat-a", "1.4.13", "sha256:abc") is None

    @pytest.mark.parametrize("version,checksum", [
        ("1.4.14", "sha256:abc"), ("1.4.13", "sha256:other"),
    ])
    async def test_other_package_is_refused(self, redis, version, checksum):
        t = await store.issue_ticket("sat-a", "1.4.13", "sha256:abc")
        assert await store.consume_ticket(t, "sat-a", version, checksum) is None

    async def test_unknown_and_empty_are_refused(self, redis):
        assert await store.consume_ticket("nope", "sat-a", "1", "c") is None
        assert await store.consume_ticket("", "sat-a", "1", "c") is None

    async def test_redis_down_is_fail_closed(self, redis):
        redis.fail = True
        assert await store.issue_ticket("sat-a", "1", "c") is None
        assert await store.consume_ticket("x", "sat-a", "1", "c") is None

    async def test_collision_issues_nothing(self, redis, monkeypatch):
        monkeypatch.setattr(store.secrets, "token_urlsafe", lambda n: "fixed")
        assert await store.issue_ticket("sat-a", "1", "c") == "fixed"
        assert await store.issue_ticket("sat-a", "1", "c") is None

    async def test_garbage_payload_is_refused(self, redis):
        redis.data[store._key("t")] = "not json"
        assert await store.consume_ticket("t", "sat-a", "1", "c") is None

    async def test_refusal_log_never_carries_the_ticket(self, redis):
        from loguru import logger

        lines: list[str] = []
        sink = logger.add(lines.append, level="WARNING")
        try:
            t = await store.issue_ticket("sat-a", "1.4.13", "sha256:abc")
            await store.consume_ticket(t, "sat-b", "1.4.13", "sha256:abc")
            await store.consume_ticket(t, "sat-a", "1.4.13", "sha256:abc")
        finally:
            logger.remove(sink)
        assert len(lines) == 2
        assert all(t not in line for line in lines)


# =============================================================================
# initiate_update puts the ticket into the URL (auth on only)
# =============================================================================

FAKE_PACKAGE = {
    "path": "/nonexistent.tar.gz",
    "checksum": "sha256:abc",
    "size": 1024,
    "version": "1.4.13",
    "manifest": None,
    "signature": None,
}


@pytest.fixture
def connected_satellite(monkeypatch):
    """A satellite on its WebSocket, and a service with a fixed package."""
    manager = SatelliteManager()
    ws = MagicMock()
    ws.send_json = AsyncMock()
    manager.satellites["sat-a"] = SatelliteInfo(
        satellite_id="sat-a", room="Kitchen", websocket=ws,
        capabilities=SatelliteCapabilities(), version="1.4.12",
    )
    monkeypatch.setattr(update_mod, "get_satellite_manager", lambda: manager)
    monkeypatch.setattr(
        update_mod.ha_glue_settings, "satellite_ota_require_signature", False
    )
    service = update_mod.SatelliteUpdateService()
    monkeypatch.setattr(service, "get_package_info", lambda: dict(FAKE_PACKAGE))
    monkeypatch.setattr(service, "is_update_available", lambda v: True)
    return service, manager, ws


def _sent_url(ws) -> str:
    ws.send_json.assert_called_once()
    return ws.send_json.call_args[0][0]["package_url"]


@pytest.mark.unit
class TestInitiateUpdate:
    async def test_auth_on_sends_a_ticket_url(self, connected_satellite, redis, monkeypatch):
        service, _manager, ws = connected_satellite
        monkeypatch.setattr(update_mod.settings, "auth_enabled", True)

        result = await service.initiate_update("sat-a")
        assert result["success"] is True

        url = _sent_url(ws)
        parts = urlsplit(url)
        assert parts.path == PACKAGE_PATH and not parts.netloc  # stays relative
        q = parse_qs(parts.query)
        assert q["satellite_id"] == ["sat-a"]
        ticket = q[store.TICKET_QUERY_PARAM][0]
        assert await store.consume_ticket(ticket, "sat-a", "1.4.13", "sha256:abc")

    async def test_auth_off_request_is_unchanged(self, connected_satellite, redis, monkeypatch):
        service, _manager, ws = connected_satellite
        monkeypatch.setattr(update_mod.settings, "auth_enabled", False)

        assert (await service.initiate_update("sat-a"))["success"] is True
        assert _sent_url(ws) == PACKAGE_PATH
        assert redis.set_calls == []

    async def test_no_ticket_no_request(self, connected_satellite, redis, monkeypatch):
        """Without a ticket the device would only 401 — so nothing is sent and
        no run is started."""
        service, manager, ws = connected_satellite
        monkeypatch.setattr(update_mod.settings, "auth_enabled", True)
        redis.fail = True

        result = await service.initiate_update("sat-a")
        assert result["success"] is False
        ws.send_json.assert_not_called()
        assert manager.satellites["sat-a"].update_run_id is None


# =============================================================================
# The route
# =============================================================================

class _FakeUser:
    """Exposes `has_permission` directly (no ORM relationship, see test_tool_health_routes)."""

    def __init__(self, is_admin: bool):
        self.id = 42 if is_admin else 7
        self._is_admin = is_admin

    def has_permission(self, perm: str) -> bool:
        return self._is_admin and perm == "admin"


@pytest.fixture
def package(tmp_path, monkeypatch):
    """A real tarball behind the route's update service."""
    path = tmp_path / "pkg.tar.gz"
    with tarfile.open(path, "w:gz") as tar:
        data = b"x"
        info = tarfile.TarInfo(name="x.txt")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    info = dict(FAKE_PACKAGE, path=str(path), size=path.stat().st_size)
    service = MagicMock()
    service.get_package_info.return_value = info
    monkeypatch.setattr(update_mod, "get_satellite_update_service", lambda: service)
    return info


@pytest.fixture
def auth_on(monkeypatch):
    from utils.config import settings
    monkeypatch.setattr(settings, "auth_enabled", True)


@pytest.fixture
def as_user(app_with_test_db):
    from services.auth_service import get_optional_user

    def _set(user):
        app_with_test_db.dependency_overrides[get_optional_user] = lambda: user
    yield _set
    app_with_test_db.dependency_overrides.pop(get_optional_user, None)


def _url(ticket: str, satellite_id: str = "sat-a") -> str:
    return f"{PACKAGE_PATH}?satellite_id={satellite_id}&ticket={ticket}"


@pytest.mark.integration
class TestUpdatePackageRoute:
    async def test_no_credential_is_401(self, async_client: AsyncClient, package, auth_on):
        r = await async_client.get(PACKAGE_PATH)
        assert r.status_code == 401

    async def test_non_admin_is_403(self, async_client, package, auth_on, as_user):
        as_user(_FakeUser(is_admin=False))
        assert (await async_client.get(PACKAGE_PATH)).status_code == 403

    async def test_admin_still_downloads(self, async_client, package, auth_on, as_user):
        as_user(_FakeUser(is_admin=True))
        r = await async_client.get(PACKAGE_PATH)
        assert r.status_code == 200
        assert r.headers["X-Package-Version"] == "1.4.13"
        assert r.headers["X-Package-Checksum"] == "sha256:abc"

    async def test_ticket_downloads_once(self, async_client, package, auth_on, redis):
        t = await store.issue_ticket("sat-a", "1.4.13", "sha256:abc")
        r = await async_client.get(_url(t))
        assert r.status_code == 200
        assert r.headers["content-type"] == "application/gzip"
        assert r.headers["X-Package-Checksum"] == "sha256:abc"

        assert (await async_client.get(_url(t))).status_code == 401

    async def test_expired_ticket_is_401(self, async_client, package, auth_on, redis):
        t = await store.issue_ticket("sat-a", "1.4.13", "sha256:abc")
        redis.data.clear()
        assert (await async_client.get(_url(t))).status_code == 401

    async def test_wrong_satellite_is_401(self, async_client, package, auth_on, redis):
        t = await store.issue_ticket("sat-a", "1.4.13", "sha256:abc")
        assert (await async_client.get(_url(t, "sat-b"))).status_code == 401

    async def test_ticket_for_older_package_is_401(self, async_client, package, auth_on, redis):
        t = await store.issue_ticket("sat-a", "1.4.12", "sha256:old")
        assert (await async_client.get(_url(t))).status_code == 401

    async def test_bad_ticket_does_not_fall_back_to_admin(
        self, async_client, package, auth_on, redis, as_user
    ):
        """A presented ticket decides alone; an admin session does not rescue a
        replayed one (and nothing about the admin path changes)."""
        as_user(_FakeUser(is_admin=True))
        assert (await async_client.get(_url("bogus"))).status_code == 401

    async def test_503_does_not_burn_the_ticket(self, async_client, package, auth_on, redis,
                                                monkeypatch):
        service = update_mod.get_satellite_update_service()
        service.get_package_info.return_value = None
        t = await store.issue_ticket("sat-a", "1.4.13", "sha256:abc")
        assert (await async_client.get(_url(t))).status_code == 503
        service.get_package_info.return_value = package
        assert (await async_client.get(_url(t))).status_code == 200

    async def test_auth_off_is_unchanged(self, async_client, package, redis, monkeypatch):
        from utils.config import settings
        monkeypatch.setattr(settings, "auth_enabled", False)
        assert (await async_client.get(PACKAGE_PATH)).status_code == 200
        # Query parameters are ignored as before -- no ticket is read.
        assert (await async_client.get(_url("bogus"))).status_code == 200
        assert redis.data == {} and redis.set_calls == []

    async def test_end_to_end_as_a_1_4_12_device(
        self, async_client, package, auth_on, redis, connected_satellite, monkeypatch
    ):
        """The URL from the update_request, fetched exactly as received (relative
        path → base URL + path), with no header — what 1.4.12 does."""
        service, _manager, ws = connected_satellite
        monkeypatch.setattr(service, "get_package_info", lambda: dict(package))
        assert (await service.initiate_update("sat-a"))["success"] is True
        url = _sent_url(ws)
        assert (await async_client.get(url)).status_code == 200
        assert (await async_client.get(url)).status_code == 401


# =============================================================================
# The ticket never reaches the access log
# =============================================================================

@pytest.mark.unit
async def test_minted_url_is_redacted_in_uvicorn_access_line(
    connected_satellite, redis, monkeypatch
):
    from uvicorn.config import LOGGING_CONFIG
    from uvicorn.logging import AccessFormatter

    from utils.log_safety import REDACTED, SECRET_QUERY_PARAMS, install_uvicorn_url_redaction

    assert store.TICKET_QUERY_PARAM in SECRET_QUERY_PARAMS

    service, _manager, ws = connected_satellite
    monkeypatch.setattr(update_mod.settings, "auth_enabled", True)
    await service.initiate_update("sat-a")
    url = _sent_url(ws)
    ticket = parse_qs(urlsplit(url).query)["ticket"][0]

    logging.config.dictConfig(LOGGING_CONFIG)
    install_uvicorn_url_redaction()
    buf = io.StringIO()
    h = logging.StreamHandler(buf)
    h.setFormatter(AccessFormatter(LOGGING_CONFIG["formatters"]["access"]["fmt"], use_colors=False))
    access = logging.getLogger("uvicorn.access")
    access.addHandler(h)
    try:
        # Exactly uvicorn's call in protocols/http/*_impl.py.
        access.info('%s - "%s %s HTTP/%s" %d', "10.0.0.9:40000", "GET", url, "1.1", 200)
    finally:
        access.removeHandler(h)
    line = buf.getvalue()
    assert ticket not in line
    assert f"ticket={REDACTED}" in line
    assert "satellite_id=sat-a" in line
