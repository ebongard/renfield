"""Unit tests for the document-worker's idempotent-consumer guard.

The Redis stream is at-least-once: reclaim_stale re-delivers stale PEL entries
on restart. `_process_entry` must branch on the doc's initial_ingest state so a
re-delivery never double-ingests (process_existing_document appends chunks +
re-fires the KG/Schicht-A hooks; it does not purge first).

Pure unit test — collaborators (session, RAGService, history service, progress,
queue) are mocked; no DB / Redis.
"""
from __future__ import annotations

import types
from unittest.mock import AsyncMock, MagicMock

import pytest

import workers.document_processor_worker as worker
from services.document_processing_history import ProcessingStatus


pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


class _FakeSessionCM:
    def __init__(self, db):
        self._db = db

    async def __aenter__(self):
        return self._db

    async def __aexit__(self, *_a):
        return False


def _wire(monkeypatch, *, ingest_status: str | None):
    """Patch the worker's collaborators; return (db, rag, queue)."""
    db = MagicMock()
    db.execute = AsyncMock()
    db.commit = AsyncMock()

    history = MagicMock()
    history.initial_ingest_status = AsyncMock(return_value=ingest_status)

    rag = MagicMock()
    rag.process_existing_document = AsyncMock()
    rag.reindex_document = AsyncMock()

    progress = MagicMock()
    progress.set_stage = AsyncMock()
    progress.clear = AsyncMock()

    monkeypatch.setattr(worker, "AsyncSessionLocal", lambda: _FakeSessionCM(db))
    monkeypatch.setattr(worker, "RAGService", MagicMock(return_value=rag))
    monkeypatch.setattr(worker, "DocumentProcessingHistoryService", MagicMock(return_value=history))
    monkeypatch.setattr(worker, "DocumentProgress", MagicMock(return_value=progress))

    queue = MagicMock()
    queue.ack = AsyncMock()
    return db, rag, queue


def _entry(doc_id: int = 5, trigger: str | None = None):
    params = {"document_id": doc_id}
    if trigger is not None:
        params["trigger"] = trigger
    return types.SimpleNamespace(entry_id="1-0", params=params)


async def test_completed_initial_ingest_is_skipped_and_acked(monkeypatch):
    """Duplicate delivery of an already-completed doc → ack, self-heal status,
    NO reprocess (so no duplicate chunks / KG entities)."""
    db, rag, queue = _wire(monkeypatch, ingest_status=ProcessingStatus.COMPLETED.value)

    await worker._process_entry(MagicMock(), queue, _entry(5))

    rag.process_existing_document.assert_not_called()
    queue.ack.assert_awaited_once_with("1-0")
    # Split-lifecycle status probe + the self-heal UPDATE (status reset for a
    # doc stuck in 'processing').
    assert db.execute.await_count == 2


async def test_incomplete_ingest_purges_then_reprocesses(monkeypatch):
    """A processing/failed (incomplete) first ingest → purge partial chunks,
    then reprocess (idempotent rebuild)."""
    db, rag, queue = _wire(monkeypatch, ingest_status=ProcessingStatus.PROCESSING.value)

    await worker._process_entry(MagicMock(), queue, _entry(5))

    assert db.execute.await_count == 2  # status probe + the partial-chunk DELETE
    rag.process_existing_document.assert_awaited_once()
    queue.ack.assert_awaited_once_with("1-0")


async def test_first_ingest_processes_without_purge(monkeypatch):
    """No prior initial_ingest row → genuine first ingest: no purge, process."""
    db, rag, queue = _wire(monkeypatch, ingest_status=None)

    await worker._process_entry(MagicMock(), queue, _entry(5))

    assert db.execute.await_count == 1  # only the split-lifecycle status probe
    rag.process_existing_document.assert_awaited_once()
    queue.ack.assert_awaited_once_with("1-0")


async def test_user_reindex_always_reprocesses(monkeypatch):
    """trigger=user_reindex must ALWAYS reprocess via reindex_document — even
    when initial_ingest is completed (otherwise the idempotent-consumer guard
    would wrongly skip every reindex). reindex_document purges+rebuilds; the
    initial-ingest skip path must NOT run."""
    db, rag, queue = _wire(monkeypatch, ingest_status=ProcessingStatus.COMPLETED.value)

    await worker._process_entry(MagicMock(), queue, _entry(5, trigger="user_reindex"))

    rag.reindex_document.assert_awaited_once()
    rag.process_existing_document.assert_not_called()  # not the initial-ingest path
    queue.ack.assert_awaited_once_with("1-0")
