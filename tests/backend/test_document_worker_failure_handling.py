"""Unit tests for the document-worker's terminal-vs-transient failure handling (T4).

Before T4 the worker's ``except`` never ACKed, so a poison document
(non-retryable bug) sat un-ACKed and re-failed on every reclaim/restart forever,
and its row never reached ``status=failed`` deterministically. Now:

- TRANSIENT infra blips (LLM/embedding host down, DB/Redis dropped) → left
  un-ACKed for reclaim_stale to retry (the old behaviour, preserved).
- TERMINAL/poison errors → row marked failed + entry ACKed, so it stops
  accumulating in the PEL and the folder-ingest D2 REINGEST branch can re-drive
  it deliberately.

Pure unit tests — collaborators are mocked; no DB / Redis.
"""
from __future__ import annotations

import asyncio
import types
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from redis import exceptions as redis_exceptions
from sqlalchemy.exc import OperationalError

import workers.document_processor_worker as worker
from models.database import DOC_STATUS_FAILED
from services.document_processing_history import ProcessingStatus

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


class _FakeSessionCM:
    def __init__(self, db):
        self._db = db

    async def __aenter__(self):
        return self._db

    async def __aexit__(self, *_a):
        return False


def _wire(monkeypatch, *, ingest_status=None, process_side_effect=None, reindex_side_effect=None):
    """Patch the worker's collaborators; return (rag, queue)."""
    db = MagicMock()
    db.execute = AsyncMock()
    db.commit = AsyncMock()

    history = MagicMock()
    history.initial_ingest_status = AsyncMock(return_value=ingest_status)

    rag = MagicMock()
    rag.process_existing_document = AsyncMock(side_effect=process_side_effect)
    rag.reindex_document = AsyncMock(side_effect=reindex_side_effect)

    progress = MagicMock()
    progress.set_stage = AsyncMock()
    progress.clear = AsyncMock()

    monkeypatch.setattr(worker, "AsyncSessionLocal", lambda: _FakeSessionCM(db))
    monkeypatch.setattr(worker, "RAGService", MagicMock(return_value=rag))
    monkeypatch.setattr(worker, "DocumentProcessingHistoryService", MagicMock(return_value=history))
    monkeypatch.setattr(worker, "DocumentProgress", MagicMock(return_value=progress))

    queue = MagicMock()
    queue.ack = AsyncMock()
    return rag, queue


def _entry(doc_id: int = 5, trigger: str | None = None):
    params = {"document_id": doc_id}
    if trigger is not None:
        params["trigger"] = trigger
    return types.SimpleNamespace(entry_id="1-0", params=params)


# ---------------------------------------------------------------------------
# The classifier
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "exc",
    [
        asyncio.TimeoutError(),
        httpx.ConnectError("conn refused"),
        httpx.ConnectTimeout("slow"),
        httpx.ReadTimeout("slow"),
        httpx.PoolTimeout("pool"),
        httpx.RemoteProtocolError("dropped"),
        redis_exceptions.ConnectionError("redis down"),
        redis_exceptions.TimeoutError("redis slow"),
        OperationalError("stmt", {}, Exception("server closed the connection")),
    ],
)
def test_transient_classifier_true(exc):
    assert worker._is_transient_error(exc) is True


@pytest.mark.parametrize(
    "exc",
    [ValueError("bad data"), KeyError("missing"), RuntimeError("bug"), TypeError("x")],
)
def test_transient_classifier_false_for_terminal(exc):
    assert worker._is_transient_error(exc) is False


def _ollama_err(status_code: int):
    """An ollama.ResponseError instance with a given status_code, built without
    invoking __init__ (constructor signature varies across ollama versions)."""
    if worker._OllamaResponseError is None:
        pytest.skip("ollama not importable in this environment")
    exc = worker._OllamaResponseError.__new__(worker._OllamaResponseError)
    exc.status_code = status_code
    return exc


@pytest.mark.parametrize("status", [500, 502, 503, 504])
def test_ollama_5xx_is_transient(status):
    # A reachable-but-degraded Ollama (model loading / gateway) → retry.
    assert worker._is_transient_error(_ollama_err(status)) is True


@pytest.mark.parametrize("status", [400, 404, 422])
def test_ollama_4xx_is_terminal(status):
    # A 4xx (bad request / model not found) is a terminal config/data error.
    assert worker._is_transient_error(_ollama_err(status)) is False


# ---------------------------------------------------------------------------
# _process_entry ack decision
# ---------------------------------------------------------------------------

async def test_transient_error_left_in_pel_not_acked(monkeypatch):
    rag, queue = _wire(monkeypatch, process_side_effect=asyncio.TimeoutError())
    mark = AsyncMock()
    monkeypatch.setattr(worker, "_mark_document_failed", mark)

    await worker._process_entry(MagicMock(), queue, _entry(5))

    queue.ack.assert_not_called()  # stays in PEL for reclaim
    mark.assert_not_called()  # not a terminal failure


async def test_transient_httpx_connecterror_not_acked(monkeypatch):
    rag, queue = _wire(monkeypatch, process_side_effect=httpx.ConnectError("down"))
    monkeypatch.setattr(worker, "_mark_document_failed", AsyncMock())

    await worker._process_entry(MagicMock(), queue, _entry(5))

    queue.ack.assert_not_called()


async def test_terminal_error_marks_failed_and_acks(monkeypatch):
    rag, queue = _wire(monkeypatch, process_side_effect=ValueError("poison doc"))
    mark = AsyncMock(return_value=True)
    monkeypatch.setattr(worker, "_mark_document_failed", mark)

    await worker._process_entry(MagicMock(), queue, _entry(7))

    mark.assert_awaited_once()
    assert mark.await_args.args[0] == 7  # the doc_id
    queue.ack.assert_awaited_once_with("1-0")  # removed from PEL — no infinite loop


async def test_terminal_error_in_reindex_path_marks_failed_and_acks(monkeypatch):
    # A terminal failure on the user-reindex branch must also stop looping.
    rag, queue = _wire(
        monkeypatch,
        ingest_status=ProcessingStatus.COMPLETED.value,
        reindex_side_effect=RuntimeError("reindex blew up"),
    )
    mark = AsyncMock(return_value=True)
    monkeypatch.setattr(worker, "_mark_document_failed", mark)

    await worker._process_entry(MagicMock(), queue, _entry(9, trigger="user_reindex"))

    rag.reindex_document.assert_awaited_once()
    mark.assert_awaited_once()
    queue.ack.assert_awaited_once_with("1-0")


async def test_terminal_error_not_acked_when_mark_failed_cannot_persist(monkeypatch):
    # If we can't even record the failed status (DB blip while marking), DON'T
    # ack — leave the entry in the PEL for reclaim rather than dropping a doc
    # whose terminal state was never written.
    rag, queue = _wire(monkeypatch, process_side_effect=ValueError("poison doc"))
    monkeypatch.setattr(worker, "_mark_document_failed", AsyncMock(return_value=False))

    await worker._process_entry(MagicMock(), queue, _entry(7))

    queue.ack.assert_not_called()


async def test_success_path_still_acks_without_marking_failed(monkeypatch):
    # Regression: the happy path is unchanged — ack, no mark-failed.
    rag, queue = _wire(monkeypatch, ingest_status=None)
    mark = AsyncMock()
    monkeypatch.setattr(worker, "_mark_document_failed", mark)

    await worker._process_entry(MagicMock(), queue, _entry(5))

    rag.process_existing_document.assert_awaited_once()
    queue.ack.assert_awaited_once_with("1-0")
    mark.assert_not_called()


# ---------------------------------------------------------------------------
# _mark_document_failed
# ---------------------------------------------------------------------------

async def test_mark_document_failed_sets_status(monkeypatch):
    doc = MagicMock(status="processing")
    db = MagicMock()
    db.get = AsyncMock(return_value=doc)
    db.commit = AsyncMock()
    monkeypatch.setattr(worker, "AsyncSessionLocal", lambda: _FakeSessionCM(db))

    ok = await worker._mark_document_failed(5, ValueError("boom"))

    assert ok is True
    assert doc.status == DOC_STATUS_FAILED
    assert "boom" in doc.error_message
    db.commit.assert_awaited_once()


async def test_mark_document_failed_idempotent_when_already_failed(monkeypatch):
    doc = MagicMock(status=DOC_STATUS_FAILED)
    db = MagicMock()
    db.get = AsyncMock(return_value=doc)
    db.commit = AsyncMock()
    monkeypatch.setattr(worker, "AsyncSessionLocal", lambda: _FakeSessionCM(db))

    ok = await worker._mark_document_failed(5, ValueError("boom"))

    assert ok is True  # state already correct → stable to ack
    db.commit.assert_not_called()  # already failed → no redundant write


async def test_mark_document_failed_missing_doc_is_noop(monkeypatch):
    db = MagicMock()
    db.get = AsyncMock(return_value=None)  # row vanished
    db.commit = AsyncMock()
    monkeypatch.setattr(worker, "AsyncSessionLocal", lambda: _FakeSessionCM(db))

    ok = await worker._mark_document_failed(5, ValueError("boom"))  # must not raise

    assert ok is True  # nothing to retry → safe to ack
    db.commit.assert_not_called()


async def test_mark_document_failed_returns_false_on_db_error(monkeypatch):
    # Could not persist the failed status → False so the caller leaves the entry
    # in the PEL instead of acking.
    db = MagicMock()
    db.get = AsyncMock(side_effect=RuntimeError("db down"))
    monkeypatch.setattr(worker, "AsyncSessionLocal", lambda: _FakeSessionCM(db))

    ok = await worker._mark_document_failed(5, ValueError("boom"))

    assert ok is False
