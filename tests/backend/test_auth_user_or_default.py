"""
Tests for `services.auth_service.get_user_or_default` — the helper that
lets routes work in both auth-enabled and auth-disabled (single-user)
deploys without crashing on `current_user.id` when auth is off.

Regression guard for the prod-deploy failure: F4a + F4d federation
routes shipped with `current_user.id` access but `get_current_user`
returns None on AUTH_ENABLED=false, so routes 500'd on every
unauthenticated call.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

from models.database import User
from services.auth_service import get_user_or_default


def _make_user(id_: int, username: str = "admin") -> User:
    u = User()
    u.id = id_
    u.username = username
    return u


class TestGetUserOrDefault:
    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_passes_through_authenticated_user(self):
        """Happy path (auth enabled + token valid) — helper returns
        the user that `get_current_user` already resolved."""
        alice = _make_user(id_=42, username="alice")
        db = MagicMock()  # db won't be touched on this path
        result = await get_user_or_default(current_user=alice, db=db)
        assert result is alice
        # The DB must NOT have been queried when a user was provided.
        db.execute.assert_not_called() if hasattr(db.execute, "assert_not_called") else None

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_auth_disabled_resolves_to_admin_user(self):
        """Auth-disabled (current_user=None) — helper queries users
        table for `admin` and returns that row."""
        admin = _make_user(id_=1, username="admin")
        db = AsyncMock()
        result_mock = MagicMock()
        result_mock.scalar_one_or_none = lambda: admin
        db.execute = AsyncMock(return_value=result_mock)

        result = await get_user_or_default(current_user=None, db=db)
        assert result is admin

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_auth_disabled_falls_back_to_first_user_if_no_admin(self):
        """Admin was renamed/deleted — helper falls back to the first
        user by id so the single-user deploy still works."""
        first = _make_user(id_=7, username="evdb")
        db = AsyncMock()

        call_count = {"n": 0}

        async def execute_side(stmt):
            call_count["n"] += 1
            r = MagicMock()
            if call_count["n"] == 1:
                # First call: WHERE username == 'admin' → no match
                r.scalar_one_or_none = lambda: None
            else:
                # Second call: ORDER BY id LIMIT 1 → first user
                r.scalar_one_or_none = lambda: first
            return r

        db.execute = AsyncMock(side_effect=execute_side)
        result = await get_user_or_default(current_user=None, db=db)
        assert result is first
        assert call_count["n"] == 2  # both queries happened

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_auth_disabled_empty_db_raises_503(self):
        """Bootstrap edge: AUTH is off AND no users exist. Better to
        fail loud with a clear 503 than silently proceed and let the
        downstream route crash on a missing user_id."""
        db = AsyncMock()
        empty = MagicMock()
        empty.scalar_one_or_none = lambda: None
        db.execute = AsyncMock(return_value=empty)

        with pytest.raises(HTTPException) as exc_info:
            await get_user_or_default(current_user=None, db=db)

        assert exc_info.value.status_code == 503
        assert "no users" in str(exc_info.value.detail).lower()
