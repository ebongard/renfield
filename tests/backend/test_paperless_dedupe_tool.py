"""Tests for internal.paperless_dedupe — duplicate finder + remover.

Duplicate definition: a document is the SAME as another when ALL its metadata is
identical (correspondent, document_type, date, title, page_count) OR its OCR bytes
are identical.

Two paths:
  - metadata-match ON (default): identity = full metadata tuple, read STRAIGHT FROM
    the search result (page_count + snippet included) — so this path makes NO
    get_document calls. Re-scans (same metadata, different OCR) are deduped.
  - metadata-match OFF: legacy byte-identical — fetches full OCR per candidate and
    compares bytes.
"""
import json

import pytest

from services.paperless_dedupe_tool import paperless_dedupe

_META = {
    "correspondent": "A",
    "document_type": "Rechnung",
    "created": "2024-01-15T00:00:00Z",
    "title": "Rechnung Jan",
}


def _doc(did, page_count=None, snippet="same", **over):
    """A search_documents result row. page_count + snippet now come from SEARCH, so
    the metadata-match path reads identity here without any get_document call."""
    return {"id": did, **_META, "page_count": page_count, "snippet": snippet, **over}


def _make_mcp(docs, contents=None):
    """Mock mcp_manager. ``docs`` = search_documents results (carry page_count +
    snippet); ``contents`` = {id: ocr_text} returned by get_document (used ONLY by the
    OFF/byte-identical path). Records delete ids in ``.deleted`` and every
    get_document call (id, kwargs) in ``.get_document_calls`` — the metadata path must
    leave that list empty."""
    contents = contents or {}

    class _MCP:
        def __init__(self):
            self.deleted: list[int] = []
            self.get_document_calls: list[tuple[int, dict]] = []

        async def execute_tool(self, tool, params, **kw):
            if tool == "mcp.paperless.search_documents":
                return {"success": True, "message": json.dumps({"results": docs})}
            if tool == "mcp.paperless.get_document":
                self.get_document_calls.append((params["document_id"], kw))
                return {
                    "success": True,
                    "message": json.dumps({"content": contents.get(params["document_id"])}),
                }
            if tool == "mcp.paperless.delete_document":
                did = params["document_id"]
                self.deleted.append(did)
                return {"success": True, "message": json.dumps({"deleted": True, "id": did})}
            return {"success": True, "message": "{}"}

    return _MCP()


# --------------------------------------------------------------------------
# metadata-match path (default ON) — identity from search, NO get_document
# --------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_deletes_identical_documents_keeps_oldest():
    docs = [_doc(1, page_count=2), _doc(2, page_count=2), _doc(3, page_count=2)]
    mcp = _make_mcp(docs)

    result = await paperless_dedupe({}, mcp_manager=mcp)

    assert result["success"] is True
    assert result["action_taken"] is True
    assert sorted(mcp.deleted) == [2, 3]  # kept the lowest id (1)
    assert result["data"]["groups"] == 1
    assert result["data"]["kept_ids"] == [1]
    assert result["data"]["metadata_groups"] == 0  # identical snippet → not a re-scan
    # THE optimization: the metadata path establishes identity from search alone.
    assert mcp.get_document_calls == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_same_metadata_deduped_even_when_ocr_differs():
    """The Audi-lease case: same metadata (no page_count → all-metadata-identical),
    DIFFERENT OCR snippet (re-scan) → deduped. Old 'different content is never
    deleted' behavior is intentionally gone."""
    docs = [_doc(1, snippet="body one"), _doc(2, snippet="totally different")]
    mcp = _make_mcp(docs)

    result = await paperless_dedupe({}, mcp_manager=mcp)

    assert result["success"] is True
    assert mcp.deleted == [2]  # kept the oldest (1), deleted the re-scan
    assert result["data"]["groups"] == 1
    assert result["data"]["metadata_groups"] == 1  # differing OCR → flagged as re-scan
    assert result["action_taken"] is True
    assert mcp.get_document_calls == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_metadata_identity_dedupes_even_without_ocr_text():
    """A member without OCR text (no snippet) is still deduped when its metadata is
    identical — sameness no longer needs OCR bytes once all metadata matches."""
    docs = [_doc(1, page_count=3, snippet="text"), _doc(2, page_count=3, snippet=None)]
    mcp = _make_mcp(docs)

    result = await paperless_dedupe({}, mcp_manager=mcp)

    assert mcp.deleted == [2]
    assert result["data"]["groups"] == 1
    assert result["data"]["skipped"] == 0
    assert mcp.get_document_calls == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_rescans_with_same_page_count_deduped():
    docs = [
        _doc(1, page_count=4, snippet="v1"),
        _doc(2, page_count=4, snippet="v2"),
        _doc(3, page_count=4, snippet="v3"),
    ]
    mcp = _make_mcp(docs)

    result = await paperless_dedupe({}, mcp_manager=mcp)

    assert sorted(mcp.deleted) == [2, 3]
    assert result["data"]["groups"] == 1
    assert result["data"]["metadata_groups"] == 1
    assert mcp.get_document_calls == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_different_page_count_keeps_documents_apart():
    """Same title/date but DIFFERENT page_count → different documents, not deleted."""
    docs = [_doc(1, page_count=4), _doc(2, page_count=7)]
    mcp = _make_mcp(docs)

    result = await paperless_dedupe({}, mcp_manager=mcp)

    assert mcp.deleted == []
    assert result["data"]["groups"] == 0
    assert result["action_taken"] is False


@pytest.mark.unit
@pytest.mark.asyncio
async def test_dedupes_when_page_count_absent():
    """When Paperless omits page_count, all-metadata-identical STILL holds (None is a
    value) — the fix must not quietly no-op just because page_count is unavailable."""
    docs = [_doc(1, snippet="a"), _doc(2, snippet="b")]  # page_count None on both
    mcp = _make_mcp(docs)

    result = await paperless_dedupe({}, mcp_manager=mcp)

    assert mcp.deleted == [2]
    assert result["data"]["groups"] == 1
    assert result["data"]["metadata_groups"] == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_byte_identical_deduped_and_not_flagged_as_rescan():
    """Identical documents (same page_count, same OCR snippet) dedupe as one group;
    metadata_groups stays 0 since the text is identical (not a re-scan)."""
    docs = [_doc(1, page_count=2, snippet="same"), _doc(2, page_count=2, snippet="same")]
    mcp = _make_mcp(docs)

    result = await paperless_dedupe({}, mcp_manager=mcp)

    assert sorted(mcp.deleted) == [2]
    assert result["data"]["groups"] == 1
    assert result["data"]["metadata_groups"] == 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_dry_run_deletes_nothing_but_reports():
    docs = [_doc(1, page_count=1), _doc(2, page_count=1)]
    mcp = _make_mcp(docs)

    result = await paperless_dedupe({"dry_run": True}, mcp_manager=mcp)

    assert mcp.deleted == []  # delete_document never called
    assert result["action_taken"] is False
    assert result["data"]["dry_run"] is True
    assert result["data"]["deleted"] == 1  # would-delete count
    assert result["data"]["deleted_ids"] == [2]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_unique_metadata_makes_no_fetches_or_deletes():
    """Distinct metadata → no duplicate groups → nothing fetched or deleted."""
    docs = [
        _doc(1, title="Rechnung Jan", page_count=2),
        _doc(2, title="Vertrag Feb", document_type="Vertrag", page_count=5),
    ]
    mcp = _make_mcp(docs)

    result = await paperless_dedupe({}, mcp_manager=mcp)

    assert mcp.deleted == []
    assert mcp.get_document_calls == []
    assert result["data"]["groups"] == 0


# --------------------------------------------------------------------------
# legacy byte-identical path (metadata-match OFF) — fetches full OCR
# --------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_off_mode_deletes_byte_identical(monkeypatch):
    from utils.config import settings

    monkeypatch.setattr(settings, "paperless_dedupe_metadata_match_enabled", False)
    docs = [_doc(1, page_count=4), _doc(2, page_count=4)]
    mcp = _make_mcp(docs, {1: "SAME BODY", 2: "SAME BODY"})

    result = await paperless_dedupe({}, mcp_manager=mcp)

    assert mcp.deleted == [2]
    assert result["data"]["groups"] == 1
    assert result["data"]["metadata_groups"] == 0  # byte-identical, not a metadata re-scan
    # the byte-identical path MUST fetch the FULL OCR text (truncate=False)
    assert mcp.get_document_calls, "OFF path must fetch content"
    assert all(kw.get("truncate") is False for _id, kw in mcp.get_document_calls)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_off_mode_keeps_non_identical_content(monkeypatch):
    """OFF = byte-identical only: same metadata + same page_count but different OCR →
    NOT deleted (this is exactly the re-scan the ON path would catch)."""
    from utils.config import settings

    monkeypatch.setattr(settings, "paperless_dedupe_metadata_match_enabled", False)
    docs = [_doc(1, page_count=4), _doc(2, page_count=4)]
    mcp = _make_mcp(docs, {1: "AUDI v1", 2: "AUDI v2"})

    result = await paperless_dedupe({}, mcp_manager=mcp)

    assert mcp.deleted == []
    assert result["data"]["groups"] == 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_off_mode_skips_member_without_ocr(monkeypatch):
    """OFF path: a member with no OCR text can't be proven identical → skipped."""
    from utils.config import settings

    monkeypatch.setattr(settings, "paperless_dedupe_metadata_match_enabled", False)
    docs = [_doc(1, page_count=4), _doc(2, page_count=4)]
    mcp = _make_mcp(docs, {1: "BODY", 2: None})

    result = await paperless_dedupe({}, mcp_manager=mcp)

    assert mcp.deleted == []
    assert result["data"]["groups"] == 0
    assert result["data"]["skipped"] == 1


# --------------------------------------------------------------------------
# permissions / guards
# --------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_permission_denied_without_admin(monkeypatch):
    from utils.config import settings

    monkeypatch.setattr(settings, "auth_enabled", True)
    mcp = _make_mcp([_doc(1, page_count=1), _doc(2, page_count=1)])

    result = await paperless_dedupe({}, mcp_manager=mcp, user_permissions=["ha.read"])

    assert result["success"] is False
    assert result["action_taken"] is False
    assert mcp.deleted == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_permission_denied_for_unidentified_turn_when_auth_on(monkeypatch):
    """Fail-closed: auth ON + user_permissions=None (device/unrecognized-voice) is
    DENIED for this destructive tool — must not trash documents without ADMIN."""
    from utils.config import settings

    monkeypatch.setattr(settings, "auth_enabled", True)
    mcp = _make_mcp([_doc(1, page_count=1), _doc(2, page_count=1)])

    result = await paperless_dedupe({}, mcp_manager=mcp, user_permissions=None)

    assert result["success"] is False
    assert result["action_taken"] is False
    assert mcp.deleted == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_no_mcp_returns_error():
    result = await paperless_dedupe({}, mcp_manager=None)
    assert result["success"] is False
    assert result["action_taken"] is False
