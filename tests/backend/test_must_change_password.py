"""Tests for must_change_password enforcement (#694).

A user flagged must_change_password may reach ONLY the allowlisted auth
endpoints until they rotate; every other route is 403 password_change_required.
Enforced in get_current_user (the single authenticated chokepoint), using DB
truth so a token minted before the flag was set is still blocked.
"""
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

import services.auth_service as auth_service
from models.database import User


def _user(must_change: bool):
    u = User()
    u.id = 1
    u.username = "alice"
    u.is_active = True
    u.must_change_password = must_change
    return u


def _request(path: str):
    req = MagicMock()
    req.url = MagicMock()
    req.url.path = path
    return req


@pytest.fixture
def _wired(monkeypatch):
    """Wire get_current_user so it resolves a fixed user from a fixed token."""
    monkeypatch.setattr(auth_service.settings, "auth_enabled", True, raising=False)
    monkeypatch.setattr(
        auth_service, "decode_token",
        lambda _t: {"type": "access", "jti": "jti-1", "sub": "1"},
    )
    # token blacklist is imported inside the function from services.token_blacklist
    import services.token_blacklist as tbl
    monkeypatch.setattr(tbl.token_blacklist, "is_blacklisted", AsyncMock(return_value=False))
    return monkeypatch


class TestMustChangePasswordEnforcement:
    @pytest.mark.unit
    async def test_flagged_user_blocked_on_normal_route(self, _wired):
        _wired.setattr(auth_service, "get_user_by_id", AsyncMock(return_value=_user(True)))
        with pytest.raises(HTTPException) as exc:
            await auth_service.get_current_user(
                token="x", db=MagicMock(), request=_request("/api/chat"),
            )
        assert exc.value.status_code == 403
        assert exc.value.detail == "password_change_required"

    @pytest.mark.unit
    async def test_flagged_user_allowed_on_change_password(self, _wired):
        _wired.setattr(auth_service, "get_user_by_id", AsyncMock(return_value=_user(True)))
        user = await auth_service.get_current_user(
            token="x", db=MagicMock(), request=_request("/api/auth/change-password"),
        )
        assert user.id == 1

    @pytest.mark.unit
    @pytest.mark.parametrize("path", [
        "/api/auth/me", "/api/auth/status", "/api/auth/logout",
    ])
    async def test_flagged_user_allowed_on_allowlisted_paths(self, _wired, path):
        _wired.setattr(auth_service, "get_user_by_id", AsyncMock(return_value=_user(True)))
        user = await auth_service.get_current_user(
            token="x", db=MagicMock(), request=_request(path),
        )
        assert user.id == 1

    @pytest.mark.unit
    async def test_unflagged_user_passes_everywhere(self, _wired):
        _wired.setattr(auth_service, "get_user_by_id", AsyncMock(return_value=_user(False)))
        user = await auth_service.get_current_user(
            token="x", db=MagicMock(), request=_request("/api/chat"),
        )
        assert user.id == 1

    @pytest.mark.unit
    async def test_no_request_skips_enforcement(self, _wired):
        """The internal get_optional_user path passes request=None → no enforcement."""
        _wired.setattr(auth_service, "get_user_by_id", AsyncMock(return_value=_user(True)))
        user = await auth_service.get_current_user(
            token="x", db=MagicMock(), request=None,
        )
        assert user.id == 1


class TestGetOptionalUserReraises:
    """get_optional_user must re-raise the forced-rotation 403, not swallow it (#694 review)."""

    @pytest.mark.unit
    async def test_optional_user_reraises_password_change_403(self, _wired):
        _wired.setattr(auth_service, "get_user_by_id", AsyncMock(return_value=_user(True)))
        with pytest.raises(HTTPException) as exc:
            await auth_service.get_optional_user(
                token="x", db=MagicMock(), request=_request("/api/preferences"),
            )
        assert exc.value.status_code == 403
        assert exc.value.detail == "password_change_required"

    @pytest.mark.unit
    async def test_optional_user_allows_flagged_on_allowlisted_path(self, _wired):
        _wired.setattr(auth_service, "get_user_by_id", AsyncMock(return_value=_user(True)))
        user = await auth_service.get_optional_user(
            token="x", db=MagicMock(), request=_request("/api/auth/me"),
        )
        assert user.id == 1

    @pytest.mark.unit
    async def test_optional_user_returns_none_on_missing_token(self, _wired):
        assert await auth_service.get_optional_user(
            token=None, db=MagicMock(), request=_request("/api/preferences"),
        ) is None


class TestWebSocketMustChangePassword:
    """The WS auth path must also reject a flagged user (#694 review — WS bypass)."""

    @pytest.mark.unit
    async def test_ws_auth_rejects_flagged_user(self, monkeypatch):
        import services.auth_service as auth_svc
        import services.websocket_auth as ws_auth

        monkeypatch.setattr(ws_auth.settings, "ws_auth_enabled", True, raising=False)
        monkeypatch.setattr(
            auth_svc, "decode_token",
            lambda _t: {"type": "access", "sub": "1"},
        )
        monkeypatch.setattr(auth_svc, "get_user_by_id", AsyncMock(return_value=_user(True)))

        class _FakeSession:
            async def __aenter__(self): return MagicMock()
            async def __aexit__(self, *a): return False
        import services.database as db_mod
        monkeypatch.setattr(db_mod, "AsyncSessionLocal", lambda: _FakeSession())

        result = await ws_auth.authenticate_websocket(MagicMock(), token="jwt")
        assert result is None  # flagged user rejected on the WS surface

    @pytest.mark.unit
    async def test_ws_auth_allows_unflagged_user(self, monkeypatch):
        import services.auth_service as auth_svc
        import services.websocket_auth as ws_auth

        monkeypatch.setattr(ws_auth.settings, "ws_auth_enabled", True, raising=False)
        monkeypatch.setattr(
            auth_svc, "decode_token",
            lambda _t: {"type": "access", "sub": "1"},
        )
        monkeypatch.setattr(auth_svc, "get_user_by_id", AsyncMock(return_value=_user(False)))

        class _FakeSession:
            async def __aenter__(self): return MagicMock()
            async def __aexit__(self, *a): return False
        import services.database as db_mod
        monkeypatch.setattr(db_mod, "AsyncSessionLocal", lambda: _FakeSession())

        result = await ws_auth.authenticate_websocket(MagicMock(), token="jwt")
        assert result is not None
        assert result["user_id"] == 1
