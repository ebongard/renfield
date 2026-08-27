"""Tests for internal.paperless_dedupe — the THIN CALLER (#1137, Review D9).

The duplicate-detection + deletion logic now lives in the Paperless MCP
(`mcp.paperless.dedupe_documents`, tested in renfield-mcp-paperless). These tests
cover only what this module owns: the ADMIN gate, the MCP call shape (dry_run /
max_delete / metadata_match passthrough), the contract-marker guard against a
fuzzy-fallback response, and the German message mapping.
"""
import json

import pytest

from services.paperless_dedupe_tool import paperless_dedupe


def _envelope(inner: dict) -> dict:
    """MCPManager envelope: the tool's dict is JSON-encoded in ``message``."""
    return {"success": True, "message": json.dumps(inner)}


def _make_mcp(inner: dict | None = None, *, transport_error: bool = False):
    """Mock mcp_manager whose dedupe_documents returns ``inner`` (wrapped). Records
    the params it was called with in ``.last_params``."""

    class _MCP:
        def __init__(self):
            self.last_params: dict | None = None
            self.last_kw: dict | None = None
            self.calls: list[str] = []

        async def execute_tool(self, tool, params, **kw):
            self.calls.append(tool)
            self.last_params = params
            self.last_kw = kw
            assert kw.get("truncate") is False  # large payloads must not be truncated
            # The single-call full-archive dedupe needs a raised per-call timeout
            # (the default 30s times out on a large archive).
            assert kw.get("call_timeout") and kw["call_timeout"] > 30
            if transport_error:
                return {"success": False, "message": "boom"}
            return _envelope(inner or {})

    return _MCP()


def _dedupe_result(**over) -> dict:
    base = {
        "scanned": 100, "groups": 0, "metadata_groups": 0, "duplicate_copies": 0,
        "kept": 0, "skipped": 0, "deleted": 0, "remaining": 0, "complete": True,
        "dry_run": False, "deleted_ids": [],
    }
    base.update(over)
    return base


@pytest.mark.unit
class TestAdminGate:
    async def test_auth_on_no_admin_denied(self, monkeypatch):
        from utils.config import settings

        monkeypatch.setattr(settings, "auth_enabled", True)
        mcp = _make_mcp(_dedupe_result())
        out = await paperless_dedupe({}, mcp_manager=mcp, user_permissions=["ha.read"])
        assert out["success"] is False
        assert "Berechtigung" in out["message"]
        assert mcp.calls == []  # never reached the MCP

    async def test_auth_on_unidentified_denied(self, monkeypatch):
        from utils.config import settings

        monkeypatch.setattr(settings, "auth_enabled", True)
        mcp = _make_mcp(_dedupe_result())
        out = await paperless_dedupe({}, mcp_manager=mcp, user_permissions=None)
        assert out["success"] is False
        assert mcp.calls == []

    async def test_auth_on_admin_allowed(self, monkeypatch):
        from models.permissions import Permission
        from utils.config import settings

        monkeypatch.setattr(settings, "auth_enabled", True)
        mcp = _make_mcp(_dedupe_result())
        out = await paperless_dedupe({}, mcp_manager=mcp, user_permissions=[Permission.ADMIN.value])
        assert out["success"] is True
        assert mcp.calls == ["mcp.paperless.dedupe_documents"]

    async def test_auth_off_skips_gate(self, monkeypatch):
        from utils.config import settings

        monkeypatch.setattr(settings, "auth_enabled", False)
        mcp = _make_mcp(_dedupe_result())
        out = await paperless_dedupe({}, mcp_manager=mcp, user_permissions=None)
        assert out["success"] is True

    async def test_no_mcp_manager(self, monkeypatch):
        from utils.config import settings

        monkeypatch.setattr(settings, "auth_enabled", False)
        out = await paperless_dedupe({}, mcp_manager=None)
        assert out["success"] is False
        assert "kein MCP" in out["message"]


@pytest.mark.unit
class TestCallShape:
    async def test_passes_dry_run_maxdelete_metadata(self, monkeypatch):
        from utils.config import settings

        monkeypatch.setattr(settings, "auth_enabled", False)
        monkeypatch.setattr(settings, "paperless_dedupe_delete_batch", 42)
        monkeypatch.setattr(settings, "paperless_dedupe_metadata_match_enabled", True)
        mcp = _make_mcp(_dedupe_result())
        await paperless_dedupe({"dry_run": True}, mcp_manager=mcp)
        assert mcp.last_params == {"dry_run": True, "max_delete": 42, "metadata_match": True}


@pytest.mark.unit
class TestResultMapping:
    async def test_no_duplicates(self, monkeypatch):
        from utils.config import settings

        monkeypatch.setattr(settings, "auth_enabled", False)
        mcp = _make_mcp(_dedupe_result(scanned=50, groups=0))
        out = await paperless_dedupe({}, mcp_manager=mcp)
        assert out["success"] is True
        assert out["action_taken"] is False
        assert "Keine Duplikate" in out["message"]

    async def test_dry_run_reports_scope(self, monkeypatch):
        from utils.config import settings

        monkeypatch.setattr(settings, "auth_enabled", False)
        mcp = _make_mcp(_dedupe_result(
            groups=3, metadata_groups=1, duplicate_copies=7, kept=3, dry_run=True,
        ))
        out = await paperless_dedupe({"dry_run": True}, mcp_manager=mcp)
        assert out["action_taken"] is False
        assert "7" in out["message"] and "nur_zeigen" in out["message"]
        assert "Metadaten" in out["message"]  # metadata_groups note

    async def test_delete_complete_clean(self, monkeypatch):
        from utils.config import settings

        monkeypatch.setattr(settings, "auth_enabled", False)
        mcp = _make_mcp(_dedupe_result(
            groups=2, duplicate_copies=5, kept=2, deleted=5, remaining=0,
            complete=True, skipped=0, deleted_ids=[2, 3, 4, 5, 6],
        ))
        out = await paperless_dedupe({}, mcp_manager=mcp)
        assert out["action_taken"] is True
        assert "bereinigt" in out["message"]
        assert out["data"]["deleted_ids"] == [2, 3, 4, 5, 6]

    async def test_delete_partial_remaining(self, monkeypatch):
        from utils.config import settings

        monkeypatch.setattr(settings, "auth_enabled", False)
        mcp = _make_mcp(_dedupe_result(
            groups=10, duplicate_copies=100, kept=10, deleted=50, remaining=50,
            complete=True, skipped=0,
        ))
        out = await paperless_dedupe({}, mcp_manager=mcp)
        assert out["action_taken"] is True
        assert "verbleiben" in out["message"] and "erneut" in out["message"]

    async def test_partial_sweep_disclosed(self, monkeypatch):
        from utils.config import settings

        monkeypatch.setattr(settings, "auth_enabled", False)
        mcp = _make_mcp(_dedupe_result(groups=0, complete=False))
        out = await paperless_dedupe({}, mcp_manager=mcp)
        assert "TEILWEISE" in out["message"]


@pytest.mark.unit
class TestGuards:
    async def test_mcp_error_reported(self, monkeypatch):
        from utils.config import settings

        monkeypatch.setattr(settings, "auth_enabled", False)
        mcp = _make_mcp(transport_error=True)
        out = await paperless_dedupe({}, mcp_manager=mcp)
        assert out["success"] is False
        assert "fehlgeschlagen" in out["message"]

    async def test_fuzzy_fallback_response_rejected(self, monkeypatch):
        """An old MCP fuzzy-falls-back dedupe_documents to another tool → the
        response lacks the marker keys → never claim clean, report unavailable."""
        from utils.config import settings

        monkeypatch.setattr(settings, "auth_enabled", False)
        # search_documents-shaped response (no scanned/complete/duplicate_copies)
        mcp = _make_mcp({"results": [], "total_matching": 0})
        out = await paperless_dedupe({}, mcp_manager=mcp)
        assert out["success"] is False
        assert "nicht verfügbar" in out["message"]

    async def test_execute_tool_exception(self, monkeypatch):
        from utils.config import settings

        monkeypatch.setattr(settings, "auth_enabled", False)

        class _Boom:
            async def execute_tool(self, *a, **k):
                raise RuntimeError("kaboom")

        out = await paperless_dedupe({}, mcp_manager=_Boom())
        assert out["success"] is False
        assert "fehlgeschlagen" in out["message"]
