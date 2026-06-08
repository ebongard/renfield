"""Tests for the folder-ingest feature (watch-folder auto-ingest).

Increment 1 covers the shared race-safe create helper (D3,
``RAGService.create_document_record_safe``) that both the upload route and
the folder-ingest bridge use, so the ``uq_documents_file_hash_kb``
concurrent-insert race is handled in exactly one place.

The classification logic (hash-race vs other IntegrityError) is unit-tested
with mocks: ``create_document_record`` commits internally, which is
incompatible with the rollback-isolated ``pg_db_session`` fixture, and the
behaviour under test is purely "given an IntegrityError, classify and react",
not the constraint itself. The constraint's real enforcement is covered
end-to-end by the route tests + the .159 E2E.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.exc import IntegrityError

import services.folder_ingest as fi
from models.database import (
    DOC_STATUS_COMPLETED,
    DOC_STATUS_FAILED,
    DOC_STATUS_PENDING,
    DOC_STATUS_PROCESSING,
    PAPERLESS_STATE_DONE,
    PAPERLESS_STATE_FAILED,
    Document,
)
from services.folder_ingest import (
    IngestMeta,
    IngestStatus,
    _Decision,
    classify_existing,
    ingest_document,
)
from services.rag_service import DuplicateDocumentError, RAGService


def _integrity_error(message: str) -> IntegrityError:
    """Build an IntegrityError whose ``str(orig)`` carries ``message`` —
    mirrors how asyncpg surfaces a unique-violation through SQLAlchemy."""
    return IntegrityError("INSERT INTO documents ...", {}, Exception(message))


def _bare_rag(create_side_effect=None, create_return=None) -> RAGService:
    """A RAGService with the heavy __init__ bypassed (no DocumentProcessor),
    wired with an AsyncMock db and a stubbed create_document_record."""
    rag = RAGService.__new__(RAGService)
    rag.db = AsyncMock()
    rag.create_document_record = AsyncMock(
        side_effect=create_side_effect, return_value=create_return
    )
    return rag


@pytest.mark.unit
@pytest.mark.asyncio
async def test_safe_create_passthrough_returns_document():
    sentinel = object()
    rag = _bare_rag(create_return=sentinel)

    out = await rag.create_document_record_safe(
        file_path="/x.pdf", knowledge_base_id=1, filename="x.pdf", file_hash="h"
    )

    assert out is sentinel
    rag.db.rollback.assert_not_called()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_safe_create_hash_race_raises_duplicate_with_winner():
    rag = _bare_rag(
        create_side_effect=_integrity_error(
            'duplicate key value violates unique constraint "uq_documents_file_hash_kb"'
        )
    )
    winner = MagicMock(id=42)
    result = MagicMock()
    result.scalar_one_or_none.return_value = winner
    # Wire rollback + execute as children of ONE parent so mock_calls records
    # their relative ORDER: the rollback MUST precede the winner SELECT, else a
    # real asyncpg session is still in InFailedSqlTransaction and the SELECT
    # raises. The mocked db has no session state, so without this ordering check
    # a SELECT-before-rollback reorder would pass green.
    manager = MagicMock()
    manager.rollback = AsyncMock()
    manager.execute = AsyncMock(return_value=result)
    rag.db.rollback = manager.rollback
    rag.db.execute = manager.execute

    with pytest.raises(DuplicateDocumentError) as excinfo:
        await rag.create_document_record_safe(
            file_path="/x.pdf", knowledge_base_id=1, filename="x.pdf", file_hash="h"
        )

    assert excinfo.value.winner is winner
    rag.db.rollback.assert_awaited_once()
    ordered = [c[0] for c in manager.mock_calls]
    assert ordered.index("rollback") < ordered.index("execute"), (
        f"rollback must precede the winner SELECT; got order {ordered}"
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_safe_create_other_integrity_error_reraises():
    # A non-hash-race IntegrityError (FK / NOT NULL) must NOT be masked as a
    # duplicate — it re-raises so the caller can surface a genuine 500.
    rag = _bare_rag(
        create_side_effect=_integrity_error(
            'insert violates foreign key constraint "fk_documents_kb"'
        )
    )

    with pytest.raises(IntegrityError):
        await rag.create_document_record_safe(
            file_path="/x.pdf", knowledge_base_id=1, filename="x.pdf", file_hash="h"
        )

    rag.db.rollback.assert_awaited_once()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_safe_create_hash_race_winner_unfetchable_is_none():
    # If the winning row can't be re-fetched (already gone), the error still
    # raises with winner=None rather than blowing up.
    rag = _bare_rag(
        create_side_effect=_integrity_error(
            'duplicate key value violates unique constraint "uq_documents_file_hash_kb"'
        )
    )
    result = MagicMock()
    result.scalar_one_or_none.return_value = None
    rag.db.execute = AsyncMock(return_value=result)

    with pytest.raises(DuplicateDocumentError) as excinfo:
        await rag.create_document_record_safe(
            file_path="/x.pdf", knowledge_base_id=1, filename="x.pdf", file_hash="h"
        )

    assert excinfo.value.winner is None


# ===========================================================================
# T2 — completion+Paperless-aware dedup matrix (D2), against real Postgres.
#
# classify_existing is SELECT-only (no commit), so it runs safely inside the
# rollback-isolated pg_db_session: rows are pushed with flush(), the decision
# is asserted, teardown rolls back. This is the risky SQL branch the eng
# review flagged — it gets real-PG coverage, not the sqlite shim.
# ===========================================================================

async def _insert_doc(db, *, file_hash, status, paperless_state=None, kb_id=None):
    doc = Document(
        filename="invoice.pdf",
        file_path=f"/uploads/{file_hash}.pdf",
        knowledge_base_id=kb_id,
        file_hash=file_hash,
        status=status,
        paperless_state=paperless_state,
        circle_tier=0,
    )
    db.add(doc)
    await db.flush()
    return doc


@pytest.mark.database
@pytest.mark.asyncio
async def test_classify_no_row_is_create(pg_db_session):
    decision, doc = await classify_existing(pg_db_session, "missinghash", None)
    assert decision is _Decision.CREATE
    assert doc is None


@pytest.mark.database
@pytest.mark.asyncio
async def test_classify_completed_paperless_done_is_duplicate(pg_db_session):
    await _insert_doc(
        pg_db_session,
        file_hash="h_done",
        status=DOC_STATUS_COMPLETED,
        paperless_state=PAPERLESS_STATE_DONE,
    )
    decision, doc = await classify_existing(pg_db_session, "h_done", None)
    assert decision is _Decision.DUPLICATE
    assert doc is not None


@pytest.mark.database
@pytest.mark.asyncio
async def test_classify_completed_paperless_failed_is_duplicate(pg_db_session):
    # A terminally-tried Paperless leg ('failed' — non-duplicate reject) is
    # SETTLED, so a re-push dedups instead of looping the leg.
    await _insert_doc(
        pg_db_session,
        file_hash="h_plfail",
        status=DOC_STATUS_COMPLETED,
        paperless_state=PAPERLESS_STATE_FAILED,
    )
    decision, _ = await classify_existing(pg_db_session, "h_plfail", None)
    assert decision is _Decision.DUPLICATE


@pytest.mark.database
@pytest.mark.asyncio
async def test_classify_completed_paperless_missing_is_paperless_only(pg_db_session):
    # completed but paperless_state NULL → run only the Paperless leg.
    await _insert_doc(
        pg_db_session,
        file_hash="h_nopl",
        status=DOC_STATUS_COMPLETED,
        paperless_state=None,
    )
    decision, _ = await classify_existing(pg_db_session, "h_nopl", None)
    assert decision is _Decision.PAPERLESS_ONLY


@pytest.mark.database
@pytest.mark.asyncio
async def test_classify_failed_is_reingest(pg_db_session):
    await _insert_doc(pg_db_session, file_hash="h_fail", status=DOC_STATUS_FAILED)
    decision, _ = await classify_existing(pg_db_session, "h_fail", None)
    assert decision is _Decision.REINGEST


@pytest.mark.database
@pytest.mark.asyncio
@pytest.mark.parametrize("status", [DOC_STATUS_PENDING, DOC_STATUS_PROCESSING])
async def test_classify_in_flight_is_retry(pg_db_session, status):
    await _insert_doc(pg_db_session, file_hash=f"h_{status}", status=status)
    decision, _ = await classify_existing(pg_db_session, f"h_{status}", None)
    assert decision is _Decision.RETRY


@pytest.mark.database
@pytest.mark.asyncio
async def test_classify_is_scoped_by_kb(pg_db_session):
    # Same hash in a different KB must not match (the uniqueness key is the
    # (file_hash, knowledge_base_id) pair). Here the only row is kb=None;
    # a lookup scoped to a different (non-existent) kb id finds nothing.
    await _insert_doc(
        pg_db_session,
        file_hash="h_kb",
        status=DOC_STATUS_COMPLETED,
        paperless_state=PAPERLESS_STATE_DONE,
        kb_id=None,
    )
    decision, _ = await classify_existing(pg_db_session, "h_kb", 999)
    assert decision is _Decision.CREATE


# ===========================================================================
# T2 — 4-state response mapping (D9), orchestrator control flow.
#
# The dedup SELECT, byte persistence, create, and enqueue are mocked at their
# seams so these assert the branch→response mapping itself. The real SQL is
# covered by the classify tests above; the real create-race by the helper
# tests; the .159 E2E covers the full wiring.
# ===========================================================================

_PDF = b"%PDF-1.4 minimal test bytes"


def _meta(filename="invoice.pdf", sha256=None):
    return IngestMeta(filename=filename, sha256=sha256)


def _patch_pipeline(monkeypatch, *, decision, existing=None, created_doc=None):
    """Wire classify_existing + the create/persist/enqueue seams. Returns the
    enqueue AsyncMock so a test can assert (not) enqueued."""
    monkeypatch.setattr(
        fi, "classify_existing", AsyncMock(return_value=(decision, existing))
    )
    monkeypatch.setattr(fi, "_persist_bytes", AsyncMock(return_value="/uploads/x.pdf"))

    enqueue = AsyncMock()
    monkeypatch.setattr(
        fi, "DocumentTaskQueue", MagicMock(return_value=MagicMock(enqueue=enqueue))
    )
    monkeypatch.setattr(fi, "get_redis", MagicMock(return_value=MagicMock()))

    if created_doc is not None:
        rag = MagicMock()
        rag.create_document_record_safe = AsyncMock(return_value=created_doc)
        monkeypatch.setattr(fi, "RAGService", MagicMock(return_value=rag))
    return enqueue


@pytest.mark.unit
@pytest.mark.asyncio
async def test_ingest_bad_extension_is_failed():
    out = await ingest_document(_PDF, _meta("notes.exe"), db=AsyncMock(), kb_id=None)
    assert out.status is IngestStatus.FAILED
    assert out.detail == "extension_not_allowed"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_ingest_oversize_is_failed(monkeypatch):
    monkeypatch.setattr(fi.settings, "max_file_size_mb", 1)
    big = b"x" * (2 * 1024 * 1024)  # 2 MB > 1 MB ceiling
    # ext must pass first, so use a .pdf name
    out = await ingest_document(big, _meta("big.pdf"), db=AsyncMock(), kb_id=None)
    assert out.status is IngestStatus.FAILED
    assert out.detail == "file_too_large"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_ingest_sha_mismatch_is_retry():
    out = await ingest_document(
        _PDF, _meta(sha256="deadbeef" * 8), db=AsyncMock(), kb_id=None
    )
    assert out.status is IngestStatus.RETRY
    assert out.detail == "sha256_mismatch"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_ingest_duplicate_decision_maps_to_duplicate(monkeypatch):
    existing = MagicMock(id=7)
    _patch_pipeline(monkeypatch, decision=_Decision.DUPLICATE, existing=existing)
    out = await ingest_document(_PDF, _meta(), db=AsyncMock(), kb_id=None)
    assert out.status is IngestStatus.DUPLICATE
    assert out.document_id == 7


@pytest.mark.unit
@pytest.mark.asyncio
async def test_ingest_in_flight_decision_maps_to_retry(monkeypatch):
    existing = MagicMock(id=8)
    enqueue = _patch_pipeline(monkeypatch, decision=_Decision.RETRY, existing=existing)
    out = await ingest_document(_PDF, _meta(), db=AsyncMock(), kb_id=None)
    assert out.status is IngestStatus.RETRY
    assert out.document_id == 8
    enqueue.assert_not_awaited()  # must NOT double-enqueue an in-flight row


@pytest.mark.unit
@pytest.mark.asyncio
async def test_ingest_paperless_only_runs_leg_and_reports_duplicate(monkeypatch):
    existing = MagicMock(id=9)
    _patch_pipeline(monkeypatch, decision=_Decision.PAPERLESS_ONLY, existing=existing)
    leg = AsyncMock(return_value=True)
    out = await ingest_document(
        _PDF, _meta(), db=AsyncMock(), kb_id=None, paperless_leg=leg
    )
    assert out.status is IngestStatus.DUPLICATE
    assert out.document_id == 9
    leg.assert_awaited_once()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_ingest_paperless_only_leg_failure_is_retry(monkeypatch):
    existing = MagicMock(id=10)
    _patch_pipeline(monkeypatch, decision=_Decision.PAPERLESS_ONLY, existing=existing)
    leg = AsyncMock(side_effect=RuntimeError("paperless down"))
    out = await ingest_document(
        _PDF, _meta(), db=AsyncMock(), kb_id=None, paperless_leg=leg
    )
    assert out.status is IngestStatus.RETRY
    assert out.detail == "paperless_retry"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_ingest_paperless_only_unsettled_leg_is_retry(monkeypatch):
    # The leg ran without raising but couldn't settle (upload error / pending) →
    # RETRY so the MCP re-pushes and the leg is attempted again.
    existing = MagicMock(id=11)
    _patch_pipeline(monkeypatch, decision=_Decision.PAPERLESS_ONLY, existing=existing)
    leg = AsyncMock(return_value=False)
    out = await ingest_document(
        _PDF, _meta(), db=AsyncMock(), kb_id=None, paperless_leg=leg
    )
    assert out.status is IngestStatus.RETRY
    assert out.detail == "paperless_pending"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_ingest_create_decision_enqueues_and_is_ingested(monkeypatch):
    created = MagicMock(id=11)
    enqueue = _patch_pipeline(
        monkeypatch, decision=_Decision.CREATE, created_doc=created
    )
    leg = AsyncMock(return_value=True)
    out = await ingest_document(
        _PDF, _meta(), db=AsyncMock(), kb_id=None, owner_user_id=3, paperless_leg=leg
    )
    assert out.status is IngestStatus.INGESTED
    assert out.document_id == 11
    enqueue.assert_awaited_once()
    # owner rides the enqueue payload as user_id (worker scoping).
    assert enqueue.await_args.args[0]["user_id"] == 3
    leg.assert_awaited_once()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_ingest_create_paperless_leg_failure_still_ingested(monkeypatch):
    # The Paperless leg is best-effort: a failure must NOT fail the KB ingest
    # (the row is already enqueued).
    created = MagicMock(id=12)
    enqueue = _patch_pipeline(
        monkeypatch, decision=_Decision.CREATE, created_doc=created
    )
    leg = AsyncMock(side_effect=RuntimeError("paperless down"))
    out = await ingest_document(
        _PDF, _meta(), db=AsyncMock(), kb_id=None, paperless_leg=leg
    )
    assert out.status is IngestStatus.INGESTED
    enqueue.assert_awaited_once()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_ingest_reingest_decision_reenqueues_failed_row(monkeypatch):
    existing = MagicMock(id=13, status=DOC_STATUS_FAILED)
    existing.file_path = "/uploads/old_failed.pdf"  # the prior failed copy
    db = AsyncMock()
    enqueue = _patch_pipeline(
        monkeypatch, decision=_Decision.REINGEST, existing=existing
    )
    cleanup = MagicMock()
    monkeypatch.setattr(fi, "_cleanup", cleanup)
    leg = AsyncMock(return_value=True)
    out = await ingest_document(_PDF, _meta(), db=db, kb_id=None, paperless_leg=leg)
    assert out.status is IngestStatus.INGESTED
    assert out.document_id == 13
    # the failed row is reset to pending + re-pointed at the fresh recovery copy,
    # then re-enqueued and the (idempotent) Paperless leg re-runs.
    assert existing.status == DOC_STATUS_PENDING
    assert existing.file_path == "/uploads/x.pdf"
    cleanup.assert_called_once_with("/uploads/old_failed.pdf")  # stale copy removed
    enqueue.assert_awaited_once()
    leg.assert_awaited_once()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_ingest_concurrent_winner_completed_is_duplicate(monkeypatch):
    # CREATE path, but create_document_record_safe loses the race and raises
    # DuplicateDocumentError whose winner is already completed AND Paperless-
    # settled → duplicate.
    winner = MagicMock(
        id=14, status=DOC_STATUS_COMPLETED, paperless_state=PAPERLESS_STATE_DONE
    )
    _patch_pipeline(monkeypatch, decision=_Decision.CREATE)
    rag = MagicMock()
    rag.create_document_record_safe = AsyncMock(
        side_effect=DuplicateDocumentError(winner)
    )
    monkeypatch.setattr(fi, "RAGService", MagicMock(return_value=rag))
    monkeypatch.setattr(fi, "_cleanup", MagicMock())

    out = await ingest_document(_PDF, _meta(), db=AsyncMock(), kb_id=None)
    assert out.status is IngestStatus.DUPLICATE
    assert out.document_id == 14


@pytest.mark.unit
@pytest.mark.asyncio
async def test_ingest_concurrent_winner_in_flight_is_retry(monkeypatch):
    winner = MagicMock(id=15, status=DOC_STATUS_PROCESSING, paperless_state=None)
    _patch_pipeline(monkeypatch, decision=_Decision.CREATE)
    rag = MagicMock()
    rag.create_document_record_safe = AsyncMock(
        side_effect=DuplicateDocumentError(winner)
    )
    monkeypatch.setattr(fi, "RAGService", MagicMock(return_value=rag))
    monkeypatch.setattr(fi, "_cleanup", MagicMock())

    out = await ingest_document(_PDF, _meta(), db=AsyncMock(), kb_id=None)
    assert out.status is IngestStatus.RETRY
    assert out.document_id == 15


@pytest.mark.unit
@pytest.mark.asyncio
async def test_ingest_concurrent_winner_completed_paperless_missing_is_retry(monkeypatch):
    # Completed winner but Paperless never settled → RETRY (not a terminal
    # move-to-processed), so the re-push routes through PAPERLESS_ONLY.
    winner = MagicMock(id=16, status=DOC_STATUS_COMPLETED, paperless_state=None)
    _patch_pipeline(monkeypatch, decision=_Decision.CREATE)
    rag = MagicMock()
    rag.create_document_record_safe = AsyncMock(
        side_effect=DuplicateDocumentError(winner)
    )
    monkeypatch.setattr(fi, "RAGService", MagicMock(return_value=rag))
    monkeypatch.setattr(fi, "_cleanup", MagicMock())

    out = await ingest_document(_PDF, _meta(), db=AsyncMock(), kb_id=None)
    assert out.status is IngestStatus.RETRY
    assert out.document_id == 16


@pytest.mark.unit
@pytest.mark.asyncio
async def test_ingest_empty_file_is_failed():
    out = await ingest_document(b"", _meta("scan.pdf"), db=AsyncMock(), kb_id=None)
    assert out.status is IngestStatus.FAILED
    assert out.detail == "empty_file"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_ingest_persist_failure_is_retry(monkeypatch):
    # Disk-full while writing the recovery copy → transient RETRY, not FAILED.
    _patch_pipeline(monkeypatch, decision=_Decision.CREATE, created_doc=MagicMock(id=17))
    monkeypatch.setattr(
        fi, "_persist_bytes", AsyncMock(side_effect=OSError("No space left"))
    )
    out = await ingest_document(_PDF, _meta(), db=AsyncMock(), kb_id=None)
    assert out.status is IngestStatus.RETRY
    assert out.detail == "persist_error"
