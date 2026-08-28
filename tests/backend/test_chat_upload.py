"""
Tests for Chat Upload API

Tests:
- File upload with text extraction
- Invalid format rejection
- File size limit
- Missing session_id validation
- DB entry creation
- Duplicate uploads allowed (no 409)
- Document context injection (Phase 2)
- KB index endpoint (Phase 3)
- Paperless forward endpoint (Phase 3)
- Auto-index background task (Phase 3)
- WebSocket notification registry (Phase 5)
- OCR image upload support (Phase 5)
"""
import io
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.database import ChatUpload, Conversation, KnowledgeBase, Message
from models.websocket_messages import WSChatMessage

# ============================================================================
# API Tests
# ============================================================================

class TestChatUploadAPI:
    """Tests for POST /api/chat/upload"""

    @pytest.mark.backend
    async def test_upload_txt_file(self, async_client: AsyncClient):
        """Async contract: TXT upload returns 200 in 'processing' immediately —
        text extraction (OCR) runs in the background and the preview is pushed
        over the chat WebSocket later (was: synchronous extract → 30s timeout)."""
        content = b"Hello, this is a test document with some content."
        files = {"file": ("test.txt", io.BytesIO(content), "text/plain")}
        data = {"session_id": "test-session-123"}

        response = await async_client.post("/api/chat/upload", files=files, data=data)

        assert response.status_code == 200
        body = response.json()
        assert body["filename"] == "test.txt"
        assert body["file_type"] == "txt"
        assert body["status"] == "processing"
        assert body["text_preview"] is None
        assert body["id"]

    @pytest.mark.backend
    async def test_upload_invalid_format(self, async_client: AsyncClient):
        """.exe files are rejected with 400"""
        content = b"MZ\x00\x00"
        files = {"file": ("malware.exe", io.BytesIO(content), "application/octet-stream")}
        data = {"session_id": "test-session-123"}

        response = await async_client.post("/api/chat/upload", files=files, data=data)

        assert response.status_code == 400
        assert "Unsupported" in response.json()["detail"]

    @pytest.mark.backend
    async def test_upload_too_large(self, async_client: AsyncClient):
        """Files exceeding max size are rejected with 400"""
        with patch("api.routes.chat_upload.settings") as mock_settings:
            mock_settings.max_file_size_mb = 1
            mock_settings.allowed_extensions_list = ["txt"]
            mock_settings.upload_dir = "/tmp/renfield-test-uploads"

            # 2MB content exceeds 1MB limit
            content = b"x" * (2 * 1024 * 1024)
            files = {"file": ("big.txt", io.BytesIO(content), "text/plain")}
            data = {"session_id": "test-session-123"}

            response = await async_client.post("/api/chat/upload", files=files, data=data)

        assert response.status_code == 400
        assert "too large" in response.json()["detail"]

    @pytest.mark.backend
    async def test_upload_missing_session_id(self, async_client: AsyncClient):
        """Missing session_id returns 422"""
        content = b"test content"
        files = {"file": ("test.txt", io.BytesIO(content), "text/plain")}

        response = await async_client.post("/api/chat/upload", files=files)

        assert response.status_code == 422

    @pytest.mark.backend
    async def test_upload_creates_db_entry(self, async_client: AsyncClient, db_session: AsyncSession):
        """Upload creates a ChatUpload entry in the database"""
        content = b"DB test content"
        files = {"file": ("db_test.txt", io.BytesIO(content), "text/plain")}
        data = {"session_id": "db-test-session"}

        with patch("api.routes.chat_upload._get_processor") as mock_proc:
            processor = AsyncMock()
            processor.extract_text_only = AsyncMock(return_value="DB test content")
            mock_proc.return_value = processor

            response = await async_client.post("/api/chat/upload", files=files, data=data)

        assert response.status_code == 200

        # Verify DB entry via response ID
        upload_id = response.json()["id"]
        result = await db_session.execute(
            select(ChatUpload).where(ChatUpload.id == upload_id)
        )
        upload = result.scalar_one_or_none()
        assert upload is not None
        assert upload.session_id == "db-test-session"
        assert upload.filename == "db_test.txt"

    @pytest.mark.backend
    async def test_upload_duplicate_allowed(self, async_client: AsyncClient):
        """Same file can be uploaded twice in chat (no 409 like KB)"""
        content = b"duplicate test content"

        with patch("api.routes.chat_upload._get_processor") as mock_proc:
            processor = AsyncMock()
            processor.extract_text_only = AsyncMock(return_value="duplicate test content")
            mock_proc.return_value = processor

            for _ in range(2):
                files = {"file": ("dup.txt", io.BytesIO(content), "text/plain")}
                data = {"session_id": "dup-session"}
                response = await async_client.post("/api/chat/upload", files=files, data=data)
                assert response.status_code == 200

    @pytest.mark.backend
    async def test_upload_pdf_format_accepted(self, async_client: AsyncClient):
        """PDF extension is accepted"""
        content = b"%PDF-1.4 fake pdf content"
        files = {"file": ("report.pdf", io.BytesIO(content), "application/pdf")}
        data = {"session_id": "pdf-session"}

        with patch("api.routes.chat_upload._get_processor") as mock_proc:
            processor = AsyncMock()
            processor.extract_text_only = AsyncMock(return_value="Extracted PDF text")
            mock_proc.return_value = processor

            response = await async_client.post("/api/chat/upload", files=files, data=data)

        assert response.status_code == 200
        assert response.json()["file_type"] == "pdf"

    @pytest.mark.backend
    async def test_get_upload_status(self, async_client: AsyncClient, db_session: AsyncSession):
        """GET /upload/{id} reflects the row's current status + preview — the
        WS-push fallback / status read for the async upload."""
        upload = ChatUpload(
            session_id="status-session", filename="s.txt", file_type="txt",
            file_size=3, file_hash="h-status", status="completed",
            extracted_text="ready text",
        )
        db_session.add(upload)
        await db_session.commit()
        await db_session.refresh(upload)

        response = await async_client.get(f"/api/chat/upload/{upload.id}")
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "completed"
        assert body["text_preview"] == "ready text"

        missing = await async_client.get("/api/chat/upload/99999999")
        assert missing.status_code == 404


@pytest.mark.backend
class TestExtractAndFinalize:
    """The background worker that the async upload schedules."""

    async def _seed(self, db_session, *, text="hi", file_hash="h"):
        from pathlib import Path as _P
        import tempfile
        f = _P(tempfile.gettempdir()) / f"renfield-bg-{file_hash}.txt"
        f.write_text(text)
        upload = ChatUpload(
            session_id="bg-session", filename=f.name, file_type="txt",
            file_size=len(text), file_hash=file_hash, status="processing",
            file_path=str(f),
        )
        db_session.add(upload)
        await db_session.commit()
        await db_session.refresh(upload)
        return upload, str(f)

    def _wire(self, monkeypatch, db_session, *, auto_index=False):
        """Patch the bg worker's collaborators. Returns (module, notify_mock,
        auto_index_mock) so tests can assert the WS push + auto-index calls."""
        from contextlib import asynccontextmanager
        from api.routes import chat_upload as cu

        @asynccontextmanager
        async def _sess():
            yield db_session

        notify = AsyncMock()
        auto = AsyncMock()
        monkeypatch.setattr(cu, "AsyncSessionLocal", _sess)
        monkeypatch.setattr(cu, "_auto_index_to_kb", auto)
        monkeypatch.setattr(cu.settings, "chat_upload_auto_index", auto_index)
        monkeypatch.setattr(cu.settings, "knowledge_graph_enabled", False)
        # notify_session is imported inside _extract_and_finalize from this module.
        monkeypatch.setattr("api.websocket.shared.notify_session", notify)
        return cu, notify, auto

    async def test_finalize_success_marks_completed_and_pushes(self, db_session, monkeypatch):
        cu, notify, auto = self._wire(monkeypatch, db_session, auto_index=True)
        upload, fpath = await self._seed(db_session, file_hash="ok")
        proc = AsyncMock()
        proc.extract_text_only = AsyncMock(return_value="extracted body text")
        monkeypatch.setattr(cu, "_get_processor", lambda: proc)

        await cu._extract_and_finalize(
            upload_id=upload.id, file_path=fpath, user_id=None,
            session_id="bg-session", safe_name=upload.filename, file_hash="ok",
        )
        await db_session.refresh(upload)
        assert upload.status == "completed"
        assert upload.extracted_text == "extracted body text"

        # WS push carries the completed result (the whole point of the rework).
        notify.assert_awaited_once()
        sess_arg, payload = notify.await_args.args
        assert sess_arg == "bg-session"
        assert payload["type"] == "upload_processed"
        assert payload["upload_id"] == upload.id
        assert payload["status"] == "completed"
        assert payload["text_preview"] == "extracted body text"
        assert payload["error"] is None
        # Auto-index fires on success.
        auto.assert_awaited_once()

    async def test_finalize_failure_marks_failed_and_skips_autoindex(self, db_session, monkeypatch):
        """Defensive path: if extract_text_only ever RAISES, the row is marked
        failed, the push carries the error, and auto-index is skipped."""
        cu, notify, auto = self._wire(monkeypatch, db_session, auto_index=True)
        upload, fpath = await self._seed(db_session, file_hash="bad")
        proc = AsyncMock()
        proc.extract_text_only = AsyncMock(side_effect=RuntimeError("Docling crash"))
        monkeypatch.setattr(cu, "_get_processor", lambda: proc)

        await cu._extract_and_finalize(
            upload_id=upload.id, file_path=fpath, user_id=None,
            session_id="bg-session", safe_name=upload.filename, file_hash="bad",
        )
        await db_session.refresh(upload)
        assert upload.status == "failed"
        assert upload.error_message and "Docling" in upload.error_message

        sess_arg, payload = notify.await_args.args
        assert payload["status"] == "failed"
        assert payload["text_preview"] is None
        assert "Docling" in (payload["error"] or "")
        auto.assert_not_awaited()  # no auto-index on failure

    async def test_finalize_none_extraction_stays_completed_empty(self, db_session, monkeypatch):
        """Reality check (extract_text_only returns None on error, it does NOT
        raise): a None result is persisted as completed-with-null-text — the
        current behavior — and the push reflects that. Documents the known
        limitation that a silent OCR failure looks like an empty doc."""
        cu, notify, auto = self._wire(monkeypatch, db_session, auto_index=True)
        upload, fpath = await self._seed(db_session, file_hash="none")
        proc = AsyncMock()
        proc.extract_text_only = AsyncMock(return_value=None)
        monkeypatch.setattr(cu, "_get_processor", lambda: proc)

        await cu._extract_and_finalize(
            upload_id=upload.id, file_path=fpath, user_id=None,
            session_id="bg-session", safe_name=upload.filename, file_hash="none",
        )
        await db_session.refresh(upload)
        assert upload.status == "completed"
        assert upload.extracted_text is None
        assert notify.await_args.args[1]["text_preview"] is None

    async def test_finalize_missing_row_is_noop(self, db_session, monkeypatch):
        """A vanished upload row (deleted before the bg task ran) must not raise
        and must not push / auto-index."""
        cu, notify, auto = self._wire(monkeypatch, db_session, auto_index=True)
        proc = AsyncMock()
        proc.extract_text_only = AsyncMock(return_value="x")
        monkeypatch.setattr(cu, "_get_processor", lambda: proc)

        await cu._extract_and_finalize(
            upload_id=999999999, file_path="/tmp/none", user_id=None,
            session_id="bg-session", safe_name="gone.txt", file_hash="gone",
        )
        notify.assert_not_awaited()
        auto.assert_not_awaited()


# ============================================================================
# Model Tests
# ============================================================================

class TestChatUploadModel:
    """Tests for ChatUpload database model"""

    @pytest.mark.database
    async def test_create_chat_upload(self, db_session: AsyncSession):
        """ChatUpload can be created and retrieved"""
        upload = ChatUpload(
            session_id="model-test-session",
            filename="test.pdf",
            file_type="pdf",
            file_size=12345,
            file_hash="abc123",
            extracted_text="Some extracted text",
            status="completed",
        )
        db_session.add(upload)
        await db_session.commit()
        await db_session.refresh(upload)

        assert upload.id is not None
        assert upload.session_id == "model-test-session"
        assert upload.created_at is not None

    @pytest.mark.database
    async def test_chat_upload_default_status(self, db_session: AsyncSession):
        """Default status is 'processing'"""
        upload = ChatUpload(
            session_id="default-status-test",
            filename="test.txt",
        )
        db_session.add(upload)
        await db_session.commit()
        await db_session.refresh(upload)

        assert upload.status == "processing"


# ============================================================================
# Document Context Injection Tests (Phase 2)
# ============================================================================

class TestDocumentContextInjection:
    """Tests for document context injection into LLM prompts.

    The _fetch_document_context function lives in chat_handler.py which has
    module-level imports requiring asyncpg. We test it via the conftest's
    app_with_test_db fixture (async_client) which patches those imports.
    For unit tests that don't need DB, we test WSChatMessage directly.
    """

    @pytest.fixture(autouse=True)
    def _patch_chat_handler_session(self, async_engine):
        """Point chat_handler's AsyncSessionLocal at the test engine.

        ``_fetch_document_context`` opens its own ``AsyncSessionLocal()``
        rather than accepting an injected session — by default that binds
        to the production Postgres engine, so rows the test wrote to the
        in-memory SQLite test DB are invisible. Rebind the session-maker
        to the shared test engine so the function reads what the test set
        up. Committed rows are visible across sessions on the StaticPool
        in-memory engine.
        """
        from sqlalchemy.ext.asyncio import AsyncSession as _AS, async_sessionmaker

        test_maker = async_sessionmaker(
            async_engine, class_=_AS, expire_on_commit=False
        )
        with patch(
            "api.websocket.chat_handler.AsyncSessionLocal", test_maker
        ):
            yield

    @pytest.mark.backend
    async def test_fetch_document_context_single(self, async_client, db_session: AsyncSession):
        """Single completed document produces document_context_section"""
        upload = ChatUpload(
            session_id="ctx-test",
            filename="report.pdf",
            file_type="pdf",
            file_size=5000,
            extracted_text="This is the report content about quarterly earnings.",
            status="completed",
        )
        db_session.add(upload)
        await db_session.commit()
        await db_session.refresh(upload)

        from api.websocket.chat_handler import _fetch_document_context
        result = await _fetch_document_context([upload.id], lang="en")

        assert "report.pdf" in result
        assert "quarterly earnings" in result
        assert "UPLOADED DOCUMENT" in result

    @pytest.mark.backend
    async def test_fetch_document_context_multiple(self, async_client, db_session: AsyncSession):
        """Multiple documents produce document_context_multi_section"""
        uploads = []
        for i, name in enumerate(["doc1.txt", "doc2.txt"]):
            u = ChatUpload(
                session_id="ctx-multi-test",
                filename=name,
                file_type="txt",
                file_size=1000 + i,
                extracted_text=f"Content of document {i+1}.",
                status="completed",
            )
            db_session.add(u)
            uploads.append(u)
        await db_session.commit()
        for u in uploads:
            await db_session.refresh(u)

        from api.websocket.chat_handler import _fetch_document_context
        result = await _fetch_document_context([u.id for u in uploads], lang="en")

        assert "UPLOADED DOCUMENTS" in result
        assert "2 files" in result
        assert "doc1.txt" in result
        assert "doc2.txt" in result

    @pytest.mark.backend
    async def test_fetch_document_context_skips_failed(self, async_client, db_session: AsyncSession):
        """Failed uploads are excluded from context"""
        good = ChatUpload(
            session_id="ctx-skip-test",
            filename="good.txt",
            file_type="txt",
            file_size=500,
            extracted_text="Good content.",
            status="completed",
        )
        bad = ChatUpload(
            session_id="ctx-skip-test",
            filename="bad.txt",
            file_type="txt",
            file_size=500,
            extracted_text=None,
            status="failed",
            error_message="Extraction failed",
        )
        db_session.add_all([good, bad])
        await db_session.commit()
        await db_session.refresh(good)
        await db_session.refresh(bad)

        from api.websocket.chat_handler import _fetch_document_context
        result = await _fetch_document_context([good.id, bad.id], lang="en")

        assert "good.txt" in result
        assert "bad.txt" not in result
        # Single doc format since only one survived
        assert "UPLOADED DOCUMENT" in result

    @pytest.mark.backend
    async def test_fetch_document_context_truncates(self, async_client, db_session: AsyncSession):
        """Text is truncated to max_context_chars"""
        long_text = "x" * 100000
        upload = ChatUpload(
            session_id="ctx-trunc-test",
            filename="huge.txt",
            file_type="txt",
            file_size=100000,
            extracted_text=long_text,
            status="completed",
        )
        db_session.add(upload)
        await db_session.commit()
        await db_session.refresh(upload)

        with patch("api.websocket.chat_handler.settings") as mock_settings:
            mock_settings.chat_upload_max_context_chars = 1000

            from api.websocket.chat_handler import _fetch_document_context
            result = await _fetch_document_context([upload.id], lang="en")

        # The result should be significantly smaller than the 100k original
        assert len(result) < 5000

    @pytest.mark.backend
    async def test_fetch_document_context_empty_ids(self, async_client):
        """Empty ID list returns empty string"""
        from api.websocket.chat_handler import _fetch_document_context
        result = await _fetch_document_context([], lang="de")
        assert result == ""

    @pytest.mark.backend
    async def test_fetch_document_context_invalid_ids(self, async_client, db_session: AsyncSession):
        """Non-existent IDs return empty string"""
        from api.websocket.chat_handler import _fetch_document_context
        result = await _fetch_document_context([99999, 99998], lang="en")
        assert result == ""

    @pytest.mark.backend
    async def test_history_includes_attachments(self, async_client: AsyncClient, db_session: AsyncSession):
        """History endpoint returns attachment details for user messages"""
        # Create a ChatUpload
        upload = ChatUpload(
            session_id="hist-attach-test",
            filename="report.pdf",
            file_type="pdf",
            file_size=5000,
            extracted_text="Some text",
            status="completed",
        )
        db_session.add(upload)
        await db_session.commit()
        await db_session.refresh(upload)

        # Create conversation + message with attachment_ids in metadata
        conv = Conversation(session_id="hist-attach-test")
        db_session.add(conv)
        await db_session.commit()
        await db_session.refresh(conv)

        msg = Message(
            conversation_id=conv.id,
            role="user",
            content="What does this say?",
            message_metadata={"attachment_ids": [upload.id]},
        )
        db_session.add(msg)
        await db_session.commit()

        response = await async_client.get("/api/chat/history/hist-attach-test")
        assert response.status_code == 200
        messages = response.json()["messages"]
        assert len(messages) == 1
        assert "attachments" in messages[0]
        assert len(messages[0]["attachments"]) == 1
        att = messages[0]["attachments"][0]
        assert att["id"] == upload.id
        assert att["filename"] == "report.pdf"
        assert att["file_type"] == "pdf"

    @pytest.mark.backend
    async def test_history_without_attachments_backward_compatible(
        self, async_client: AsyncClient, db_session: AsyncSession
    ):
        """Messages without attachment_ids have no attachments key — no error"""
        conv = Conversation(session_id="hist-no-attach-test")
        db_session.add(conv)
        await db_session.commit()
        await db_session.refresh(conv)

        msg = Message(
            conversation_id=conv.id,
            role="user",
            content="Hello",
            message_metadata=None,
        )
        db_session.add(msg)
        await db_session.commit()

        response = await async_client.get("/api/chat/history/hist-no-attach-test")
        assert response.status_code == 200
        messages = response.json()["messages"]
        assert len(messages) == 1
        assert "attachments" not in messages[0]

    @pytest.mark.backend
    async def test_history_skips_missing_attachments(
        self, async_client: AsyncClient, db_session: AsyncSession
    ):
        """Non-existent attachment IDs are silently skipped"""
        conv = Conversation(session_id="hist-missing-attach-test")
        db_session.add(conv)
        await db_session.commit()
        await db_session.refresh(conv)

        msg = Message(
            conversation_id=conv.id,
            role="user",
            content="Check this doc",
            message_metadata={"attachment_ids": [99999]},
        )
        db_session.add(msg)
        await db_session.commit()

        response = await async_client.get("/api/chat/history/hist-missing-attach-test")
        assert response.status_code == 200
        messages = response.json()["messages"]
        assert len(messages) == 1
        # attachment_ids existed but none resolved, so attachments is empty list
        assert messages[0].get("attachments", []) == []

    @pytest.mark.unit
    def test_ws_message_attachment_ids_accepted(self):
        """WSChatMessage accepts attachment_ids field"""
        msg = WSChatMessage(
            content="What does the document say?",
            attachment_ids=[1, 2, 3],
        )
        assert msg.attachment_ids == [1, 2, 3]

    @pytest.mark.unit
    def test_ws_message_attachment_ids_default_none(self):
        """WSChatMessage defaults attachment_ids to None"""
        msg = WSChatMessage(content="Hello")
        assert msg.attachment_ids is None


# ============================================================================
# Phase 3: KB Index Endpoint Tests
# ============================================================================

class TestChatUploadIndex:
    """Tests for POST /api/chat/upload/{id}/index"""

    @pytest.mark.backend
    async def test_index_success(self, async_client: AsyncClient, db_session: AsyncSession):
        """Successful index returns 200 with document_id"""
        # Create a temp file
        with tempfile.NamedTemporaryFile(suffix=".txt", delete=False) as f:
            f.write(b"test content for indexing")
            tmp_path = f.name

        try:
            # Create KB
            kb = KnowledgeBase(name="Test KB", description="For testing")
            db_session.add(kb)
            await db_session.commit()
            await db_session.refresh(kb)

            # Create ChatUpload with file_path
            upload = ChatUpload(
                session_id="index-test",
                filename="test.txt",
                file_type="txt",
                file_size=100,
                file_hash="abc123",
                extracted_text="test content",
                status="completed",
                file_path=tmp_path,
            )
            db_session.add(upload)
            await db_session.commit()
            await db_session.refresh(upload)

            mock_doc = MagicMock()
            mock_doc.id = 42
            mock_doc.chunk_count = 5

            # RAGService is imported lazily inside the route handler
            # (`from services.rag_service import RAGService`), so the patch
            # must target the definition module, not the route module.
            with patch("services.rag_service.RAGService") as MockRAG:
                rag_instance = AsyncMock()
                rag_instance.ingest_document = AsyncMock(return_value=mock_doc)
                MockRAG.return_value = rag_instance

                response = await async_client.post(
                    f"/api/chat/upload/{upload.id}/index",
                    json={"knowledge_base_id": kb.id},
                )

            assert response.status_code == 200
            body = response.json()
            assert body["success"] is True
            assert body["document_id"] == 42
            assert body["knowledge_base_id"] == kb.id
            assert body["chunk_count"] == 5
        finally:
            Path(tmp_path).unlink(missing_ok=True)

    @pytest.mark.backend
    async def test_index_not_found(self, async_client: AsyncClient):
        """Non-existent upload returns 404"""
        response = await async_client.post(
            "/api/chat/upload/99999/index",
            json={"knowledge_base_id": 1},
        )
        assert response.status_code == 404

    @pytest.mark.backend
    async def test_index_already_indexed(self, async_client: AsyncClient, db_session: AsyncSession):
        """Upload with document_id already set returns 409"""
        with tempfile.NamedTemporaryFile(suffix=".txt", delete=False) as f:
            f.write(b"already indexed")
            tmp_path = f.name

        try:
            upload = ChatUpload(
                session_id="already-indexed-test",
                filename="indexed.txt",
                file_type="txt",
                file_size=100,
                status="completed",
                file_path=tmp_path,
                document_id=99,
            )
            db_session.add(upload)
            await db_session.commit()
            await db_session.refresh(upload)

            response = await async_client.post(
                f"/api/chat/upload/{upload.id}/index",
                json={"knowledge_base_id": 1},
            )
            assert response.status_code == 409
            assert "Already indexed" in response.json()["detail"]
        finally:
            Path(tmp_path).unlink(missing_ok=True)


# ============================================================================
# Phase 3: Paperless Forward Endpoint Tests
# ============================================================================

class TestChatUploadPaperless:
    """Tests for POST /api/chat/upload/{id}/paperless"""

    @pytest.mark.backend
    async def test_paperless_success(self, async_client: AsyncClient, db_session: AsyncSession):
        """Successful Paperless forward returns 200"""
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
            f.write(b"%PDF-1.4 test content")
            tmp_path = f.name

        try:
            upload = ChatUpload(
                session_id="paperless-test",
                filename="invoice.pdf",
                file_type="pdf",
                file_size=500,
                status="completed",
                file_path=tmp_path,
            )
            db_session.add(upload)
            await db_session.commit()
            await db_session.refresh(upload)

            import json
            mcp_result = {
                "success": True,
                "message": json.dumps({
                    "success": True,
                    "data": {"task_id": "abc-123", "title": "invoice.pdf", "filename": "invoice.pdf"},
                }),
            }

            mock_manager = AsyncMock()
            mock_manager.execute_tool = AsyncMock(return_value=mcp_result)

            # Patch app.state.mcp_manager via the Request object
            from main import app
            app.state.mcp_manager = mock_manager

            try:
                response = await async_client.post(f"/api/chat/upload/{upload.id}/paperless")
            finally:
                app.state.mcp_manager = None

            assert response.status_code == 200
            body = response.json()
            assert body["success"] is True
            assert body["paperless_task_id"] == "abc-123"
        finally:
            Path(tmp_path).unlink(missing_ok=True)

    @pytest.mark.backend
    async def test_paperless_not_found(self, async_client: AsyncClient):
        """Non-existent upload returns 404"""
        response = await async_client.post("/api/chat/upload/99999/paperless")
        assert response.status_code == 404

    @pytest.mark.backend
    async def test_paperless_uses_extracted_metadata(
        self, async_client: AsyncClient, db_session: AsyncSession
    ):
        """Auto-extracted metadata flows into mcp.paperless.upload_document params.

        When the metadata extractor returns a non-empty PaperlessMetadata
        (correspondent, document_type, tags, etc.), the route must include
        those fields in the MCP tool call — not just title/filename. This
        is the scenario that turns a one-click forward into a fully-tagged
        Paperless document.
        """
        from datetime import date as _date

        from services.paperless_metadata_extractor import (
            ExtractionResult,
            PaperlessMetadata,
        )

        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
            f.write(b"%PDF-1.4 test content")
            tmp_path = f.name

        try:
            upload = ChatUpload(
                session_id="paperless-meta-test",
                filename="rechnung.pdf",
                file_type="pdf",
                file_size=500,
                status="completed",
                file_path=tmp_path,
            )
            db_session.add(upload)
            await db_session.commit()
            await db_session.refresh(upload)

            import json
            mcp_result = {
                "success": True,
                "message": json.dumps({
                    "success": True,
                    "data": {"task_id": "meta-456"},
                }),
            }

            mock_manager = AsyncMock()
            mock_manager.execute_tool = AsyncMock(return_value=mcp_result)

            # Mock the extractor to return resolved metadata. We patch the
            # class on the import path the route uses (lazy import inside
            # the handler) so ``PaperlessMetadataExtractor(mcp_manager=...)``
            # returns our mock instance.
            extracted = ExtractionResult(
                metadata=PaperlessMetadata(
                    title="Rechnung Wehrmann 2026-04",
                    correspondent="Wehrmann GmbH",
                    document_type="Rechnung",
                    tags=["steuer", "2026"],
                    storage_path="Belege/2026",
                    created_date=_date(2026, 4, 15),
                ),
                doc_text="dummy",
                error=None,
            )
            mock_extractor = AsyncMock()
            mock_extractor.extract = AsyncMock(return_value=extracted)

            from main import app
            app.state.mcp_manager = mock_manager

            try:
                with patch(
                    "services.paperless_metadata_extractor.PaperlessMetadataExtractor",
                    return_value=mock_extractor,
                ):
                    response = await async_client.post(
                        f"/api/chat/upload/{upload.id}/paperless"
                    )
            finally:
                app.state.mcp_manager = None

            assert response.status_code == 200
            body = response.json()
            assert body["success"] is True
            assert body["paperless_task_id"] == "meta-456"
            # Response message names which fields were auto-filled — UI uses
            # this to show the user what got attached. Empty fields stay out.
            assert "title" in body["message"]
            assert "correspondent" in body["message"]

            # The actual MCP call must carry every resolved field. A
            # regression here means metadata was extracted but lost on the
            # way to Paperless — silent and hard to spot in the UI.
            sent_params = mock_manager.execute_tool.call_args[0][1]
            assert sent_params["title"] == "Rechnung Wehrmann 2026-04"
            assert sent_params["correspondent"] == "Wehrmann GmbH"
            assert sent_params["document_type"] == "Rechnung"
            assert sent_params["tags"] == ["steuer", "2026"]
            assert sent_params["storage_path"] == "Belege/2026"
            assert sent_params["created_date"] == "2026-04-15"
            assert sent_params["filename"] == "rechnung.pdf"
            assert "file_content_base64" in sent_params
        finally:
            Path(tmp_path).unlink(missing_ok=True)

    @pytest.mark.backend
    async def test_paperless_falls_back_when_extractor_errors(
        self, async_client: AsyncClient, db_session: AsyncSession
    ):
        """Extractor failure must NOT block the bare upload.

        OCR errors, taxonomy fetch failures, and LLM timeouts all funnel
        into the same fallback path. The user still gets the document into
        Paperless — just without auto-extracted metadata.
        """
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
            f.write(b"%PDF-1.4 unreadable")
            tmp_path = f.name

        try:
            upload = ChatUpload(
                session_id="paperless-fallback-test",
                filename="scan.pdf",
                file_type="pdf",
                file_size=500,
                status="completed",
                file_path=tmp_path,
            )
            db_session.add(upload)
            await db_session.commit()
            await db_session.refresh(upload)

            import json
            mcp_result = {
                "success": True,
                "message": json.dumps({"success": True, "data": {"task_id": "fb-789"}}),
            }

            mock_manager = AsyncMock()
            mock_manager.execute_tool = AsyncMock(return_value=mcp_result)

            mock_extractor = AsyncMock()
            mock_extractor.extract = AsyncMock(side_effect=RuntimeError("Docling crashed"))

            from main import app
            app.state.mcp_manager = mock_manager
            try:
                with patch(
                    "services.paperless_metadata_extractor.PaperlessMetadataExtractor",
                    return_value=mock_extractor,
                ):
                    response = await async_client.post(
                        f"/api/chat/upload/{upload.id}/paperless"
                    )
            finally:
                app.state.mcp_manager = None

            assert response.status_code == 200
            body = response.json()
            assert body["success"] is True
            assert body["paperless_task_id"] == "fb-789"

            # Filename-only fallback — no metadata fields in the MCP call.
            sent_params = mock_manager.execute_tool.call_args[0][1]
            assert sent_params["title"] == "scan.pdf"
            assert "correspondent" not in sent_params
            assert "document_type" not in sent_params
            assert "tags" not in sent_params
            assert "created_date" not in sent_params
        finally:
            Path(tmp_path).unlink(missing_ok=True)

    @pytest.mark.backend
    async def test_paperless_mcp_not_available(self, async_client: AsyncClient, db_session: AsyncSession):
        """No MCP manager returns 503"""
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
            f.write(b"%PDF-1.4 test")
            tmp_path = f.name

        try:
            upload = ChatUpload(
                session_id="no-mcp-test",
                filename="doc.pdf",
                file_type="pdf",
                file_size=200,
                status="completed",
                file_path=tmp_path,
            )
            db_session.add(upload)
            await db_session.commit()
            await db_session.refresh(upload)

            # Ensure no MCP manager on app.state
            from main import app
            original = getattr(app.state, "mcp_manager", None)
            app.state.mcp_manager = None
            try:
                response = await async_client.post(f"/api/chat/upload/{upload.id}/paperless")
            finally:
                app.state.mcp_manager = original

            assert response.status_code == 503
        finally:
            Path(tmp_path).unlink(missing_ok=True)


# ============================================================================
# Phase 3: Auto-Index Background Task Tests
# ============================================================================

class TestAutoIndex:
    """Tests for auto-index background task helpers"""

    @pytest.mark.backend
    async def test_get_or_create_default_kb_creates(self, async_client, db_session: AsyncSession):
        """_get_or_create_default_kb creates KB if not exists"""
        from api.routes.chat_upload import _get_or_create_default_kb

        with patch("api.routes.chat_upload.settings") as mock_settings:
            mock_settings.chat_upload_default_kb_name = "Auto-Test KB"

            kb = await _get_or_create_default_kb(db_session)

        assert kb is not None
        assert kb.name == "Auto-Test KB"
        assert kb.id is not None

        # Calling again returns the same KB
        with patch("api.routes.chat_upload.settings") as mock_settings:
            mock_settings.chat_upload_default_kb_name = "Auto-Test KB"
            kb2 = await _get_or_create_default_kb(db_session)

        assert kb2.id == kb.id


# ============================================================================
# Phase 4: Email Forward Endpoint Tests
# ============================================================================

class TestChatUploadEmail:
    """Tests for POST /api/chat/upload/{id}/email"""

    @pytest.mark.backend
    async def test_email_success(self, async_client: AsyncClient, db_session: AsyncSession):
        """Successful email forward returns 200"""
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
            f.write(b"%PDF-1.4 test content")
            tmp_path = f.name

        try:
            upload = ChatUpload(
                session_id="email-test",
                filename="report.pdf",
                file_type="pdf",
                file_size=500,
                status="completed",
                file_path=tmp_path,
            )
            db_session.add(upload)
            await db_session.commit()
            await db_session.refresh(upload)

            import json
            mcp_result = {
                "success": True,
                "message": json.dumps({"success": True, "data": {}}),
            }

            mock_manager = AsyncMock()
            mock_manager.execute_tool = AsyncMock(return_value=mcp_result)

            from main import app
            app.state.mcp_manager = mock_manager

            try:
                response = await async_client.post(
                    f"/api/chat/upload/{upload.id}/email",
                    json={"to": "user@example.com"},
                )
            finally:
                app.state.mcp_manager = None

            assert response.status_code == 200
            body = response.json()
            assert body["success"] is True
            assert "user@example.com" in body["message"]

            # Verify MCP tool was called with correct params
            mock_manager.execute_tool.assert_called_once()
            call_args = mock_manager.execute_tool.call_args
            assert call_args[0][0] == "mcp.email.send_email"
            assert call_args[0][1]["account"] == "primary"
            assert call_args[0][1]["to"] == "user@example.com"
            assert call_args[0][1]["subject"] == "Document: report.pdf"
            assert len(call_args[0][1]["attachments"]) == 1
            assert call_args[0][1]["attachments"][0]["filename"] == "report.pdf"
        finally:
            Path(tmp_path).unlink(missing_ok=True)

    @pytest.mark.backend
    async def test_email_not_found(self, async_client: AsyncClient):
        """Non-existent upload returns 404"""
        response = await async_client.post(
            "/api/chat/upload/99999/email",
            json={"to": "user@example.com"},
        )
        assert response.status_code == 404

    @pytest.mark.backend
    async def test_email_mcp_not_available(self, async_client: AsyncClient, db_session: AsyncSession):
        """No MCP manager returns 503"""
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
            f.write(b"%PDF-1.4 test")
            tmp_path = f.name

        try:
            upload = ChatUpload(
                session_id="no-mcp-email-test",
                filename="doc.pdf",
                file_type="pdf",
                file_size=200,
                status="completed",
                file_path=tmp_path,
            )
            db_session.add(upload)
            await db_session.commit()
            await db_session.refresh(upload)

            from main import app
            original = getattr(app.state, "mcp_manager", None)
            app.state.mcp_manager = None
            try:
                response = await async_client.post(
                    f"/api/chat/upload/{upload.id}/email",
                    json={"to": "user@example.com"},
                )
            finally:
                app.state.mcp_manager = original

            assert response.status_code == 503
        finally:
            Path(tmp_path).unlink(missing_ok=True)

    @pytest.mark.backend
    async def test_email_file_missing(self, async_client: AsyncClient, db_session: AsyncSession):
        """Missing file on disk returns 400"""
        upload = ChatUpload(
            session_id="missing-file-email-test",
            filename="gone.pdf",
            file_type="pdf",
            file_size=200,
            status="completed",
            file_path="/tmp/nonexistent-file-12345.pdf",
        )
        db_session.add(upload)
        await db_session.commit()
        await db_session.refresh(upload)

        response = await async_client.post(
            f"/api/chat/upload/{upload.id}/email",
            json={"to": "user@example.com"},
        )
        assert response.status_code == 400
        assert "no longer available" in response.json()["detail"]


# ============================================================================
# Phase 4: Cleanup Endpoint Tests
# ============================================================================

class TestChatUploadOwnership:
    """Ownership scoping on the /upload/{id}/paperless, /email, /index endpoints.

    Regression guard for #434 — in multi-user (AUTH_ENABLED=true) setups,
    an authenticated user A could previously forward user B's chat upload
    to Paperless/email by guessing the integer id. All three endpoints now
    scope the ChatUpload lookup via Conversation.user_id.
    """

    @pytest.mark.backend
    async def test_paperless_rejects_cross_user_upload(
        self, async_client: AsyncClient, db_session: AsyncSession, test_user
    ):
        """Authenticated user B cannot forward an upload owned by user A."""
        from main import app
        from models.database import Conversation, User
        from services.auth_service import get_optional_user

        # User A uploaded a file tied to conversation C_A
        user_a_conv = Conversation(session_id="sess-A", user_id=test_user.id)
        db_session.add(user_a_conv)
        await db_session.commit()

        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
            f.write(b"%PDF-1.4 test")
            tmp_path = f.name
        try:
            upload = ChatUpload(
                session_id="sess-A",
                filename="secret.pdf",
                file_type="pdf",
                file_size=200,
                status="completed",
                file_path=tmp_path,
            )
            db_session.add(upload)
            await db_session.commit()
            await db_session.refresh(upload)

            # A different user (B) attempts to forward
            user_b = User(
                username="user_b", email="b@test",
                password_hash="x", role_id=test_user.role_id,
            )
            db_session.add(user_b)
            await db_session.commit()
            await db_session.refresh(user_b)

            app.dependency_overrides[get_optional_user] = lambda: user_b
            try:
                response = await async_client.post(
                    f"/api/chat/upload/{upload.id}/paperless"
                )
            finally:
                app.dependency_overrides.pop(get_optional_user, None)

            # Soft 404 — don't leak existence to cross-user probes
            assert response.status_code == 404
        finally:
            Path(tmp_path).unlink(missing_ok=True)

    @pytest.mark.backend
    async def test_paperless_accepts_owner(
        self, async_client: AsyncClient, db_session: AsyncSession, test_user
    ):
        """The owning user CAN forward their own upload (happy path)."""
        from main import app
        from models.database import Conversation
        from services.auth_service import get_optional_user

        conv = Conversation(session_id="sess-owner", user_id=test_user.id)
        db_session.add(conv)
        await db_session.commit()

        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
            f.write(b"%PDF-1.4 mine")
            tmp_path = f.name
        try:
            upload = ChatUpload(
                session_id="sess-owner",
                filename="mine.pdf",
                file_type="pdf",
                file_size=200,
                status="completed",
                file_path=tmp_path,
            )
            db_session.add(upload)
            await db_session.commit()
            await db_session.refresh(upload)

            import json
            mcp_result = {
                "success": True,
                "message": json.dumps({
                    "success": True,
                    "data": {"task_id": "t-1", "title": "mine.pdf", "filename": "mine.pdf"},
                }),
            }
            mock_manager = AsyncMock()
            mock_manager.execute_tool = AsyncMock(return_value=mcp_result)
            app.state.mcp_manager = mock_manager
            app.dependency_overrides[get_optional_user] = lambda: test_user
            try:
                response = await async_client.post(
                    f"/api/chat/upload/{upload.id}/paperless"
                )
            finally:
                app.state.mcp_manager = None
                app.dependency_overrides.pop(get_optional_user, None)

            assert response.status_code == 200
            assert response.json()["paperless_task_id"] == "t-1"
        finally:
            Path(tmp_path).unlink(missing_ok=True)

    @pytest.mark.backend
    async def test_anonymous_bypass_when_auth_disabled(
        self, async_client: AsyncClient, db_session: AsyncSession
    ):
        """Without an authenticated user (AUTH_ENABLED=false), the scoping
        filter is skipped — matches single-user dev-mode convention."""
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
            f.write(b"%PDF-1.4 solo")
            tmp_path = f.name
        try:
            upload = ChatUpload(
                session_id="sess-solo",
                filename="solo.pdf",
                file_type="pdf",
                file_size=200,
                status="completed",
                file_path=tmp_path,
            )
            db_session.add(upload)
            await db_session.commit()
            await db_session.refresh(upload)

            # No dep override → get_optional_user returns None by default in tests
            import json
            from main import app
            mcp_result = {
                "success": True,
                "message": json.dumps({
                    "success": True,
                    "data": {"task_id": "t-solo"},
                }),
            }
            mock_manager = AsyncMock()
            mock_manager.execute_tool = AsyncMock(return_value=mcp_result)
            app.state.mcp_manager = mock_manager
            try:
                response = await async_client.post(
                    f"/api/chat/upload/{upload.id}/paperless"
                )
            finally:
                app.state.mcp_manager = None
            assert response.status_code == 200
        finally:
            Path(tmp_path).unlink(missing_ok=True)


class TestChatUploadCleanup:
    """Tests for DELETE /api/chat/upload/cleanup and _cleanup_uploads"""

    @pytest.mark.backend
    async def test_cleanup_deletes_old_unindexed(self, async_client, db_session: AsyncSession):
        """Old uploads without document_id are deleted"""
        from datetime import datetime, timedelta

        old_upload = ChatUpload(
            session_id="cleanup-test",
            filename="old.txt",
            file_type="txt",
            file_size=100,
            status="completed",
            file_path="/tmp/nonexistent.txt",
        )
        db_session.add(old_upload)
        await db_session.commit()
        await db_session.refresh(old_upload)

        # Manually set created_at to 60 days ago
        old_upload.created_at = datetime.utcnow() - timedelta(days=60)
        await db_session.commit()

        from api.routes.chat_upload import _cleanup_uploads
        deleted_count, _deleted_files = await _cleanup_uploads(db_session, days=30)

        assert deleted_count == 1

    @pytest.mark.backend
    async def test_cleanup_preserves_indexed(self, async_client, db_session: AsyncSession):
        """Old uploads with document_id are preserved"""
        from datetime import datetime, timedelta

        indexed_upload = ChatUpload(
            session_id="cleanup-indexed-test",
            filename="indexed.txt",
            file_type="txt",
            file_size=100,
            status="completed",
            document_id=42,
        )
        db_session.add(indexed_upload)
        await db_session.commit()
        await db_session.refresh(indexed_upload)

        indexed_upload.created_at = datetime.utcnow() - timedelta(days=60)
        await db_session.commit()

        from api.routes.chat_upload import _cleanup_uploads
        deleted_count, _deleted_files = await _cleanup_uploads(db_session, days=30)

        assert deleted_count == 0

    @pytest.mark.backend
    async def test_cleanup_preserves_recent(self, async_client, db_session: AsyncSession):
        """Recent uploads are preserved regardless of index status"""
        recent_upload = ChatUpload(
            session_id="cleanup-recent-test",
            filename="recent.txt",
            file_type="txt",
            file_size=100,
            status="completed",
        )
        db_session.add(recent_upload)
        await db_session.commit()

        from api.routes.chat_upload import _cleanup_uploads
        deleted_count, _deleted_files = await _cleanup_uploads(db_session, days=30)

        assert deleted_count == 0

    @pytest.mark.backend
    async def test_cleanup_endpoint(self, async_client: AsyncClient, db_session: AsyncSession):
        """DELETE endpoint returns counts"""
        response = await async_client.delete("/api/chat/upload/cleanup?days=30")
        assert response.status_code == 200
        body = response.json()
        assert body["success"] is True
        assert "deleted_count" in body
        assert "deleted_files" in body

    @pytest.mark.backend
    async def test_cleanup_removes_files(self, async_client, db_session: AsyncSession):
        """Cleanup actually deletes files from disk"""
        from datetime import datetime, timedelta

        with tempfile.NamedTemporaryFile(suffix=".txt", delete=False) as f:
            f.write(b"cleanup test content")
            tmp_path = f.name

        try:
            upload = ChatUpload(
                session_id="cleanup-files-test",
                filename="deleteme.txt",
                file_type="txt",
                file_size=100,
                status="completed",
                file_path=tmp_path,
            )
            db_session.add(upload)
            await db_session.commit()
            await db_session.refresh(upload)

            upload.created_at = datetime.utcnow() - timedelta(days=60)
            await db_session.commit()

            assert Path(tmp_path).is_file()

            from api.routes.chat_upload import _cleanup_uploads
            deleted_count, deleted_files = await _cleanup_uploads(db_session, days=30)

            assert deleted_count == 1
            assert deleted_files == 1
            assert not Path(tmp_path).is_file()
        finally:
            Path(tmp_path).unlink(missing_ok=True)


# ============================================================================
# Phase 5: WebSocket Notification Registry Tests
# ============================================================================

class TestWSNotificationRegistry:
    """Tests for the WS connection registry in api.websocket.shared.

    The shared module imports WhisperService which chains to services.database
    (requires asyncpg). We pre-mock services.whisper_service in sys.modules
    so the import succeeds in local test environments without asyncpg.
    """

    @staticmethod
    def _ensure_shared():
        """Import shared module, pre-mocking heavy deps to avoid asyncpg requirement.

        Importing `api.websocket.shared` triggers `api/websocket/__init__.py`
        which imports chat_handler → services.database → asyncpg.  We mock
        the package __init__ out of the way so we can import shared directly.
        """
        import importlib
        import sys

        # Ensure the parent package exists without its __init__ side-effects
        if "api.websocket" not in sys.modules:
            pkg = type(sys)("api.websocket")
            pkg.__path__ = []  # make it a package
            sys.modules.setdefault("api", type(sys)("api"))
            sys.modules["api.websocket"] = pkg

        # Mock whisper_service to avoid openai-whisper dependency
        sys.modules.setdefault("services.whisper_service", MagicMock())

        # Import the shared module file directly
        if "api.websocket.shared" in sys.modules:
            return sys.modules["api.websocket.shared"]

        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "api.websocket.shared",
            "src/backend/api/websocket/shared.py",
        )
        mod = importlib.util.module_from_spec(spec)
        sys.modules["api.websocket.shared"] = mod
        spec.loader.exec_module(mod)
        return mod

    @pytest.mark.unit
    def test_register_and_unregister(self):
        """register + unregister round-trip works"""
        shared = self._ensure_shared()

        mock_ws = MagicMock()
        shared.register_ws_connection("sess-1", mock_ws)
        assert "sess-1" in shared._ws_connections

        shared.unregister_ws_connection("sess-1")
        assert "sess-1" not in shared._ws_connections

    @pytest.mark.unit
    def test_unregister_nonexistent_is_noop(self):
        """Unregistering a missing session_id does not raise"""
        shared = self._ensure_shared()
        # Should not raise
        shared.unregister_ws_connection("nonexistent-session")

    @pytest.mark.unit
    async def test_notify_session_sends_json(self):
        """notify_session calls send_json on the registered WS"""
        shared = self._ensure_shared()

        mock_ws = AsyncMock()
        shared.register_ws_connection("sess-notify", mock_ws)

        try:
            result = await shared.notify_session("sess-notify", {"type": "test", "data": 123})
            assert result is True
            mock_ws.send_json.assert_called_once_with({"type": "test", "data": 123})
        finally:
            shared.unregister_ws_connection("sess-notify")

    @pytest.mark.unit
    async def test_notify_session_unknown_returns_false(self):
        """notify_session returns False for unknown session"""
        shared = self._ensure_shared()

        result = await shared.notify_session("unknown-session", {"type": "test"})
        assert result is False

    @pytest.mark.unit
    async def test_notify_session_handles_closed_ws(self):
        """notify_session handles dead connection and auto-cleans"""
        shared = self._ensure_shared()

        mock_ws = AsyncMock()
        mock_ws.send_json.side_effect = RuntimeError("Connection closed")
        shared.register_ws_connection("sess-dead", mock_ws)

        result = await shared.notify_session("sess-dead", {"type": "test"})
        assert result is False
        assert "sess-dead" not in shared._ws_connections


# ============================================================================
# Phase 5: OCR Image Upload Support Tests
# ============================================================================

class TestOCRSupport:
    """Tests for PNG/JPG image upload support"""

    @pytest.fixture(autouse=True)
    def _allow_image_extensions(self, monkeypatch):
        """Ensure png/jpg/jpeg are accepted regardless of ambient .env.

        The upload route gates on ``settings.allowed_extensions_list``,
        derived from the ``ALLOWED_EXTENSIONS`` env var. The test
        container's .env omits the image extensions, so override the
        underlying setting to the full code-default list.
        """
        from utils.config import settings as _settings

        monkeypatch.setattr(
            _settings,
            "allowed_extensions",
            "pdf,docx,doc,txt,md,html,pptx,xlsx,png,jpg,jpeg",
        )

    @pytest.mark.backend
    async def test_upload_png_accepted(self, async_client: AsyncClient):
        """PNG upload is accepted"""
        content = b"\x89PNG\r\n\x1a\n fake png"
        files = {"file": ("photo.png", io.BytesIO(content), "image/png")}
        data = {"session_id": "ocr-png-session"}

        with patch("api.routes.chat_upload._get_processor") as mock_proc:
            processor = AsyncMock()
            processor.extract_text_only = AsyncMock(return_value="OCR text from image")
            mock_proc.return_value = processor

            response = await async_client.post("/api/chat/upload", files=files, data=data)

        assert response.status_code == 200
        body = response.json()
        assert body["filename"] == "photo.png"
        assert body["file_type"] == "png"
        assert body["status"] == "processing"  # async: OCR runs in background

    @pytest.mark.backend
    async def test_upload_jpg_accepted(self, async_client: AsyncClient):
        """JPG upload is accepted"""
        content = b"\xff\xd8\xff\xe0 fake jpeg"
        files = {"file": ("scan.jpg", io.BytesIO(content), "image/jpeg")}
        data = {"session_id": "ocr-jpg-session"}

        with patch("api.routes.chat_upload._get_processor") as mock_proc:
            processor = AsyncMock()
            processor.extract_text_only = AsyncMock(return_value="OCR text from scan")
            mock_proc.return_value = processor

            response = await async_client.post("/api/chat/upload", files=files, data=data)

        assert response.status_code == 200
        body = response.json()
        assert body["filename"] == "scan.jpg"
        assert body["file_type"] == "jpg"
        assert body["status"] == "processing"  # async: OCR runs in background


class TestChatUploadSimba:
    """Tests for the Simba tax-portal transfer endpoints."""

    @pytest.mark.backend
    async def test_simba_categories(self, async_client: AsyncClient):
        """GET categories returns the category→type map from the simba MCP."""
        import json as _json
        from unittest.mock import AsyncMock

        from main import app

        mock = AsyncMock()
        mock.execute_tool = AsyncMock(return_value={
            "success": True,
            "message": _json.dumps({"kategorien": {"Belege": ["Ausgangsrechnung", "Eingangsrechnung"]}}),
        })
        app.state.mcp_manager = mock
        try:
            resp = await async_client.get("/api/chat/upload/simba/categories")
        finally:
            app.state.mcp_manager = None
        assert resp.status_code == 200
        assert resp.json()["categories"]["Belege"] == ["Ausgangsrechnung", "Eingangsrechnung"]

    @pytest.mark.backend
    async def test_simba_categories_unavailable(self, async_client: AsyncClient):
        """No MCP → 503 (the UI hides the Simba menu)."""
        from main import app
        app.state.mcp_manager = None
        resp = await async_client.get("/api/chat/upload/simba/categories")
        assert resp.status_code == 503

    @pytest.mark.backend
    async def test_simba_upload_success(self, async_client: AsyncClient, db_session: AsyncSession):
        """A real transfer that lands (uebertragen>0) returns 200."""
        import json as _json
        from unittest.mock import AsyncMock

        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
            f.write(b"%PDF-1.4 test")
            tmp = f.name
        try:
            upload = ChatUpload(
                session_id="simba-test", filename="2026_07_14.pdf", file_type="pdf",
                file_size=500, status="completed", file_path=tmp,
            )
            db_session.add(upload)
            await db_session.commit()
            await db_session.refresh(upload)

            from main import app
            mock = AsyncMock()
            mock.execute_tool = AsyncMock(return_value={
                "success": True, "message": _json.dumps({"uebertragen": 1, "fehlgeschlagen": 0}),
            })
            app.state.mcp_manager = mock
            try:
                resp = await async_client.post(
                    f"/api/chat/upload/{upload.id}/simba",
                    json={"category": "Posteingang", "type": "Schriftverkehr", "month": 7, "year": 2026},
                )
            finally:
                app.state.mcp_manager = None
            assert resp.status_code == 200
            assert resp.json()["success"] is True
            # real upload was requested (dry_run false + confirm true + base64)
            args = mock.execute_tool.await_args.args[1]
            assert args["dry_run"] is False and args["confirm"] is True
            assert args["files"][0]["content_base64"]
        finally:
            Path(tmp).unlink(missing_ok=True)

    @pytest.mark.backend
    async def test_simba_upload_zero_transferred_is_502(self, async_client: AsyncClient, db_session: AsyncSession):
        """MCP call succeeds but 0 files landed → 502 (never a false success)."""
        import json as _json
        from unittest.mock import AsyncMock

        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
            f.write(b"%PDF-1.4 test")
            tmp = f.name
        try:
            upload = ChatUpload(
                session_id="simba-fail", filename="x.pdf", file_type="pdf",
                file_size=500, status="completed", file_path=tmp,
            )
            db_session.add(upload)
            await db_session.commit()
            await db_session.refresh(upload)

            from main import app
            mock = AsyncMock()
            mock.execute_tool = AsyncMock(return_value={
                "success": True,
                "message": _json.dumps({"uebertragen": 0, "fehlgeschlagen": 1,
                                        "ergebnisse": [{"ok": False, "status": 401, "response": "no"}]}),
            })
            app.state.mcp_manager = mock
            try:
                resp = await async_client.post(
                    f"/api/chat/upload/{upload.id}/simba",
                    json={"category": "Belege", "type": "x"},
                )
            finally:
                app.state.mcp_manager = None
            assert resp.status_code == 502
        finally:
            Path(tmp).unlink(missing_ok=True)

    @pytest.mark.backend
    async def test_simba_not_found(self, async_client: AsyncClient):
        resp = await async_client.post(
            "/api/chat/upload/99999/simba", json={"category": "Belege", "type": "x"},
        )
        assert resp.status_code == 404

    @pytest.mark.backend
    async def test_simba_suggest(self, async_client: AsyncClient, db_session: AsyncSession):
        """Suggest returns a content-derived category/type."""
        import json as _json
        from unittest.mock import AsyncMock, patch

        upload = ChatUpload(
            session_id="simba-suggest", filename="brief.pdf", file_type="pdf",
            file_size=500, status="completed", file_path="/tmp/none",
            extracted_text="Sehr geehrte Damen und Herren, anbei das Schreiben vom Finanzamt.",
        )
        db_session.add(upload)
        await db_session.commit()
        await db_session.refresh(upload)

        from main import app
        mock = AsyncMock()
        mock.execute_tool = AsyncMock(return_value={
            "success": True,
            "message": _json.dumps({"kategorien": {"Posteingang": ["Schriftverkehr"]}}),
        })
        app.state.mcp_manager = mock
        try:
            with patch("services.simba_classify.classify_simba",
                       AsyncMock(return_value=("Posteingang", "Schriftverkehr"))):
                resp = await async_client.get(f"/api/chat/upload/{upload.id}/simba/suggest")
        finally:
            app.state.mcp_manager = None
        assert resp.status_code == 200
        assert resp.json() == {"category": "Posteingang", "type": "Schriftverkehr"}

    @pytest.mark.backend
    async def test_simba_suggest_no_text(self, async_client: AsyncClient, db_session: AsyncSession):
        """No extracted text → empty suggestion (no classification)."""
        upload = ChatUpload(
            session_id="simba-suggest-empty", filename="x.pdf", file_type="pdf",
            file_size=1, status="completed", file_path="/tmp/none", extracted_text="",
        )
        db_session.add(upload)
        await db_session.commit()
        await db_session.refresh(upload)
        resp = await async_client.get(f"/api/chat/upload/{upload.id}/simba/suggest")
        assert resp.status_code == 200
        assert resp.json() == {"category": None, "type": None}
