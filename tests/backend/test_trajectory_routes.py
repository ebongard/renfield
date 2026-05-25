"""Route-level tests for /api/trajectories.

Specifically the 2nd-pass review surfaces:
  - export.jsonl returns 409 when require_redacted=true AND no redacted
    rows exist (don't silently drip empty body)
  - export.jsonl with require_redacted=false emits a structured WARNING
    audit log so the verbatim-export path is traceable
  - export.jsonl returns 409 when trajectory_capture_enabled=false
"""
from __future__ import annotations

import logging

import pytest
from httpx import AsyncClient


@pytest.fixture
def admin_user_override(app_with_test_db):
    """Bypass require_permission(Permission.ADMIN) for the trajectory
    route by overriding get_current_user — require_permission's
    permission_checker takes the user from there, and the
    `if not settings.auth_enabled: return user` short-circuit means our
    fake user reaches the route handler with the right shape (.id +
    .username present)."""
    from services.auth_service import get_current_user

    class _FakeAdmin:
        id = 42
        username = "admin_tester"

    app_with_test_db.dependency_overrides[get_current_user] = lambda: _FakeAdmin()
    try:
        yield _FakeAdmin
    finally:
        app_with_test_db.dependency_overrides.pop(get_current_user, None)


@pytest.mark.asyncio
class TestExportPreflight:
    async def test_capture_disabled_409(
        self, async_client: AsyncClient, admin_user_override, monkeypatch
    ):
        monkeypatch.setattr(
            "api.routes.trajectories.settings.trajectory_capture_enabled", False,
        )
        resp = await async_client.get("/api/trajectories/export.jsonl")
        assert resp.status_code == 409
        assert "nothing to export" in resp.text

    async def test_require_redacted_with_no_redacted_rows_409(
        self, async_client: AsyncClient, admin_user_override, monkeypatch
    ):
        """Pre-flight gate: require_redacted=true is the default, and v1
        leaves redacted_payload NULL. Returning an empty stream would
        look like a capture failure; we 409 with actionable detail."""
        monkeypatch.setattr(
            "api.routes.trajectories.settings.trajectory_capture_enabled", True,
        )
        resp = await async_client.get(
            "/api/trajectories/export.jsonl?require_redacted=true"
        )
        assert resp.status_code == 409
        assert "PII scrubber" in resp.text
        assert "require_redacted=false" in resp.text

    async def test_require_redacted_false_emits_audit_log(
        self, admin_user_override, monkeypatch, caplog
    ):
        """Override path: ?require_redacted=false bypasses the gate but
        MUST emit a structured WARNING line so the verbatim-export path
        is auditable via standard log shipping.

        Calls the route handler directly instead of going through httpx
        AsyncClient: the audit warning fires BEFORE the StreamingResponse
        is built, so we don't need to exercise the stream-body path
        (which trips an unrelated engine-level checkin hook on sqlite).
        """
        from fastapi import Request

        from api.routes.trajectories import export_jsonl

        monkeypatch.setattr(
            "api.routes.trajectories.settings.trajectory_capture_enabled", True,
        )

        # Minimal ASGI scope so request.client is populated.
        scope = {
            "type": "http",
            "method": "GET",
            "path": "/api/trajectories/export.jsonl",
            "headers": [],
            "client": ("10.0.0.42", 12345),
        }
        request = Request(scope)

        # loguru → stdlib bridge so caplog captures the WARNING.
        from loguru import logger as loguru_logger

        handler_id = loguru_logger.add(
            lambda msg: logging.getLogger().warning(msg.record["message"]),
            level="WARNING",
        )
        try:
            with caplog.at_level(logging.WARNING):
                response = await export_jsonl(
                    request=request,
                    outcome=None,
                    since_days=None,
                    flagged_only=False,
                    require_redacted=False,
                    admin=admin_user_override(),
                )
            # Handler returned a StreamingResponse without raising — the
            # audit warning has already been emitted at this point.
            assert response.media_type == "application/jsonl"
        finally:
            loguru_logger.remove(handler_id)

        audit_lines = [
            r.message for r in caplog.records
            if "require_redacted=false" in r.message
        ]
        assert audit_lines, "expected an audit WARNING for raw-export path"
        line = audit_lines[0]
        assert "admin_user_id=42" in line
        assert "admin_username='admin_tester'" in line
        assert "10.0.0.42" in line


# ---------------------------------------------------------------------
# Unauthorized-access coverage. The export route is admin-only, so are
# list/flag/stats. The eng-review flagged that no test asserted these
# return 401/403 for a non-admin caller. Each test here pins
# get_current_user to a User with NO admin permission and asserts the
# route rejects before any data is read.
# ---------------------------------------------------------------------
@pytest.fixture
def regular_user_override(app_with_test_db, monkeypatch):
    """Pin get_current_user to a non-admin user AND enable auth so
    require_permission actually runs its permission check (without
    auth_enabled=True it short-circuits and returns the user as-is)."""
    from services.auth_service import get_current_user

    class _FakeRegular:
        id = 7
        username = "regular_tester"

        def has_permission(self, perm: str) -> bool:
            return False  # never admin

    # require_permission early-returns when settings.auth_enabled is False.
    # Default in tests is False; flip it on so the permission check runs.
    monkeypatch.setattr(
        "services.auth_service.settings.auth_enabled", True,
    )
    app_with_test_db.dependency_overrides[get_current_user] = lambda: _FakeRegular()
    try:
        yield _FakeRegular
    finally:
        app_with_test_db.dependency_overrides.pop(get_current_user, None)


@pytest.mark.asyncio
class TestUnauthorizedAccess:
    """Every trajectory route MUST reject non-admins. Trajectories carry
    verbatim user chat history; a downgrade of the admin gate would leak
    other users' PII silently."""

    async def test_list_blocked(
        self, async_client: AsyncClient, regular_user_override,
    ):
        resp = await async_client.get("/api/trajectories")
        assert resp.status_code in (401, 403)

    async def test_export_blocked(
        self, async_client: AsyncClient, regular_user_override, monkeypatch,
    ):
        monkeypatch.setattr(
            "api.routes.trajectories.settings.trajectory_capture_enabled", True,
        )
        resp = await async_client.get(
            "/api/trajectories/export.jsonl?require_redacted=false"
        )
        assert resp.status_code in (401, 403)

    async def test_flag_blocked(
        self, async_client: AsyncClient, regular_user_override,
    ):
        resp = await async_client.post(
            "/api/trajectories/1/flag", json={"flagged": True}
        )
        assert resp.status_code in (401, 403)

    async def test_stats_blocked(
        self, async_client: AsyncClient, regular_user_override,
    ):
        resp = await async_client.get("/api/trajectories/stats")
        assert resp.status_code in (401, 403)


@pytest.mark.asyncio
class TestStats:
    """Smoke test for the dashboard endpoint."""

    async def test_admin_stats_returns_shape(
        self, async_client: AsyncClient, admin_user_override, monkeypatch,
    ):
        monkeypatch.setattr(
            "api.routes.trajectories.settings.trajectory_capture_enabled", True,
        )
        resp = await async_client.get("/api/trajectories/stats")
        assert resp.status_code == 200
        body = resp.json()
        assert "total" in body
        assert "by_outcome" in body
        assert "last_7d" in body
        assert "flagged_total" in body
        assert body["capture_enabled"] is True


@pytest.mark.asyncio
class TestListFilter:
    """List endpoint filter contract.

    The `outcome` query param is now validated against Literal["success",
    "tool_fail", "abort"] so an unknown value fails fast at 422 rather
    than silently returning zero rows."""

    async def test_unknown_outcome_value_422(
        self, async_client: AsyncClient, admin_user_override,
    ):
        resp = await async_client.get("/api/trajectories?outcome=banana")
        assert resp.status_code == 422

    async def test_since_days_lower_bound(
        self, async_client: AsyncClient, admin_user_override,
    ):
        resp = await async_client.get("/api/trajectories?since_days=0")
        assert resp.status_code == 422

    async def test_since_days_upper_bound(
        self, async_client: AsyncClient, admin_user_override,
    ):
        resp = await async_client.get("/api/trajectories?since_days=999999")
        assert resp.status_code == 422
