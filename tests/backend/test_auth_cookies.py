"""JWT HttpOnly-cookie session + CSRF (auth cookie migration).

Covers the flag-gated cookie mode end to end: the config validator hard-fails,
the cookie-first-then-Bearer token reader, the cookie set/clear helpers, the
double-submit CSRF middleware, and the WebSocket Strategy-0 cookie read. Flag
OFF is asserted byte-identical (no cookies, reader = bearer, CSRF no-op).
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI, Response
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from utils.config import Settings

_STRONG = "x" * 48  # passes the insecure-JWT-key boot guard that runs first


# =============================================================================
# Config validator hard-fails
# =============================================================================
class TestCookieValidator:
    @pytest.mark.unit
    def test_cookie_with_wildcard_cors_raises(self):
        with pytest.raises(ValueError, match="CORS_ORIGINS"):
            Settings(
                auth_cookie_enabled=True, auth_enabled=True, ws_auth_enabled=True,
                cors_origins="*", secret_key=SecretStr(_STRONG),
            )

    @pytest.mark.unit
    def test_cookie_without_auth_raises(self):
        with pytest.raises(ValueError, match="AUTH_ENABLED"):
            Settings(
                auth_cookie_enabled=True, auth_enabled=False,
                cors_origins="https://x.local", secret_key=SecretStr(_STRONG),
            )

    @pytest.mark.unit
    def test_cookie_insecure_on_prod_raises(self):
        with pytest.raises(ValueError, match="COOKIE_SECURE"):
            Settings(
                auth_cookie_enabled=True, auth_enabled=True, ws_auth_enabled=True,
                cors_origins="https://x.local", cookie_secure=False,
                renfield_env="production", secret_key=SecretStr(_STRONG),
            )

    @pytest.mark.unit
    def test_cookie_happy_path_constructs(self):
        s = Settings(
            auth_cookie_enabled=True, auth_enabled=True, ws_auth_enabled=True,
            cors_origins="https://x.local", cookie_secure=True,
            secret_key=SecretStr(_STRONG),
        )
        assert s.auth_cookie_enabled is True

    @pytest.mark.unit
    def test_flag_off_is_noop(self):
        # Default posture: everything off → never raises (byte-identical).
        s = Settings(auth_cookie_enabled=False, auth_enabled=False)
        assert s.auth_cookie_enabled is False


# =============================================================================
# Cookie-first-then-Bearer token reader
# =============================================================================
def _fake_request(*, cookies=None, auth_header=None):
    headers = {}
    if auth_header:
        headers["Authorization"] = auth_header
    return SimpleNamespace(cookies=cookies or {}, headers=headers, state=SimpleNamespace())


@pytest.mark.asyncio
class TestTokenReader:
    async def test_cookie_wins_when_enabled(self, monkeypatch):
        from services import auth_service as a
        monkeypatch.setattr(a.settings, "auth_cookie_enabled", True)
        monkeypatch.setattr(a.settings, "auth_cookie_name", "renfield_access")
        req = _fake_request(cookies={"renfield_access": "COOKIEJWT"},
                            auth_header="Bearer HEADERJWT")
        assert await a._cookie_or_bearer_token(req) == "COOKIEJWT"
        assert req.state.auth_via == "cookie"

    async def test_falls_back_to_bearer(self, monkeypatch):
        from services import auth_service as a
        monkeypatch.setattr(a.settings, "auth_cookie_enabled", True)
        monkeypatch.setattr(a.settings, "auth_cookie_name", "renfield_access")
        req = _fake_request(cookies={}, auth_header="Bearer HEADERJWT")
        assert await a._cookie_or_bearer_token(req) == "HEADERJWT"
        assert req.state.auth_via == "bearer"

    async def test_flag_off_ignores_cookie(self, monkeypatch):
        from services import auth_service as a
        monkeypatch.setattr(a.settings, "auth_cookie_enabled", False)
        req = _fake_request(cookies={"renfield_access": "COOKIEJWT"}, auth_header=None)
        assert await a._cookie_or_bearer_token(req) is None
        assert req.state.auth_via == "none"


# =============================================================================
# Cookie set / clear helpers
# =============================================================================
class TestCookieHelpers:
    @pytest.mark.unit
    def test_set_cookies_flag_on(self, monkeypatch):
        from api.routes import auth as ar
        monkeypatch.setattr(ar.settings, "auth_cookie_enabled", True)
        resp = MagicMock(spec=Response)
        ar._set_auth_cookies(resp, access_token="A", refresh_token="R")
        names = {c.args[0] if c.args else c.kwargs.get("key") for c in resp.set_cookie.call_args_list}
        assert names == {"renfield_access", "renfield_refresh", "renfield_csrf"}
        # access + refresh are HttpOnly; csrf is not.
        by_name = {}
        for c in resp.set_cookie.call_args_list:
            n = c.args[0] if c.args else c.kwargs["key"]
            by_name[n] = c.kwargs
        assert by_name["renfield_access"]["httponly"] is True
        assert by_name["renfield_refresh"]["httponly"] is True
        assert by_name["renfield_refresh"]["path"] == "/api/auth/refresh"
        assert by_name["renfield_csrf"]["httponly"] is False

    @pytest.mark.unit
    def test_set_cookies_flag_off_noop(self, monkeypatch):
        from api.routes import auth as ar
        monkeypatch.setattr(ar.settings, "auth_cookie_enabled", False)
        resp = MagicMock(spec=Response)
        ar._set_auth_cookies(resp, access_token="A", refresh_token="R")
        resp.set_cookie.assert_not_called()

    @pytest.mark.unit
    def test_helpers_tolerate_none_response(self, monkeypatch):
        from api.routes import auth as ar
        monkeypatch.setattr(ar.settings, "auth_cookie_enabled", True)
        # Direct unit-call path (FastAPI would inject a real Response on a request).
        ar._set_auth_cookies(None, access_token="A", refresh_token="R")
        ar._clear_auth_cookies(None)


# =============================================================================
# CSRF middleware (double-submit)
# =============================================================================
def _csrf_client(monkeypatch, *, enabled=True):
    from main import CSRFMiddleware
    import main as m
    monkeypatch.setattr(m.settings, "auth_cookie_enabled", enabled)
    app = FastAPI()
    app.add_middleware(CSRFMiddleware)

    @app.get("/api/x")
    def _gx():
        return {"ok": True}

    @app.post("/api/x")
    def _px():
        return {"ok": True}

    @app.post("/api/internal/x")
    def _pix():
        return {"ok": True}

    return TestClient(app)


class TestCsrfMiddleware:
    @pytest.mark.unit
    def test_get_is_exempt(self, monkeypatch):
        c = _csrf_client(monkeypatch)
        c.cookies.set("renfield_access", "A")
        assert c.get("/api/x").status_code == 200

    @pytest.mark.unit
    def test_post_cookie_valid_token_passes(self, monkeypatch):
        c = _csrf_client(monkeypatch)
        c.cookies.set("renfield_access", "A")
        c.cookies.set("renfield_csrf", "TOK")
        r = c.post("/api/x", headers={"X-CSRF-Token": "TOK"})
        assert r.status_code == 200

    @pytest.mark.unit
    def test_post_cookie_missing_header_403(self, monkeypatch):
        c = _csrf_client(monkeypatch)
        c.cookies.set("renfield_access", "A")
        c.cookies.set("renfield_csrf", "TOK")
        assert c.post("/api/x").status_code == 403

    @pytest.mark.unit
    def test_post_cookie_mismatched_header_403(self, monkeypatch):
        c = _csrf_client(monkeypatch)
        c.cookies.set("renfield_access", "A")
        c.cookies.set("renfield_csrf", "TOK")
        assert c.post("/api/x", headers={"X-CSRF-Token": "WRONG"}).status_code == 403

    @pytest.mark.unit
    def test_post_bearer_no_cookie_is_exempt(self, monkeypatch):
        # No auth cookie present → Bearer/legacy request → structurally CSRF-immune.
        c = _csrf_client(monkeypatch)
        assert c.post("/api/x").status_code == 200

    @pytest.mark.unit
    def test_internal_path_exempt(self, monkeypatch):
        c = _csrf_client(monkeypatch)
        c.cookies.set("renfield_access", "A")
        assert c.post("/api/internal/x").status_code == 200

    @pytest.mark.unit
    def test_flag_off_no_enforcement(self, monkeypatch):
        c = _csrf_client(monkeypatch, enabled=False)
        c.cookies.set("renfield_access", "A")
        assert c.post("/api/x").status_code == 200


# =============================================================================
# Refresh cookie dual-read (High finding: {} body 422'd before the cookie read)
# =============================================================================
def _cookie_limiter_request(cookie_header: str, path="/api/auth/refresh"):
    """A real starlette Request (so @limiter.limit is happy) carrying a Cookie
    header, mirroring test_auth_audit_remediation._limiter_request."""
    from starlette.requests import Request

    from services.api_rate_limiter import limiter as app_limiter
    state = type("S", (), {})()
    state.limiter = app_limiter
    app = type("A", (), {})()
    app.state = state
    return Request({
        "type": "http", "method": "POST", "path": path,
        "headers": [(b"cookie", cookie_header.encode())],
        "client": ("127.0.0.1", 12345), "app": app,
    })


@pytest.mark.database
@pytest.mark.asyncio
class TestRefreshCookieDualRead:
    def test_refresh_request_now_optional(self):
        # The fix: a present `{}` body no longer 422s — RefreshRequest constructs
        # with no token (handler then reads the HttpOnly refresh cookie).
        from api.routes.auth import RefreshRequest
        assert RefreshRequest().refresh_token is None

    async def test_refresh_reads_cookie_when_body_absent(self, db_session, monkeypatch):
        from unittest.mock import patch
        from api.routes.auth import refresh_token
        from services import auth_service
        from services.auth_service import create_refresh_token
        monkeypatch.setattr(auth_service.settings, "auth_enabled", True, raising=False)
        monkeypatch.setattr(auth_service.settings, "auth_cookie_enabled", True, raising=False)
        monkeypatch.setattr(auth_service.settings, "refresh_cookie_name", "renfield_refresh", raising=False)

        from models.database import Role, User
        role = Role(name="RCK", description="", permissions=["chat.own"])
        db_session.add(role)
        await db_session.flush()
        user = User(username="rck", password_hash="x", role_id=role.id,
                    is_active=True, token_epoch=0)
        db_session.add(user)
        await db_session.commit()
        await db_session.refresh(user)

        token = create_refresh_token(user.id, token_epoch=0)
        req = _cookie_limiter_request(f"renfield_refresh={token}")
        with patch("services.token_blacklist.token_blacklist.is_blacklisted",
                   new=AsyncMock(return_value=False)), \
             patch("services.token_blacklist.token_blacklist.add",
                   new=AsyncMock(return_value=True)):
            # refresh_request=None (no body) → handler must fall through to the cookie.
            resp = await refresh_token(request=req, refresh_request=None, db=db_session)
        assert resp.access_token and resp.refresh_token


# =============================================================================
# WebSocket Strategy-0 cookie read
# =============================================================================
@pytest.fixture
def ws_session_factory(monkeypatch, db_session):
    import services.database as db_mod
    smk = async_sessionmaker(db_session.bind, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(db_mod, "AsyncSessionLocal", smk)
    return smk


async def _mk_user(smk, *, epoch=0):
    from models.database import Role, User
    async with smk() as db:
        role = Role(name="WSC", description="", permissions=["chat.own"])
        db.add(role)
        await db.flush()
        user = User(username="wsc", password_hash="x", role_id=role.id,
                    is_active=True, token_epoch=epoch)
        db.add(user)
        await db.commit()
        await db.refresh(user)
        return user.id


def _fake_ws(cookies):
    m = MagicMock()
    m.headers = {}          # no Origin → non-browser, allowlist-exempt
    m.cookies = cookies
    return m


@pytest.mark.database
@pytest.mark.asyncio
class TestWebSocketCookieAuth:
    async def test_cookie_authenticates_ws(self, ws_session_factory, monkeypatch):
        from services import websocket_auth as wa
        from services.auth_service import create_access_token
        monkeypatch.setattr(wa.settings, "ws_auth_enabled", True)
        monkeypatch.setattr(wa.settings, "auth_cookie_enabled", True)
        monkeypatch.setattr(wa.settings, "auth_cookie_name", "renfield_access")
        uid = await _mk_user(ws_session_factory, epoch=0)
        cookie = create_access_token(data={"sub": str(uid)}, token_epoch=0)
        result = await wa.authenticate_websocket(_fake_ws({"renfield_access": cookie}))
        assert result and result.get("user_id") == uid

    async def test_epoch_stale_cookie_rejected(self, ws_session_factory, monkeypatch):
        from services import websocket_auth as wa
        from services.auth_service import create_access_token
        monkeypatch.setattr(wa.settings, "ws_auth_enabled", True)
        monkeypatch.setattr(wa.settings, "auth_cookie_enabled", True)
        monkeypatch.setattr(wa.settings, "auth_cookie_name", "renfield_access")
        uid = await _mk_user(ws_session_factory, epoch=3)
        stale = create_access_token(data={"sub": str(uid)}, token_epoch=2)  # < 3
        assert await wa.authenticate_websocket(_fake_ws({"renfield_access": stale})) is None

    async def test_flag_off_ignores_cookie(self, ws_session_factory, monkeypatch):
        from services import websocket_auth as wa
        from services.auth_service import create_access_token
        monkeypatch.setattr(wa.settings, "ws_auth_enabled", True)
        monkeypatch.setattr(wa.settings, "auth_cookie_enabled", False)
        uid = await _mk_user(ws_session_factory, epoch=0)
        cookie = create_access_token(data={"sub": str(uid)}, token_epoch=0)
        # Flag off → cookie not read; no query token → unauthenticated → None.
        assert await wa.authenticate_websocket(_fake_ws({"renfield_access": cookie})) is None
