"""
Tests for Chat Upload API

Tests:
- File upload with text extraction
- Invalid format rejection
- File size limit
- Missing session_id validation
- DB entry creation
- Duplicate uploads allowed (no 409)
"""
import io
from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.database import ChatUpload

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
