"""Tests for the chat-triggerable KB maintenance tools.

`internal.reindex_documents` reindexes completed docs with 0 chunks (enqueues
user_reindex worker tasks); `internal.ingest_status` reports pipeline state;
`internal.list_chunkless_documents` lists them by name. All three now classify a
chunkless doc as REPAIRABLE vs genuinely UNINDEXABLE (unreadable scans the
quality gate keeps rejecting) via `_unindexable_exists()`.

DB + queue + redis are mocked at their seams; the SQL itself is exercised on
.159. `test_unindexable_queries_compile` compiles the classification clause
against the Postgres dialect so a correlation/syntax break is caught without a
live DB.
"""
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock

import pytest

import services.kb_maintenance_tool as kb

pytestmark = [pytest.mark.unit]


def _all_result(rows):
    r = MagicMock()
    r.all.return_value = rows
    return r


def _scalars_result(ids):
    r = MagicMock()
    r.scalars.return_value.all.return_value = ids
    return r


def _scalar_result(val):
    r = MagicMock()
    r.scalar.return_value = val
    return r


def _session(execute_results):
    """Session whose execute() returns the given result objects in order."""
    session = MagicMock()
    session.execute = AsyncMock(side_effect=list(execute_results))
    session.commit = AsyncMock()

    @asynccontextmanager
    async def _cm():
        yield session

    return _cm, session


def _patch_queue(monkeypatch):
    q = MagicMock()
    q.enqueue = AsyncMock()
    monkeypatch.setattr(
        "services.task_queue.DocumentTaskQueue", MagicMock(return_value=q)
    )
    monkeypatch.setattr("services.redis_client.get_redis", MagicMock(return_value=MagicMock()))
    return q


# --------------------------------------------------------------------------
# classification clause — compile-smoke (mocked tests never compile the SQL)
# --------------------------------------------------------------------------

def test_unindexable_queries_compile():
    from sqlalchemy import func, select
    from sqlalchemy.dialects import postgresql

    from models.database import DOC_STATUS_COMPLETED, Document

    clause = kb._unindexable_exists()
    # count query shape (ingest_status / list totals)
    count_q = (
        select(func.count())
        .select_from(Document)
        .where(Document.status == DOC_STATUS_COMPLETED, clause)
    )
    # column-projection shape (reindex / list rows)
    col_q = select(Document.id, kb._unindexable_exists().label("unindexable"))
    for q in (count_q, col_q):
        sql = str(q.compile(dialect=postgresql.dialect()))
        # the correlated EXISTS references the history table + correlates to documents
        assert "document_processing_history" in sql
        assert "chunks_dropped_low_quality" in sql


def test_searchable_chunk_subquery_filters_embedding():
    """The 'not searchable' predicate must key on EMBEDDED chunks, so a parent-only
    doc (chunks present, all embedding NULL) is detected, not just zero-chunk docs.
    Compile-smoke against Postgres — mocked tests never exercise this SQL."""
    from sqlalchemy import func, select
    from sqlalchemy.dialects import postgresql

    from models.database import DOC_STATUS_COMPLETED, Document

    sub = kb._searchable_chunk_subquery()
    q = (
        select(func.count())
        .select_from(Document)
        .outerjoin(sub, sub.c.document_id == Document.id)
        .where(Document.status == DOC_STATUS_COMPLETED, sub.c.document_id.is_(None))
    )
    sql = str(q.compile(dialect=postgresql.dialect())).lower()
    assert "document_chunks" in sql
    # the searchable predicate: only chunks WITH an embedding count
    assert "embedding is not null" in sql


def test_as_bool():
    assert kb._as_bool(True) is True
    assert kb._as_bool("true") is True and kb._as_bool("ja") is True
    assert kb._as_bool("false") is False and kb._as_bool("") is False
    assert kb._as_bool(None) is False


# --------------------------------------------------------------------------
# reindex_documents
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_reindex_enqueues_user_reindex_for_chunkless(monkeypatch):
    # select (id, unindexable) -> both repairable; then the UPDATE result (ignored)
    cm, session = _session([_scalar_result(0), _scalars_result([5, 9]), MagicMock()])
    monkeypatch.setattr(kb, "AsyncSessionLocal", cm)
    monkeypatch.setattr(kb.settings, "auth_enabled", False)
    q = _patch_queue(monkeypatch)

    out = await kb.reindex_documents({}, user_id=7)
    assert out["success"] and out["data"]["reindexed"] == 2
    assert out["data"]["skipped_unindexable"] == 0
    payloads = [c.args[0] for c in q.enqueue.await_args_list]
    assert {p["document_id"] for p in payloads} == {5, 9}
    assert all(p["trigger"] == "user_reindex" for p in payloads)
    assert all(p["user_id"] == 7 for p in payloads)
    assert all(p["force_ocr"] is False for p in payloads)  # default: reuse text layer
    assert session.commit.await_count == 1


@pytest.mark.asyncio
async def test_reindex_force_ocr_threads_into_enqueue(monkeypatch):
    """force_ocr=true enqueues force_full_page_ocr so garbled scans get re-OCR'd."""
    cm, _ = _session([_scalar_result(0), _scalars_result([5, 9]), MagicMock()])
    monkeypatch.setattr(kb, "AsyncSessionLocal", cm)
    monkeypatch.setattr(kb.settings, "auth_enabled", False)
    q = _patch_queue(monkeypatch)

    out = await kb.reindex_documents({"force_ocr": True}, user_id=7)
    assert out["success"] and out["data"]["reindexed"] == 2
    payloads = [c.args[0] for c in q.enqueue.await_args_list]
    assert all(p["force_ocr"] is True for p in payloads)


@pytest.mark.asyncio
async def test_reindex_skips_unindexable_by_default(monkeypatch):
    # one repairable (5) + two unindexable (9, 12) → only 5 reindexed
    cm, _ = _session([_scalar_result(2), _scalars_result([5]), MagicMock()])
    monkeypatch.setattr(kb, "AsyncSessionLocal", cm)
    monkeypatch.setattr(kb.settings, "auth_enabled", False)
    q = _patch_queue(monkeypatch)

    out = await kb.reindex_documents({}, user_id=1)
    assert out["data"]["reindexed"] == 1
    assert out["data"]["skipped_unindexable"] == 2
    assert "übersprungen" in out["message"]
    assert {c.args[0]["document_id"] for c in q.enqueue.await_args_list} == {5}


@pytest.mark.asyncio
async def test_reindex_force_includes_unindexable(monkeypatch):
    cm, _ = _session([_scalar_result(1), _scalars_result([5, 9]), MagicMock()])
    monkeypatch.setattr(kb, "AsyncSessionLocal", cm)
    monkeypatch.setattr(kb.settings, "auth_enabled", False)
    q = _patch_queue(monkeypatch)

    out = await kb.reindex_documents({"force": True}, user_id=1)
    assert out["data"]["reindexed"] == 2
    assert out["data"]["skipped_unindexable"] == 0
    assert {c.args[0]["document_id"] for c in q.enqueue.await_args_list} == {5, 9}


@pytest.mark.asyncio
async def test_reindex_all_unindexable_noop(monkeypatch):
    # every chunkless doc is unindexable and force is off → nothing enqueued
    cm, session = _session([_scalar_result(2), _scalars_result([])])
    monkeypatch.setattr(kb, "AsyncSessionLocal", cm)
    monkeypatch.setattr(kb.settings, "auth_enabled", False)
    q = _patch_queue(monkeypatch)

    out = await kb.reindex_documents({})
    assert out["success"] and out["data"]["reindexed"] == 0
    assert out["data"]["skipped_unindexable"] == 2
    assert out.get("empty_result") is True
    assert "unlesbare" in out["message"] and "force=true" in out["message"]
    q.enqueue.assert_not_awaited()
    session.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_reindex_noop_when_none_chunkless(monkeypatch):
    cm, session = _session([_scalar_result(0), _scalars_result([])])
    monkeypatch.setattr(kb, "AsyncSessionLocal", cm)
    monkeypatch.setattr(kb.settings, "auth_enabled", False)
    q = _patch_queue(monkeypatch)

    out = await kb.reindex_documents({})
    assert out["success"] and out["data"]["reindexed"] == 0
    assert out["data"]["skipped_unindexable"] == 0
    q.enqueue.assert_not_awaited()
    session.commit.assert_not_awaited()  # nothing flipped


@pytest.mark.asyncio
async def test_reindex_denied_for_low_priv_user(monkeypatch):
    monkeypatch.setattr(kb.settings, "auth_enabled", True)
    # a session that would blow up if reached — proves we short-circuit on the gate
    monkeypatch.setattr(kb, "AsyncSessionLocal", MagicMock(side_effect=AssertionError("must not query")))
    out = await kb.reindex_documents({}, user_id=3, user_permissions=["rag.use"])
    assert out["success"] is False
    assert out["action_taken"] is False


@pytest.mark.asyncio
async def test_reindex_allowed_with_rag_manage(monkeypatch):
    cm, _ = _session([_scalar_result(0), _scalars_result([11]), MagicMock()])
    monkeypatch.setattr(kb, "AsyncSessionLocal", cm)
    monkeypatch.setattr(kb.settings, "auth_enabled", True)
    _patch_queue(monkeypatch)
    out = await kb.reindex_documents({}, user_id=3, user_permissions=["rag.manage"])
    assert out["success"] and out["data"]["reindexed"] == 1


@pytest.mark.asyncio
async def test_reindex_allowed_when_permissions_none(monkeypatch):
    # auth on but user_permissions None (auth-off context / unidentified voice) → allowed
    cm, _ = _session([_scalar_result(0), _scalars_result([1]), MagicMock()])
    monkeypatch.setattr(kb, "AsyncSessionLocal", cm)
    monkeypatch.setattr(kb.settings, "auth_enabled", True)
    _patch_queue(monkeypatch)
    out = await kb.reindex_documents({}, user_id=None, user_permissions=None)
    assert out["success"] and out["data"]["reindexed"] == 1


@pytest.mark.asyncio
async def test_reindex_limit_clamped_and_reported(monkeypatch):
    # exactly cap rows → message flags "weitere folgen"
    cm, _ = _session([_scalar_result(0), _scalars_result([1, 2]), MagicMock()])
    monkeypatch.setattr(kb, "AsyncSessionLocal", cm)
    monkeypatch.setattr(kb.settings, "auth_enabled", False)
    _patch_queue(monkeypatch)
    out = await kb.reindex_documents({"limit": 2})
    assert out["data"]["reindexed"] == 2
    assert "weitere folgen" in out["message"]


@pytest.mark.asyncio
async def test_reindex_fails_when_all_enqueues_fail(monkeypatch):
    # Redis/queue outage: docs found but every enqueue raises → report FAILURE,
    # not a misleading success with reindexed=0.
    cm, session = _session([_scalar_result(0), _scalars_result([5, 9])])
    monkeypatch.setattr(kb, "AsyncSessionLocal", cm)
    monkeypatch.setattr(kb.settings, "auth_enabled", False)
    q = MagicMock()
    q.enqueue = AsyncMock(side_effect=RuntimeError("redis down"))
    monkeypatch.setattr("services.task_queue.DocumentTaskQueue", MagicMock(return_value=q))
    monkeypatch.setattr("services.redis_client.get_redis", MagicMock(return_value=MagicMock()))

    out = await kb.reindex_documents({})
    assert out["success"] is False
    assert out["action_taken"] is False
    assert out["data"]["reindexed"] == 0
    session.commit.assert_not_awaited()  # no status flip on total failure


@pytest.mark.asyncio
async def test_reindex_bad_limit_falls_back(monkeypatch):
    cm, _ = _session([_scalar_result(0), _scalars_result([])])
    monkeypatch.setattr(kb, "AsyncSessionLocal", cm)
    monkeypatch.setattr(kb.settings, "auth_enabled", False)
    _patch_queue(monkeypatch)
    out = await kb.reindex_documents({"limit": "not-a-number"})
    assert out["success"] is True  # bad limit ignored, no crash


# --------------------------------------------------------------------------
# ingest_status
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_ingest_status_reports_counts(monkeypatch):
    cm, _ = _session([
        _all_result([("completed", 10), ("pending", 5), ("processing", 1)]),  # status group
        _scalar_result(3),                                                     # chunkless total
        _scalar_result(1),                                                     # chunkless unindexable
        _all_result([("done", 8), (None, 2), ("pending", 5)]),                 # paperless group
        _scalar_result(6),                                                     # done docs LINKED to a paperless id (#1166)
    ])
    monkeypatch.setattr(kb, "AsyncSessionLocal", cm)
    # force the worker/queue probe down the except path (keep the test hermetic)
    monkeypatch.setattr(
        "services.redis_client.get_redis", MagicMock(side_effect=RuntimeError("no redis")))

    out = await kb.ingest_status({})
    assert out["success"] is True
    d = out["data"]
    assert d["documents_by_status"] == {"completed": 10, "pending": 5, "processing": 1}
    assert d["completed_without_chunks"] == 3
    assert d["chunkless_reindexable"] == 2
    assert d["chunkless_unindexable"] == 1
    assert d["paperless_state"]["done"] == 8
    assert d["paperless_state"]["unfiled"] == 2   # NULL → unfiled
    assert d["paperless_pending"] == 5
    # #1166: 6 of the 8 'done' docs actually link to a Paperless id; 2 are unverified.
    assert d["paperless_done_linked"] == 6
    assert d["paperless_done_unlinked"] == 2
    assert "noch nicht mit ihrer Paperless-ID verknüpft" in out["message"]
    assert "KB-Verarbeitung" in out["message"]
    assert "3 fertige Dokument(e) haben KEINE Chunks" in out["message"]
    assert "2 reparierbar" in out["message"] and "1 vermutlich unlesbar" in out["message"]


@pytest.mark.asyncio
async def test_ingest_status_all_unindexable_message(monkeypatch):
    cm, _ = _session([
        _all_result([("completed", 4)]),
        _scalar_result(2),   # chunkless total
        _scalar_result(2),   # all unindexable
        _all_result([("done", 4)]),
        _scalar_result(4),   # all done docs linked (#1166) → no unlinked hint
    ])
    monkeypatch.setattr(kb, "AsyncSessionLocal", cm)
    monkeypatch.setattr(
        "services.redis_client.get_redis", MagicMock(side_effect=RuntimeError("no redis")))
    out = await kb.ingest_status({})
    assert out["data"]["chunkless_reindexable"] == 0
    assert out["data"]["chunkless_unindexable"] == 2
    assert "unlesbare Scans" in out["message"]


# --------------------------------------------------------------------------
# list_chunkless_documents
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_list_chunkless_returns_names(monkeypatch):
    # execute #1 -> total; #2 -> unindexable total; #3 -> rows (id, name, flag)
    cm, _ = _session([
        _scalar_result(2),
        _scalar_result(0),
        _all_result([(9, "Doc B", False), (5, "Doc A", False)]),
    ])
    monkeypatch.setattr(kb, "AsyncSessionLocal", cm)
    out = await kb.list_chunkless_documents({})
    assert out["success"] is True
    assert out["data"]["count"] == 2 and out["data"]["total"] == 2
    assert out["data"]["total_repairable"] == 2 and out["data"]["total_unindexable"] == 0
    assert [d["id"] for d in out["data"]["documents"]] == [9, 5]
    assert "Doc B (#9)" in out["message"] and "Doc A (#5)" in out["message"]
    assert "Reparierbar" in out["message"]


@pytest.mark.asyncio
async def test_list_chunkless_labels_and_groups(monkeypatch):
    cm, _ = _session([
        _scalar_result(3),
        _scalar_result(1),
        _all_result([(9, "Bad Scan", True), (5, "Good", False)]),
    ])
    monkeypatch.setattr(kb, "AsyncSessionLocal", cm)
    out = await kb.list_chunkless_documents({})
    docs = {d["id"]: d for d in out["data"]["documents"]}
    assert docs[9]["unindexable"] is True and docs[5]["unindexable"] is False
    assert out["data"]["total_repairable"] == 2 and out["data"]["total_unindexable"] == 1
    assert "Vermutlich unlesbar" in out["message"] and "Bad Scan (#9)" in out["message"]
    assert "Reparierbar" in out["message"] and "Good (#5)" in out["message"]


@pytest.mark.asyncio
async def test_list_chunkless_empty(monkeypatch):
    cm, _ = _session([_scalar_result(0), _scalar_result(0), _all_result([])])
    monkeypatch.setattr(kb, "AsyncSessionLocal", cm)
    out = await kb.list_chunkless_documents({})
    assert out["success"] is True and out.get("empty_result") is True
    assert out["data"]["count"] == 0


@pytest.mark.asyncio
async def test_list_chunkless_truncated(monkeypatch):
    cm, _ = _session([
        _scalar_result(100),
        _scalar_result(0),
        _all_result([(1, "A", False), (2, "B", False)]),
    ])
    monkeypatch.setattr(kb, "AsyncSessionLocal", cm)
    out = await kb.list_chunkless_documents({"limit": 2})
    assert out["data"]["truncated"] is True and out["data"]["total"] == 100
    assert "zeige 2 von 100" in out["message"]


@pytest.mark.asyncio
async def test_ingest_status_reports_split_lifecycle(monkeypatch):
    """Split-parked documents must not vanish from the narrative (status
    contract): archived combined originals + split-lane in-flight rows get
    their own 'PDF-Split:' sentence, and the raw group-by carries them."""
    cm, _ = _session([
        _all_result([
            ("completed", 10),
            ("split_archived", 2),
            ("split_pending", 1),
            ("split_review", 1),
        ]),
        _scalar_result(0),
        _scalar_result(0),
        _all_result([("done", 12)]),
        _scalar_result(12),  # all done docs linked (#1166)
    ])
    monkeypatch.setattr(kb, "AsyncSessionLocal", cm)
    monkeypatch.setattr(
        "services.redis_client.get_redis", MagicMock(side_effect=RuntimeError("no redis")))

    out = await kb.ingest_status({})

    assert out["data"]["documents_by_status"]["split_archived"] == 2
    assert "PDF-Split:" in out["message"]
    assert "2 kombinierte Original-PDF(s)" in out["message"]
    assert "2 PDF(s) in der Split-Prüfung/-Verarbeitung" in out["message"]


@pytest.mark.asyncio
async def test_ingest_status_no_split_line_when_no_split_docs(monkeypatch):
    cm, _ = _session([
        _all_result([("completed", 10)]),
        _scalar_result(0),
        _scalar_result(0),
        _all_result([("done", 10)]),
        _scalar_result(10),  # all done docs linked (#1166)
    ])
    monkeypatch.setattr(kb, "AsyncSessionLocal", cm)
    monkeypatch.setattr(
        "services.redis_client.get_redis", MagicMock(side_effect=RuntimeError("no redis")))

    out = await kb.ingest_status({})

    assert "PDF-Split:" not in out["message"]


# --------------------------------------------------------------------------
# list_unfiled_documents — Paperless filing status by name (#1170 follow-up)
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_list_unfiled_lists_failed_and_pending(monkeypatch):
    # list mode: count query then rows query
    cm, _ = _session([
        _scalar_result(2),
        _all_result([
            (44, "Rechnung A", "failed", None),
            (45, "Rechnung B", "pending", None),
        ]),
    ])
    monkeypatch.setattr(kb, "AsyncSessionLocal", cm)

    out = await kb.list_unfiled_documents({})
    assert out["success"] is True
    assert out["data"]["total"] == 2
    assert "nicht (erfolgreich) in Paperless" in out["message"]
    states = {d["paperless_state"] for d in out["data"]["documents"]}
    assert states == {"failed", "pending"}
    assert all(d["in_paperless"] is False for d in out["data"]["documents"])


@pytest.mark.asyncio
async def test_list_unfiled_all_filed(monkeypatch):
    cm, _ = _session([_scalar_result(0), _all_result([])])
    monkeypatch.setattr(kb, "AsyncSessionLocal", cm)

    out = await kb.list_unfiled_documents({})
    assert out["success"] is True
    assert out["empty_result"] is True
    assert "Alle" in out["message"] and "abgelegt" in out["message"]


@pytest.mark.asyncio
async def test_list_unfiled_query_reports_filed_doc(monkeypatch):
    # query mode: ONE rows query; a done+linked doc → in_paperless True
    cm, _ = _session([
        _all_result([(44, "Rechnung Taxon", "done", 50)]),
    ])
    monkeypatch.setattr(kb, "AsyncSessionLocal", cm)

    out = await kb.list_unfiled_documents({"query": "Taxon"})
    assert out["success"] is True
    doc = out["data"]["documents"][0]
    assert doc["in_paperless"] is True
    assert doc["paperless_document_id"] == 50
    assert "Paperless-Dokument #50" in out["message"]


@pytest.mark.asyncio
async def test_list_unfiled_query_reports_failed_doc(monkeypatch):
    cm, _ = _session([
        _all_result([(60, "Rechnung X", "failed", None)]),
    ])
    monkeypatch.setattr(kb, "AsyncSessionLocal", cm)

    out = await kb.list_unfiled_documents({"query": "Rechnung X"})
    doc = out["data"]["documents"][0]
    assert doc["in_paperless"] is False
    assert "FEHLGESCHLAGEN" in out["message"]


@pytest.mark.asyncio
async def test_list_unfiled_query_done_unlinked_is_in_paperless(monkeypatch):
    # done but no linked id = Paperless accepted it as a duplicate → still "in Paperless"
    cm, _ = _session([
        _all_result([(45, "Rechnung Dup", "done", None)]),
    ])
    monkeypatch.setattr(kb, "AsyncSessionLocal", cm)

    out = await kb.list_unfiled_documents({"query": "Dup"})
    doc = out["data"]["documents"][0]
    assert doc["in_paperless"] is True
    assert "Duplikat" in out["message"]


@pytest.mark.asyncio
async def test_list_unfiled_query_no_match(monkeypatch):
    cm, _ = _session([_all_result([])])
    monkeypatch.setattr(kb, "AsyncSessionLocal", cm)

    out = await kb.list_unfiled_documents({"query": "nichts"})
    assert out["empty_result"] is True
    assert "kein Dokument" in out["message"]


# --------------------------------------------------------------------------
# refile_to_paperless — retry failed Paperless filings (#1170 follow-up)
# --------------------------------------------------------------------------

def _refile_redis(monkeypatch, lease_ok=True):
    redis = MagicMock()
    redis.set = AsyncMock(return_value=(True if lease_ok else None))
    monkeypatch.setattr(
        "services.redis_client.get_redis", MagicMock(return_value=redis))
    q = MagicMock()
    q.enqueue = AsyncMock()
    monkeypatch.setattr(
        "services.task_queue.DocumentTaskQueue", MagicMock(return_value=q))
    return q


@pytest.mark.asyncio
async def test_refile_queues_failed_docs(monkeypatch):
    # rows query (failed docs: id, owner) then the UPDATE result
    cm, session = _session([_all_result([(60, 1), (61, 2)]), MagicMock()])
    monkeypatch.setattr(kb, "AsyncSessionLocal", cm)
    monkeypatch.setattr(kb.settings, "auth_enabled", False)
    q = _refile_redis(monkeypatch)

    out = await kb.refile_to_paperless({})
    assert out["success"] is True
    assert out["data"]["targeted"] == 2
    assert out["data"]["queued"] == 2
    payloads = [c.args[0] for c in q.enqueue.await_args_list]
    assert {p["document_id"] for p in payloads} == {60, 61}
    assert all(p["trigger"] == "paperless_refile" for p in payloads)
    assert session.commit.await_count == 1  # flip failed→pending committed


@pytest.mark.asyncio
async def test_refile_nothing_failed(monkeypatch):
    cm, _ = _session([_all_result([])])
    monkeypatch.setattr(kb, "AsyncSessionLocal", cm)
    monkeypatch.setattr(kb.settings, "auth_enabled", False)
    _refile_redis(monkeypatch)

    out = await kb.refile_to_paperless({})
    assert out["success"] is True
    assert out["empty_result"] is True
    assert out["data"]["queued"] == 0


@pytest.mark.asyncio
async def test_refile_permission_denied(monkeypatch):
    monkeypatch.setattr(kb.settings, "auth_enabled", True)
    out = await kb.refile_to_paperless({}, user_permissions=[])
    assert out["success"] is False
    assert "Berechtigung" in out["message"]


@pytest.mark.asyncio
async def test_refile_lease_held_skips_enqueue(monkeypatch):
    # the periodic reconciler already leased this doc → we skip the direct enqueue,
    # but it's still flipped to pending (targeted=1, queued=0), reconciler backstops
    cm, _ = _session([_all_result([(60, 1)]), MagicMock()])
    monkeypatch.setattr(kb, "AsyncSessionLocal", cm)
    monkeypatch.setattr(kb.settings, "auth_enabled", False)
    q = _refile_redis(monkeypatch, lease_ok=False)

    out = await kb.refile_to_paperless({})
    assert out["success"] is True
    assert out["data"]["targeted"] == 1
    assert out["data"]["queued"] == 0
    assert q.enqueue.await_count == 0


# --------------------------------------------------------------------------
# list_unfiled_documents — owner scoping + wildcard escaping (#1171 review)
# --------------------------------------------------------------------------

def _compiled(session, call_index=0):
    from sqlalchemy.dialects import postgresql
    stmt = session.execute.await_args_list[call_index].args[0]
    return str(stmt.compile(dialect=postgresql.dialect())).lower()


@pytest.mark.asyncio
async def test_list_unfiled_owner_scoped_for_non_admin(monkeypatch):
    cm, session = _session([_all_result([])])
    monkeypatch.setattr(kb, "AsyncSessionLocal", cm)
    monkeypatch.setattr(kb.settings, "auth_enabled", True)
    monkeypatch.setattr(kb, "has_permission", lambda *_a: False)  # non-admin

    await kb.list_unfiled_documents({"query": "x"}, user_id=5, user_permissions=[])
    sql = _compiled(session)
    assert "atoms" in sql and "owner_user_id" in sql  # scoped to the caller's docs


@pytest.mark.asyncio
async def test_list_unfiled_admin_sees_all(monkeypatch):
    cm, session = _session([_all_result([])])
    monkeypatch.setattr(kb, "AsyncSessionLocal", cm)
    monkeypatch.setattr(kb.settings, "auth_enabled", True)
    monkeypatch.setattr(kb, "has_permission", lambda *_a: True)  # admin

    await kb.list_unfiled_documents({"query": "x"}, user_id=5, user_permissions=["admin"])
    sql = _compiled(session)
    assert "atoms" not in sql  # admin: no owner scope


@pytest.mark.asyncio
async def test_list_unfiled_unscoped_when_auth_off(monkeypatch):
    cm, session = _session([_all_result([])])
    monkeypatch.setattr(kb, "AsyncSessionLocal", cm)
    monkeypatch.setattr(kb.settings, "auth_enabled", False)

    await kb.list_unfiled_documents({"query": "x"}, user_id=5, user_permissions=[])
    sql = _compiled(session)
    assert "atoms" not in sql  # single-user: no scope


def test_escape_like_neutralizes_wildcards():
    assert kb._escape_like("50%_off") == "50\\%\\_off"
    assert kb._escape_like("a\\b") == "a\\\\b"
