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


def _make_upload(
    file_path: str | None,
    filename: str = "Invoice-001.pdf",
    session_id: str | None = "session-abc",
):
    """Build a stand-in for a ``ChatUpload`` ORM row."""
    upload = MagicMock()
    upload.id = 42
    upload.filename = filename
    upload.file_path = file_path
    upload.session_id = session_id
    return upload


def _mock_db_returning(upload, captured_query_holder: list | None = None):
    """Mock AsyncSessionLocal + query result returning the given upload (or None).

    When ``captured_query_holder`` is a list, the executed ``select`` statement
    is appended to it so tests can assert on the resulting WHERE clause.
    """
    mock_db = AsyncMock()
    mock_result = MagicMock()
    mock_result.scalar_one_or_none = MagicMock(return_value=upload)

    async def _capture_execute(stmt):
        if captured_query_holder is not None:
            captured_query_holder.append(stmt)
        return mock_result

    mock_db.execute = _capture_execute

    @asynccontextmanager
    async def mock_session():
        yield mock_db

    return mock_session


class TestForwardAttachmentToPaperless:

    @pytest.mark.unit
    async def test_no_id_no_session_returns_no_document(self):
        """No attachment_id AND no session to fall back on → clear no-document
        message, no DB/MCP access (nothing to resolve)."""
        result = await forward_attachment_to_paperless({}, mcp_manager=MagicMock())
        assert result["success"] is False
        assert "dokument" in result["message"].lower()

    @pytest.mark.unit
    async def test_non_integer_attachment_id_without_session(self):
        """A non-integer id is treated as 'no usable id' (a hint, not a hard
        requirement); with no session to fall back on, returns the no-document
        message instead of a type error."""
        result = await forward_attachment_to_paperless(
            {"attachment_id": "not-a-number"}, mcp_manager=MagicMock()
        )
        assert result["success"] is False
        assert "dokument" in result["message"].lower()

    @pytest.mark.unit
    async def test_missing_mcp_manager(self):
        """Without an MCP manager the tool cannot reach Paperless — fail fast."""
        result = await forward_attachment_to_paperless(
            {"attachment_id": 42}, mcp_manager=None
        )
        assert result["success"] is False
        assert "mcp" in result["message"].lower()

    @pytest.mark.unit
    async def test_unknown_id_and_empty_session_returns_no_document(self):
        """Unknown id AND no completed upload in the session → clear no-document
        message (no leak of the fabricated/guessed id)."""
        stubs = _stub_db_module()
        try:
            with patch(
                "services.database.AsyncSessionLocal",
                _mock_db_returning(None),
                create=True,
            ):
                result = await forward_attachment_to_paperless(
                    {"attachment_id": 999},
                    mcp_manager=AsyncMock(),
                    session_id="session-abc",
                )
        finally:
            _teardown_stubs(stubs)
        assert result["success"] is False
        assert "dokument" in result["message"].lower()

    @pytest.mark.unit
    async def test_session_fallback_resolves_latest_upload(self):
        """No attachment_id in the message (agent forwards as a follow-up) → the
        tool resolves the most recent completed upload in the session instead of
        failing or making the agent guess an id."""
        pdf_bytes = b"%PDF-1.4\n" + b"f" * 200
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
            f.write(pdf_bytes)
            tmp_path = f.name
        try:
            upload = _make_upload(
                file_path=tmp_path, filename="Fallback.pdf", session_id="session-abc"
            )
            mock_mcp = AsyncMock()
            mock_mcp.execute_tool = AsyncMock(return_value={
                "success": True,
                "message": json.dumps({"task_id": "fb-1"}),
            })
            stubs = _stub_db_module()
            try:
                with patch(
                    "services.database.AsyncSessionLocal",
                    _mock_db_returning(upload),
                    create=True,
                ):
                    result = await forward_attachment_to_paperless(
                        {"skip_metadata": True},  # no attachment_id passed
                        mcp_manager=mock_mcp,
                        session_id="session-abc",
                    )
            finally:
                _teardown_stubs(stubs)
            assert result["success"] is True
            assert result["data"]["attachment_id"] == 42
        finally:
            Path(tmp_path).unlink(missing_ok=True)

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
            ):
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
                ):
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
                ):
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
                ):
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
    async def test_session_scope_filters_query(self):
        """When session_id is passed, the DB query must include a
        ``session_id ==`` clause to scope the lookup to the user's own
        conversation. Without scoping the agent could reference
        attachments from other sessions just by guessing the integer id.
        """
        # Upload is "found" (non-None) but we only care that the query
        # carried the scoping clause — the tool will go on to try a
        # filesystem read and fail, which is fine for this assertion.
        upload = _make_upload(file_path="/tmp/nonexistent-scope-test")
        captured: list = []
        stubs = _stub_db_module()
        try:
            with patch(
                "services.database.AsyncSessionLocal",
                _mock_db_returning(upload, captured_query_holder=captured),
                create=True,
            ):
                await forward_attachment_to_paperless(
                    {"attachment_id": 42},
                    mcp_manager=AsyncMock(),
                    session_id="session-abc",
                )
        finally:
            _teardown_stubs(stubs)
        assert len(captured) == 1, "expected exactly one DB query"
        # The literal-compiled SQL exposes both SELECT projection and WHERE
        # clauses. ``session_id`` appears in the SELECT list of every query
        # (it's a column), so we count occurrences: an unscoped query has
        # 1 mention (projection), a scoped query has 2 (projection + WHERE).
        from sqlalchemy.dialects import sqlite
        compiled = str(captured[0].compile(
            compile_kwargs={"literal_binds": True},
            dialect=sqlite.dialect(),
        ))
        assert compiled.count("session_id") >= 2, (
            f"query must include session_id in WHERE clause when scoped; got:\n{compiled}"
        )

    @pytest.mark.unit
    async def test_session_scope_none_skips_filter(self):
        """Backwards-compat: without session_id, no scoping filter is added.

        Covers legacy call sites and single-user dev setups where auth is off.
        """
        upload = _make_upload(file_path="/tmp/nonexistent-no-scope")
        captured: list = []
        stubs = _stub_db_module()
        try:
            with patch(
                "services.database.AsyncSessionLocal",
                _mock_db_returning(upload, captured_query_holder=captured),
                create=True,
            ):
                await forward_attachment_to_paperless(
                    {"attachment_id": 42},
                    mcp_manager=AsyncMock(),
                    # no session_id
                )
        finally:
            _teardown_stubs(stubs)
        assert len(captured) == 1
        from sqlalchemy.dialects import sqlite
        compiled = str(captured[0].compile(
            compile_kwargs={"literal_binds": True},
            dialect=sqlite.dialect(),
        ))
        # session_id appears in the SELECT projection of every query (it's
        # a column). Scoping adds it to the WHERE clause, doubling the count.
        # Without scoping we expect exactly 1 mention.
        assert compiled.count("session_id") == 1, (
            f"no session scoping should be added when session_id is None; got:\n{compiled}"
        )

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
                ):
                    result = await forward_attachment_to_paperless(
                        {"attachment_id": 42}, mcp_manager=mock_mcp
                    )
            finally:
                _teardown_stubs(stubs)

            assert result["success"] is True
            assert result["data"]["task_id"] is None
        finally:
            Path(tmp_path).unlink(missing_ok=True)


# ===========================================================================
# Tool registry — both halves of the cold-start confirm flow must register
# ===========================================================================


@pytest.mark.unit
def test_chat_upload_tools_declares_both_confirm_flow_steps():
    """Regression for the prod 'Unbekanntes Tool: internal.paperless_commit_upload'
    error. The cold-start confirm flow has TWO steps:

      1. internal.forward_attachment_to_paperless (creates pending row)
      2. internal.paperless_commit_upload (reads the pending row + commits)

    action_executor.py dispatches both, but the agent's tool registry
    builds its tool list from CHAT_UPLOAD_TOOLS. Step 2 was missing
    from the registry, so the agent never saw it as a valid action and
    the flow got stuck at "Unbekanntes Tool" after step 1's preview.

    This test fails fast if either tool is removed from CHAT_UPLOAD_TOOLS.
    """
    from services.chat_upload_tool import CHAT_UPLOAD_TOOLS

    assert "internal.forward_attachment_to_paperless" in CHAT_UPLOAD_TOOLS, (
        "forward_attachment_to_paperless missing from CHAT_UPLOAD_TOOLS"
    )
    assert "internal.paperless_commit_upload" in CHAT_UPLOAD_TOOLS, (
        "paperless_commit_upload missing from CHAT_UPLOAD_TOOLS — "
        "the agent will fail with 'Unbekanntes Tool' after the user "
        "replies to a paperless_confirm preview."
    )

    commit = CHAT_UPLOAD_TOOLS["internal.paperless_commit_upload"]
    # The two parameters the action_executor reads from `params`:
    # confirm_token (required) and user_response_text.
    assert "confirm_token" in commit["parameters"]
    assert "user_response_text" in commit["parameters"]


# ── cold-start confirm counter: single-user (AUTH-off) global key ──────────


class TestColdStartConfirmCounter:
    """In single-user mode (user_id=None) the 'trust after N' counter is tracked
    in a global SystemSetting key instead of users.paperless_confirms_used, so
    the confirm doesn't fire on every archive forever."""

    @pytest.mark.unit
    async def test_bump_singleuser_creates_then_increments_setting(self):
        from types import SimpleNamespace

        from services.chat_upload_tool import _SINGLEUSER_CONFIRMS_KEY, _bump_confirms_used

        # No row yet → create with value 1.
        db = MagicMock()
        db.get = AsyncMock(return_value=None)
        db.add = MagicMock()
        db.execute = AsyncMock()
        await _bump_confirms_used(db, None)
        added = db.add.call_args.args[0]
        assert added.key == _SINGLEUSER_CONFIRMS_KEY
        assert json.loads(added.value) == 1
        db.execute.assert_not_awaited()  # single-user path doesn't touch User

        # Existing row → increment in place.
        row = SimpleNamespace(key=_SINGLEUSER_CONFIRMS_KEY, value=json.dumps(4))
        db2 = MagicMock()
        db2.get = AsyncMock(return_value=row)
        db2.add = MagicMock()
        await _bump_confirms_used(db2, None)
        assert json.loads(row.value) == 5
        db2.add.assert_not_called()

    @pytest.mark.unit
    async def test_bump_per_user_updates_user_row(self):
        from services.chat_upload_tool import _bump_confirms_used

        db = MagicMock()
        db.execute = AsyncMock()
        db.add = MagicMock()
        db.get = AsyncMock()
        await _bump_confirms_used(db, 7)
        db.execute.assert_awaited_once()  # update(User)... issued
        db.add.assert_not_called()
        db.get.assert_not_called()

    @pytest.mark.unit
    async def test_get_singleuser_reads_setting(self, monkeypatch):
        from types import SimpleNamespace

        from services.chat_upload_tool import _SINGLEUSER_CONFIRMS_KEY, _get_confirms_used

        db = MagicMock()
        db.get = AsyncMock(return_value=SimpleNamespace(
            key=_SINGLEUSER_CONFIRMS_KEY, value=json.dumps(9),
        ))

        @asynccontextmanager
        async def _sess():
            yield db

        monkeypatch.setattr("services.database.AsyncSessionLocal", _sess, raising=False)
        assert await _get_confirms_used(None) == 9

    @pytest.mark.unit
    async def test_get_singleuser_zero_when_no_setting(self, monkeypatch):
        from services.chat_upload_tool import _get_confirms_used

        db = MagicMock()
        db.get = AsyncMock(return_value=None)

        @asynccontextmanager
        async def _sess():
            yield db

        monkeypatch.setattr("services.database.AsyncSessionLocal", _sess, raising=False)
        assert await _get_confirms_used(None) == 0
