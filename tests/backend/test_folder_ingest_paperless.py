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
    resolve_correspondent_from_metadata,
    resolve_document_type_from_metadata,
    resolve_or_create_correspondent,
    resolve_or_create_taxonomy,
    resolve_tags_from_metadata,
)
from services.paperless_metadata_extractor import (
    ExtractionResult,
    FieldResolution,
    PaperlessMetadata,
)

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
# worker-supplied doc_text path + OCR content transport (Design Z)
# ---------------------------------------------------------------------------

def _patch_extractor_doc_text(monkeypatch, *, metadata=None, error=None):
    """Patch the extractor for the worker-supplied ``doc_text`` path: it must use
    ``extract_from_doc_text`` (reuse the OCR) and NOT re-OCR via
    ``extract_from_file``."""
    result = ExtractionResult(
        metadata=metadata or PaperlessMetadata(), doc_text="text", error=error
    )
    inst = MagicMock()
    inst.extract_from_doc_text = AsyncMock(return_value=result)
    inst.extract_from_file = AsyncMock(
        side_effect=AssertionError("must not re-OCR when doc_text is supplied")
    )
    monkeypatch.setattr(
        "services.paperless_metadata_extractor.PaperlessMetadataExtractor",
        MagicMock(return_value=inst),
    )
    return inst


def _mcp_transport(await_inner=None):
    """Mock mgr that also answers ``update_document`` (OCR content transport),
    recording the params on ``mgr._updates``."""
    await_inner = await_inner if await_inner is not None else {"status": "success", "document_id": 5}
    updates: list[dict] = []

    async def _execute(tool, params):
        if tool == "mcp.paperless.upload_document":
            return _envelope({"task_id": "t1"})
        if tool == "mcp.paperless.await_consume_result":
            return _envelope(await_inner)
        if tool == "mcp.paperless.update_document":
            updates.append(params)
            return _envelope({"document_id": params["document_id"], "updated": True})
        raise AssertionError(f"unexpected tool {tool}")

    mgr = MagicMock()
    mgr.execute_tool = AsyncMock(side_effect=_execute)
    mgr._updates = updates
    return mgr


async def test_doc_text_uses_extract_from_doc_text_not_file(monkeypatch):
    inst = _patch_extractor_doc_text(monkeypatch)
    mgr = _mcp_transport()
    leg = make_paperless_leg(mgr)
    assert await leg(AsyncMock(), _doc(), _PDF, _meta(), "worker ocr text") is True
    inst.extract_from_doc_text.assert_awaited_once()
    inst.extract_from_file.assert_not_called()


async def test_content_transport_on_success(monkeypatch):
    # After a fresh 'success' file, Renfield's OCR is PATCHed into the Paperless
    # document's searchable content (overwriting Paperless's weaker OCR).
    _patch_extractor_doc_text(monkeypatch)
    mgr = _mcp_transport(await_inner={"status": "success", "document_id": 42})
    leg = make_paperless_leg(mgr)
    assert await leg(AsyncMock(), _doc(), _PDF, _meta(), "worker ocr text") is True
    assert mgr._updates == [{"document_id": 42, "content": "worker ocr text"}]


async def test_no_content_transport_on_duplicate(monkeypatch):
    # A 'duplicate' already exists in Paperless → leave its content untouched.
    _patch_extractor_doc_text(monkeypatch)
    mgr = _mcp_transport(await_inner={"status": "duplicate", "document_id": 42})
    leg = make_paperless_leg(mgr)
    assert await leg(AsyncMock(), _doc(), _PDF, _meta(), "worker ocr text") is True
    assert mgr._updates == []


async def test_content_transport_failure_does_not_unsettle(monkeypatch):
    # update_document error is best-effort: the doc IS filed (state done, True).
    _patch_extractor_doc_text(monkeypatch)

    async def _execute(tool, params):
        if tool == "mcp.paperless.upload_document":
            return _envelope({"task_id": "t1"})
        if tool == "mcp.paperless.await_consume_result":
            return _envelope({"status": "success", "document_id": 42})
        if tool == "mcp.paperless.update_document":
            return {"success": False, "message": "paperless down"}
        raise AssertionError(f"unexpected tool {tool}")

    mgr = MagicMock()
    mgr.execute_tool = AsyncMock(side_effect=_execute)
    doc, db = _doc(), AsyncMock()
    assert await make_paperless_leg(mgr)(db, doc, _PDF, _meta(), "ocr text") is True
    assert doc.paperless_state == PAPERLESS_STATE_DONE


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


# ---------------------------------------------------------------------------
# resolve_or_create_correspondent (Option A + full-taxonomy guardrail)
# ---------------------------------------------------------------------------

def _corr_mgr(names, *, create_inner=None):
    """A mock mcp_manager dispatching list_correspondents + create_correspondent."""
    create_inner = create_inner if create_inner is not None else {"id": 999, "name": "_created_"}

    async def _execute(tool, params):
        if tool == "mcp.paperless.list_correspondents":
            return _envelope({"items": [{"id": i + 1, "name": n} for i, n in enumerate(names)]})
        if tool == "mcp.paperless.create_correspondent":
            inner = dict(create_inner)
            if inner.get("name") == "_created_":
                inner["name"] = params["name"]
            return _envelope(inner)
        raise AssertionError(f"unexpected tool {tool}")

    mgr = MagicMock()
    mgr.execute_tool = AsyncMock(side_effect=_execute)
    return mgr


def _patch_fuzzy(monkeypatch, *, strict, loose):
    monkeypatch.setattr("services.paperless_metadata_extractor._fuzzy_match", lambda v, t: strict)
    monkeypatch.setattr("services.paperless_metadata_extractor._fuzzy_top_candidates", lambda v, t, **k: loose)


def _created_tool_names(mgr):
    return [c.args[0] for c in mgr.execute_tool.await_args_list]


async def test_resolve_strong_match_reuses_existing_never_creates(monkeypatch):
    # A full-list strong match (recovers a pruned-window miss) → reuse, no create.
    _patch_fuzzy(monkeypatch, strict="regfish GmbH", loose=[])
    mgr = _corr_mgr(["regfish GmbH", "Stadtwerke"])
    out = await resolve_or_create_correspondent(mgr, "Regfish  GmbH")
    assert out == "regfish GmbH"
    assert "mcp.paperless.create_correspondent" not in _created_tool_names(mgr)


async def test_resolve_fuzzy_near_skips_and_never_creates(monkeypatch):
    # No strict match but a LOOSE near candidate exists → guardrail: leave unset.
    _patch_fuzzy(monkeypatch, strict=None, loose=["Stadtwerke Korschenbroich"])
    mgr = _corr_mgr(["Stadtwerke Korschenbroich"])
    out = await resolve_or_create_correspondent(mgr, "Stadtwerke Korschnbroich GmbH")
    assert out is None
    assert "mcp.paperless.create_correspondent" not in _created_tool_names(mgr)


async def test_resolve_genuinely_new_creates(monkeypatch):
    # No strict AND no loose match anywhere in the full list → create it.
    _patch_fuzzy(monkeypatch, strict=None, loose=[])
    mgr = _corr_mgr(["Stadtwerke"], create_inner={"id": 42, "name": "regfish GmbH"})
    out = await resolve_or_create_correspondent(mgr, "regfish GmbH")
    assert out == "regfish GmbH"
    assert "mcp.paperless.create_correspondent" in _created_tool_names(mgr)


async def test_resolve_taxonomy_unreadable_returns_none_no_create(monkeypatch):
    # list_correspondents transport failure → never guess / create.
    async def _execute(tool, params):
        if tool == "mcp.paperless.list_correspondents":
            return {"success": False, "message": "boom"}
        raise AssertionError(f"unexpected tool {tool}")

    mgr = MagicMock()
    mgr.execute_tool = AsyncMock(side_effect=_execute)
    out = await resolve_or_create_correspondent(mgr, "Anything")
    assert out is None


async def test_resolve_empty_value_is_noop(monkeypatch):
    mgr = _corr_mgr([])
    assert await resolve_or_create_correspondent(mgr, "   ") is None
    mgr.execute_tool.assert_not_called()


async def test_resolve_create_already_exists_reuses(monkeypatch):
    # A concurrent/exact dup at create time → reuse the existing name, not error.
    _patch_fuzzy(monkeypatch, strict=None, loose=[])
    mgr = _corr_mgr(["X"], create_inner={"error": "already_exists", "existing_id": 7, "existing_name": "regfish GmbH"})
    out = await resolve_or_create_correspondent(mgr, "regfish GmbH")
    assert out == "regfish GmbH"


# ---------------------------------------------------------------------------
# leg: auto-create wiring + paperless_document_id persistence
# ---------------------------------------------------------------------------

def _meta_new_sender():
    return PaperlessMetadata(
        resolutions=[FieldResolution(field="correspondent", extracted_value="regfish GmbH")]
    )


def _full_mgr(names, *, create_inner=None, await_inner=None):
    """Mock dispatching the leg's tools AND the helper's taxonomy tools."""
    await_inner = await_inner if await_inner is not None else {"status": "success", "document_id": 5}
    create_inner = create_inner if create_inner is not None else {"id": 999, "name": "_created_"}

    async def _execute(tool, params):
        if tool == "mcp.paperless.upload_document":
            return _envelope({"task_id": "t1"})
        if tool == "mcp.paperless.await_consume_result":
            return _envelope(await_inner)
        if tool == "mcp.paperless.list_correspondents":
            return _envelope({"items": [{"id": i + 1, "name": n} for i, n in enumerate(names)]})
        if tool == "mcp.paperless.create_correspondent":
            inner = dict(create_inner)
            if inner.get("name") == "_created_":
                inner["name"] = params["name"]
            return _envelope(inner)
        raise AssertionError(f"unexpected tool {tool}")

    mgr = MagicMock()
    mgr.execute_tool = AsyncMock(side_effect=_execute)
    return mgr


def _upload_params(mgr):
    for c in mgr.execute_tool.await_args_list:
        if c.args[0] == "mcp.paperless.upload_document":
            return c.args[1]
    raise AssertionError("upload_document was not called")


async def test_leg_auto_creates_new_correspondent(monkeypatch):
    _patch_extractor(monkeypatch, metadata=_meta_new_sender())
    _patch_fuzzy(monkeypatch, strict=None, loose=[])  # genuinely new
    mgr = _full_mgr(["Stadtwerke"], create_inner={"id": 42, "name": "regfish GmbH"})
    doc, db = _doc(), AsyncMock()

    assert await leg_mod.make_paperless_leg(mgr)(db, doc, _PDF, _meta()) is True
    assert _upload_params(mgr)["correspondent"] == "regfish GmbH"  # created + applied


async def test_leg_skips_correspondent_when_fuzzy_near(monkeypatch):
    _patch_extractor(monkeypatch, metadata=_meta_new_sender())
    _patch_fuzzy(monkeypatch, strict=None, loose=["regfish Domains GmbH"])  # ambiguous
    mgr = _full_mgr(["regfish Domains GmbH"])
    doc, db = _doc(), AsyncMock()

    await leg_mod.make_paperless_leg(mgr)(db, doc, _PDF, _meta())
    assert "correspondent" not in _upload_params(mgr)  # guardrail: left unset
    assert "mcp.paperless.create_correspondent" not in _created_tool_names(mgr)


async def test_leg_persists_paperless_document_id(monkeypatch):
    _patch_extractor(monkeypatch)  # no correspondent path needed
    mgr = _full_mgr([], await_inner={"status": "success", "document_id": 77})
    doc, db = _doc(), AsyncMock()

    assert await leg_mod.make_paperless_leg(mgr)(db, doc, _PDF, _meta()) is True
    assert doc.paperless_document_id == 77


# ---------------------------------------------------------------------------
# resolve_correspondent_from_metadata (shared leg/backfill source of truth)
# ---------------------------------------------------------------------------

async def test_metadata_exact_match_returned_directly(monkeypatch):
    # An exact taxonomy hit already populated m.correspondent → no MCP calls.
    mgr = MagicMock()
    mgr.execute_tool = AsyncMock(side_effect=AssertionError("should not call MCP"))
    meta = PaperlessMetadata(correspondent="Stadtwerke")
    assert await resolve_correspondent_from_metadata(mgr, meta) == "Stadtwerke"


async def test_metadata_none_resolution_creates(monkeypatch):
    _patch_fuzzy(monkeypatch, strict=None, loose=[])
    mgr = _corr_mgr(["X"], create_inner={"id": 9, "name": "regfish GmbH"})
    meta = PaperlessMetadata(
        resolutions=[FieldResolution(field="correspondent", extracted_value="regfish GmbH")]
    )
    assert await resolve_correspondent_from_metadata(mgr, meta) == "regfish GmbH"


async def test_metadata_NEAR_resolution_still_routes_through_full_list(monkeypatch):
    # Finding #4: a status=="near" resolution (a near match only in the extractor's
    # PRUNED window) must still go through the full-list helper — here the full
    # list has NO match, so the genuinely-new sender is created (not dropped).
    _patch_fuzzy(monkeypatch, strict=None, loose=[])
    mgr = _corr_mgr(["Unrelated"], create_inner={"id": 9, "name": "regfish GmbH"})
    meta = PaperlessMetadata(
        resolutions=[
            FieldResolution(
                field="correspondent", extracted_value="regfish GmbH",
                near_matches=["Some Recency-Window Name"],  # status == "near"
            )
        ]
    )
    assert meta.resolutions[0].status == "near"
    assert await resolve_correspondent_from_metadata(mgr, meta) == "regfish GmbH"
    assert "mcp.paperless.create_correspondent" in _created_tool_names(mgr)


async def test_metadata_no_correspondent_anywhere_returns_none(monkeypatch):
    mgr = MagicMock()
    mgr.execute_tool = AsyncMock(side_effect=AssertionError("should not call MCP"))
    # only a tag resolution, no correspondent → None, no MCP calls
    meta = PaperlessMetadata(resolutions=[FieldResolution(field="tag", extracted_value="energie")])
    assert await resolve_correspondent_from_metadata(mgr, meta) is None


async def test_resolve_or_create_uses_passed_names_no_list_call(monkeypatch):
    # Batch caller passes names → list_correspondents is NOT called.
    _patch_fuzzy(monkeypatch, strict="regfish GmbH", loose=[])
    calls = []

    async def _execute(tool, params):
        calls.append(tool)
        return _envelope({"id": 1, "name": "x"})

    mgr = MagicMock()
    mgr.execute_tool = AsyncMock(side_effect=_execute)
    out = await resolve_or_create_correspondent(mgr, "regfish GmbH", names=["regfish GmbH"])
    assert out == "regfish GmbH"
    assert "mcp.paperless.list_correspondents" not in calls  # used the passed list


async def test_resolve_dry_run_previews_without_creating(monkeypatch):
    # create=False: a genuinely-new sender is REPORTED but not created (dry-run).
    _patch_fuzzy(monkeypatch, strict=None, loose=[])
    mgr = _corr_mgr(["Unrelated"])
    out = await resolve_or_create_correspondent(mgr, "regfish GmbH", create=False)
    assert out == "regfish GmbH"  # previewed
    assert "mcp.paperless.create_correspondent" not in _created_tool_names(mgr)  # not created


async def test_metadata_dry_run_threads_create_flag(monkeypatch):
    _patch_fuzzy(monkeypatch, strict=None, loose=[])
    mgr = _corr_mgr(["Unrelated"])
    meta = PaperlessMetadata(
        resolutions=[FieldResolution(field="correspondent", extracted_value="regfish GmbH")]
    )
    out = await resolve_correspondent_from_metadata(mgr, meta, create=False)
    assert out == "regfish GmbH"
    assert "mcp.paperless.create_correspondent" not in _created_tool_names(mgr)


# ---------------------------------------------------------------------------
# document_type + tag resolve-or-create (self-populating taxonomy) — 2026-07:
# extends the correspondent resolve-or-create to doc_type/tags so a fresh/wiped
# Paperless self-populates these fields instead of leaving them empty.
# ---------------------------------------------------------------------------

def _tax_mgr(list_tool, names, create_tool, *, create_inner=None):
    """Mock mcp_manager dispatching one list_* + one create_* taxonomy tool."""
    create_inner = create_inner if create_inner is not None else {"id": 999, "name": "_created_"}

    async def _execute(tool, params):
        if tool == list_tool:
            return _envelope({"items": [{"id": i + 1, "name": n} for i, n in enumerate(names)]})
        if tool == create_tool:
            inner = dict(create_inner)
            if inner.get("name") == "_created_":
                inner["name"] = params["name"]
            return _envelope(inner)
        raise AssertionError(f"unexpected tool {tool}")

    mgr = MagicMock()
    mgr.execute_tool = AsyncMock(side_effect=_execute)
    return mgr


async def test_taxonomy_document_type_genuinely_new_creates(monkeypatch):
    _patch_fuzzy(monkeypatch, strict=None, loose=[])
    mgr = _tax_mgr("mcp.paperless.list_document_types", ["Vertrag"],
                   "mcp.paperless.create_document_type", create_inner={"id": 5, "name": "Rechnung"})
    out = await resolve_or_create_taxonomy(mgr, "document_type", "Rechnung")
    assert out == "Rechnung"
    assert "mcp.paperless.create_document_type" in _created_tool_names(mgr)


async def test_taxonomy_strong_match_reuses_never_creates(monkeypatch):
    _patch_fuzzy(monkeypatch, strict="Rechnung", loose=[])
    mgr = _tax_mgr("mcp.paperless.list_tags", ["Rechnung"], "mcp.paperless.create_tag")
    out = await resolve_or_create_taxonomy(mgr, "tag", "rechnung")
    assert out == "Rechnung"
    assert "mcp.paperless.create_tag" not in _created_tool_names(mgr)


async def test_taxonomy_fuzzy_near_skips(monkeypatch):
    _patch_fuzzy(monkeypatch, strict=None, loose=["Rechnungen"])
    mgr = _tax_mgr("mcp.paperless.list_document_types", ["Rechnungen"],
                   "mcp.paperless.create_document_type")
    out = await resolve_or_create_taxonomy(mgr, "document_type", "Rechnung")
    assert out is None
    assert "mcp.paperless.create_document_type" not in _created_tool_names(mgr)


async def test_document_type_exact_hit_used_directly(monkeypatch):
    # metadata.document_type set (exact) → return it, no MCP call needed.
    meta = PaperlessMetadata(document_type="Gehaltsabrechnung")
    mgr = MagicMock(); mgr.execute_tool = AsyncMock()
    out = await resolve_document_type_from_metadata(mgr, meta)
    assert out == "Gehaltsabrechnung"
    mgr.execute_tool.assert_not_called()


async def test_document_type_new_from_resolution_creates(monkeypatch):
    _patch_fuzzy(monkeypatch, strict=None, loose=[])
    meta = PaperlessMetadata(
        resolutions=[FieldResolution(field="document_type", extracted_value="Mahnung")]
    )
    mgr = _tax_mgr("mcp.paperless.list_document_types", ["Vertrag"],
                   "mcp.paperless.create_document_type", create_inner={"id": 7, "name": "Mahnung"})
    out = await resolve_document_type_from_metadata(mgr, meta)
    assert out == "Mahnung"


async def test_document_type_flag_off_is_resolve_only(monkeypatch):
    import utils.config as cfg_mod
    monkeypatch.setattr(cfg_mod.settings, "paperless_autocreate_document_type", False)
    meta = PaperlessMetadata(
        resolutions=[FieldResolution(field="document_type", extracted_value="Mahnung")]
    )
    mgr = MagicMock(); mgr.execute_tool = AsyncMock()
    out = await resolve_document_type_from_metadata(mgr, meta)
    assert out is None
    mgr.execute_tool.assert_not_called()


async def test_tags_exact_plus_created_deduped(monkeypatch):
    _patch_fuzzy(monkeypatch, strict=None, loose=[])
    meta = PaperlessMetadata(
        tags=["Steuer"],
        resolutions=[
            FieldResolution(field="tag", extracted_value="Versicherung"),
            FieldResolution(field="tag", extracted_value="Steuer"),  # dup of exact → collapse
        ],
    )
    mgr = _tax_mgr("mcp.paperless.list_tags", [], "mcp.paperless.create_tag")
    out = await resolve_tags_from_metadata(mgr, meta)
    assert "Steuer" in out and "Versicherung" in out
    assert len(out) == len(set(out))  # de-duplicated
