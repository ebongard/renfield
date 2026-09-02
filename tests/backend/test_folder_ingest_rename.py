"""Tests for the post-ingest processed-file rename (#881).

After the worker synthesizes ``documents.generated_title`` for a folder-ingest
doc, a best-effort hook renames the already-moved archive copy in the share's
``processed/`` dir to that human title via ``mcp.files.rename_processed``.

Everything here is unit-mocked: the flag, the MCP manager, and the source/title
inputs. The MCP is never actually reached.
"""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

import services.folder_ingest_rename as rename_mod
from models.database import FOLDER_INGEST_SOURCE
from services.folder_ingest_rename import rename_processed_to_title, sanitize_smb_filename

pytestmark = [pytest.mark.unit]


# ---------------------------------------------------------------- sanitization

@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Rechnung der PVS Rhein-Ruhr GmbH vom 18.05.2026", "Rechnung der PVS Rhein-Ruhr GmbH vom 18.05.2026"),
        ('a/b\\c:d*e?f"g<h>i|j', "a b c d e f g h i j"),
        ("  lots   of\t whitespace  ", "lots of whitespace"),
        ("trailing dots...", "trailing dots"),
        (".leading dots.", "leading dots"),
        ("///", ""),      # nothing usable → empty
        ("", ""),
    ],
)
def test_sanitize_smb_filename(raw, expected):
    assert sanitize_smb_filename(raw) == expected


def test_sanitize_caps_length():
    out = sanitize_smb_filename("x" * 500)
    assert len(out) == 150


# ------------------------------------------------------------------- the hook

def _mgr():
    mgr = AsyncMock()
    mgr.execute_tool = AsyncMock(return_value={"success": True})
    return mgr


@pytest.mark.asyncio
async def test_calls_mcp_when_enabled_folder_ingest_with_title(monkeypatch):
    monkeypatch.setattr(rename_mod.settings, "folder_ingest_rename_processed_enabled", True)
    mgr = _mgr()
    ok = await rename_processed_to_title(
        source=FOLDER_INGEST_SOURCE,
        filename="2026_03_29_14_33_13.pdf",
        generated_title='Rechnung: PVS/Rhein vom 18.05.2026',
        mcp_manager=mgr,
    )
    assert ok is True
    mgr.execute_tool.assert_awaited_once()
    name, args = mgr.execute_tool.await_args.args
    assert name == "mcp.files.rename_processed"
    # original filename passed through; title sanitized (: and / scrubbed).
    assert args == {
        "original_name": "2026_03_29_14_33_13.pdf",
        "new_base": "Rechnung PVS Rhein vom 18.05.2026",
    }


@pytest.mark.asyncio
async def test_no_call_when_flag_off(monkeypatch):
    monkeypatch.setattr(rename_mod.settings, "folder_ingest_rename_processed_enabled", False)
    mgr = _mgr()
    ok = await rename_processed_to_title(
        source=FOLDER_INGEST_SOURCE, filename="x.pdf", generated_title="Title", mcp_manager=mgr
    )
    assert ok is False
    mgr.execute_tool.assert_not_awaited()


@pytest.mark.asyncio
async def test_no_call_when_not_folder_ingest(monkeypatch):
    monkeypatch.setattr(rename_mod.settings, "folder_ingest_rename_processed_enabled", True)
    mgr = _mgr()
    ok = await rename_processed_to_title(
        source="upload", filename="x.pdf", generated_title="Title", mcp_manager=mgr
    )
    assert ok is False
    mgr.execute_tool.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("title", [None, "", "   "])
async def test_no_call_when_no_title(monkeypatch, title):
    monkeypatch.setattr(rename_mod.settings, "folder_ingest_rename_processed_enabled", True)
    mgr = _mgr()
    ok = await rename_processed_to_title(
        source=FOLDER_INGEST_SOURCE, filename="x.pdf", generated_title=title, mcp_manager=mgr
    )
    assert ok is False
    mgr.execute_tool.assert_not_awaited()


@pytest.mark.asyncio
async def test_no_call_when_title_sanitizes_to_empty(monkeypatch):
    monkeypatch.setattr(rename_mod.settings, "folder_ingest_rename_processed_enabled", True)
    mgr = _mgr()
    ok = await rename_processed_to_title(
        source=FOLDER_INGEST_SOURCE, filename="x.pdf", generated_title="///", mcp_manager=mgr
    )
    assert ok is False
    mgr.execute_tool.assert_not_awaited()


@pytest.mark.asyncio
async def test_no_call_when_no_filename(monkeypatch):
    monkeypatch.setattr(rename_mod.settings, "folder_ingest_rename_processed_enabled", True)
    mgr = _mgr()
    ok = await rename_processed_to_title(
        source=FOLDER_INGEST_SOURCE, filename="", generated_title="Title", mcp_manager=mgr
    )
    assert ok is False
    mgr.execute_tool.assert_not_awaited()


@pytest.mark.asyncio
async def test_mcp_exception_does_not_propagate(monkeypatch):
    """A rename failure must NEVER break ingest — the helper swallows + logs."""
    monkeypatch.setattr(rename_mod.settings, "folder_ingest_rename_processed_enabled", True)
    mgr = _mgr()
    mgr.execute_tool = AsyncMock(side_effect=RuntimeError("MCP down"))
    ok = await rename_processed_to_title(
        source=FOLDER_INGEST_SOURCE, filename="x.pdf", generated_title="Title", mcp_manager=mgr
    )
    assert ok is False  # swallowed, no raise
    mgr.execute_tool.assert_awaited_once()


@pytest.mark.asyncio
async def test_no_manager_available_is_noop(monkeypatch):
    """Flag on + valid inputs but the worker MCP client returns None → no crash."""
    monkeypatch.setattr(rename_mod.settings, "folder_ingest_rename_processed_enabled", True)
    # The helper imports get_files_mcp_manager lazily from this module; patch it
    # to return None (filesystem MCP unavailable in the worker).
    import services.files_worker_client as fwc

    monkeypatch.setattr(fwc, "get_files_mcp_manager", AsyncMock(return_value=None))
    ok = await rename_processed_to_title(
        source=FOLDER_INGEST_SOURCE, filename="x.pdf", generated_title="Title"
    )
    assert ok is False
