"""internal.reextract_paperless_metadata — gap-fill Paperless metadata backfill.

Backend-safe: no Docling (re-uses stored chunk text), no held DB connection, batch
taxonomy, include_content=False, filename fallback. These tests cover the cap,
permission gate, and the gap-fill decision (skip already-complete, patch empties).
"""
from unittest.mock import AsyncMock, MagicMock

import pytest

import services.paperless_reextract_tool as rt
from models.permissions import Permission

pytestmark = [pytest.mark.unit]


def test_cap_defaults_and_clamps():
    assert rt._cap({}) == rt.REEXTRACT_DEFAULT_CAP
    assert rt._cap({"limit": 5}) == 5
    assert rt._cap({"limit": 9999}) == rt.REEXTRACT_MAX_CAP
    assert rt._cap({"limit": "nonsense"}) == rt.REEXTRACT_DEFAULT_CAP
    assert rt._cap({"limit": 0}) == 1  # floored to >=1


async def test_permission_denied_for_low_privilege(monkeypatch):
    monkeypatch.setattr(rt.settings, "auth_enabled", True)
    out = await rt.reextract_paperless_metadata(
        {}, mcp_manager=MagicMock(), user_id=2, user_permissions=["rag.read"]
    )
    assert out["success"] is False
    assert "Berechtigung" in out["message"]


async def test_no_mcp_manager_is_graceful():
    out = await rt.reextract_paperless_metadata({}, mcp_manager=None, user_permissions=None)
    assert out["success"] is False
    assert "MCP" in out["message"]


def _envelope(inner):
    import json
    return {"success": True, "message": json.dumps(inner)}


async def test_gapfill_skips_already_complete_and_patches_empty(monkeypatch):
    """A doc whose Paperless fields are already set is left alone; a doc with an
    empty document_type gets it re-extracted + patched (gap-fill only)."""
    # Two filed docs with stored text.
    monkeypatch.setattr(rt.settings, "auth_enabled", False)
    monkeypatch.setattr(
        rt, "_gather_worklist",
        AsyncMock(return_value=[
            {"id": 1, "filename": "a.pdf", "paperless_id": 11, "user_id": None, "text": "Rechnung ..."},
            {"id": 2, "filename": "b.pdf", "paperless_id": 22, "user_id": None, "text": "Mahnung ..."},
        ]),
    )
    monkeypatch.setattr(rt, "_fetch_correspondent_names", AsyncMock(return_value=[]))
    monkeypatch.setattr(rt, "_fetch_taxonomy_names", AsyncMock(return_value=[]))

    # doc 11 already complete; doc 22 has empty document_type → should be patched.
    async def _execute(tool, params):
        if tool == "mcp.paperless.get_document":
            if params["document_id"] == 11:
                return _envelope({"correspondent": "X", "document_type": "Rechnung", "tags": [1]})
            return _envelope({"correspondent": "Y", "document_type": "", "tags": [1]})
        if tool == "mcp.paperless.update_document":
            return _envelope({"id": params["document_id"]})
        raise AssertionError(f"unexpected tool {tool}")

    mgr = MagicMock()
    mgr.execute_tool = AsyncMock(side_effect=_execute)

    # extractor returns a document_type for doc 22
    from services.paperless_metadata_extractor import PaperlessMetadata, ExtractionResult
    inst = MagicMock()
    inst.extract_from_doc_text = AsyncMock(
        return_value=ExtractionResult(metadata=PaperlessMetadata(document_type="Mahnung"), doc_text="t")
    )
    monkeypatch.setattr(
        "services.paperless_metadata_extractor.PaperlessMetadataExtractor",
        MagicMock(return_value=inst),
    )

    out = await rt.reextract_paperless_metadata({}, mcp_manager=mgr, user_permissions=None)
    assert out["success"] is True
    assert out["data"]["already"] == 1  # doc 11 untouched
    assert out["data"]["fixed"] == 1    # doc 22 patched
    # and the patch targeted document_type (gap-fill)
    upd = [c for c in mgr.execute_tool.await_args_list if c.args[0] == "mcp.paperless.update_document"]
    assert upd and upd[0].args[1].get("document_type") == "Mahnung"


async def test_no_docling_only_extract_from_doc_text(monkeypatch):
    """Regression guard: the tool must NEVER call extract_from_file (Docling OCR in
    the backend = OOM). It uses extract_from_doc_text on stored text only."""
    monkeypatch.setattr(rt.settings, "auth_enabled", False)
    monkeypatch.setattr(
        rt, "_gather_worklist",
        AsyncMock(return_value=[{"id": 1, "filename": "a.pdf", "paperless_id": 11, "user_id": None, "text": "x"}]),
    )
    monkeypatch.setattr(rt, "_fetch_correspondent_names", AsyncMock(return_value=[]))
    monkeypatch.setattr(rt, "_fetch_taxonomy_names", AsyncMock(return_value=[]))
    mgr = MagicMock()
    mgr.execute_tool = AsyncMock(return_value=_envelope({"correspondent": "", "document_type": "", "tags": []}))

    from services.paperless_metadata_extractor import PaperlessMetadata, ExtractionResult
    inst = MagicMock()
    inst.extract_from_doc_text = AsyncMock(return_value=ExtractionResult(metadata=PaperlessMetadata(), doc_text="t"))
    inst.extract_from_file = AsyncMock(side_effect=AssertionError("extract_from_file must NOT be called (Docling OOM)"))
    monkeypatch.setattr(
        "services.paperless_metadata_extractor.PaperlessMetadataExtractor",
        MagicMock(return_value=inst),
    )

    out = await rt.reextract_paperless_metadata({}, mcp_manager=mgr, user_permissions=None)
    assert out["success"] is True
    inst.extract_from_file.assert_not_called()
    inst.extract_from_doc_text.assert_awaited()
