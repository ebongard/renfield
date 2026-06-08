"""Tests for the internal.ingest_file agent tool (T6).

The interactive folder-ingest path: pull a file's bytes through the filesystem
MCP (truncate=False) and run them through the shared ingest bridge. All
collaborators (MCP, DB session, bridge) are mocked.
"""
from __future__ import annotations

import base64
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

import services.folder_ingest_tool as tool_mod
from services.folder_ingest import IngestResult, IngestStatus
from services.folder_ingest_tool import ingest_file

pytestmark = [pytest.mark.unit]

_B64 = base64.b64encode(b"%PDF-1.4 real bytes").decode("ascii")


class _FakeCM:
    def __init__(self, db):
        self._db = db

    async def __aenter__(self):
        return self._db

    async def __aexit__(self, *_a):
        return False


def _mcp(read_inner: dict | None = None):
    read_inner = read_inner if read_inner is not None else {
        "content_base64": _B64, "filename": "invoice.pdf"
    }
    mgr = MagicMock()
    mgr.execute_tool = AsyncMock(
        return_value={"success": True, "message": json.dumps(read_inner)}
    )
    return mgr


def _wire(monkeypatch, *, ingest_result=None, worker_alive=True, to_paperless=False):
    """Patch the tool's collaborators. Returns the ingest_document AsyncMock."""
    monkeypatch.setattr(tool_mod.settings, "folder_ingest_enabled", True)
    monkeypatch.setattr(tool_mod.settings, "folder_ingest_to_paperless", to_paperless)
    monkeypatch.setattr(tool_mod.settings, "folder_ingest_default_tier", 0)

    ingest = AsyncMock(
        return_value=ingest_result
        or IngestResult(IngestStatus.INGESTED, document_id=42, detail="enqueued")
    )
    monkeypatch.setattr(tool_mod, "ingest_document", ingest)
    monkeypatch.setattr(tool_mod, "resolve_target_kb", AsyncMock(return_value=MagicMock(id=1)))
    monkeypatch.setattr(tool_mod, "resolve_owner_user_id", AsyncMock(return_value=None))
    monkeypatch.setattr("services.database.AsyncSessionLocal", lambda: _FakeCM(MagicMock()))
    monkeypatch.setattr(
        "api.routes.knowledge._worker_is_alive", AsyncMock(return_value=worker_alive)
    )
    return ingest


@pytest.mark.asyncio
async def test_missing_path_fails():
    out = await ingest_file({}, mcp_manager=_mcp())
    assert out["success"] is False
    assert out["action_taken"] is False


@pytest.mark.asyncio
async def test_disabled_fails(monkeypatch):
    monkeypatch.setattr(tool_mod.settings, "folder_ingest_enabled", False)
    out = await ingest_file({"path": "/inbox/x.pdf"}, mcp_manager=_mcp())
    assert out["success"] is False
    assert "disabled" in out["message"].lower()


@pytest.mark.asyncio
async def test_no_mcp_manager_fails(monkeypatch):
    monkeypatch.setattr(tool_mod.settings, "folder_ingest_enabled", True)
    out = await ingest_file({"path": "/inbox/x.pdf"}, mcp_manager=None)
    assert out["success"] is False


@pytest.mark.asyncio
async def test_read_file_error_fails(monkeypatch):
    _wire(monkeypatch)
    mgr = MagicMock()
    mgr.execute_tool = AsyncMock(
        return_value={"success": True, "message": json.dumps({"error": "not found"})}
    )
    out = await ingest_file({"path": "/inbox/missing.pdf"}, mcp_manager=mgr)
    assert out["success"] is False
    assert "missing.pdf" in out["message"]


@pytest.mark.asyncio
async def test_invalid_base64_fails(monkeypatch):
    _wire(monkeypatch)
    mgr = _mcp(read_inner={"content_base64": "!!!not base64!!!", "filename": "x.pdf"})
    out = await ingest_file({"path": "/inbox/x.pdf"}, mcp_manager=mgr)
    assert out["success"] is False
    assert "base64" in out["message"].lower()


@pytest.mark.asyncio
async def test_worker_down_fails(monkeypatch):
    _wire(monkeypatch, worker_alive=False)
    out = await ingest_file({"path": "/inbox/x.pdf"}, mcp_manager=_mcp())
    assert out["success"] is False
    assert "worker" in out["message"].lower()


@pytest.mark.asyncio
async def test_happy_path_ingested(monkeypatch):
    ingest = _wire(monkeypatch)
    out = await ingest_file({"path": "/inbox/invoice.pdf"}, mcp_manager=_mcp(), user_id=9)

    assert out["success"] is True
    assert out["action_taken"] is True
    assert out["data"] == {"status": "ingested", "document_id": 42}
    # the asker (user_id) owns the file; tier from config; KB resolved server-side
    _, kwargs = ingest.await_args
    assert kwargs["owner_user_id"] == 9
    assert kwargs["default_tier"] == 0
    assert kwargs["kb_id"] == 1


@pytest.mark.asyncio
async def test_read_file_called_with_truncate_false(monkeypatch):
    _wire(monkeypatch)
    mgr = _mcp()
    await ingest_file({"path": "/inbox/x.pdf"}, mcp_manager=mgr, user_id=1)
    tool, params = mgr.execute_tool.await_args.args
    assert tool == "mcp.files.read_file"
    assert params == {"path": "/inbox/x.pdf", "truncate": False}


@pytest.mark.asyncio
async def test_duplicate_is_friendly_no_action(monkeypatch):
    _wire(monkeypatch, ingest_result=IngestResult(IngestStatus.DUPLICATE, document_id=7))
    out = await ingest_file({"path": "/inbox/x.pdf"}, mcp_manager=_mcp(), user_id=1)
    assert out["success"] is True
    assert out["action_taken"] is False
    assert out["data"]["status"] == "duplicate"


@pytest.mark.asyncio
async def test_failed_reports_unsuccessful(monkeypatch):
    _wire(monkeypatch, ingest_result=IngestResult(IngestStatus.FAILED, detail="extension_not_allowed"))
    out = await ingest_file({"path": "/inbox/x.exe"}, mcp_manager=_mcp(), user_id=1)
    assert out["success"] is False
    assert out["action_taken"] is False


@pytest.mark.asyncio
async def test_owner_falls_back_to_configured_when_unauthenticated(monkeypatch):
    ingest = _wire(monkeypatch)
    monkeypatch.setattr(tool_mod, "resolve_owner_user_id", AsyncMock(return_value=3))
    await ingest_file({"path": "/inbox/x.pdf"}, mcp_manager=_mcp(), user_id=None)
    _, kwargs = ingest.await_args
    assert kwargs["owner_user_id"] == 3  # configured target_user, not None