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
        assert await svc.authorize_handshake(db_session, "sat.ghost.secret", None) is None
        quiet_lockout["record_failure"].assert_awaited_once()

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
        result = await websocket_auth.authenticate_websocket(ws, None)
        assert result == {
            "authenticated": True, "auth_method": "satellite_psk", "satellite_id": "sat-kueche",
        }
        assert "user_id" not in result  # device-account binding is P0 Nr. 3

    async def test_flag_off_leaves_the_token_to_the_other_strategies(
        self, session_from, quiet_lockout, monkeypatch,
    ):
        monkeypatch.setattr(websocket_auth.settings, "auth_enabled", True)
        monkeypatch.setattr(websocket_auth.settings, "satellite_psk_handshake_enabled", False)
        monkeypatch.setattr(websocket_auth.settings, "auth_cookie_enabled", False)
        monkeypatch.setattr(websocket_auth.settings, "cors_origins", "*")
        secret = await svc.enroll_satellite(session_from, "sat-kueche")
        ws = _ws(svc.format_handshake_token("sat-kueche", secret))
        # Not a JWT, not a device token → None; the PSK strategy never ran.
        assert await websocket_auth.authenticate_websocket(ws, None) is None
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
        assert await websocket_auth.authenticate_websocket(ws, None) is None
        store.validate_token.assert_not_called()

    async def test_psk_in_the_url_is_refused(self, psk_on, session_from, quiet_lockout):
        secret = await svc.enroll_satellite(session_from, "sat-kueche")
        ws = _ws(None)
        tok = svc.format_handshake_token("sat-kueche", secret)
        assert await websocket_auth.authenticate_websocket(ws, tok) is None
        quiet_lockout["record_failure"].assert_not_awaited()  # refused before any verify

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
        assert await websocket_auth.authenticate_websocket(_ws("sat.sat-x.s"), None) is None
