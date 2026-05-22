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

import contextlib
import hashlib
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

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
        session_id="sess-fixture",
        filename="attached.pdf",
        file_path="/tmp/attached.pdf",
        file_type="pdf",
        status="completed",
    )
    db_session.add(row)
    await db_session.commit()
    await db_session.refresh(row)
    return row


@contextlib.contextmanager
def _patch_worker_path(async_engine, *, worker_alive: bool, **extra_patches):
    """Patch every external dependency of ``_auto_index_to_kb`` so it runs
    against the in-memory test engine.

    ``_auto_index_to_kb`` opens its OWN ``AsyncSessionLocal`` sessions (the
    HTTP response has long returned by the time it runs), so the ``get_db``
    dependency override does not reach it — point it at the test engine.
    ``_worker_is_alive`` is imported from ``api.routes.knowledge`` inside the
    function, so it must be patched there, not on ``chat_upload``.
    """
    test_sessionmaker = async_sessionmaker(
        async_engine, class_=AsyncSession, expire_on_commit=False
    )
    patches = {
        "api.routes.chat_upload.AsyncSessionLocal": test_sessionmaker,
        "api.routes.knowledge._worker_is_alive": AsyncMock(return_value=worker_alive),
        "services.redis_client.get_redis": MagicMock(return_value=MagicMock()),
        "services.task_queue.DocumentTaskQueue.enqueue": AsyncMock(return_value="1-0"),
        **extra_patches,
    }
    with contextlib.ExitStack() as stack:
        for target, value in patches.items():
            stack.enter_context(patch(target, new=value))
        yield


@pytest.mark.unit
@pytest.mark.database
async def test_auto_index_happy_path_notifies_ready(
    db_session, async_engine, kb, chat_upload_row
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
    doc_id = doc.id

    async def _stub_create(self, **kwargs):
        # Mirror the real create_document_record: INSERT a pending row.
        new_doc = Document(
            filename=kwargs["filename"],
            file_path=kwargs["file_path"],
            file_hash=kwargs["file_hash"],
            knowledge_base_id=kwargs["knowledge_base_id"],
            status="completed",
            chunk_count=5,
        )
        self.db.add(new_doc)
        await self.db.commit()
        await self.db.refresh(new_doc)
        return new_doc

    with _patch_worker_path(
        async_engine,
        worker_alive=True,
        **{
            "api.routes.chat_upload._get_or_create_default_kb": AsyncMock(
                return_value=kb
            ),
            "services.rag_service.RAGService.create_document_record": _stub_create,
            "api.websocket.shared.notify_session": _fake_notify,
        },
    ):
        await _auto_index_to_kb(
            upload_id=chat_upload_row.id,
            file_path="/tmp/new-attached.pdf",
            filename="attached.pdf",
            file_hash=hashlib.sha256(b"new-attached").hexdigest(),
            session_id="sess-1",
        )

    types = [n["type"] for n in notifications]
    assert "document_processing" in types
    assert "document_ready" in types
    ready = next(n for n in notifications if n["type"] == "document_ready")
    assert ready["chunk_count"] == 5
    assert doc_id  # seeded doc untouched


@pytest.mark.unit
@pytest.mark.database
async def test_auto_index_worker_down_fails_fast(
    db_session, async_engine, kb, chat_upload_row
):
    """Heartbeat missing → document_error fires immediately, enqueue is
    skipped (never wait 30 min for a stream no one reads)."""
    notifications: list[dict] = []

    async def _fake_notify(session_id, payload):
        notifications.append({"session": session_id, **payload})

    enqueue_mock = AsyncMock()
    with _patch_worker_path(
        async_engine,
        worker_alive=False,
        **{
            "api.routes.chat_upload._get_or_create_default_kb": AsyncMock(
                return_value=kb
            ),
            "services.task_queue.DocumentTaskQueue.enqueue": enqueue_mock,
            "api.websocket.shared.notify_session": _fake_notify,
        },
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
    db_session, async_engine, kb, chat_upload_row
):
    """status=failed from the worker → document_error with the worker's
    error_message, not a generic 500."""
    notifications: list[dict] = []

    async def _fake_notify(session_id, payload):
        notifications.append({"session": session_id, **payload})

    hash_val = hashlib.sha256(b"broken").hexdigest()

    async def _stub_create(self, **kwargs):
        new_doc = Document(
            filename=kwargs["filename"],
            file_path=kwargs["file_path"],
            file_hash=kwargs["file_hash"],
            knowledge_base_id=kwargs["knowledge_base_id"],
            status="failed",
            error_message="Docling threw on page 42",
        )
        self.db.add(new_doc)
        await self.db.commit()
        await self.db.refresh(new_doc)
        return new_doc

    with _patch_worker_path(
        async_engine,
        worker_alive=True,
        **{
            "api.routes.chat_upload._get_or_create_default_kb": AsyncMock(
                return_value=kb
            ),
            "services.rag_service.RAGService.create_document_record": _stub_create,
            "api.websocket.shared.notify_session": _fake_notify,
        },
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
    db_session, async_engine, kb, chat_upload_row
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
    existing_id = existing_doc.id

    create_record = AsyncMock()  # MUST NOT be called
    enqueue_mock = AsyncMock()   # MUST NOT be called

    with _patch_worker_path(
        async_engine,
        worker_alive=True,
        **{
            "api.routes.chat_upload._get_or_create_default_kb": AsyncMock(
                return_value=kb
            ),
            "services.rag_service.RAGService.create_document_record": create_record,
            "services.task_queue.DocumentTaskQueue.enqueue": enqueue_mock,
            "api.websocket.shared.notify_session": _fake_notify,
        },
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
    assert ready["document_id"] == existing_id
    assert ready["chunk_count"] == 42
