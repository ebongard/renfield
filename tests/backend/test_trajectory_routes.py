"""Route-level tests for /api/trajectories.

Specifically the 2nd-pass review surfaces:
  - export.jsonl returns 409 when require_redacted=true AND no redacted
    rows exist (don't silently drip empty body)
  - export.jsonl with require_redacted=false emits a structured WARNING
    audit log so the verbatim-export path is traceable
  - export.jsonl returns 409 when trajectory_capture_enabled=false
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from httpx import AsyncClient


@pytest.fixture
def bypass_admin():
    """Bypass require_permission(Permission.ADMIN) for the trajectory
    route. Same pattern test_camera.py uses for ha_glue routes."""
    fake_user = MagicMock(id=42, username="admin_tester")
    with patch(
        "api.routes.trajectories.require_permission",
        return_value=lambda: fake_user,
    ) as p:
        yield p


@pytest.mark.asyncio
class TestExportPreflight:
    async def test_capture_disabled_409(
        self, async_client: AsyncClient, bypass_admin, monkeypatch
    ):
        monkeypatch.setattr(
            "api.routes.trajectories.settings.trajectory_capture_enabled", False,
        )
        resp = await async_client.get("/api/trajectories/export.jsonl")
        assert resp.status_code == 409
        assert "nothing to export" in resp.text

    async def test_require_redacted_with_no_redacted_rows_409(
        self, async_client: AsyncClient, bypass_admin, monkeypatch
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
        self, async_client: AsyncClient, bypass_admin, monkeypatch, caplog
    ):
        """Override path: ?require_redacted=false bypasses the gate but
        MUST emit a structured WARNING line so the verbatim-export path
        is auditable via standard log shipping."""
        import logging

        monkeypatch.setattr(
            "api.routes.trajectories.settings.trajectory_capture_enabled", True,
        )

        # loguru → stdlib bridge: route the loguru sink into caplog so
        # pytest captures it. Same approach federation_audit tests use.
        from loguru import logger as loguru_logger

        handler_id = loguru_logger.add(
            lambda msg: logging.getLogger().warning(msg.record["message"]),
            level="WARNING",
        )
        try:
            with caplog.at_level(logging.WARNING):
                resp = await async_client.get(
                    "/api/trajectories/export.jsonl?require_redacted=false"
                )
            # 200 (streaming response) — the body is empty if no rows, but
            # the audit log fires BEFORE the stream starts.
            assert resp.status_code == 200
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
