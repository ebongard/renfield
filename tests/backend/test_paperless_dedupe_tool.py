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


def _make_mcp(docs, contents=None, pages=None, rate_limit_first=0, search_error_after=None,
              all_docs=None, total_count=None):
    """Mock mcp_manager.
    ``docs``           = single-page search results (len < 500 ends pagination).
    ``contents``       = {id: ocr_text} returned by get_document (OFF/byte path only).
    ``pages``          = optional list of pages returned by successive searches
                         (multi-page pagination test).
    ``rate_limit_first`` = the first N delete calls return an MCP rate-limit error.
    ``search_error_after`` = the search errors once this many pages have been returned
                         (simulates a mid-sweep failure → partial coverage).
    ``all_docs``       = when set, list_all_documents serves these (with checksum);
                         when None, list_all_documents ERRORS so the code falls back to
                         the legacy search_documents sweep (preserves the old tests).
    ``total_count``    = list_all_documents summary total (default len(all_docs)).
    Records deletes in ``.deleted`` and get_document calls in ``.get_document_calls``."""
    contents = contents or {}

    class _MCP:
        def __init__(self):
            self.deleted: list[int] = []
            self.get_document_calls: list[tuple[int, dict]] = []
            self._search_i = 0
            self._del_i = 0

        async def execute_tool(self, tool, params, **kw):
            if tool == "mcp.paperless.list_all_documents":
                if all_docs is None:
                    # Reproduce MCPManager's REAL behavior on an OLD MCP that lacks
                    # this tool: it does NOT error — it fuzzy-falls-back to
                    # search_documents, returning a success-shaped result with a
                    # DIFFERENT summary (total_matching, NO total_count, no checksum).
                    # The dedupe MUST reject this and fall back to the date sweep.
                    return {"success": True, "message": json.dumps(
                        {"summary": {"total_matching": len(docs), "returned": len(docs)},
                         "results": docs})}
                tc = total_count if total_count is not None else len(all_docs)
                return {"success": True, "message": json.dumps({
                    "summary": {"total_count": tc, "returned": len(all_docs),
                                "truncated": len(all_docs) < tc},
                    "results": all_docs,
                })}
            if tool == "mcp.paperless.search_documents":
                if search_error_after is not None and self._search_i >= search_error_after:
                    self._search_i += 1
                    return {"success": False, "message": "mid-sweep search error"}
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
async def test_partial_sweep_is_not_reported_clean():
    """A mid-sweep search error must mark the sweep INCOMPLETE: even if every found
    duplicate is deleted, the tool must disclose partial coverage and never claim the
    archive is clean (the invariant the whole rework protects)."""
    page1 = [_doc(1, page_count=2), _doc(2, page_count=2)] + [
        _doc(1000 + i, title=f"unique-{i}", page_count=1) for i in range(498)
    ]  # 500 docs → forces a second page, which then errors
    mcp = _make_mcp([], pages=[page1], search_error_after=1)

    result = await paperless_dedupe({}, mcp_manager=mcp)

    assert mcp.deleted == [2]  # the found dup WAS deleted
    assert result["data"]["sweep_complete"] is False
    assert result["data"]["remaining"] == 0  # nothing remains IN THE SWEPT PART...
    assert "teilweise" in result["message"].lower()  # ...but coverage was partial
    assert "bereinigt" not in result["message"].lower()  # never claims fully clean


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


# --------------------------------------------------------------------------
# checksum-primary path (Fix C) — index-independent list_all_documents,
# exact byte-identical grouping that survives metadata drift
# --------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_checksum_groups_survive_metadata_drift():
    # Two byte-identical copies whose TITLE, DATE and page_count all differ — the
    # metadata key would NOT group them, but the checksum does. This is the
    # re-ingest-loop case the old path missed.
    all_docs = [
        _doc(1, checksum="X", title="Alpha", created="2024-01-01T00:00:00Z", page_count=2),
        _doc(2, checksum="X", title="Beta", created="2024-09-09T00:00:00Z", page_count=3),
    ]
    mcp = _make_mcp([], all_docs=all_docs)
    res = await paperless_dedupe({}, mcp_manager=mcp)
    assert res["data"]["groups"] == 1
    assert res["data"]["duplicate_copies"] == 1
    assert res["data"]["sweep_complete"] is True
    assert mcp.deleted == [2]  # keep lowest id


@pytest.mark.unit
@pytest.mark.asyncio
async def test_large_same_checksum_same_date_group_fully_enumerated():
    # 600 byte-identical copies ALL sharing one creation date — the exact shape the
    # legacy created_before day-window sweep could not page past (>500 in one date).
    # list_all_documents returns them all → the group is seen in full.
    all_docs = [_doc(i, checksum="LOOP", page_count=1) for i in range(1, 601)]
    mcp = _make_mcp([], all_docs=all_docs)
    res = await paperless_dedupe({"dry_run": True}, mcp_manager=mcp)
    assert res["data"]["documents_scanned"] == 600
    assert res["data"]["groups"] == 1
    assert res["data"]["duplicate_copies"] == 599  # keep 1, 599 extras
    assert res["data"]["sweep_complete"] is True


@pytest.mark.unit
@pytest.mark.asyncio
async def test_completeness_guard_when_enumeration_short_of_total():
    # list_all_documents returns fewer than total_count (e.g. hit the ceiling) →
    # sweep must NOT be reported complete; the message discloses partial coverage.
    all_docs = [_doc(1, checksum="X"), _doc(2, checksum="X")]
    mcp = _make_mcp([], all_docs=all_docs, total_count=5000)
    res = await paperless_dedupe({"dry_run": True}, mcp_manager=mcp)
    assert res["data"]["sweep_complete"] is False
    assert "TEILWEISE" in res["message"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_checksum_and_metadata_rescan_both_found():
    # A byte-identical pair (same checksum) AND a re-scan pair (same metadata+page,
    # DIFFERENT checksums) — both must be caught, by the two passes respectively.
    all_docs = [
        _doc(1, checksum="X", page_count=2),
        _doc(2, checksum="X", page_count=2),                       # byte-identical dup of 1
        _doc(3, checksum="Y", page_count=5, snippet="scan-a"),
        _doc(4, checksum="Z", page_count=5, snippet="scan-b"),     # re-scan of 3 (diff bytes)
    ]
    mcp = _make_mcp([], all_docs=all_docs)
    res = await paperless_dedupe({"dry_run": True}, mcp_manager=mcp)
    assert res["data"]["groups"] == 2
    assert res["data"]["duplicate_copies"] == 2       # one extra per group (2 and 4)
    assert res["data"]["metadata_groups"] == 1        # the re-scan group (text differs)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_falls_back_to_search_sweep_on_old_mcp_fuzzy_result():
    # all_docs=None → list_all_documents fuzzy-falls-back to a search-shaped result
    # (no total_count). The dedupe must REJECT that and dedupe via the legacy sweep.
    docs = [_doc(1, page_count=2), _doc(2, page_count=2)]
    mcp = _make_mcp(docs)
    res = await paperless_dedupe({}, mcp_manager=mcp)
    assert res["data"]["groups"] == 1
    assert mcp.deleted == [2]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_gather_rejects_totalcount_less_result_and_falls_back(monkeypatch):
    # CRITICAL guard: a success-shaped response WITHOUT total_count (the old-MCP
    # fuzzy fallback to search_documents) must NOT be accepted — must invoke the sweep.
    called = {"sweep": False}

    async def _fake_sweep(_mgr):
        called["sweep"] = True
        return [{"id": 9}], True, None

    monkeypatch.setattr(mod, "_gather_via_date_windows", _fake_sweep)
    mcp = _make_mcp([_doc(1)])  # all_docs=None → fuzzy (no total_count)
    gathered, complete, err = await mod._gather_all_documents(mcp)
    assert called["sweep"] is True
    assert [d["id"] for d in gathered] == [9] and err is None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_gather_uses_list_all_when_total_count_present(monkeypatch):
    # A genuine list_all_documents response (has total_count) is used directly — the
    # date-window sweep is NOT invoked.
    called = {"sweep": False}

    async def _fake_sweep(_mgr):
        called["sweep"] = True
        return [], True, None

    monkeypatch.setattr(mod, "_gather_via_date_windows", _fake_sweep)
    mcp = _make_mcp([], all_docs=[_doc(1, checksum="X")])
    gathered, complete, err = await mod._gather_all_documents(mcp)
    assert called["sweep"] is False
    assert [d["id"] for d in gathered] == [1] and complete is True
