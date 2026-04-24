"""chat_upload._auto_index_to_kb now goes through the document-worker (#388).

The previous implementation called ``rag.ingest_document`` inline in the
backend pod — the only remaining reason the backend needed a large
memory cap after the main upload path was migrated. These tests cover
the three interesting states of the new worker + poll path:

  - happy path: enqueue → status=completed → notify_session fires
    ``document_ready`` with the correct filename + chunk count.
  - worker down: heartbeat check fails → notify_session fires
    ``document_error`` immediately, no 30-minute wait.
  - worker failure: status=failed → notify_session fires
    ``document_error`` with the worker's error_message surfaced.
"""
from __future__ import annotations

import hashlib
from unittest.mock import AsyncMock, patch

import pytest

from api.routes.chat_upload import _auto_index_to_kb
from models.database import ChatUpload, Document, KnowledgeBase


@pytest.fixture
async def kb(db_session):
    kb = KnowledgeBase(name="default", description="test default KB")
    db_session.add(kb)
    await db_session.commit()
    await db_session.refresh(kb)
    return kb


@pytest.fixture
async def chat_upload_row(db_session):
    row = ChatUpload(
        filename="attached.pdf",
        file_path="/tmp/attached.pdf",
        file_type="pdf",
        status="completed",
    )
    db_session.add(row)
    await db_session.commit()
    await db_session.refresh(row)
    return row


@pytest.mark.unit
@pytest.mark.database
async def test_auto_index_happy_path_notifies_ready(
    db_session, kb, chat_upload_row, monkeypatch
):
    """Worker alive, document completes → document_ready notification."""
    notifications: list[dict] = []

    async def _fake_notify(session_id, payload):
        notifications.append({"session": session_id, **payload})

    # Pre-seed a Document in status=completed so the poll loop's first
    # tick observes a terminal state immediately.
    hash_val = hashlib.sha256(b"attached").hexdigest()
    doc = Document(
        filename="attached.pdf",
        file_path="/tmp/attached.pdf",
        file_hash=hash_val,
        knowledge_base_id=kb.id,
        status="completed",
        chunk_count=5,
    )
    db_session.add(doc)
    await db_session.commit()
    await db_session.refresh(doc)

    async def _stub_create(self, **kwargs):
        # Return the already-seeded Document so the poll reads it back.
        return doc

    with patch(
        "api.routes.chat_upload._get_or_create_default_kb",
        new=AsyncMock(return_value=kb),
    ), patch(
        "services.rag_service.RAGService.create_document_record",
        new=_stub_create,
    ), patch(
        "api.routes.chat_upload._worker_is_alive",
        new=AsyncMock(return_value=True),
    ), patch(
        "services.task_queue.DocumentTaskQueue.enqueue",
        new=AsyncMock(return_value="1-0"),
    ), patch(
        "api.websocket.shared.notify_session",
        new=_fake_notify,
    ):
        await _auto_index_to_kb(
            upload_id=chat_upload_row.id,
            file_path="/tmp/attached.pdf",
            filename="attached.pdf",
            file_hash=hash_val,
            session_id="sess-1",
        )

    types = [n["type"] for n in notifications]
    assert "document_processing" in types
    assert "document_ready" in types
    ready = next(n for n in notifications if n["type"] == "document_ready")
    assert ready["document_id"] == doc.id
    assert ready["chunk_count"] == 5


@pytest.mark.unit
@pytest.mark.database
async def test_auto_index_worker_down_fails_fast(
    db_session, kb, chat_upload_row, monkeypatch
):
    """Heartbeat missing → document_error fires immediately, enqueue is
    skipped (never wait 30 min for a stream no one reads)."""
    notifications: list[dict] = []

    async def _fake_notify(session_id, payload):
        notifications.append({"session": session_id, **payload})

    enqueue_mock = AsyncMock()
    with patch(
        "api.routes.chat_upload._get_or_create_default_kb",
        new=AsyncMock(return_value=kb),
    ), patch(
        "api.routes.chat_upload._worker_is_alive",
        new=AsyncMock(return_value=False),
    ), patch(
        "services.task_queue.DocumentTaskQueue.enqueue",
        new=enqueue_mock,
    ), patch(
        "api.websocket.shared.notify_session",
        new=_fake_notify,
    ):
        await _auto_index_to_kb(
            upload_id=chat_upload_row.id,
            file_path="/tmp/attached.pdf",
            filename="attached.pdf",
            file_hash="abc",
            session_id="sess-2",
        )

    types = [n["type"] for n in notifications]
    assert types[0] == "document_processing"
    assert types[-1] == "document_error"
    assert "unavailable" in notifications[-1]["error"].lower()
    enqueue_mock.assert_not_called()


@pytest.mark.unit
@pytest.mark.database
async def test_auto_index_worker_failure_surfaces_error(
    db_session, kb, chat_upload_row, monkeypatch
):
    """status=failed from the worker → document_error with the worker's
    error_message, not a generic 500."""
    notifications: list[dict] = []

    async def _fake_notify(session_id, payload):
        notifications.append({"session": session_id, **payload})

    hash_val = hashlib.sha256(b"broken").hexdigest()
    doc = Document(
        filename="broken.pdf",
        file_path="/tmp/broken.pdf",
        file_hash=hash_val,
        knowledge_base_id=kb.id,
        status="failed",
        error_message="Docling threw on page 42",
    )
    db_session.add(doc)
    await db_session.commit()
    await db_session.refresh(doc)

    async def _stub_create(self, **kwargs):
        return doc

    with patch(
        "api.routes.chat_upload._get_or_create_default_kb",
        new=AsyncMock(return_value=kb),
    ), patch(
        "services.rag_service.RAGService.create_document_record",
        new=_stub_create,
    ), patch(
        "api.routes.chat_upload._worker_is_alive",
        new=AsyncMock(return_value=True),
    ), patch(
        "services.task_queue.DocumentTaskQueue.enqueue",
        new=AsyncMock(return_value="1-0"),
    ), patch(
        "api.websocket.shared.notify_session",
        new=_fake_notify,
    ):
        await _auto_index_to_kb(
            upload_id=chat_upload_row.id,
            file_path="/tmp/broken.pdf",
            filename="broken.pdf",
            file_hash=hash_val,
            session_id="sess-3",
        )

    err = next(n for n in notifications if n["type"] == "document_error")
    assert "Docling threw on page 42" in err["error"]


@pytest.mark.unit
@pytest.mark.database
async def test_auto_index_reuses_existing_doc_on_duplicate_hash(
    db_session, kb, chat_upload_row,
):
    """Re-uploading the same file into the same KB used to crash the
    auto-index path with `IntegrityError: duplicate key value violates
    unique constraint "uq_documents_file_hash_kb"` because
    create_document_record blindly INSERTed.

    The fix: pre-check for an existing (file_hash, kb_id) row and
    reuse it — link the chat upload to the existing doc, skip the
    enqueue, and let the poll loop deliver document_ready immediately
    if the existing doc is already completed.
    """
    notifications: list[dict] = []

    async def _fake_notify(session_id, payload):
        notifications.append({"session": session_id, **payload})

    hash_val = hashlib.sha256(b"already-uploaded-once").hexdigest()
    existing_doc = Document(
        filename="duplicate.pdf",
        file_path="/tmp/duplicate.pdf",
        file_hash=hash_val,
        knowledge_base_id=kb.id,
        status="completed",
        chunk_count=42,
    )
    db_session.add(existing_doc)
    await db_session.commit()
    await db_session.refresh(existing_doc)

    create_record = AsyncMock()  # MUST NOT be called
    enqueue_mock = AsyncMock()    # MUST NOT be called

    with patch(
        "api.routes.chat_upload._get_or_create_default_kb",
        new=AsyncMock(return_value=kb),
    ), patch(
        "api.routes.chat_upload._worker_is_alive",
        new=AsyncMock(return_value=True),
    ), patch(
        "services.rag_service.RAGService.create_document_record",
        new=create_record,
    ), patch(
        "services.task_queue.DocumentTaskQueue.enqueue",
        new=enqueue_mock,
    ), patch(
        "api.websocket.shared.notify_session",
        new=_fake_notify,
    ):
        await _auto_index_to_kb(
            upload_id=chat_upload_row.id,
            file_path="/tmp/duplicate.pdf",
            filename="duplicate.pdf",
            file_hash=hash_val,
            session_id="sess-dup",
        )

    # No INSERT, no enqueue: we reused the existing doc cleanly.
    create_record.assert_not_called()
    enqueue_mock.assert_not_called()

    # The session got a document_ready for the existing doc.
    types = [n["type"] for n in notifications]
    assert "document_ready" in types
    ready = next(n for n in notifications if n["type"] == "document_ready")
    assert ready["document_id"] == existing_doc.id
    assert ready["chunk_count"] == 42
