"""Tests for KB near-duplicate DOCUMENT detection (#1170).

The detector's self-join is Postgres-shaped (correlated recurring-identifier
count + NOT EXISTS on pending proposals), so the SQL is exercised via a
compile-smoke against the Postgres dialect (mirrors
test_kb_maintenance_tool.test_unindexable_queries_compile) rather than a live DB.
The survivor selection, the tool's dark-mode gate, and the message formatting are
unit-tested with mocked seams.
"""
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock

import pytest

import services.document_dedupe_service as dd
import services.document_dedupe_tool as tool
from services.document_dedupe_service import DedupeReport, DocumentDedupeService

pytestmark = [pytest.mark.unit]


# --------------------------------------------------------------------------
# SQL compile-smoke — the correlated self-join must be valid Postgres
# --------------------------------------------------------------------------

def test_pairs_query_compiles_auth_on():
    from sqlalchemy.dialects import postgresql

    q = DocumentDedupeService.build_pairs_query(user_id=7, auth_enabled=True)
    sql = str(q.compile(dialect=postgresql.dialect())).lower()
    assert "document_facts" in sql
    assert "documents" in sql
    # idempotency guard references the proposal table
    assert "document_duplicate_proposals" in sql
    # recurring-identifier gate: a correlated COUNT(DISTINCT ...) subquery
    assert "count(distinct" in sql
    # owner scope joins atoms when auth is on
    assert "atoms" in sql
    assert "owner_user_id" in sql


def test_pairs_query_no_atom_join_when_auth_off():
    from sqlalchemy.dialects import postgresql

    q = DocumentDedupeService.build_pairs_query(user_id=None, auth_enabled=False)
    sql = str(q.compile(dialect=postgresql.dialect())).lower()
    assert "document_facts" in sql
    # no owner scope → no atoms join
    assert "atoms" not in sql


def test_find_pairs_returns_empty_on_sqlite():
    """A non-postgres bind short-circuits (the self-join is PG-shaped)."""
    db = MagicMock()
    db.bind.dialect.name = "sqlite"
    svc = DocumentDedupeService(db)
    import asyncio
    assert asyncio.get_event_loop().run_until_complete(svc.find_duplicate_pairs(1)) == []


# --------------------------------------------------------------------------
# survivor selection
# --------------------------------------------------------------------------

def _rows(*specs):
    """specs = list of (id, paperless_document_id, nfacts)."""
    r = MagicMock()
    rows = []
    for _id, pid, nf in specs:
        row = MagicMock()
        row.id = _id
        row.paperless_document_id = pid
        row.nfacts = nf
        rows.append(row)
    r.all.return_value = rows
    return r


def _svc_with_execute(result):
    db = MagicMock()
    db.execute = AsyncMock(return_value=result)
    return DocumentDedupeService(db)


@pytest.mark.asyncio
async def test_pick_survivor_prefers_paperless_linked():
    # doc 44 is Paperless-linked, doc 45 is not → keep 44 even though 45 is higher id
    svc = _svc_with_execute(_rows((44, 50, 1), (45, None, 3)))
    assert await svc._pick_survivor(44, 45) == 44


@pytest.mark.asyncio
async def test_pick_survivor_prefers_more_facts_when_neither_linked():
    svc = _svc_with_execute(_rows((10, None, 2), (11, None, 5)))
    assert await svc._pick_survivor(10, 11) == 11


@pytest.mark.asyncio
async def test_pick_survivor_tiebreaks_lower_id():
    svc = _svc_with_execute(_rows((10, None, 3), (11, None, 3)))
    assert await svc._pick_survivor(10, 11) == 10


# --------------------------------------------------------------------------
# chat tool: dark-mode gate + message formatting
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_tool_disabled_reports_off(monkeypatch):
    monkeypatch.setattr(tool.settings, "document_dedupe_enabled", False)
    out = await tool.find_duplicate_documents({}, user_id=1)
    assert out["success"] is True
    assert out["data"]["enabled"] is False
    assert out["action_taken"] is False


def _fake_session(proposals, doc_lookup):
    """Session whose execute() first returns the proposals list, then a Document
    (scalar_one_or_none) for each subsequent per-doc lookup."""
    session = MagicMock()
    results = []
    pres = MagicMock()
    pres_scalars = MagicMock()
    pres_scalars.all.return_value = proposals
    pres.scalars.return_value = pres_scalars
    results.append(pres)
    for doc in doc_lookup:
        r = MagicMock()
        r.scalar_one_or_none.return_value = doc
        results.append(r)
    session.execute = AsyncMock(side_effect=results)

    @asynccontextmanager
    async def _cm():
        yield session

    return _cm


def _proposal(pid, a, b, survivor, key):
    p = MagicMock()
    p.id = pid
    p.document_a_id = a
    p.document_b_id = b
    p.suggested_survivor_id = survivor
    p.shared_key = key
    return p


def _doc(_id, name):
    d = MagicMock()
    d.id = _id
    d.generated_title = name
    d.title = None
    d.filename = None
    return d


@pytest.mark.asyncio
async def test_tool_reports_zero_pairs(monkeypatch):
    monkeypatch.setattr(tool.settings, "document_dedupe_enabled", True)
    monkeypatch.setattr(tool.settings, "auth_enabled", False)
    monkeypatch.setattr(
        DocumentDedupeService, "run_for_user",
        AsyncMock(return_value=DedupeReport(user_id=1, candidates=0, proposed=0)),
    )
    monkeypatch.setattr(tool, "AsyncSessionLocal", _fake_session([], []))
    out = await tool.find_duplicate_documents({}, user_id=1)
    assert out["success"] is True
    assert out["data"]["pending_pairs"] == 0
    assert "keine doppelten" in out["message"].lower()


@pytest.mark.asyncio
async def test_tool_reports_found_pairs(monkeypatch):
    monkeypatch.setattr(tool.settings, "document_dedupe_enabled", True)
    monkeypatch.setattr(tool.settings, "auth_enabled", False)
    monkeypatch.setattr(
        DocumentDedupeService, "run_for_user",
        AsyncMock(return_value=DedupeReport(user_id=1, candidates=1, proposed=1)),
    )
    proposals = [_proposal(1, 44, 45, 44, "invoice_number=1SOGUR2D-0011")]
    # per-proposal: doc_a lookup, then doc_b lookup
    docs = [_doc(44, "Rechnung 1SOGUR2D-0011"), _doc(45, "Rechnung 1SOGUR2D-0011 (2)")]
    monkeypatch.setattr(tool, "AsyncSessionLocal", _fake_session(proposals, docs))
    out = await tool.find_duplicate_documents({}, user_id=1)
    assert out["success"] is True
    assert out["data"]["pending_pairs"] == 1
    assert out["data"]["newly_proposed"] == 1
    assert out["action_taken"] is True
    assert "1SOGUR2D-0011" in out["message"]
    assert out["data"]["pairs"][0]["suggested_survivor_id"] == 44


# --------------------------------------------------------------------------
# Phase 2: resolve (approve) / reject
# --------------------------------------------------------------------------

def _rc(rowcount=1):
    r = MagicMock()
    r.rowcount = rowcount
    return r


def _resolve_session(execute_results):
    db = MagicMock()
    db.execute = AsyncMock(side_effect=list(execute_results))
    db.commit = AsyncMock()
    db.rollback = AsyncMock()
    db.flush = AsyncMock()
    return DocumentDedupeService(db), db


def _proposal_obj(sid=44, a=44, b=45):
    p = MagicMock()
    p.id = 1
    p.document_a_id = a
    p.document_b_id = b
    p.suggested_survivor_id = sid
    return p


@pytest.mark.asyncio
async def test_resolve_supersede_sets_column_and_approves():
    svc, db = _resolve_session([_rc(), _rc(1)])  # Document UPDATE, proposal claim
    out = await svc.resolve_proposal(_proposal_obj(), user_id=7, resolution="supersede")
    assert out["status"] == "approved"
    assert out["resolution"] == "supersede"
    assert out["survivor_id"] == 44 and out["loser_id"] == 45
    assert db.commit.await_count == 1


@pytest.mark.asyncio
async def test_resolve_survivor_override_flips_loser():
    svc, db = _resolve_session([_rc(), _rc(1)])
    out = await svc.resolve_proposal(_proposal_obj(), user_id=7, resolution="supersede", survivor_id=45)
    assert out["survivor_id"] == 45 and out["loser_id"] == 44


@pytest.mark.asyncio
async def test_resolve_invalid_survivor_falls_back_to_suggested():
    svc, db = _resolve_session([_rc(), _rc(1)])
    out = await svc.resolve_proposal(_proposal_obj(sid=44), user_id=7, resolution="supersede", survivor_id=999)
    assert out["survivor_id"] == 44  # 999 not in the pair → suggested


@pytest.mark.asyncio
async def test_resolve_supersede_double_resolve_is_noop(monkeypatch):
    svc, db = _resolve_session([_rc(), _rc(0)])  # claim finds 0 rows (already resolved)
    out = await svc.resolve_proposal(_proposal_obj(), user_id=7, resolution="supersede")
    assert out["status"] == "superseded"
    assert db.rollback.await_count == 1
    assert db.commit.await_count == 0


@pytest.mark.asyncio
async def test_resolve_delete_calls_delete_document(monkeypatch):
    svc, db = _resolve_session([_rc(1)])  # only the proposal claim
    rag = MagicMock()
    rag.delete_document = AsyncMock(return_value=True)
    monkeypatch.setattr("services.rag_service.RAGService", MagicMock(return_value=rag))
    out = await svc.resolve_proposal(_proposal_obj(), user_id=7, resolution="delete")
    assert out["status"] == "approved" and out["resolution"] == "delete"
    assert out["loser_id"] == 45
    rag.delete_document.assert_awaited_once_with(45)


@pytest.mark.asyncio
async def test_resolve_delete_double_resolve_noop(monkeypatch):
    svc, db = _resolve_session([_rc(0)])  # claim finds 0 rows
    rag = MagicMock()
    rag.delete_document = AsyncMock(return_value=True)
    monkeypatch.setattr("services.rag_service.RAGService", MagicMock(return_value=rag))
    out = await svc.resolve_proposal(_proposal_obj(), user_id=7, resolution="delete")
    assert out["status"] == "superseded"
    rag.delete_document.assert_not_awaited()


@pytest.mark.asyncio
async def test_reject_marks_rejected():
    svc, db = _resolve_session([_rc(1)])
    out = await svc.reject_proposal(_proposal_obj(), user_id=7)
    assert out["status"] == "rejected"
    assert db.commit.await_count == 1


@pytest.mark.asyncio
async def test_reject_double_is_noop():
    svc, db = _resolve_session([_rc(0)])
    out = await svc.reject_proposal(_proposal_obj(), user_id=7)
    assert out["status"] == "superseded"
    assert db.rollback.await_count == 1
