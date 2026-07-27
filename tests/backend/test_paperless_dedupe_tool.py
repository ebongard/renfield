"""Tests for internal.paperless_dedupe — duplicate finder + rate-limit-safe remover.

Duplicate definition: same document when ALL metadata is identical (correspondent,
document_type, date, title, page_count) OR OCR is byte-identical. Metadata deletion
requires a non-empty title AND a present page_count on every group member; otherwise
the group falls back to byte-identical (never delete a distinct doc on a weak signal).

The tool sweeps the FULL archive (paginated) and deletes a bounded batch per call
(``paperless_dedupe_delete_batch``, default 50) with retry/backoff under the Paperless
MCP's 60/min rate limit, reporting how many remain.
"""
import json

import pytest

import services.paperless_dedupe_tool as mod
from services.paperless_dedupe_tool import paperless_dedupe

_META = {
    "correspondent": "A",
    "document_type": "Rechnung",
    "created": "2024-01-15T00:00:00Z",
    "title": "Rechnung Jan",
}


def _doc(did, page_count=None, snippet="same", **over):
    """A search_documents result row (page_count + snippet come from search)."""
    return {"id": did, **_META, "page_count": page_count, "snippet": snippet, **over}


def _make_mcp(docs, contents=None, pages=None, rate_limit_first=0):
    """Mock mcp_manager.
    ``docs``           = single-page search results (len < 500 ends pagination).
    ``contents``       = {id: ocr_text} returned by get_document (OFF/byte path only).
    ``pages``          = optional list of pages returned by successive searches
                         (multi-page pagination test).
    ``rate_limit_first`` = the first N delete calls return an MCP rate-limit error.
    Records deletes in ``.deleted`` and get_document calls in ``.get_document_calls``."""
    contents = contents or {}

    class _MCP:
        def __init__(self):
            self.deleted: list[int] = []
            self.get_document_calls: list[tuple[int, dict]] = []
            self._search_i = 0
            self._del_i = 0

        async def execute_tool(self, tool, params, **kw):
            if tool == "mcp.paperless.search_documents":
                if pages is not None:
                    page = pages[self._search_i] if self._search_i < len(pages) else []
                    self._search_i += 1
                    return {"success": True, "message": json.dumps({"results": page})}
                return {"success": True, "message": json.dumps({"results": docs})}
            if tool == "mcp.paperless.get_document":
                self.get_document_calls.append((params["document_id"], kw))
                return {
                    "success": True,
                    "message": json.dumps({"content": contents.get(params["document_id"])}),
                }
            if tool == "mcp.paperless.delete_document":
                self._del_i += 1
                if self._del_i <= rate_limit_first:
                    return {"success": False, "message": "Rate limit exceeded for MCP server 'paperless'"}
                did = params["document_id"]
                self.deleted.append(did)
                return {"success": True, "message": json.dumps({"deleted": True, "id": did})}
            return {"success": True, "message": "{}"}

    return _MCP()


# --------------------------------------------------------------------------
# metadata-match path — identity from search, NO get_document
# --------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_deletes_identical_documents_keeps_oldest():
    docs = [_doc(1, page_count=2), _doc(2, page_count=2), _doc(3, page_count=2)]
    mcp = _make_mcp(docs)

    result = await paperless_dedupe({}, mcp_manager=mcp)

    assert result["success"] is True and result["action_taken"] is True
    assert sorted(mcp.deleted) == [2, 3]  # kept the lowest id (1)
    assert result["data"]["groups"] == 1
    assert result["data"]["deleted"] == 2
    assert result["data"]["remaining"] == 0
    assert result["data"]["kept"] == 1
    assert result["data"]["metadata_groups"] == 0  # identical snippet → not a re-scan
    assert mcp.get_document_calls == []  # identity from search alone


@pytest.mark.unit
@pytest.mark.asyncio
async def test_same_metadata_deduped_even_when_ocr_differs():
    """Audi-lease case: identical metadata (title + page_count present), DIFFERENT OCR
    snippet (re-scan) → deduped once ALL metadata (incl. page_count) matches."""
    docs = [_doc(1, page_count=2, snippet="body one"), _doc(2, page_count=2, snippet="different")]
    mcp = _make_mcp(docs)

    result = await paperless_dedupe({}, mcp_manager=mcp)

    assert mcp.deleted == [2]
    assert result["data"]["groups"] == 1
    assert result["data"]["metadata_groups"] == 1  # differing OCR → flagged as re-scan
    assert mcp.get_document_calls == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_metadata_identity_dedupes_even_without_ocr_text():
    docs = [_doc(1, page_count=3, snippet="text"), _doc(2, page_count=3, snippet=None)]
    mcp = _make_mcp(docs)

    result = await paperless_dedupe({}, mcp_manager=mcp)

    assert mcp.deleted == [2]
    assert result["data"]["groups"] == 1
    assert result["data"]["skipped"] == 0
    assert mcp.get_document_calls == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_different_page_count_keeps_documents_apart():
    docs = [_doc(1, page_count=4), _doc(2, page_count=7)]
    mcp = _make_mcp(docs)

    result = await paperless_dedupe({}, mcp_manager=mcp)

    assert mcp.deleted == []
    assert result["data"]["groups"] == 0
    assert result["action_taken"] is False


# --------------------------------------------------------------------------
# SAFETY (review): weak metadata → byte-identical fallback, distinct docs kept
# --------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_missing_page_count_falls_back_to_byte_identical():
    docs = [_doc(1), _doc(2)]  # page_count None on both
    mcp = _make_mcp(docs, {1: "BODY ONE", 2: "DIFFERENT BODY"})

    result = await paperless_dedupe({}, mcp_manager=mcp)

    assert mcp.deleted == []  # different content → not deleted
    assert result["data"]["groups"] == 0
    assert mcp.get_document_calls, "no page_count → must fall back to fetching OCR"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_missing_page_count_byte_identical_still_deduped():
    docs = [_doc(1), _doc(2)]
    mcp = _make_mcp(docs, {1: "IDENTICAL", 2: "IDENTICAL"})

    result = await paperless_dedupe({}, mcp_manager=mcp)

    assert mcp.deleted == [2]
    assert result["data"]["groups"] == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_empty_title_falls_back_to_byte_identical():
    docs = [_doc(1, title="", page_count=1), _doc(2, title="", page_count=1)]
    mcp = _make_mcp(docs, {1: "LETTER A", 2: "LETTER B"})

    result = await paperless_dedupe({}, mcp_manager=mcp)

    assert mcp.deleted == []
    assert result["data"]["groups"] == 0
    assert mcp.get_document_calls, "empty title → must fall back to fetching OCR"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_one_member_missing_page_count_makes_group_byte_identical():
    docs = [_doc(1, page_count=2), _doc(2)]  # doc 2 has no page_count
    mcp = _make_mcp(docs, {1: "X", 2: "Y"})

    result = await paperless_dedupe({}, mcp_manager=mcp)

    assert mcp.deleted == []
    assert result["data"]["groups"] == 0
    assert mcp.get_document_calls


# --------------------------------------------------------------------------
# batched delete + rate-limit retry + full-corpus pagination
# --------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_batch_cap_limits_deletes_and_reports_remaining(monkeypatch):
    """A single call deletes at most paperless_dedupe_delete_batch extras and reports
    the rest as remaining, so the caller re-runs to continue."""
    monkeypatch.setattr(mod.settings, "paperless_dedupe_delete_batch", 2)
    docs = [_doc(i, page_count=2) for i in (1, 2, 3, 4)]  # one group of 4 → 3 extras
    mcp = _make_mcp(docs)

    result = await paperless_dedupe({}, mcp_manager=mcp)

    assert len(mcp.deleted) == 2  # capped at the batch size
    assert result["data"]["deleted"] == 2
    assert result["data"]["remaining"] == 1
    assert result["data"]["duplicate_copies"] == 3
    assert "verbleiben" in result["message"].lower() or "räum" in result["message"].lower()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_rate_limited_delete_retries_then_succeeds(monkeypatch):
    """A delete rejected by the MCP rate limit is retried (after a short backoff) and
    then succeeds — the copy is deleted, not silently skipped."""
    monkeypatch.setattr(mod, "_RATE_SLEEP", 0)  # don't actually wait in the test
    docs = [_doc(1, page_count=2), _doc(2, page_count=2)]
    mcp = _make_mcp(docs, rate_limit_first=1)  # first delete call is rate-limited

    result = await paperless_dedupe({}, mcp_manager=mcp)

    assert mcp.deleted == [2]  # succeeded on retry
    assert result["data"]["deleted"] == 1
    assert result["data"]["remaining"] == 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_rate_limited_delete_gives_up_after_retries(monkeypatch):
    """If the rate limit never clears, the delete is abandoned (counted as remaining),
    never reported as deleted."""
    monkeypatch.setattr(mod, "_RATE_SLEEP", 0)
    monkeypatch.setattr(mod, "_RATE_RETRY", 3)
    docs = [_doc(1, page_count=2), _doc(2, page_count=2)]
    mcp = _make_mcp(docs, rate_limit_first=99)  # every delete rate-limited

    result = await paperless_dedupe({}, mcp_manager=mcp)

    assert mcp.deleted == []
    assert result["data"]["deleted"] == 0
    assert result["data"]["remaining"] == 1  # the one extra couldn't be deleted


@pytest.mark.unit
@pytest.mark.asyncio
async def test_full_corpus_pagination_finds_dupes_beyond_first_page():
    """The sweep pages the whole archive, so a duplicate group on a later page (beyond
    the 500-doc first page) is still found — the SWEEP_CAP blind spot is closed."""
    page1 = [_doc(1000 + i, title=f"unique-{i}", page_count=1) for i in range(500)]
    page2 = [_doc(1, page_count=2), _doc(2, page_count=2)]  # a dup pair, older page
    mcp = _make_mcp([], pages=[page1, page2])

    result = await paperless_dedupe({}, mcp_manager=mcp)

    assert result["data"]["documents_scanned"] == 502
    assert result["data"]["groups"] == 1
    assert mcp.deleted == [2]


# --------------------------------------------------------------------------
# dry-run + no-op
# --------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_dry_run_reports_full_scope_deletes_nothing():
    docs = [_doc(i, page_count=1) for i in (1, 2, 3)]
    mcp = _make_mcp(docs)

    result = await paperless_dedupe({"dry_run": True}, mcp_manager=mcp)

    assert mcp.deleted == []
    assert result["action_taken"] is False
    assert result["data"]["dry_run"] is True
    assert result["data"]["groups"] == 1
    assert result["data"]["duplicate_copies"] == 2  # would delete 2, keep 1
    assert result["data"]["remaining"] == 2


@pytest.mark.unit
@pytest.mark.asyncio
async def test_unique_metadata_no_dupes():
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
# legacy byte-identical path (metadata-match OFF)
# --------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_off_mode_deletes_byte_identical(monkeypatch):
    monkeypatch.setattr(mod.settings, "paperless_dedupe_metadata_match_enabled", False)
    docs = [_doc(1, page_count=4), _doc(2, page_count=4)]
    mcp = _make_mcp(docs, {1: "SAME BODY", 2: "SAME BODY"})

    result = await paperless_dedupe({}, mcp_manager=mcp)

    assert mcp.deleted == [2]
    assert result["data"]["groups"] == 1
    assert result["data"]["metadata_groups"] == 0
    assert mcp.get_document_calls
    assert all(kw.get("truncate") is False for _id, kw in mcp.get_document_calls)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_off_mode_keeps_non_identical_content(monkeypatch):
    monkeypatch.setattr(mod.settings, "paperless_dedupe_metadata_match_enabled", False)
    docs = [_doc(1, page_count=4), _doc(2, page_count=4)]
    mcp = _make_mcp(docs, {1: "AUDI v1", 2: "AUDI v2"})

    result = await paperless_dedupe({}, mcp_manager=mcp)

    assert mcp.deleted == []
    assert result["data"]["groups"] == 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_off_mode_skips_member_without_ocr(monkeypatch):
    monkeypatch.setattr(mod.settings, "paperless_dedupe_metadata_match_enabled", False)
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
    monkeypatch.setattr(mod.settings, "auth_enabled", True)
    mcp = _make_mcp([_doc(1, page_count=1), _doc(2, page_count=1)])

    result = await paperless_dedupe({}, mcp_manager=mcp, user_permissions=["ha.read"])

    assert result["success"] is False and result["action_taken"] is False
    assert mcp.deleted == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_permission_denied_for_unidentified_turn_when_auth_on(monkeypatch):
    monkeypatch.setattr(mod.settings, "auth_enabled", True)
    mcp = _make_mcp([_doc(1, page_count=1), _doc(2, page_count=1)])

    result = await paperless_dedupe({}, mcp_manager=mcp, user_permissions=None)

    assert result["success"] is False and result["action_taken"] is False
    assert mcp.deleted == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_no_mcp_returns_error():
    result = await paperless_dedupe({}, mcp_manager=None)
    assert result["success"] is False
    assert result["action_taken"] is False
