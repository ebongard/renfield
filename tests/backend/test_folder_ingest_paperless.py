"""Tests for the folder-ingest Paperless leg (T5/T10).

The leg files a document into Paperless and records the terminal outcome on
``Document.paperless_state``. It drives two MCP tools — ``upload_document``
(non-blocking) then ``await_consume_result`` (the MCP owns the duplicate-marker
knowledge) — and a best-effort metadata extraction. All collaborators are
mocked here; the MCP-side polling + duplicate classification is covered in the
renfield-mcp-paperless test suite.
"""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

import services.folder_ingest_paperless as leg_mod
from models.database import PAPERLESS_STATE_DONE, PAPERLESS_STATE_FAILED
from services.folder_ingest import IngestMeta
from services.folder_ingest_paperless import (
    _parse_paperless_result,
    make_paperless_leg,
)
from services.paperless_metadata_extractor import ExtractionResult, PaperlessMetadata

# Module-level unit mark; async tests are picked up by asyncio_mode=auto. The
# sync _parse_* tests stay sync (no spurious asyncio mark).
pytestmark = [pytest.mark.unit]

_PDF = b"%PDF-1.4 hello world bytes"


def _envelope(inner: dict) -> dict:
    """Mimic the MCPManager envelope: tool dict JSON-encoded in `message`."""
    return {"success": True, "message": json.dumps(inner)}


def _mcp(upload_inner: dict | None = None, await_inner: dict | None = None):
    """A mock mcp_manager whose execute_tool dispatches by tool name."""
    upload_inner = upload_inner if upload_inner is not None else {"task_id": "t1"}
    await_inner = await_inner if await_inner is not None else {"status": "success", "document_id": 5}

    async def _execute(tool, params):
        if tool == "mcp.paperless.upload_document":
            return _envelope(upload_inner)
        if tool == "mcp.paperless.await_consume_result":
            return _envelope(await_inner)
        raise AssertionError(f"unexpected tool {tool}")

    mgr = MagicMock()
    mgr.execute_tool = AsyncMock(side_effect=_execute)
    return mgr


def _patch_extractor(monkeypatch, *, metadata=None, error=None):
    """Patch PaperlessMetadataExtractor so extract_from_file returns a
    controlled ExtractionResult."""
    result = ExtractionResult(
        metadata=metadata or PaperlessMetadata(), doc_text="text", error=error
    )
    inst = MagicMock()
    inst.extract_from_file = AsyncMock(return_value=result)
    monkeypatch.setattr(
        "services.paperless_metadata_extractor.PaperlessMetadataExtractor",
        MagicMock(return_value=inst),
    )
    return inst


def _doc(paperless_state=None):
    return MagicMock(id=1, paperless_state=paperless_state, file_path="/uploads/x.pdf")


def _meta():
    return IngestMeta(filename="invoice.pdf")


# ---------------------------------------------------------------------------
# _parse_paperless_result
# ---------------------------------------------------------------------------

def test_parse_unwraps_envelope():
    assert _parse_paperless_result(_envelope({"task_id": "t9"})) == {"task_id": "t9"}


def test_parse_transport_failure_is_error():
    out = _parse_paperless_result({"success": False, "message": "boom"})
    assert "error" in out


def test_parse_none_is_error():
    assert "error" in _parse_paperless_result(None)


def test_parse_unparseable_message_is_error():
    assert "error" in _parse_paperless_result({"success": True, "message": "not json"})


# ---------------------------------------------------------------------------
# leg outcomes
# ---------------------------------------------------------------------------

async def test_idempotent_skip_when_already_done(monkeypatch):
    mgr = _mcp()
    leg = make_paperless_leg(mgr)
    doc = _doc(paperless_state=PAPERLESS_STATE_DONE)
    db = AsyncMock()

    assert await leg(db, doc, _PDF, _meta()) is True
    mgr.execute_tool.assert_not_called()  # no upload — already settled


async def test_success_marks_done(monkeypatch):
    _patch_extractor(monkeypatch)
    leg = make_paperless_leg(_mcp(await_inner={"status": "success", "document_id": 7}))
    doc, db = _doc(), AsyncMock()

    assert await leg(db, doc, _PDF, _meta()) is True
    assert doc.paperless_state == PAPERLESS_STATE_DONE
    db.commit.assert_awaited()


async def test_duplicate_marks_done(monkeypatch):
    _patch_extractor(monkeypatch)
    leg = make_paperless_leg(_mcp(await_inner={"status": "duplicate", "detail": "dup"}))
    doc, db = _doc(), AsyncMock()

    assert await leg(db, doc, _PDF, _meta()) is True
    assert doc.paperless_state == PAPERLESS_STATE_DONE  # D10: duplicate is terminal success


async def test_non_duplicate_failure_marks_failed_and_settles(monkeypatch):
    _patch_extractor(monkeypatch)
    leg = make_paperless_leg(_mcp(await_inner={"status": "failure", "detail": "bad pdf"}))
    doc, db = _doc(), AsyncMock()

    # settled (True) so the bridge stops looping, but recorded as FAILED (not in
    # Paperless) — distinct from done.
    assert await leg(db, doc, _PDF, _meta()) is True
    assert doc.paperless_state == PAPERLESS_STATE_FAILED


async def test_pending_is_unsettled(monkeypatch):
    _patch_extractor(monkeypatch)
    leg = make_paperless_leg(_mcp(await_inner={"status": "pending", "detail": "timeout"}))
    doc, db = _doc(), AsyncMock()

    assert await leg(db, doc, _PDF, _meta()) is False  # retry later
    assert doc.paperless_state is None  # left unset
    db.commit.assert_not_awaited()


async def test_upload_tool_rejection_is_terminal_failed(monkeypatch):
    _patch_extractor(monkeypatch)
    # Paperless/tool rejected the upload (bad field / 4xx / config) — a body
    # error with envelope success=True. Re-sending won't help → terminal FAILED,
    # settled (True) so PAPERLESS_ONLY doesn't loop.
    leg = make_paperless_leg(_mcp(upload_inner={"error": "Unknown correspondent"}))
    doc, db = _doc(), AsyncMock()

    assert await leg(db, doc, _PDF, _meta()) is True
    assert doc.paperless_state == PAPERLESS_STATE_FAILED


async def test_upload_transport_failure_is_unsettled(monkeypatch):
    _patch_extractor(monkeypatch)
    # MCP unreachable (envelope success=False) → transient, leave unset, retry.
    mgr = MagicMock()
    mgr.execute_tool = AsyncMock(return_value={"success": False, "message": "mcp down"})
    leg = make_paperless_leg(mgr)
    doc, db = _doc(), AsyncMock()

    assert await leg(db, doc, _PDF, _meta()) is False
    assert doc.paperless_state is None


async def test_idempotent_skip_when_already_failed(monkeypatch):
    # A settled-failed doc is skipped too (defence in depth).
    mgr = _mcp()
    leg = make_paperless_leg(mgr)
    assert await leg(AsyncMock(), _doc(paperless_state=PAPERLESS_STATE_FAILED), _PDF, _meta()) is True
    mgr.execute_tool.assert_not_called()


# ---------------------------------------------------------------------------
# metadata wiring
# ---------------------------------------------------------------------------

async def test_extracted_metadata_passed_to_upload(monkeypatch):
    _patch_extractor(
        monkeypatch,
        metadata=PaperlessMetadata(
            title="Stromrechnung", correspondent="Stadtwerke",
            document_type="Rechnung", tags=["energie"],
        ),
    )
    mgr = _mcp()
    leg = make_paperless_leg(mgr)
    await leg(AsyncMock(), _doc(), _PDF, _meta())

    upload_call = mgr.execute_tool.await_args_list[0]
    params = upload_call.args[1]
    assert params["title"] == "Stromrechnung"
    assert params["correspondent"] == "Stadtwerke"
    assert params["document_type"] == "Rechnung"
    assert params["tags"] == ["energie"]
    assert params["wait_for_consume"] is False  # we drive the consume poll ourselves


async def test_extractor_error_falls_back_to_bare_upload(monkeypatch):
    _patch_extractor(monkeypatch, error="Paperless-Taxonomie nicht erreichbar")
    mgr = _mcp()
    leg = make_paperless_leg(mgr)
    await leg(AsyncMock(), _doc(), _PDF, _meta())

    params = mgr.execute_tool.await_args_list[0].args[1]
    assert params["title"] == "invoice.pdf"  # filename fallback
    assert "correspondent" not in params  # no metadata on error


async def test_extractor_exception_does_not_break_leg(monkeypatch):
    inst = MagicMock()
    inst.extract_from_file = AsyncMock(side_effect=RuntimeError("docling boom"))
    monkeypatch.setattr(
        "services.paperless_metadata_extractor.PaperlessMetadataExtractor",
        MagicMock(return_value=inst),
    )
    mgr = _mcp()
    leg = make_paperless_leg(mgr)

    # extractor blew up → bare upload still proceeds → success
    assert await leg(AsyncMock(), _doc(), _PDF, _meta()) is True
