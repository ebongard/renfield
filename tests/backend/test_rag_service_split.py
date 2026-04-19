"""Tests for the RAGService ingest split (#388, PR B).

Covers:

- create_document_record: persists a Document row with status=pending
- process_existing_document: happy path + handled Docling failure + unexpected exception
- DocumentProgress: stage and page writes refresh TTL; clear() removes both keys

The 409-duplicate behaviour (test #2 in the plan's matrix) lives in the
upload route, not here; that's PR C1.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from models.database import (
    DOC_STATUS_COMPLETED,
    DOC_STATUS_FAILED,
    DOC_STATUS_PENDING,
    Document,
    KnowledgeBase,
)
from services.progress import (
    DEFAULT_TTL_S,
    DocumentProgress,
    _progress_key,
    _stage_key,
)
from services.rag_service import RAGService


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def knowledge_base(db_session):
    """A KnowledgeBase row for Document.knowledge_base_id to point at."""
    kb = KnowledgeBase(name="test-kb", description="fixture")
    db_session.add(kb)
    await db_session.commit()
    await db_session.refresh(kb)
    return kb


def _fake_docling_result(
    num_chunks: int = 2,
    title: str = "Testdokument",
    page_count: int = 3,
) -> dict:
    """Build a Docling-shaped result dict, deterministic for tests."""
    return {
        "status": "ok",
        "metadata": {
            "title": title,
            "author": "fixture",
            "file_type": "txt",
            "file_size": 100,
            "page_count": page_count,
        },
        "chunks": [
            {"text": f"Testabschnitt {i}", "chunk_index": i}
            for i in range(num_chunks)
        ],
    }


# ---------------------------------------------------------------------------
# create_document_record
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.database
async def test_create_document_record_persists_pending_row(db_session, knowledge_base):
    rag = RAGService(db_session)
    doc = await rag.create_document_record(
        file_path="/tmp/foo.pdf",
        knowledge_base_id=knowledge_base.id,
        filename="foo.pdf",
        file_hash="sha256:deadbeef",
    )

    assert doc.id is not None
    assert doc.status == DOC_STATUS_PENDING
    assert doc.filename == "foo.pdf"
    assert doc.file_hash == "sha256:deadbeef"
    assert doc.knowledge_base_id == knowledge_base.id
    # No processing has happened yet.
    assert doc.chunk_count is None or doc.chunk_count == 0
    assert doc.processed_at is None


@pytest.mark.unit
@pytest.mark.database
async def test_create_document_record_defaults_filename_from_path(db_session):
    """When ``filename`` is omitted, it is derived from ``file_path``."""
    rag = RAGService(db_session)
    doc = await rag.create_document_record(file_path="/uploads/abc/report.txt")
    assert doc.filename == "report.txt"
    assert doc.status == DOC_STATUS_PENDING


# ---------------------------------------------------------------------------
# process_existing_document
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.database
async def test_process_existing_document_happy_path(db_session, knowledge_base):
    """Docling + embedding mocked; row transitions pending → completed."""
    rag = RAGService(db_session)
    doc = await rag.create_document_record(
        file_path="/tmp/happy.txt",
        knowledge_base_id=knowledge_base.id,
        filename="happy.txt",
    )

    with patch.object(
        rag.processor,
        "process_document",
        new=AsyncMock(return_value=_fake_docling_result(num_chunks=2)),
    ), patch.object(
        rag,
        "_contextualize_chunks",
        new=AsyncMock(side_effect=lambda chunks, summary: chunks),
    ), patch.object(
        rag,
        "_ingest_flat",
        new=AsyncMock(return_value=[]),
    ), patch.object(
        rag,
        "_ingest_parent_child",
        new=AsyncMock(return_value=[]),
    ):
        await rag.process_existing_document(document_id=doc.id)

    await db_session.refresh(doc)
    assert doc.status == DOC_STATUS_COMPLETED
    assert doc.error_message is None
    assert doc.title == "Testdokument"
    assert doc.processed_at is not None


@pytest.mark.unit
@pytest.mark.database
async def test_process_existing_document_handled_docling_failure(
    db_session, knowledge_base
):
    """Docling returning status=failed must mark the row failed without raising."""
    rag = RAGService(db_session)
    doc = await rag.create_document_record(
        file_path="/tmp/corrupt.pdf",
        knowledge_base_id=knowledge_base.id,
        filename="corrupt.pdf",
    )

    with patch.object(
        rag.processor,
        "process_document",
        new=AsyncMock(return_value={"status": "failed", "error": "scan illegible"}),
    ):
        # Should NOT raise — handled Docling failures update the row and return.
        await rag.process_existing_document(document_id=doc.id)

    await db_session.refresh(doc)
    assert doc.status == DOC_STATUS_FAILED
    assert doc.error_message == "scan illegible"
    assert doc.processed_at is None


@pytest.mark.unit
@pytest.mark.database
async def test_process_existing_document_embedder_exception_rolls_status(
    db_session, knowledge_base
):
    """An unexpected exception inside the pipeline must mark the row failed
    AND re-raise so the task-queue caller leaves the PEL entry un-ACKed for
    reclaim."""
    rag = RAGService(db_session)
    doc = await rag.create_document_record(
        file_path="/tmp/crash.pdf",
        knowledge_base_id=knowledge_base.id,
        filename="crash.pdf",
    )

    boom = RuntimeError("embedder went boom")

    # Mock both ingestion strategies so the test doesn't depend on the
    # current default of `rag_parent_child_enabled` (which selects one).
    with patch.object(
        rag.processor,
        "process_document",
        new=AsyncMock(return_value=_fake_docling_result(num_chunks=1)),
    ), patch.object(
        rag,
        "_contextualize_chunks",
        new=AsyncMock(side_effect=lambda chunks, summary: chunks),
    ), patch.object(
        rag, "_ingest_flat", new=AsyncMock(side_effect=boom)
    ), patch.object(
        rag, "_ingest_parent_child", new=AsyncMock(side_effect=boom)
    ):
        with pytest.raises(RuntimeError, match="embedder went boom"):
            await rag.process_existing_document(document_id=doc.id)

    await db_session.refresh(doc)
    assert doc.status == DOC_STATUS_FAILED
    assert doc.error_message == "embedder went boom"


@pytest.mark.unit
@pytest.mark.database
async def test_process_existing_document_missing_id_raises(db_session):
    """Asking the worker to process a document that doesn't exist in the DB
    must raise loudly — otherwise a corrupted payload silently acks and the
    user never learns their upload vanished."""
    rag = RAGService(db_session)
    with pytest.raises(ValueError, match="not found"):
        await rag.process_existing_document(document_id=999999)


# ---------------------------------------------------------------------------
# ingest_document back-compat wrapper
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.database
async def test_ingest_document_wrapper_creates_and_processes(
    db_session, knowledge_base
):
    """The legacy wrapper must still create + process inline and return the
    refreshed Document so existing callers (chat_upload, reindex_document)
    keep working unchanged."""
    rag = RAGService(db_session)

    with patch.object(
        rag.processor,
        "process_document",
        new=AsyncMock(return_value=_fake_docling_result(num_chunks=1)),
    ), patch.object(
        rag,
        "_contextualize_chunks",
        new=AsyncMock(side_effect=lambda chunks, summary: chunks),
    ), patch.object(
        rag,
        "_ingest_flat",
        new=AsyncMock(return_value=[]),
    ), patch.object(
        rag,
        "_ingest_parent_child",
        new=AsyncMock(return_value=[]),
    ):
        doc = await rag.ingest_document(
            file_path="/tmp/legacy.txt",
            knowledge_base_id=knowledge_base.id,
            filename="legacy.txt",
        )

    assert doc.status == DOC_STATUS_COMPLETED


# ---------------------------------------------------------------------------
# DocumentProgress
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_document_progress_stage_refreshes_ttl():
    """Every set_stage must call SET with ex=TTL; no silent ttl-less writes."""
    redis = AsyncMock()
    redis.set = AsyncMock()
    progress = DocumentProgress(redis, doc_id=42)

    await progress.set_stage("ocr")

    redis.set.assert_awaited_once_with(_stage_key(42), "ocr", ex=DEFAULT_TTL_S)


@pytest.mark.unit
async def test_document_progress_pages_clamps_and_refreshes_ttl():
    """set_pages must clamp `current` into [0, total] and refresh TTL.

    Each sub-case uses ``reset_mock()`` so a silent-skip regression in
    one clamp direction cannot be masked by the next correct call's
    ``assert_awaited_with`` (which only sees the latest call).
    """
    redis = AsyncMock()
    progress = DocumentProgress(redis, doc_id=7)

    await progress.set_pages(current=5, total=10)
    redis.set.assert_awaited_once_with(_progress_key(7), "5/10", ex=DEFAULT_TTL_S)
    redis.set.reset_mock()

    # Clamp negative current to 0
    await progress.set_pages(current=-3, total=10)
    redis.set.assert_awaited_once_with(_progress_key(7), "0/10", ex=DEFAULT_TTL_S)
    redis.set.reset_mock()

    # Clamp overshoot current to total
    await progress.set_pages(current=15, total=10)
    redis.set.assert_awaited_once_with(_progress_key(7), "10/10", ex=DEFAULT_TTL_S)


@pytest.mark.unit
async def test_document_progress_pages_ignores_zero_total():
    """Non-paginated inputs (TXT, images) call set_pages(0, 0); nothing
    should be written because "0/0" is meaningless in the UI."""
    redis = AsyncMock()
    progress = DocumentProgress(redis, doc_id=9)

    await progress.set_pages(current=0, total=0)
    redis.set.assert_not_called()


@pytest.mark.unit
async def test_document_progress_clear_removes_both_keys():
    redis = AsyncMock()
    progress = DocumentProgress(redis, doc_id=11)

    await progress.clear()

    redis.delete.assert_awaited_once_with(_stage_key(11), _progress_key(11))


@pytest.mark.unit
async def test_document_progress_read_returns_structured_view():
    redis = AsyncMock()
    redis.mget = AsyncMock(return_value=["ocr", "47/120"])
    progress = DocumentProgress(redis, doc_id=3)

    result = await progress.read()

    assert result == {"stage": "ocr", "pages": {"current": 47, "total": 120}}


@pytest.mark.unit
async def test_document_progress_read_handles_missing_keys():
    redis = AsyncMock()
    redis.mget = AsyncMock(return_value=[None, None])
    progress = DocumentProgress(redis, doc_id=3)

    result = await progress.read()

    assert result == {"stage": None, "pages": None}


@pytest.mark.unit
async def test_document_progress_read_tolerates_malformed_pages():
    """A malformed progress string (log-noise, not a crash) maps to None."""
    redis = AsyncMock()
    redis.mget = AsyncMock(return_value=["chunking", "garbage"])
    progress = DocumentProgress(redis, doc_id=3)

    result = await progress.read()

    assert result == {"stage": "chunking", "pages": None}
