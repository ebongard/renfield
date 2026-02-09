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
"""
import io
from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.database import ChatUpload
from models.websocket_messages import WSChatMessage

# ============================================================================
# API Tests
# ============================================================================

class TestChatUploadAPI:
    """Tests for POST /api/chat/upload"""

    @pytest.mark.backend
    async def test_upload_txt_file(self, async_client: AsyncClient):
        """TXT upload returns 200 with text_preview"""
        content = b"Hello, this is a test document with some content."
        files = {"file": ("test.txt", io.BytesIO(content), "text/plain")}
        data = {"session_id": "test-session-123"}

        with patch("api.routes.chat_upload._get_processor") as mock_proc:
            processor = AsyncMock()
            processor.extract_text_only = AsyncMock(return_value="Hello, this is a test document with some content.")
            mock_proc.return_value = processor

            response = await async_client.post("/api/chat/upload", files=files, data=data)

        assert response.status_code == 200
        body = response.json()
        assert body["filename"] == "test.txt"
        assert body["file_type"] == "txt"
        assert body["status"] == "completed"
        assert body["text_preview"] is not None
        assert "Hello" in body["text_preview"]

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
    async def test_upload_extraction_failure(self, async_client: AsyncClient):
        """When text extraction fails, status is 'failed' but HTTP 200"""
        content = b"some content"
        files = {"file": ("broken.txt", io.BytesIO(content), "text/plain")}
        data = {"session_id": "fail-session"}

        with patch("api.routes.chat_upload._get_processor") as mock_proc:
            processor = AsyncMock()
            processor.extract_text_only = AsyncMock(side_effect=RuntimeError("Docling crash"))
            mock_proc.return_value = processor

            response = await async_client.post("/api/chat/upload", files=files, data=data)

        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "failed"
        assert body["error_message"] is not None


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
