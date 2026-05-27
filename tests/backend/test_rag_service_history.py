"""Tests for ``RAGService`` ↔ ``DocumentProcessingHistoryService`` integration.

Verifies that every ingestion path writes exactly one history row with
the correct ``trigger`` + ``force_ocr`` values, and that metrics
(chunks_produced, chunks_dropped, ocr_engine) flow from
``DocumentProcessor.process_document``'s return into the history row
via the ``track()`` context manager.

The DocumentProcessor itself is mocked so these tests don't depend on
Docling, OCR, or filesystem state. The integration boundary under test
is ``process_existing_document`` ↔ ``history.track()`` ↔ DB row.
"""
from __future__ import annotations

import os
from typing import AsyncGenerator
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from models.database import Document, DocumentProcessingHistory
from services.document_processing_history import (
    ProcessingStatus,
    ProcessingTrigger,
)
from services.rag_service import RAGService


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
    maker = async_sessionmaker(pg_async_engine, class_=AsyncSession, expire_on_commit=False)
    async with maker() as session:
        yield session
    async with pg_async_engine.begin() as conn:
        await conn.execute(text(
            "TRUNCATE document_processing_history, document_chunks, documents "
            "RESTART IDENTITY CASCADE"
        ))


async def _make_doc(session: AsyncSession) -> Document:
    doc = Document(filename="t.pdf", file_path="/tmp/t.pdf", status="pending")
    session.add(doc)
    await session.commit()
    await session.refresh(doc)
    return doc


def _success_result(*, chunks_dropped: int = 0, ocr_engine: str = "docling") -> dict:
    return {
        "status": "completed",
        "metadata": {
            "title": "t",
            "author": None,
            "file_type": "pdf",
            "file_size": 100,
            "page_count": 1,
        },
        "chunks": [
            {"text": "hello world", "chunk_type": "text", "page_number": 1, "section_title": None},
        ],
        "ocr_engine": ocr_engine,
        "chunks_dropped_low_quality": chunks_dropped,
    }


def _failure_result(error: str = "docling crashed") -> dict:
    return {"status": "failed", "error": error}


async def _fetch_history(session: AsyncSession, doc_id: int) -> list[DocumentProcessingHistory]:
    from sqlalchemy import select
    rows = await session.execute(
        select(DocumentProcessingHistory)
        .where(DocumentProcessingHistory.document_id == doc_id)
        .order_by(DocumentProcessingHistory.id.asc())
    )
    return list(rows.scalars().all())


# ============================================================================
# Happy path: process_existing_document writes a completed history row
# ============================================================================


async def test_process_writes_history_with_metrics(committing_session):
    doc = await _make_doc(committing_session)
    rag = RAGService(committing_session)

    # Stub out the actual processor + chunk-insertion plumbing.
    with patch.object(rag.processor, "process_document", AsyncMock(
        return_value=_success_result(chunks_dropped=2, ocr_engine="docling_full_page_ocr"),
    )), patch.object(rag, "_contextualize_chunks", AsyncMock(side_effect=lambda chunks, _s: chunks)), \
         patch.object(rag, "_ingest_flat", AsyncMock(return_value=[])), \
         patch.object(rag, "_ingest_parent_child", AsyncMock(return_value=[])):
        await rag.process_existing_document(
            doc.id, force_ocr=True, trigger=ProcessingTrigger.SCRIPT_PURGE,
        )

    rows = await _fetch_history(committing_session, doc.id)
    assert len(rows) == 1
    h = rows[0]
    assert h.status == ProcessingStatus.COMPLETED.value
    assert h.force_ocr is True
    assert h.trigger == ProcessingTrigger.SCRIPT_PURGE.value
    assert h.chunks_dropped_low_quality == 2
    assert h.ocr_engine == "docling_full_page_ocr"


async def test_default_trigger_is_initial_ingest(committing_session):
    doc = await _make_doc(committing_session)
    rag = RAGService(committing_session)
    with patch.object(rag.processor, "process_document", AsyncMock(
        return_value=_success_result(),
    )), patch.object(rag, "_contextualize_chunks", AsyncMock(side_effect=lambda chunks, _s: chunks)), \
         patch.object(rag, "_ingest_flat", AsyncMock(return_value=[])), \
         patch.object(rag, "_ingest_parent_child", AsyncMock(return_value=[])):
        await rag.process_existing_document(doc.id)

    rows = await _fetch_history(committing_session, doc.id)
    assert len(rows) == 1
    assert rows[0].trigger == ProcessingTrigger.INITIAL_INGEST.value
    assert rows[0].force_ocr is False


# ============================================================================
# Soft-fail path: Docling returned status=failed → history row is failed,
# function returns None (legacy contract)
# ============================================================================


async def test_soft_failure_writes_failed_history_and_returns_none(committing_session):
    doc = await _make_doc(committing_session)
    rag = RAGService(committing_session)

    with patch.object(rag.processor, "process_document", AsyncMock(
        return_value=_failure_result("docling segfault"),
    )):
        result = await rag.process_existing_document(doc.id)

    assert result is None
    rows = await _fetch_history(committing_session, doc.id)
    assert len(rows) == 1
    assert rows[0].status == ProcessingStatus.FAILED.value
    assert "docling segfault" in rows[0].error_message


# ============================================================================
# Hard exception path: unexpected Python exception → history row failed,
# exception re-raised
# ============================================================================


async def test_hard_exception_writes_failed_history_and_reraises(committing_session):
    doc = await _make_doc(committing_session)
    rag = RAGService(committing_session)

    with patch.object(rag.processor, "process_document", AsyncMock(
        side_effect=ValueError("boom"),
    )):
        with pytest.raises(ValueError, match="boom"):
            await rag.process_existing_document(doc.id)

    rows = await _fetch_history(committing_session, doc.id)
    assert len(rows) == 1
    assert rows[0].status == ProcessingStatus.FAILED.value
    assert "boom" in rows[0].error_message


# ============================================================================
# reindex_document path: writes USER_REINDEX history (or whatever trigger
# the caller passes), does NOT create a new Document row
# ============================================================================


async def test_reindex_default_trigger_is_user_reindex(committing_session):
    doc = await _make_doc(committing_session)
    rag = RAGService(committing_session)
    with patch.object(rag.processor, "process_document", AsyncMock(
        return_value=_success_result(),
    )), patch.object(rag, "_contextualize_chunks", AsyncMock(side_effect=lambda chunks, _s: chunks)), \
         patch.object(rag, "_ingest_flat", AsyncMock(return_value=[])), \
         patch.object(rag, "_ingest_parent_child", AsyncMock(return_value=[])):
        await rag.reindex_document(doc.id)

    rows = await _fetch_history(committing_session, doc.id)
    assert len(rows) == 1
    assert rows[0].trigger == ProcessingTrigger.USER_REINDEX.value


async def test_reindex_with_script_trigger(committing_session):
    """The cleanup script calls reindex with trigger=SCRIPT_PURGE."""
    doc = await _make_doc(committing_session)
    rag = RAGService(committing_session)
    with patch.object(rag.processor, "process_document", AsyncMock(
        return_value=_success_result(chunks_dropped=5, ocr_engine="docling_full_page_ocr"),
    )), patch.object(rag, "_contextualize_chunks", AsyncMock(side_effect=lambda chunks, _s: chunks)), \
         patch.object(rag, "_ingest_flat", AsyncMock(return_value=[])), \
         patch.object(rag, "_ingest_parent_child", AsyncMock(return_value=[])):
        await rag.reindex_document(
            doc.id,
            force_ocr=True,
            trigger=ProcessingTrigger.SCRIPT_PURGE,
        )

    rows = await _fetch_history(committing_session, doc.id)
    assert len(rows) == 1
    assert rows[0].force_ocr is True
    assert rows[0].trigger == ProcessingTrigger.SCRIPT_PURGE.value
    assert rows[0].chunks_dropped_low_quality == 5
    assert rows[0].ocr_engine == "docling_full_page_ocr"


async def test_reindex_does_not_double_track(committing_session):
    """reindex_document must NOT chain through ingest_document — that would
    write two history rows for one user-visible reindex action."""
    doc = await _make_doc(committing_session)
    rag = RAGService(committing_session)
    with patch.object(rag.processor, "process_document", AsyncMock(
        return_value=_success_result(),
    )), patch.object(rag, "_contextualize_chunks", AsyncMock(side_effect=lambda chunks, _s: chunks)), \
         patch.object(rag, "_ingest_flat", AsyncMock(return_value=[])), \
         patch.object(rag, "_ingest_parent_child", AsyncMock(return_value=[])):
        await rag.reindex_document(doc.id)

    rows = await _fetch_history(committing_session, doc.id)
    assert len(rows) == 1
