"""Route-level tests for /api/tool-health (admin-only, Phase-3 surface).

Both endpoints (`GET /` and `GET /warnings/{user_id}`) are gated by
`require_permission(Permission.ADMIN)`. The eng-review flagged that
neither the unauthorized-rejection path nor the happy-path response
shape was covered.
"""
from __future__ import annotations

from datetime import datetime, UTC

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from models.database import Role, ToolOutcomeStat, User


# ---------------------------------------------------------- fixtures
async def _make_role(
    db_session: AsyncSession, name: str, perms: list[str] | None = None,
) -> Role:
    role = Role(name=name, permissions=perms or [])
    db_session.add(role)
    await db_session.commit()
    await db_session.refresh(role)
    return role


@pytest.fixture
async def regular_user(db_session: AsyncSession) -> User:
    role = await _make_role(db_session, "regular_role", perms=[])
    user = User(
        username="th_regular", email="reg@example.test",
        password_hash="x", role_id=role.id, is_active=True,
    )
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user


@pytest.fixture
async def admin_user(db_session: AsyncSession) -> User:
    role = await _make_role(db_session, "th_admin_role", perms=["admin"])
    user = User(
        username="th_admin", email="adm@example.test",
        password_hash="x", role_id=role.id, is_active=True,
    )
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user


class _FakeUser:
    """Mock user that avoids ORM lazy-load.

    Using a real `User` row from db_session trips
    `sqlalchemy.exc.MissingGreenlet` when `require_permission` traverses
    `user.role.has_permission(...)` — the route handler runs in a
    different session than the fixture created the user in. The fake
    bypasses the relationship entirely by exposing `has_permission` as
    a direct method."""

    def __init__(self, uid: int, name: str, is_admin: bool):
        self.id = uid
        self.username = name
        self._is_admin = is_admin

    def has_permission(self, perm: str) -> bool:
        return self._is_admin and perm == "admin"


@pytest.fixture
def auth_as_regular(app_with_test_db, monkeypatch):
    """Pin auth + enable enforcement. Without auth_enabled=True
    `require_permission` short-circuits to "allow"."""
    from services.auth_service import get_current_user
    fake = _FakeUser(uid=7, name="th_regular", is_admin=False)
    monkeypatch.setattr(
        "services.auth_service.settings.auth_enabled", True,
    )
    app_with_test_db.dependency_overrides[get_current_user] = lambda: fake
    try:
        yield fake
    finally:
        app_with_test_db.dependency_overrides.pop(get_current_user, None)


@pytest.fixture
def auth_as_admin(app_with_test_db, monkeypatch):
    from services.auth_service import get_current_user
    fake = _FakeUser(uid=42, name="th_admin", is_admin=True)
    monkeypatch.setattr(
        "services.auth_service.settings.auth_enabled", True,
    )
    app_with_test_db.dependency_overrides[get_current_user] = lambda: fake
    try:
        yield fake
    finally:
        app_with_test_db.dependency_overrides.pop(get_current_user, None)


async def _make_stat(
    db_session: AsyncSession, user_id: int, tool: str,
    *, succ: int = 0, fail: int = 0, summary: str | None = None,
) -> ToolOutcomeStat:
    now = datetime.now(UTC).replace(tzinfo=None)
    row = ToolOutcomeStat(
        user_id=user_id,
        tool_name=tool,
        success_count=succ,
        failure_count=fail,
        last_used_at=now,
        last_failure_at=now if fail else None,
        last_failure_summary=summary,
    )
    db_session.add(row)
    await db_session.commit()
    await db_session.refresh(row)
    return row


# ============================================================ AUTH GATE
@pytest.mark.asyncio
class TestAuthGate:
    async def test_list_requires_admin(
        self, async_client: AsyncClient, auth_as_regular,
    ):
        resp = await async_client.get("/api/tool-health")
        assert resp.status_code in (401, 403)

    async def test_warnings_requires_admin(
        self, async_client: AsyncClient, auth_as_regular,
    ):
        resp = await async_client.get(
            f"/api/tool-health/warnings/{auth_as_regular.id}"
        )
        assert resp.status_code in (401, 403)


# ============================================================ LIST
@pytest.mark.asyncio
class TestListStats:
    async def test_admin_happy_path(
        self, async_client: AsyncClient, auth_as_admin,
        db_session: AsyncSession,
    ):
        await _make_stat(db_session, auth_as_admin.id, "mcp.x.foo",
                          succ=7, fail=3)
        resp = await async_client.get("/api/tool-health")
        assert resp.status_code == 200
        body = resp.json()
        assert isinstance(body, list)
        assert len(body) >= 1
        row = next(r for r in body if r["tool_name"] == "mcp.x.foo")
        assert row["success_count"] == 7
        assert row["failure_count"] == 3
        # success_rate = 7 / (7+3) = 0.7
        assert abs(row["success_rate"] - 0.7) < 0.01

    async def test_success_rate_one_when_no_calls(
        self, async_client: AsyncClient, auth_as_admin,
        db_session: AsyncSession,
    ):
        """rate = success_count / total when total > 0 else 1.0 (the
        success-rate fallback at the route's response build)."""
        await _make_stat(db_session, auth_as_admin.id, "mcp.x.unused",
                          succ=0, fail=0)
        resp = await async_client.get("/api/tool-health")
        assert resp.status_code == 200
        row = next(r for r in resp.json() if r["tool_name"] == "mcp.x.unused")
        assert row["success_rate"] == 1.0

    async def test_limit_filter(
        self, async_client: AsyncClient, auth_as_admin,
        db_session: AsyncSession,
    ):
        for i in range(5):
            await _make_stat(
                db_session, auth_as_admin.id, f"mcp.x.t{i}", succ=1,
            )
        resp = await async_client.get("/api/tool-health?limit=3")
        assert resp.status_code == 200
        assert len(resp.json()) == 3

    async def test_limit_lower_bound_validation(
        self, async_client: AsyncClient, auth_as_admin,
    ):
        resp = await async_client.get("/api/tool-health?limit=0")
        assert resp.status_code == 422

    async def test_limit_upper_bound_validation(
        self, async_client: AsyncClient, auth_as_admin,
    ):
        resp = await async_client.get("/api/tool-health?limit=10000")
        assert resp.status_code == 422

    async def test_user_id_filter(
        self, async_client: AsyncClient, auth_as_admin,
        regular_user, db_session: AsyncSession,
    ):
        await _make_stat(db_session, auth_as_admin.id, "mcp.x.admin",
                          succ=1)
        await _make_stat(db_session, regular_user.id, "mcp.x.reg",
                          succ=1)
        resp = await async_client.get(
            f"/api/tool-health?user_id={regular_user.id}"
        )
        assert resp.status_code == 200
        tools = [r["tool_name"] for r in resp.json()]
        assert "mcp.x.reg" in tools
        assert "mcp.x.admin" not in tools


# ============================================================ WARNINGS
@pytest.mark.asyncio
class TestWarnings:
    async def test_admin_can_preview_user_warnings(
        self, async_client: AsyncClient, auth_as_admin,
        regular_user, db_session: AsyncSession, monkeypatch,
    ):
        """A tool with high failure rate + enough uses should appear in
        the preview the user would see at their next prompt build."""
        # Lower the warn-min-uses so a few rows are enough.
        monkeypatch.setattr(
            "services.tool_outcome_service.settings.tool_health_tracking_enabled",
            True,
        )
        monkeypatch.setattr(
            "services.tool_outcome_service.settings.tool_health_warn_enabled",
            True,
        )
        monkeypatch.setattr(
            "services.tool_outcome_service.settings.tool_health_warn_min_uses", 3,
        )
        monkeypatch.setattr(
            "services.tool_outcome_service.settings.tool_health_warn_success_rate", 0.9,
        )
        monkeypatch.setattr(
            "services.tool_outcome_service.settings.tool_health_warn_top_k", 10,
        )
        await _make_stat(
            db_session, regular_user.id, "mcp.bad.tool",
            succ=1, fail=4, summary="ECONNREFUSED",
        )
        resp = await async_client.get(
            f"/api/tool-health/warnings/{regular_user.id}"
        )
        assert resp.status_code == 200
        body = resp.json()
        assert len(body) == 1
        assert body[0]["tool_name"] == "mcp.bad.tool"
        assert body[0]["failure_count"] == 4

    async def test_warnings_empty_when_no_stats(
        self, async_client: AsyncClient, auth_as_admin,
        regular_user, monkeypatch,
    ):
        monkeypatch.setattr(
            "services.tool_outcome_service.settings.tool_health_tracking_enabled",
            True,
        )
        monkeypatch.setattr(
            "services.tool_outcome_service.settings.tool_health_warn_enabled",
            True,
        )
        resp = await async_client.get(
            f"/api/tool-health/warnings/{regular_user.id}"
        )
        assert resp.status_code == 200
        assert resp.json() == []
