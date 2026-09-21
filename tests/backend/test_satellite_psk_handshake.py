"""Satellite enrollment PSK as the WebSocket HANDSHAKE credential (D-4c).

`docs/design/household-auth-on-cutover.md` §6.1 Nr. 1: under AUTH_ENABLED=true a
satellite needs a durable credential at the handshake; nothing durable existed
(user JWT 24 h, device token 60 min / in memory). The per-satellite PSK is
accepted in the self-identifying form ``sat.<satellite_id>.<secret>`` so exactly
one row is verified. Dark behind SATELLITE_PSK_HANDSHAKE_ENABLED; auth-off never
reads the header at all.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock

import pytest

import ha_glue.services.satellite_enrollment_service as svc
from services import websocket_auth


# ------------------------------------------------------------------ helpers
def _ws(token: str | None, host: str | None = "10.0.0.7") -> MagicMock:
    ws = MagicMock()
    ws.headers = {"authorization": f"Bearer {token}"} if token else {}
    ws.cookies = {}
    ws.client = MagicMock(host=host) if host else None
    return ws


@pytest.fixture
def psk_on(monkeypatch):
    monkeypatch.setattr(websocket_auth.settings, "auth_enabled", True)
    monkeypatch.setattr(websocket_auth.settings, "satellite_psk_handshake_enabled", True)
    monkeypatch.setattr(websocket_auth.settings, "auth_cookie_enabled", False)
    monkeypatch.setattr(websocket_auth.settings, "cors_origins", "*")


@pytest.fixture
def session_from(db_session, monkeypatch):
    """Route the strategy's AsyncSessionLocal() to the test session."""
    import services.database as db_mod

    @asynccontextmanager
    async def _session():
        yield db_session

    monkeypatch.setattr(db_mod, "AsyncSessionLocal", _session)
    return db_session


@pytest.fixture
def quiet_lockout(monkeypatch):
    """Lockout store out of the way (its own tests cover it); returns the spies."""
    from services.login_lockout import login_lockout

    spies = {
        "is_locked": AsyncMock(return_value=False),
        "record_failure": AsyncMock(return_value=False),
        "clear": AsyncMock(return_value=None),
    }
    for name, spy in spies.items():
        monkeypatch.setattr(login_lockout, name, spy)
    return spies


# ------------------------------------------------------------------ parsing
@pytest.mark.unit
class TestHandshakeTokenFormat:
    def test_round_trip(self):
        tok = svc.format_handshake_token("sat-kueche", "s3cr3t")
        assert tok == "sat.sat-kueche.s3cr3t"
        assert svc.parse_handshake_token(tok) == ("sat-kueche", "s3cr3t")

    @pytest.mark.parametrize("bad", [
        None, "", "sat", "sat.", "sat.only-id", "sat..secret", "sat.id.",
        "rfi.client.secret", "eyJhbGciOi.jwt.like", "SAT.id.secret",
    ])
    def test_rejects_foreign_or_malformed(self, bad):
        assert svc.parse_handshake_token(bad) is None

    def test_secret_may_contain_dots_id_may_not(self):
        # Ids are slugs; the first two dots delimit, the rest belongs to the secret.
        assert svc.parse_handshake_token("sat.sat-x.a.b.c") == ("sat-x", "a.b.c")


# ------------------------------------------------------------- authorization
@pytest.mark.database
class TestAuthorizeHandshake:
    async def test_valid_credential_returns_the_satellite_id(self, db_session, quiet_lockout):
        secret = await svc.enroll_satellite(db_session, "sat-wohnzimmer", room="Wohnzimmer")
        got = await svc.authorize_handshake(
            db_session, svc.format_handshake_token("sat-wohnzimmer", secret), "10.0.0.7",
        )
        assert got == "sat-wohnzimmer"
        quiet_lockout["clear"].assert_awaited_once()
        quiet_lockout["record_failure"].assert_not_awaited()

    async def test_wrong_secret_is_refused_and_counted(self, db_session, quiet_lockout):
        await svc.enroll_satellite(db_session, "sat-x")
        assert await svc.authorize_handshake(db_session, "sat.sat-x.nope", "10.0.0.7") is None
        quiet_lockout["record_failure"].assert_awaited_once()
        args = quiet_lockout["record_failure"].await_args.args
        assert args == ("sat:sat-x", "10.0.0.7")  # its own namespace, never a user

    async def test_unknown_id_is_refused(self, db_session, quiet_lockout):
        assert await svc.authorize_handshake(db_session, "sat.ghost.secret", "10.0.0.7") is None
        quiet_lockout["record_failure"].assert_awaited_once()

    async def test_no_spoof_resistant_address_means_no_lockout_at_all(self, db_session, quiet_lockout):
        """client_ip None (no TRUSTED_PROXIES): keying a lock on the satellite id
        alone would let any LAN host lock a real device out with five guesses
        against a room slug — so nothing is counted and nothing is checked."""
        await svc.enroll_satellite(db_session, "sat-x")
        assert await svc.authorize_handshake(db_session, "sat.sat-x.nope", None) is None
        quiet_lockout["is_locked"].assert_not_awaited()
        quiet_lockout["record_failure"].assert_not_awaited()
        quiet_lockout["clear"].assert_not_awaited()

    async def test_oversized_secret_is_malformed_not_an_error(self, db_session, quiet_lockout, monkeypatch):
        # passlib would raise PasswordSizeError above 4096 bytes — an error path
        # that bypassed the failure counter. Cap it as malformed instead.
        spy = AsyncMock(return_value=svc.VERDICT_BAD)
        monkeypatch.setattr(svc, "evaluate_credential", spy)
        assert svc.parse_handshake_token("sat.sat-x." + "a" * 257) is None
        assert await svc.authorize_handshake(db_session, "sat.sat-x." + "a" * 5000, "10.0.0.7") is None
        spy.assert_not_awaited()

    async def test_revoked_satellite_is_refused(self, db_session, quiet_lockout):
        secret = await svc.enroll_satellite(db_session, "sat-x")
        assert await svc.revoke_satellite(db_session, "sat-x") is True
        assert await svc.authorize_handshake(
            db_session, svc.format_handshake_token("sat-x", secret), None,
        ) is None

    async def test_locked_scope_never_reaches_the_hash_compare(
        self, db_session, quiet_lockout, monkeypatch,
    ):
        secret = await svc.enroll_satellite(db_session, "sat-x")
        quiet_lockout["is_locked"].return_value = True
        spy = AsyncMock(return_value=svc.VERDICT_OK)
        monkeypatch.setattr(svc, "evaluate_credential", spy)
        assert await svc.authorize_handshake(
            db_session, svc.format_handshake_token("sat-x", secret), "10.0.0.7",
        ) is None
        spy.assert_not_awaited()

    async def test_independent_of_the_enrollment_gate(self, db_session, quiet_lockout, monkeypatch):
        # The register-frame soak/enforce state machine is gated on
        # SATELLITE_ENROLLMENT_ENABLED; the handshake path always verifies.
        monkeypatch.setattr(svc.settings, "satellite_enrollment_enabled", False)
        secret = await svc.enroll_satellite(db_session, "sat-x")
        assert await svc.authorize_handshake(
            db_session, svc.format_handshake_token("sat-x", secret), None,
        ) == "sat-x"

    async def test_foreign_token_is_not_ours(self, db_session, quiet_lockout):
        assert await svc.authorize_handshake(db_session, "eyJ.jwt.like", None) is None
        quiet_lockout["record_failure"].assert_not_awaited()


# ------------------------------------------------------- websocket strategy
@pytest.mark.database
class TestWebsocketStrategy:
    async def test_bearer_psk_authenticates_without_a_user(
        self, psk_on, session_from, quiet_lockout,
    ):
        secret = await svc.enroll_satellite(session_from, "sat-kueche")
        ws = _ws(svc.format_handshake_token("sat-kueche", secret))
        result = await websocket_auth.authenticate_websocket(ws, None, allow_satellite_psk=True)
        assert result == {
            "authenticated": True, "auth_method": "satellite_psk", "satellite_id": "sat-kueche",
        }
        assert "user_id" not in result  # device-account binding is P0 Nr. 3

    async def test_only_the_satellite_endpoint_accepts_the_psk(
        self, psk_on, session_from, quiet_lockout, monkeypatch,
    ):
        """A valid satellite credential must not open /ws, /ws/kiosk, /ws/user,
        the wakeword or KG-live sockets: every other endpoint calls
        authenticate_websocket WITHOUT allow_satellite_psk and gets None — and
        the token is never handed to the JWT / device-token strategies."""
        secret = await svc.enroll_satellite(session_from, "sat-kueche")
        store = MagicMock()
        store.validate_token.return_value = {"authenticated": True, "device_id": "x"}
        monkeypatch.setattr(websocket_auth, "get_token_store", lambda: store)
        verify = AsyncMock(return_value="sat-kueche")
        monkeypatch.setattr(svc, "authorize_handshake", verify)

        ws = _ws(svc.format_handshake_token("sat-kueche", secret))
        assert await websocket_auth.authenticate_websocket(ws, None) is None
        verify.assert_not_awaited()
        store.validate_token.assert_not_called()

    async def test_every_other_ws_endpoint_calls_without_the_satellite_flag(self):
        # Pin the call sites: only satellite_handler passes allow_satellite_psk=True.
        import inspect

        from api.websocket import chat_handler, kg_live_handler, kiosk_handler, user_events_handler
        from ha_glue.api.websocket import device_handler, satellite_handler
        import main

        for mod in (chat_handler, kg_live_handler, kiosk_handler, user_events_handler, device_handler, main):
            src = inspect.getsource(mod)
            assert "allow_satellite_psk=True" not in src, mod.__name__
        assert "authenticate_websocket(websocket, token, allow_satellite_psk=True)" in inspect.getsource(
            satellite_handler.satellite_websocket
        )

    async def test_flag_off_leaves_the_token_to_the_other_strategies(
        self, session_from, quiet_lockout, monkeypatch,
    ):
        monkeypatch.setattr(websocket_auth.settings, "auth_enabled", True)
        monkeypatch.setattr(websocket_auth.settings, "satellite_psk_handshake_enabled", False)
        monkeypatch.setattr(websocket_auth.settings, "auth_cookie_enabled", False)
        monkeypatch.setattr(websocket_auth.settings, "cors_origins", "*")
        secret = await svc.enroll_satellite(session_from, "sat-kueche")
        ws = _ws(svc.format_handshake_token("sat-kueche", secret))
        # Flag off: a `sat.` token is refused outright (before: it fell through
        # to JWT + device-token decoding and failed both — same outcome).
        assert await websocket_auth.authenticate_websocket(ws, None, allow_satellite_psk=True) is None
        quiet_lockout["clear"].assert_not_awaited()

    async def test_auth_off_never_reads_the_header(self, session_from, monkeypatch):
        monkeypatch.setattr(websocket_auth.settings, "auth_enabled", False)
        monkeypatch.setattr(websocket_auth.settings, "satellite_psk_handshake_enabled", True)
        monkeypatch.setattr(websocket_auth.settings, "cors_origins", "*")
        ws = _ws("sat.sat-kueche.whatever")
        assert await websocket_auth.authenticate_websocket(ws, None) == {
            "authenticated": True, "auth_skipped": True,
        }

    async def test_bad_psk_does_not_fall_through_to_jwt_or_device_token(
        self, psk_on, session_from, quiet_lockout, monkeypatch,
    ):
        await svc.enroll_satellite(session_from, "sat-kueche")
        store = MagicMock()
        store.validate_token.return_value = {"authenticated": True, "device_id": "x"}
        monkeypatch.setattr(websocket_auth, "get_token_store", lambda: store)
        ws = _ws("sat.sat-kueche.wrong")
        assert await websocket_auth.authenticate_websocket(ws, None, allow_satellite_psk=True) is None
        store.validate_token.assert_not_called()

    async def test_lockout_scope_is_per_address_only_when_spoof_resistant(
        self, psk_on, session_from, quiet_lockout, monkeypatch,
    ):
        """Mirrors the login route: behind Traefik the socket peer is the proxy
        pod for every client, so the address joins the lockout key only when
        TRUSTED_PROXIES makes it spoof-resistant; otherwise the scope is
        satellite-wide (None)."""
        import services.api_rate_limiter as rl

        secret = await svc.enroll_satellite(session_from, "sat-kueche")
        tok = svc.format_handshake_token("sat-kueche", secret)
        spy = AsyncMock(return_value="sat-kueche")
        monkeypatch.setattr(svc, "authorize_handshake", spy)

        monkeypatch.setattr(rl, "client_ip_is_spoof_resistant", lambda: False)
        await websocket_auth.authenticate_websocket(_ws(tok), None, allow_satellite_psk=True)
        assert spy.await_args.args[2] is None

        monkeypatch.setattr(rl, "client_ip_is_spoof_resistant", lambda: True)
        monkeypatch.setattr(rl, "get_client_ip", lambda ws: "192.0.2.10")
        await websocket_auth.authenticate_websocket(_ws(tok), None, allow_satellite_psk=True)
        assert spy.await_args.args[2] == "192.0.2.10"

    async def test_empty_query_token_does_not_poison_the_header_path(
        self, psk_on, session_from, quiet_lockout,
    ):
        # `?token=` (empty) used to mark the token as URL-borne even though the
        # credential then came from the Authorization header.
        secret = await svc.enroll_satellite(session_from, "sat-kueche")
        ws = _ws(svc.format_handshake_token("sat-kueche", secret))
        result = await websocket_auth.authenticate_websocket(ws, "", allow_satellite_psk=True)
        assert result and result["satellite_id"] == "sat-kueche"

    async def test_psk_in_the_url_is_refused(self, psk_on, session_from, quiet_lockout):
        secret = await svc.enroll_satellite(session_from, "sat-kueche")
        ws = _ws(None)
        tok = svc.format_handshake_token("sat-kueche", secret)
        assert await websocket_auth.authenticate_websocket(ws, tok, allow_satellite_psk=True) is None
        quiet_lockout["record_failure"].assert_not_awaited()  # refused before any verify

    async def test_satellite_route_is_still_registered_on_the_endpoint(self):
        """Regression guard: a helper inserted between `@router.websocket` and
        the endpoint silently re-targets the decorator (review catch on this
        very branch). Pin the route → endpoint binding."""
        from fastapi.routing import APIWebSocketRoute

        from ha_glue.api.websocket import satellite_handler as sh

        ws_routes = {
            r.path: r.endpoint for r in sh.router.routes if isinstance(r, APIWebSocketRoute)
        }
        assert ws_routes.get("/ws/satellite") is sh.satellite_websocket
        assert sh._handshake_identity_mismatch not in ws_routes.values()

    async def test_psk_handshake_satisfies_the_register_gate_only_without_a_token(self):
        """One secret, provisioned once: a handshake-authenticated satellite whose
        register frame carries no token counts as enrolled; a presented token is
        still verified (a wrong one must stay loud)."""
        import inspect

        from ha_glue.api.websocket import satellite_handler as sh

        psk = {"authenticated": True, "auth_method": "satellite_psk", "satellite_id": "sat-a"}
        assert sh._handshake_satisfies_enrollment(psk, None) is True
        assert sh._handshake_satisfies_enrollment(psk, "") is True
        assert sh._handshake_satisfies_enrollment(psk, "some-token") is False
        assert sh._handshake_satisfies_enrollment({"authenticated": True, "user_id": 1}, None) is False
        assert sh._handshake_satisfies_enrollment({"authenticated": True, "auth_skipped": True}, None) is False
        src = inspect.getsource(sh.satellite_websocket)
        assert "_handshake_satisfies_enrollment(auth_result, enrollment_psk)" in src
        # Connection limiter runs BEFORE the (bcrypt-costing) handshake auth.
        assert src.index("connection_limiter.can_connect(") < src.index("authenticate_websocket(")

    async def test_bcrypt_runs_off_the_event_loop(self):
        import inspect

        src = inspect.getsource(svc.evaluate_credential)
        assert "asyncio.to_thread(pwd_context.verify" in src
        assert "asyncio.to_thread(pwd_context.dummy_verify" in src

    async def test_handshake_identity_is_bound_to_the_register_frame(self):
        import inspect

        from ha_glue.api.websocket import satellite_handler as sh

        psk = {"authenticated": True, "auth_method": "satellite_psk", "satellite_id": "sat-a"}
        assert sh._handshake_identity_mismatch(psk, "sat-b") is True
        assert sh._handshake_identity_mismatch(psk, "sat-a") is False
        # No handshake identity (JWT / device token / auth-off) → never a mismatch.
        assert sh._handshake_identity_mismatch({"authenticated": True, "user_id": 3}, "sat-b") is False
        assert sh._handshake_identity_mismatch({"authenticated": True, "auth_skipped": True}, "x") is False
        assert sh._handshake_identity_mismatch(None, "x") is False
        # The handler consults it on the register frame and closes 4001.
        src = inspect.getsource(sh.satellite_websocket)
        assert "_handshake_identity_mismatch(auth_result, satellite_id)" in src
        assert 'reason="identity-mismatch"' in src

    async def test_db_error_fails_closed(self, psk_on, quiet_lockout, monkeypatch):
        import services.database as db_mod

        @asynccontextmanager
        async def _boom():
            raise RuntimeError("db down")
            yield  # pragma: no cover

        monkeypatch.setattr(db_mod, "AsyncSessionLocal", _boom)
        assert await websocket_auth.authenticate_websocket(
            _ws("sat.sat-x.s"), None, allow_satellite_psk=True,
        ) is None
