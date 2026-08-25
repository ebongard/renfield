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

    async def test_falls_back_to_bearer(self, monkeypatch):
        from services import auth_service as a
        monkeypatch.setattr(a.settings, "auth_cookie_enabled", True)
        monkeypatch.setattr(a.settings, "auth_cookie_name", "renfield_access")
        req = _fake_request(cookies={}, auth_header="Bearer HEADERJWT")
        assert await a._cookie_or_bearer_token(req) == "HEADERJWT"

    async def test_flag_off_ignores_cookie(self, monkeypatch):
        from services import auth_service as a
        monkeypatch.setattr(a.settings, "auth_cookie_enabled", False)
        req = _fake_request(cookies={"renfield_access": "COOKIEJWT"}, auth_header=None)
        assert await a._cookie_or_bearer_token(req) is None


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

    @pytest.mark.asyncio
    async def test_non_ascii_csrf_header_403_not_500(self, monkeypatch):
        # The fix-2 regression: compare_digest on a non-ASCII str would TypeError
        # → 500. With bytes-encoding it's a clean mismatch → 403. Call dispatch
        # directly with a latin-1 header value (as Starlette hands it to the
        # middleware on a real server) — httpx's TestClient rejects non-ASCII
        # header values client-side, so it can't exercise this path.
        import main as m
        from main import CSRFMiddleware
        monkeypatch.setattr(m.settings, "auth_cookie_enabled", True)
        mw = CSRFMiddleware(app=None)
        req = SimpleNamespace(
            method="POST",
            url=SimpleNamespace(path="/api/x"),
            cookies={"renfield_access": "A", "renfield_csrf": "TOK"},
            headers={"X-CSRF-Token": "café"},
        )

        async def _call_next(_r):
            raise AssertionError("handler must not be reached on a CSRF failure")

        resp = await mw.dispatch(req, _call_next)
        assert resp.status_code == 403


@pytest.mark.unit
def test_set_cookies_emits_real_setcookie(monkeypatch):
    """End-to-end helper wiring: a real fastapi Response actually gets the three
    Set-Cookie headers with the right attributes (the MagicMock test only checks
    the call, this checks the emitted headers)."""
    from api.routes import auth as ar
    monkeypatch.setattr(ar.settings, "auth_cookie_enabled", True)
    r = Response()
    ar._set_auth_cookies(r, access_token="AAA", refresh_token="RRR")
    emitted = " | ".join(v.decode() for k, v in r.raw_headers if k == b"set-cookie")
    assert "renfield_access=AAA" in emitted and "httponly" in emitted.lower()
    assert "renfield_refresh=RRR" in emitted and "/api/auth/refresh" in emitted
    assert "renfield_csrf=" in emitted


@pytest.mark.asyncio
async def test_ws_scoped_token_rejected_on_rest(monkeypatch):
    """A scope='ws' token delivered via the access cookie must NOT authenticate
    the REST API (only the WS handshake) — get_current_user rejects it before any
    DB use, so db=None is safe here."""
    from fastapi import HTTPException
    from services import auth_service as a
    monkeypatch.setattr(a.settings, "auth_enabled", True)
    ws_token = a.create_ws_token_jwt(user_id=1, token_epoch=0)
    with pytest.raises(HTTPException) as exc:
        await a.get_current_user(token=ws_token, db=None, request=None)
    assert exc.value.status_code == 401


# =============================================================================
# Voice-WS faucet token (voice-WS migration): short-lived scope:"voice"
# =============================================================================
class TestVoiceFaucetToken:
    @pytest.mark.unit
    def test_minter_voice_scope(self):
        from jose import jwt
        from services.auth_service import ALGORITHM, WS_FAUCET_SCOPES, create_ws_token_jwt
        from utils.config import settings
        assert "voice" in WS_FAUCET_SCOPES
        tok = create_ws_token_jwt(1, 0, scope="voice")
        p = jwt.decode(tok, settings.secret_key.get_secret_value(), algorithms=[ALGORITHM])
        assert p["scope"] == "voice" and p["type"] == "access"

    @pytest.mark.unit
    def test_minter_default_is_ws(self):
        from jose import jwt
        from services.auth_service import ALGORITHM, create_ws_token_jwt
        from utils.config import settings
        p = jwt.decode(
            create_ws_token_jwt(1, 0),
            settings.secret_key.get_secret_value(), algorithms=[ALGORITHM],
        )
        assert p["scope"] == "ws"

    @pytest.mark.unit
    def test_minter_invalid_scope_raises(self):
        from services.auth_service import create_ws_token_jwt
        with pytest.raises(ValueError, match="scope"):
            create_ws_token_jwt(1, 0, scope="bogus")

    @pytest.mark.asyncio
    async def test_voice_token_rejected_on_rest(self, monkeypatch):
        """A harvested scope:voice token must NOT work against the REST API."""
        from fastapi import HTTPException
        from services import auth_service as a
        monkeypatch.setattr(a.settings, "auth_enabled", True)
        tok = a.create_ws_token_jwt(1, 0, scope="voice")
        with pytest.raises(HTTPException) as exc:
            await a.get_current_user(token=tok, db=None, request=None)
        assert exc.value.status_code == 401

    @pytest.mark.database
    @pytest.mark.asyncio
    async def test_voice_token_rejected_on_chat_ws(self, ws_session_factory, monkeypatch):
        """A scope:voice token must NOT open renfield's own /ws/* (chat) — only the
        external voice-server's /ws/voice accepts it (via internal_auth.verify)."""
        from services import websocket_auth as wa
        from services.auth_service import create_ws_token_jwt
        monkeypatch.setattr(wa.settings, "ws_auth_enabled", True)
        uid = await _mk_user(ws_session_factory, epoch=0)
        tok = create_ws_token_jwt(uid, 0, scope="voice")
        # via query token (chat WS reads ?token=) → rejected
        assert await wa.authenticate_websocket(_fake_ws({}), token=tok) is None

    @pytest.mark.asyncio
    async def test_faucet_route_purpose_threading(self, monkeypatch):
        """/api/ws/token: bad purpose → 422; purpose=voice → a voice-scoped token;
        WS auth off → {token:None}."""
        from fastapi import HTTPException
        from jose import jwt
        from main import create_ws_token
        from services import auth_service as a
        from utils.config import settings
        user = MagicMock(id=7, token_epoch=0)

        monkeypatch.setattr(a.settings, "ws_auth_enabled", True, raising=False)
        # bad purpose → 422 (before any minting)
        with pytest.raises(HTTPException) as exc:
            await create_ws_token(purpose="bogus", current_user=user)
        assert exc.value.status_code == 422

        # purpose=voice → voice-scoped token bound to the caller
        res = await create_ws_token(purpose="voice", current_user=user)
        p = jwt.decode(res["token"], settings.secret_key.get_secret_value(),
                       algorithms=[a.ALGORITHM])
        assert p["scope"] == "voice" and p["sub"] == "7"

        # WS auth disabled (household) → no token regardless of purpose
        monkeypatch.setattr(a.settings, "ws_auth_enabled", False, raising=False)
        res_off = await create_ws_token(purpose="voice", current_user=user)
        assert res_off["token"] is None

    @pytest.mark.database
    @pytest.mark.asyncio
    async def test_voice_token_accepted_by_internal_verify(self, ws_session_factory, monkeypatch):
        """internal_auth.verify accepts a scope:voice token (the voice-server path)
        — it only rejects scope:ws."""
        from unittest.mock import patch
        from api.routes.internal_auth import VerifyRequest, verify_token
        from services import auth_service as a
        monkeypatch.setattr(a.settings, "auth_enabled", True)
        monkeypatch.setattr(a.settings, "internal_auth_verify_secret", None, raising=False)
        uid = await _mk_user(ws_session_factory, epoch=0)
        tok = a.create_ws_token_jwt(uid, 0, scope="voice")
        # verify_token has no db param — it opens AsyncSessionLocal internally,
        # which the ws_session_factory fixture patches to the test engine.
        with patch("services.token_blacklist.token_blacklist.is_blacklisted",
                   new=AsyncMock(return_value=False)):
            result = await verify_token(VerifyRequest(token=tok), x_verify_secret=None)
        assert int(result.get("user_id")) == uid and result.get("scope") == "voice"


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
