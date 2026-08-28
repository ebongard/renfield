"""
Unit tests for `internal.forward_attachment_to_simba` — the chat-attachment →
Simba tax-portal bridge.

Guarantees: it resolves the ChatUpload (by id or session fallback), reads the
real bytes and hands them to mcp.simba.upload_documents as base64 (the LLM never
supplies bytes), requires category/type, defaults to a dry-run, and passes the
dry_run/confirm safety flags through to the simba server.
"""
import base64
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, mock_open, patch

import pytest

from services.simba_forward_tool import SIMBA_FORWARD_TOOL, forward_attachment_to_simba

pytestmark = pytest.mark.unit

FILE_BYTES = b"%PDF-1.4 hello"


def _upload(uid=5, filename="invoice.pdf", path="/data/x.pdf", status="completed"):
    u = MagicMock()
    u.id = uid
    u.filename = filename
    u.file_path = path
    u.status = status
    return u


def _mcp(result=None):
    m = MagicMock()
    m.execute_tool = AsyncMock(return_value=result or {"success": True, "message": "OK"})
    return m


def _db_patch(upload):
    """Patch AsyncSessionLocal so any query resolves to `upload` (execute mocked;
    the real select(ChatUpload) statement is built but never run)."""
    db = MagicMock()
    result = MagicMock()
    result.scalar_one_or_none = MagicMock(return_value=upload)
    db.execute = AsyncMock(return_value=result)

    @asynccontextmanager
    async def _session(*_a, **_k):
        yield db

    return patch("services.database.AsyncSessionLocal", lambda *a, **k: _session())


# ---------------------------------------------------------------------------

def test_tool_definition_shape():
    assert "internal.forward_attachment_to_simba" in SIMBA_FORWARD_TOOL
    params = SIMBA_FORWARD_TOOL["internal.forward_attachment_to_simba"]["parameters"]
    assert {"category", "type", "attachment_id", "dry_run", "confirm"} <= set(params)


@pytest.mark.asyncio
async def test_no_mcp_manager():
    r = await forward_attachment_to_simba({"category": "Belege", "type": "x"}, mcp_manager=None)
    assert r["success"] is False and r["action_taken"] is False


@pytest.mark.asyncio
async def test_requires_category_and_type():
    r = await forward_attachment_to_simba({"category": "Belege"}, mcp_manager=_mcp(), session_id="s")
    assert r["success"] is False
    assert "type" in r["message"]


@pytest.mark.asyncio
async def test_happy_path_dryrun_default_passes_base64():
    upload = _upload()
    mcp = _mcp({"success": True, "message": '{"modus":"Probelauf"}'})
    p_isfile, p_open = patch("pathlib.Path.is_file", return_value=True), patch(
        "builtins.open", mock_open(read_data=FILE_BYTES)
    )
    with _db_patch(upload), p_isfile, p_open:
        r = await forward_attachment_to_simba(
            {"category": "Belege", "type": "Ausgangsrechnung"},
            mcp_manager=mcp, session_id="sess", user_id=3,
        )
    assert r["success"] is True
    assert r["action_taken"] is False  # dry-run default → nothing sent
    args = mcp.execute_tool.await_args.args
    assert args[0] == "mcp.simba.upload_documents"
    payload = mcp.execute_tool.await_args.args[1]
    assert payload["dry_run"] is True and payload["confirm"] is False
    assert payload["category"] == "Belege" and payload["type"] == "Ausgangsrechnung"
    f0 = payload["files"][0]
    assert f0["filename"] == "invoice.pdf"
    assert f0["content_base64"] == base64.b64encode(FILE_BYTES).decode("ascii")


@pytest.mark.asyncio
async def test_real_upload_passes_confirm_and_marks_action():
    upload = _upload()
    mcp = _mcp({"success": True, "message": '{"uebertragen":1}'})
    with _db_patch(upload), patch("pathlib.Path.is_file", return_value=True), patch(
        "builtins.open", mock_open(read_data=FILE_BYTES)
    ):
        r = await forward_attachment_to_simba(
            {"category": "Belege", "type": "Ausgangsrechnung", "dry_run": False, "confirm": True},
            mcp_manager=mcp, session_id="sess",
        )
    payload = mcp.execute_tool.await_args.args[1]
    assert payload["dry_run"] is False and payload["confirm"] is True
    assert r["action_taken"] is True


@pytest.mark.asyncio
async def test_llm_supplied_content_base64_is_ignored():
    """The agent can't smuggle bytes — the tool reads them from disk itself."""
    upload = _upload()
    mcp = _mcp()
    with _db_patch(upload), patch("pathlib.Path.is_file", return_value=True), patch(
        "builtins.open", mock_open(read_data=FILE_BYTES)
    ):
        await forward_attachment_to_simba(
            {"category": "Belege", "type": "x", "content_base64": "PLACEHOLDER", "files": []},
            mcp_manager=mcp, session_id="sess",
        )
    f0 = mcp.execute_tool.await_args.args[1]["files"][0]
    assert f0["content_base64"] == base64.b64encode(FILE_BYTES).decode("ascii")


@pytest.mark.asyncio
async def test_no_upload_found():
    mcp = _mcp()
    with _db_patch(None):
        r = await forward_attachment_to_simba(
            {"category": "Belege", "type": "x"}, mcp_manager=mcp, session_id="sess",
        )
    assert r["success"] is False and r["action_taken"] is False
    mcp.execute_tool.assert_not_awaited()


@pytest.mark.asyncio
async def test_file_missing_on_disk():
    upload = _upload()
    mcp = _mcp()
    with _db_patch(upload), patch("pathlib.Path.is_file", return_value=False):
        r = await forward_attachment_to_simba(
            {"category": "Belege", "type": "x"}, mcp_manager=mcp, session_id="sess",
        )
    assert r["success"] is False
    mcp.execute_tool.assert_not_awaited()


@pytest.mark.asyncio
async def test_mcp_failure_relayed():
    upload = _upload()
    mcp = _mcp({"success": False, "message": "portal down"})
    with _db_patch(upload), patch("pathlib.Path.is_file", return_value=True), patch(
        "builtins.open", mock_open(read_data=FILE_BYTES)
    ):
        r = await forward_attachment_to_simba(
            {"category": "Belege", "type": "x"}, mcp_manager=mcp, session_id="sess",
        )
    assert r["success"] is False and "portal down" in r["message"]
