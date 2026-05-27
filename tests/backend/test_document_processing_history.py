"""Tests for ``DocumentProcessingHistoryService`` + the pc20260530 migration.

Two test layers:
  - Service unit tests (require Postgres + real commits because the
    service uses short-lived self-committing transactions; the standard
    ``pg_db_session`` outer-txn rollback fixture would conflict with
    those commits).
  - Migration / schema tests (require Postgres; verify CHECK constraints,
    partial unique index, CASCADE behavior).
"""
from __future__ import annotations

import os
from typing import AsyncGenerator

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from models.database import Document, DocumentProcessingHistory
from services.document_processing_history import (
    DocumentProcessingHistoryService,
    HistoryRow,
    ProcessingStatus,
    ProcessingTrigger,
)


pytestmark = [
    pytest.mark.database,
    pytest.mark.postgres,
    pytest.mark.skipif(
        not os.environ.get("RENFIELD_TEST_PG_URL"),
        reason="RENFIELD_TEST_PG_URL not set — Postgres tests disabled",
    ),
]


@pytest.fixture
async def committing_session(pg_async_engine) -> AsyncGenerator[AsyncSession, None]:
    """A session that allows real commits. Tears down by truncating the
    tables this test file writes to (history + documents) instead of
    relying on outer-txn rollback. ``pg_async_engine`` itself is per-test
    and tears down via ``Base.metadata.drop_all``, so truncation is belt-
    and-suspenders only — it keeps a single test's writes from leaking
    into a parallel test within the same engine if that ever changes."""
    maker = async_sessionmaker(pg_async_engine, class_=AsyncSession, expire_on_commit=False)
    async with maker() as session:
        yield session
    # Defensive cleanup; pg_async_engine's drop_all on teardown is the real GC.
    async with pg_async_engine.begin() as conn:
        await conn.execute(text("TRUNCATE document_processing_history, documents RESTART IDENTITY CASCADE"))


async def _make_doc(session: AsyncSession, *, filename: str = "x.pdf") -> Document:
    doc = Document(filename=filename, file_path=f"/tmp/{filename}", status="pending")
    session.add(doc)
    await session.commit()
    await session.refresh(doc)
    return doc


# ============================================================================
# Service: open()
# ============================================================================


async def test_open_creates_processing_row(committing_session):
    doc = await _make_doc(committing_session)
    svc = DocumentProcessingHistoryService(committing_session)

    hid = await svc.open(doc.id, force_ocr=False, trigger=ProcessingTrigger.INITIAL_INGEST)

    assert hid > 0
    row = await committing_session.get(DocumentProcessingHistory, hid)
    assert row.status == ProcessingStatus.PROCESSING.value
    assert row.force_ocr is False
    assert row.trigger == ProcessingTrigger.INITIAL_INGEST.value
    assert row.started_at is not None
    assert row.finished_at is None


async def test_open_with_force_ocr_true(committing_session):
    doc = await _make_doc(committing_session)
    svc = DocumentProcessingHistoryService(committing_session)
    hid = await svc.open(doc.id, force_ocr=True, trigger=ProcessingTrigger.SCRIPT_PURGE)
    row = await committing_session.get(DocumentProcessingHistory, hid)
    assert row.force_ocr is True
    assert row.trigger == ProcessingTrigger.SCRIPT_PURGE.value


# ============================================================================
# Service: close_success()
# ============================================================================


async def test_close_success_updates_metrics_and_status(committing_session):
    doc = await _make_doc(committing_session)
    svc = DocumentProcessingHistoryService(committing_session)
    hid = await svc.open(doc.id, force_ocr=False, trigger=ProcessingTrigger.INITIAL_INGEST)

    await svc.close_success(hid, chunks_produced=42, chunks_dropped=3, ocr_engine="docling")

    await committing_session.expire_all()
    row = await committing_session.get(DocumentProcessingHistory, hid)
    assert row.status == ProcessingStatus.COMPLETED.value
    assert row.chunks_produced == 42
    assert row.chunks_dropped_low_quality == 3
    assert row.ocr_engine == "docling"
    assert row.finished_at is not None


async def test_close_success_accepts_none_metrics(committing_session):
    """Bulk-import workers can omit metrics they don't track."""
    doc = await _make_doc(committing_session)
    svc = DocumentProcessingHistoryService(committing_session)
    hid = await svc.open(doc.id, force_ocr=False, trigger=ProcessingTrigger.INITIAL_INGEST)
    await svc.close_success(hid, chunks_produced=None, chunks_dropped=None, ocr_engine=None)
    await committing_session.expire_all()
    row = await committing_session.get(DocumentProcessingHistory, hid)
    assert row.status == ProcessingStatus.COMPLETED.value
    assert row.chunks_produced is None


# ============================================================================
# Service: close_failure()
# ============================================================================


async def test_close_failure_sets_failed_status_and_error_message(committing_session):
    doc = await _make_doc(committing_session)
    svc = DocumentProcessingHistoryService(committing_session)
    hid = await svc.open(doc.id, force_ocr=False, trigger=ProcessingTrigger.INITIAL_INGEST)

    await svc.close_failure(hid, "OCR engine crashed: docling segfault")

    await committing_session.expire_all()
    row = await committing_session.get(DocumentProcessingHistory, hid)
    assert row.status == ProcessingStatus.FAILED.value
    assert row.error_message == "OCR engine crashed: docling segfault"
    assert row.finished_at is not None


async def test_close_failure_accepts_long_error_text(committing_session):
    """error_message is Text, not VARCHAR(N) — should accept multi-KB strings."""
    doc = await _make_doc(committing_session)
    svc = DocumentProcessingHistoryService(committing_session)
    hid = await svc.open(doc.id, force_ocr=False, trigger=ProcessingTrigger.INITIAL_INGEST)
    long_err = "Traceback line\n" * 1000
    await svc.close_failure(hid, long_err)
    await committing_session.expire_all()
    row = await committing_session.get(DocumentProcessingHistory, hid)
    assert row.error_message == long_err


# ============================================================================
# Service: has_force_ocr_succeeded()
# ============================================================================


async def test_has_force_ocr_succeeded_false_when_no_history(committing_session):
    doc = await _make_doc(committing_session)
    svc = DocumentProcessingHistoryService(committing_session)
    assert await svc.has_force_ocr_succeeded(doc.id) is False


async def test_has_force_ocr_succeeded_false_when_only_processing(committing_session):
    """A zombie (status=processing) row must NOT count as success."""
    doc = await _make_doc(committing_session)
    svc = DocumentProcessingHistoryService(committing_session)
    await svc.open(doc.id, force_ocr=True, trigger=ProcessingTrigger.SCRIPT_PURGE)
    assert await svc.has_force_ocr_succeeded(doc.id) is False


async def test_has_force_ocr_succeeded_false_when_force_ocr_false(committing_session):
    """A completed initial_ingest row (force_ocr=false) must NOT satisfy the guard."""
    doc = await _make_doc(committing_session)
    svc = DocumentProcessingHistoryService(committing_session)
    hid = await svc.open(doc.id, force_ocr=False, trigger=ProcessingTrigger.INITIAL_INGEST)
    await svc.close_success(hid, 10, 0, "docling")
    assert await svc.has_force_ocr_succeeded(doc.id) is False


async def test_has_force_ocr_succeeded_true_when_completed_force_ocr(committing_session):
    doc = await _make_doc(committing_session)
    svc = DocumentProcessingHistoryService(committing_session)
    hid = await svc.open(doc.id, force_ocr=True, trigger=ProcessingTrigger.SCRIPT_PURGE)
    await svc.close_success(hid, 8, 2, "docling_full_page_ocr")
    assert await svc.has_force_ocr_succeeded(doc.id) is True


async def test_has_force_ocr_succeeded_false_when_force_ocr_failed(committing_session):
    """A failed force_ocr attempt must NOT satisfy the guard — re-run is appropriate."""
    doc = await _make_doc(committing_session)
    svc = DocumentProcessingHistoryService(committing_session)
    hid = await svc.open(doc.id, force_ocr=True, trigger=ProcessingTrigger.SCRIPT_PURGE)
    await svc.close_failure(hid, "engine died")
    assert await svc.has_force_ocr_succeeded(doc.id) is False


# ============================================================================
# Service: latest()
# ============================================================================


async def test_latest_returns_most_recent(committing_session):
    doc = await _make_doc(committing_session)
    svc = DocumentProcessingHistoryService(committing_session)
    hid1 = await svc.open(doc.id, force_ocr=False, trigger=ProcessingTrigger.INITIAL_INGEST)
    await svc.close_success(hid1, 5, 0, "docling")
    hid2 = await svc.open(doc.id, force_ocr=True, trigger=ProcessingTrigger.USER_REINDEX)

    latest = await svc.latest(doc.id)

    assert latest is not None
    assert latest.id == hid2


async def test_latest_returns_none_for_unknown_doc(committing_session):
    svc = DocumentProcessingHistoryService(committing_session)
    assert await svc.latest(999999) is None


# ============================================================================
# Service: track() context manager
# ============================================================================


async def test_track_writes_completed_on_clean_exit(committing_session):
    doc = await _make_doc(committing_session)
    svc = DocumentProcessingHistoryService(committing_session)

    async with svc.track(doc.id, force_ocr=True, trigger=ProcessingTrigger.SCRIPT_PURGE) as hrow:
        assert isinstance(hrow, HistoryRow)
        assert hrow.hid > 0
        hrow.chunks_produced = 11
        hrow.chunks_dropped = 4
        hrow.ocr_engine = "docling_full_page_ocr"

    await committing_session.expire_all()
    row = await committing_session.get(DocumentProcessingHistory, hrow.hid)
    assert row.status == ProcessingStatus.COMPLETED.value
    assert row.chunks_produced == 11
    assert row.chunks_dropped_low_quality == 4
    assert row.ocr_engine == "docling_full_page_ocr"


async def test_track_writes_failed_on_exception_and_reraises(committing_session):
    doc = await _make_doc(committing_session)
    svc = DocumentProcessingHistoryService(committing_session)

    with pytest.raises(RuntimeError, match="boom"):
        async with svc.track(doc.id, force_ocr=False, trigger=ProcessingTrigger.INITIAL_INGEST) as hrow:
            captured_hid = hrow.hid
            raise RuntimeError("boom")

    await committing_session.expire_all()
    row = await committing_session.get(DocumentProcessingHistory, captured_hid)
    assert row.status == ProcessingStatus.FAILED.value
    assert "boom" in row.error_message


async def test_track_unset_metrics_default_to_null(committing_session):
    doc = await _make_doc(committing_session)
    svc = DocumentProcessingHistoryService(committing_session)
    async with svc.track(doc.id, force_ocr=False, trigger=ProcessingTrigger.INITIAL_INGEST) as hrow:
        pass  # caller assigns nothing

    await committing_session.expire_all()
    row = await committing_session.get(DocumentProcessingHistory, hrow.hid)
    assert row.status == ProcessingStatus.COMPLETED.value
    assert row.chunks_produced is None
    assert row.chunks_dropped_low_quality is None
    assert row.ocr_engine is None


# ============================================================================
# Migration / schema constraints
# ============================================================================


async def test_status_check_constraint_rejects_invalid(committing_session):
    doc = await _make_doc(committing_session)
    with pytest.raises(IntegrityError):
        await committing_session.execute(
            text(
                "INSERT INTO document_processing_history "
                "(document_id, status, force_ocr, trigger) "
                "VALUES (:d, 'nonsense', false, 'initial_ingest')"
            ),
            {"d": doc.id},
        )
        await committing_session.commit()


async def test_trigger_check_constraint_rejects_invalid(committing_session):
    doc = await _make_doc(committing_session)
    with pytest.raises(IntegrityError):
        await committing_session.execute(
            text(
                "INSERT INTO document_processing_history "
                "(document_id, status, force_ocr, trigger) "
                "VALUES (:d, 'completed', false, 'made_up_trigger')"
            ),
            {"d": doc.id},
        )
        await committing_session.commit()


async def test_partial_unique_initial_ingest_per_doc(committing_session):
    """The uq_dph_initial_ingest_per_doc partial index forbids two
    initial_ingest rows for the same document — but allows arbitrary
    user_reindex / script_purge rows."""
    doc = await _make_doc(committing_session)
    svc = DocumentProcessingHistoryService(committing_session)

    await svc.open(doc.id, force_ocr=False, trigger=ProcessingTrigger.INITIAL_INGEST)

    # Second initial_ingest must violate the partial unique constraint.
    with pytest.raises(IntegrityError):
        await svc.open(doc.id, force_ocr=False, trigger=ProcessingTrigger.INITIAL_INGEST)


async def test_partial_unique_allows_many_reindexes(committing_session):
    """Multiple user_reindex / script_purge rows per doc must be allowed."""
    doc = await _make_doc(committing_session)
    svc = DocumentProcessingHistoryService(committing_session)
    await svc.open(doc.id, force_ocr=False, trigger=ProcessingTrigger.USER_REINDEX)
    await svc.open(doc.id, force_ocr=True, trigger=ProcessingTrigger.USER_REINDEX)
    await svc.open(doc.id, force_ocr=True, trigger=ProcessingTrigger.SCRIPT_PURGE)
    rows = (await committing_session.execute(
        text("SELECT COUNT(*) FROM document_processing_history WHERE document_id = :d"),
        {"d": doc.id},
    )).scalar()
    assert rows == 3


async def test_document_cascade_deletes_history(committing_session):
    """document_id FK has ON DELETE CASCADE — deleting the parent doc
    must remove all its history rows."""
    doc = await _make_doc(committing_session)
    svc = DocumentProcessingHistoryService(committing_session)
    hid = await svc.open(doc.id, force_ocr=False, trigger=ProcessingTrigger.INITIAL_INGEST)
    await svc.close_success(hid, 1, 0, "docling")

    await committing_session.execute(text("DELETE FROM documents WHERE id = :d"), {"d": doc.id})
    await committing_session.commit()

    remaining = (await committing_session.execute(
        text("SELECT COUNT(*) FROM document_processing_history WHERE document_id = :d"),
        {"d": doc.id},
    )).scalar()
    assert remaining == 0
