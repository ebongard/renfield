"""Wiring of the login route to the lockout store (BL-0125).

The per-IP scope exists only if ``/auth/login`` actually hands the client IP to
``login_lockout`` — a refactor that dropped the second argument would silently
fall back to the strict username-wide lock (the DoS the change removed) while
every unit test of the store stayed green. These tests call the route function
directly (pattern from test_auth_audit_remediation.py) with the store and the
IP helpers replaced, and pin each call site.
"""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from api.routes.auth import login


def _limiter_request(path="/api/auth/login"):
    """A real starlette Request so slowapi's @limiter.limit doesn't choke when the
    route handler is called directly."""
    from starlette.requests import Request

    from services.api_rate_limiter import limiter as app_limiter

    state = type("S", (), {})()
    state.limiter = app_limiter
    app = type("A", (), {})()
    app.state = state
    return Request({
        "type": "http", "method": "POST", "path": path,
        "headers": [], "client": ("127.0.0.1", 12345), "app": app,
    })


def _store(locked: bool = False, tripped: bool = False) -> MagicMock:
    return MagicMock(
        is_locked=AsyncMock(return_value=locked),
        record_failure=AsyncMock(return_value=tripped),
        clear=AsyncMock(),
    )


def _form(username="alice", password="x") -> MagicMock:
    return MagicMock(username=username, password=password)


IP = "203.0.113.5"


class TestLoginLockoutWiring:
    @pytest.mark.unit
    async def test_locked_user_gets_401_before_the_credential_walk(self):
        store = _store(locked=True)
        resolve = AsyncMock(return_value=None)
        with patch("services.login_lockout.login_lockout", store), \
             patch("services.api_rate_limiter.get_client_ip", return_value=IP), \
             patch("services.api_rate_limiter.client_ip_is_spoof_resistant", return_value=True), \
             patch("auth.login_flow.resolve_login", resolve):
            with pytest.raises(HTTPException) as exc:
                await login(request=_limiter_request(), form_data=_form(), db=AsyncMock(), response=MagicMock())
        assert exc.value.status_code == 401
        store.is_locked.assert_awaited_once_with("alice", IP)
        resolve.assert_not_awaited()
        store.record_failure.assert_not_awaited()

    @pytest.mark.unit
    async def test_bad_credentials_record_failure_with_client_ip(self):
        store = _store()
        with patch("services.login_lockout.login_lockout", store), \
             patch("services.api_rate_limiter.get_client_ip", return_value=IP), \
             patch("services.api_rate_limiter.client_ip_is_spoof_resistant", return_value=True), \
             patch("auth.login_flow.resolve_login", AsyncMock(return_value=None)):
            with pytest.raises(HTTPException) as exc:
                await login(request=_limiter_request(), form_data=_form(), db=AsyncMock(), response=MagicMock())
        assert exc.value.status_code == 401
        store.record_failure.assert_awaited_once_with("alice", IP)
        store.clear.assert_not_awaited()

    @pytest.mark.unit
    async def test_inactive_account_also_counts_with_client_ip(self):
        store = _store()
        outcome = MagicMock(user_id=7)
        inactive = MagicMock(is_active=False)
        with patch("services.login_lockout.login_lockout", store), \
             patch("services.api_rate_limiter.get_client_ip", return_value=IP), \
             patch("services.api_rate_limiter.client_ip_is_spoof_resistant", return_value=True), \
             patch("auth.login_flow.resolve_login", AsyncMock(return_value=outcome)), \
             patch("api.routes.auth.get_user_by_id", AsyncMock(return_value=inactive)):
            with pytest.raises(HTTPException) as exc:
                await login(request=_limiter_request(), form_data=_form(), db=AsyncMock(), response=MagicMock())
        assert exc.value.status_code == 401
        store.record_failure.assert_awaited_once_with("alice", IP)

    @pytest.mark.unit
    async def test_success_clears_this_address_only(self):
        # clear(username, ip): the backstop + THIS address — the owner's login
        # must not free an attacker's per-IP lock, so the ip must be passed.
        store = _store()
        outcome = MagicMock(user_id=7, display_name="Alice", provider_id="db")
        active = MagicMock(id=7, username="alice", is_active=True, token_epoch=0, must_change_password=False)
        with patch("services.login_lockout.login_lockout", store), \
             patch("services.api_rate_limiter.get_client_ip", return_value=IP), \
             patch("services.api_rate_limiter.client_ip_is_spoof_resistant", return_value=True), \
             patch("auth.login_flow.resolve_login", AsyncMock(return_value=outcome)), \
             patch("api.routes.auth.get_user_by_id", AsyncMock(return_value=active)), \
             patch("api.routes.auth.create_access_token", return_value="at"), \
             patch("api.routes.auth.create_refresh_token", return_value="rt"), \
             patch("api.routes.auth._set_auth_cookies"):
            body = await login(request=_limiter_request(), form_data=_form(), db=AsyncMock(), response=MagicMock())
        assert body.access_token == "at"
        store.clear.assert_awaited_once_with("alice", IP)
        store.record_failure.assert_not_awaited()

    @pytest.mark.unit
    async def test_without_trusted_proxies_the_ip_scope_is_not_used(self):
        # Legacy XFF[0] is client-chosen → username-only lock at the strict
        # threshold; the store must receive ip=None, never the spoofable value.
        store = _store()
        with patch("services.login_lockout.login_lockout", store), \
             patch("services.api_rate_limiter.get_client_ip", return_value=IP) as get_ip, \
             patch("services.api_rate_limiter.client_ip_is_spoof_resistant", return_value=False), \
             patch("auth.login_flow.resolve_login", AsyncMock(return_value=None)):
            with pytest.raises(HTTPException):
                await login(request=_limiter_request(), form_data=_form(), db=AsyncMock(), response=MagicMock())
        get_ip.assert_not_called()
        store.is_locked.assert_awaited_once_with("alice", None)
        store.record_failure.assert_awaited_once_with("alice", None)
