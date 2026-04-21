"""
Tests for ``services.chat_upload_tool.forward_attachment_to_paperless``.

The tool is the architectural fix for the "agent hallucinates base64" bug:
instead of the LLM inventing ``file_content_base64`` for
``mcp.paperless.upload_document`` (it has no access to real file bytes),
it passes an ``attachment_id`` and this tool reads the real bytes from
server storage and forwards them to Paperless via the MCP under the hood.
"""

from __future__ import annotations

import base64
import json
import sys
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path
from types import ModuleType
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services.chat_upload_tool import forward_attachment_to_paperless


def _stub_db_module() -> list[str]:
    """Ensure ``services.database`` is importable (asyncpg isn't installed in
    the minimal test env). Mirrors the pattern used in test_knowledge_tool.py.
    """
    added: list[str] = []
    for mod_name in ("services.database", "models.database"):
        if mod_name not in sys.modules:
            sys.modules[mod_name] = ModuleType(mod_name)
            added.append(mod_name)
    return added


def _teardown_stubs(added: list[str]) -> None:
    for mod_name in added:
        sys.modules.pop(mod_name, None)


def _make_upload(file_path: str | None, filename: str = "Invoice-001.pdf"):
    """Build a stand-in for a ``ChatUpload`` ORM row."""
    upload = MagicMock()
    upload.id = 42
    upload.filename = filename
    upload.file_path = file_path
    return upload


def _mock_db_returning(upload):
    """Mock AsyncSessionLocal + query result returning the given upload (or None)."""
    mock_db = AsyncMock()
    mock_result = MagicMock()
    mock_result.scalar_one_or_none = MagicMock(return_value=upload)
    mock_db.execute = AsyncMock(return_value=mock_result)

    @asynccontextmanager
    async def mock_session():
        yield mock_db

    return mock_session


class TestForwardAttachmentToPaperless:

    @pytest.mark.unit
    async def test_missing_attachment_id(self):
        """Required attachment_id triggers a clear error, no DB/MCP access."""
        result = await forward_attachment_to_paperless({}, mcp_manager=MagicMock())
        assert result["success"] is False
        assert "attachment_id" in result["message"]

    @pytest.mark.unit
    async def test_non_integer_attachment_id(self):
        """Non-integer attachment_id is rejected with a message including the bad value."""
        result = await forward_attachment_to_paperless(
            {"attachment_id": "not-a-number"}, mcp_manager=MagicMock()
        )
        assert result["success"] is False
        assert "integer" in result["message"].lower()

    @pytest.mark.unit
    async def test_missing_mcp_manager(self):
        """Without an MCP manager the tool cannot reach Paperless — fail fast."""
        result = await forward_attachment_to_paperless(
            {"attachment_id": 42}, mcp_manager=None
        )
        assert result["success"] is False
        assert "mcp" in result["message"].lower()

    @pytest.mark.unit
    async def test_attachment_not_found(self):
        """Unknown attachment_id returns a not-found error."""
        stubs = _stub_db_module()
        try:
            with patch(
                "services.database.AsyncSessionLocal",
                _mock_db_returning(None),
                create=True,
            ), patch("models.database.ChatUpload", MagicMock(), create=True):
                result = await forward_attachment_to_paperless(
                    {"attachment_id": 999}, mcp_manager=AsyncMock()
                )
        finally:
            _teardown_stubs(stubs)
        assert result["success"] is False
        assert "999" in result["message"]

    @pytest.mark.unit
    async def test_file_missing_on_disk(self):
        """ChatUpload row without a valid file_path returns a clear error."""
        upload = _make_upload(file_path="/tmp/does-not-exist-123.pdf")
        stubs = _stub_db_module()
        try:
            with patch(
                "services.database.AsyncSessionLocal",
                _mock_db_returning(upload),
                create=True,
            ), patch("models.database.ChatUpload", MagicMock(), create=True):
                result = await forward_attachment_to_paperless(
                    {"attachment_id": 42}, mcp_manager=AsyncMock()
                )
        finally:
            _teardown_stubs(stubs)
        assert result["success"] is False
        assert "disk" in result["message"].lower()

    @pytest.mark.unit
    async def test_happy_path_calls_mcp_with_real_bytes(self):
        """Valid attachment → real file bytes base64-encoded → MCP call carries them."""
        pdf_bytes = b"%PDF-1.4\n" + b"x" * 200  # >= 100 bytes (MCP size floor)
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
            f.write(pdf_bytes)
            tmp_path = f.name

        try:
            upload = _make_upload(file_path=tmp_path, filename="Invoice-001.pdf")

            mock_mcp = AsyncMock()
            mock_mcp.execute_tool = AsyncMock(return_value={
                "success": True,
                "message": json.dumps({
                    "task_id": "abc-123",
                    "title": "Invoice-001.pdf",
                    "filename": "Invoice-001.pdf",
                }),
            })

            stubs = _stub_db_module()
            try:
                with patch(
                    "services.database.AsyncSessionLocal",
                    _mock_db_returning(upload),
                    create=True,
                ), patch("models.database.ChatUpload", MagicMock(), create=True):
                    result = await forward_attachment_to_paperless(
                        {"attachment_id": 42, "correspondent": "ACME"},
                        mcp_manager=mock_mcp,
                    )
            finally:
                _teardown_stubs(stubs)

            assert result["success"] is True
            assert result["data"]["task_id"] == "abc-123"
            assert result["data"]["filename"] == "Invoice-001.pdf"
            assert result["data"]["attachment_id"] == 42

            # MCP was called with the REAL base64, not a placeholder
            mock_mcp.execute_tool.assert_called_once()
            call_args = mock_mcp.execute_tool.call_args
            assert call_args.args[0] == "mcp.paperless.upload_document"
            sent_params = call_args.args[1]
            decoded = base64.b64decode(sent_params["file_content_base64"])
            assert decoded == pdf_bytes
            assert sent_params["filename"] == "Invoice-001.pdf"
            assert sent_params["correspondent"] == "ACME"
        finally:
            Path(tmp_path).unlink(missing_ok=True)

    @pytest.mark.unit
    async def test_title_override(self):
        """When the agent passes a title, it overrides the filename default."""
        pdf_bytes = b"%PDF-1.4\n" + b"y" * 200
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
            f.write(pdf_bytes)
            tmp_path = f.name

        try:
            upload = _make_upload(file_path=tmp_path, filename="raw.pdf")
            mock_mcp = AsyncMock()
            mock_mcp.execute_tool = AsyncMock(return_value={
                "success": True,
                "message": json.dumps({"task_id": "t-1"}),
            })

            stubs = _stub_db_module()
            try:
                with patch(
                    "services.database.AsyncSessionLocal",
                    _mock_db_returning(upload),
                    create=True,
                ), patch("models.database.ChatUpload", MagicMock(), create=True):
                    await forward_attachment_to_paperless(
                        {"attachment_id": 42, "title": "Invoice January"},
                        mcp_manager=mock_mcp,
                    )
            finally:
                _teardown_stubs(stubs)

            sent_params = mock_mcp.execute_tool.call_args.args[1]
            assert sent_params["title"] == "Invoice January"
            assert sent_params["filename"] == "raw.pdf"
        finally:
            Path(tmp_path).unlink(missing_ok=True)

    @pytest.mark.unit
    async def test_mcp_failure_surfaces_detail(self):
        """When the MCP call fails, the detail is propagated to the agent."""
        pdf_bytes = b"%PDF-1.4\n" + b"z" * 200
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
            f.write(pdf_bytes)
            tmp_path = f.name

        try:
            upload = _make_upload(file_path=tmp_path)
            mock_mcp = AsyncMock()
            mock_mcp.execute_tool = AsyncMock(return_value={
                "success": False,
                "message": "Invalid base64 content.",
            })

            stubs = _stub_db_module()
            try:
                with patch(
                    "services.database.AsyncSessionLocal",
                    _mock_db_returning(upload),
                    create=True,
                ), patch("models.database.ChatUpload", MagicMock(), create=True):
                    result = await forward_attachment_to_paperless(
                        {"attachment_id": 42}, mcp_manager=mock_mcp
                    )
            finally:
                _teardown_stubs(stubs)

            assert result["success"] is False
            assert "Invalid base64" in result["message"]
        finally:
            Path(tmp_path).unlink(missing_ok=True)

    @pytest.mark.unit
    async def test_malformed_mcp_message_still_succeeds_with_null_task_id(self):
        """MCP returns success but the inner message isn't parseable JSON — we
        should still report success (the upload went through) with task_id=None."""
        pdf_bytes = b"%PDF-1.4\n" + b"q" * 200
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
            f.write(pdf_bytes)
            tmp_path = f.name

        try:
            upload = _make_upload(file_path=tmp_path)
            mock_mcp = AsyncMock()
            mock_mcp.execute_tool = AsyncMock(return_value={
                "success": True,
                "message": "not json at all",
            })

            stubs = _stub_db_module()
            try:
                with patch(
                    "services.database.AsyncSessionLocal",
                    _mock_db_returning(upload),
                    create=True,
                ), patch("models.database.ChatUpload", MagicMock(), create=True):
                    result = await forward_attachment_to_paperless(
                        {"attachment_id": 42}, mcp_manager=mock_mcp
                    )
            finally:
                _teardown_stubs(stubs)

            assert result["success"] is True
            assert result["data"]["task_id"] is None
        finally:
            Path(tmp_path).unlink(missing_ok=True)
