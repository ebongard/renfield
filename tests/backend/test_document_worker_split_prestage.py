"""Unit tests for the document-worker PDF-split pre-stage wiring.

Asserts the seam behavior in ``_process_entry``: the split-lifecycle status
guard runs REGARDLESS of the feature flag (a flag-off rollback must not
resurrect an archived parent — including via user_reindex); flag off → the
split module is never touched; split-owned docs → ack without Docling;
split-declined docs → the normal pipeline runs unchanged; skip_split rides the
task params through; a SplitExecutionError is terminal while a
SplitTransientError stays in the PEL. Collaborators are mocked (no DB / Redis
/ pdfium).
"""
from __future__ import annotations

import types
from unittest.mock import AsyncMock, MagicMock

import pytest
import workers.document_processor_worker as worker

import services.pdf_splitter as pdf_splitter
from models.database import DOC_SPLIT_OWNED_STATUSES

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


class _FakeSessionCM:
    def __init__(self, db):
        self._db = db

    async def __aenter__(self):
        return self._db

    async def __aexit__(self, *_a):
        return False


def _wire(monkeypatch, *, split_enabled: bool, split_result=False, doc_status="pending"):
    db = MagicMock()
    status_result = MagicMock()
    status_result.scalar_one_or_none.return_value = doc_status
    db.execute = AsyncMock(return_value=status_result)
    db.commit = AsyncMock()

    history = MagicMock()
    history.initial_ingest_status = AsyncMock(return_value=None)

    rag = MagicMock()
    rag.process_existing_document = AsyncMock()
    rag.reindex_document = AsyncMock()

    progress = MagicMock()
    progress.clear = AsyncMock()

    monkeypatch.setattr(worker, "AsyncSessionLocal", lambda: _FakeSessionCM(db))
    monkeypatch.setattr(worker, "RAGService", MagicMock(return_value=rag))
    monkeypatch.setattr(
        worker, "DocumentProcessingHistoryService", MagicMock(return_value=history)
    )
    monkeypatch.setattr(worker, "DocumentProgress", MagicMock(return_value=progress))
    monkeypatch.setattr(worker.settings, "pdf_split_enabled", split_enabled)

    maybe_split = AsyncMock(return_value=split_result)
    monkeypatch.setattr(pdf_splitter, "maybe_split_at_ingest", maybe_split)

    queue = MagicMock()
    queue.ack = AsyncMock()
    redis = MagicMock()
    redis.delete = AsyncMock()
    return rag, queue, redis, maybe_split


def _entry(doc_id: int = 5, trigger: str | None = None, **extra):
    params = {"document_id": doc_id, **extra}
    if trigger is not None:
        params["trigger"] = trigger
    return types.SimpleNamespace(entry_id="1-0", params=params)


async def test_flag_off_never_touches_split_module(monkeypatch):
    rag, queue, redis, maybe_split = _wire(monkeypatch, split_enabled=False)

    await worker._process_entry(redis, queue, _entry())

    maybe_split.assert_not_called()
    rag.process_existing_document.assert_awaited_once()
    queue.ack.assert_awaited_once_with("1-0")


@pytest.mark.parametrize("status", list(DOC_SPLIT_OWNED_STATUSES))
@pytest.mark.parametrize("flag", [True, False])
async def test_split_owned_status_acked_regardless_of_flag(monkeypatch, status, flag):
    """THE rollback-safety property: a doc owned by the split lifecycle is
    acked without processing even with PDF_SPLIT_ENABLED=false — otherwise a
    flag-off incident rollback would re-ingest the archived combined PDF on a
    redelivered entry."""
    rag, queue, redis, maybe_split = _wire(
        monkeypatch, split_enabled=flag, doc_status=status
    )

    await worker._process_entry(redis, queue, _entry())

    rag.process_existing_document.assert_not_called()
    queue.ack.assert_awaited_once_with("1-0")


async def test_user_reindex_refuses_split_owned_doc(monkeypatch):
    """A user_reindex on an archived parent must NOT rebuild chunks for the
    combined original (it would resurrect it in retrieval next to its
    children)."""
    rag, queue, redis, _ = _wire(
        monkeypatch, split_enabled=False, doc_status="split_archived"
    )

    await worker._process_entry(redis, queue, _entry(trigger="user_reindex"))

    rag.reindex_document.assert_not_called()
    rag.process_existing_document.assert_not_called()
    queue.ack.assert_awaited_once_with("1-0")


async def test_split_owned_doc_is_acked_without_processing(monkeypatch):
    rag, queue, redis, maybe_split = _wire(
        monkeypatch, split_enabled=True, split_result=True
    )

    await worker._process_entry(redis, queue, _entry())

    maybe_split.assert_awaited_once()
    rag.process_existing_document.assert_not_called()
    queue.ack.assert_awaited_once_with("1-0")


async def test_split_declined_runs_normal_pipeline(monkeypatch):
    rag, queue, redis, maybe_split = _wire(
        monkeypatch, split_enabled=True, split_result=False
    )

    await worker._process_entry(redis, queue, _entry())

    maybe_split.assert_awaited_once()
    rag.process_existing_document.assert_awaited_once()
    queue.ack.assert_awaited_once_with("1-0")


async def test_skip_split_param_reaches_prestage(monkeypatch):
    _, queue, redis, maybe_split = _wire(
        monkeypatch, split_enabled=True, split_result=False
    )

    await worker._process_entry(redis, queue, _entry(skip_split=True))

    assert maybe_split.await_args.kwargs["skip_split"] is True


async def test_split_execution_error_is_terminal_not_swallowed(monkeypatch):
    """A SplitExecutionError from the pre-stage must flow into the worker's
    terminal handling (mark failed + ack) — NOT fall through to normal ingest
    of the combined parent."""
    rag, queue, redis, maybe_split = _wire(monkeypatch, split_enabled=True)
    maybe_split.side_effect = pdf_splitter.SplitExecutionError("part failed")
    marked = AsyncMock(return_value=True)
    monkeypatch.setattr(worker, "_mark_document_failed", marked)

    await worker._process_entry(redis, queue, _entry())

    rag.process_existing_document.assert_not_called()
    marked.assert_awaited_once()
    queue.ack.assert_awaited_once_with("1-0")


async def test_split_transient_error_stays_in_pel(monkeypatch):
    """A SplitTransientError (LLM host down mid-detection, disk-full child
    ingest) is RETRYABLE: no failed-mark, no ack — reclaim redelivers and the
    idempotent resume continues."""
    rag, queue, redis, maybe_split = _wire(monkeypatch, split_enabled=True)
    maybe_split.side_effect = pdf_splitter.SplitTransientError("ollama down")
    marked = AsyncMock(return_value=True)
    monkeypatch.setattr(worker, "_mark_document_failed", marked)
    # transient path touches redis.incr/expire (best-effort)
    redis.incr = AsyncMock()
    redis.expire = AsyncMock()

    await worker._process_entry(redis, queue, _entry())

    rag.process_existing_document.assert_not_called()
    marked.assert_not_called()
    queue.ack.assert_not_called()  # stays in PEL for reclaim
