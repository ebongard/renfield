"""Tests for internal.paperless_dedupe — exact-duplicate finder + remover."""
import json

import pytest

from services.paperless_dedupe_tool import paperless_dedupe


def _make_mcp(docs, contents):
    """Build a mock mcp_manager whose execute_tool dispatches the three paperless
    tools. ``docs`` = search_documents results; ``contents`` = {id: ocr_text}.
    Records every delete_document id in ``.deleted``."""

    class _MCP:
        def __init__(self):
            self.deleted: list[int] = []
            self.get_document_kwargs: list[dict] = []

        async def execute_tool(self, tool, params, **_kw):
            if tool == "mcp.paperless.search_documents":
                return {"success": True, "message": json.dumps({"results": docs})}
            if tool == "mcp.paperless.get_document":
                self.get_document_kwargs.append(_kw)
                did = params["document_id"]
                return {"success": True, "message": json.dumps({"content": contents.get(did)})}
            if tool == "mcp.paperless.delete_document":
                did = params["document_id"]
                self.deleted.append(did)
                return {"success": True, "message": json.dumps({"deleted": True, "id": did})}
            return {"success": True, "message": "{}"}

    return _MCP()


_META = {"correspondent": "A", "document_type": "Rechnung", "created": "2024-01-15T00:00:00Z", "title": "Rechnung Jan"}


def _doc(did, **over):
    return {"id": did, **_META, **over}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_deletes_exact_duplicates_keeps_oldest():
    docs = [_doc(1), _doc(2), _doc(3)]
    contents = {1: "INVOICE BODY", 2: "INVOICE BODY", 3: "INVOICE BODY"}
    mcp = _make_mcp(docs, contents)

    result = await paperless_dedupe({}, mcp_manager=mcp)

    assert result["success"] is True
    assert result["action_taken"] is True
    assert sorted(mcp.deleted) == [2, 3]  # kept the lowest id (1)
    assert result["data"]["groups"] == 1
    assert result["data"]["deleted"] == 2
    assert result["data"]["kept_ids"] == [1]
    # Identity check MUST compare the FULL OCR text → get_document with truncate=False.
    assert mcp.get_document_kwargs, "get_document was never called"
    assert all(kw.get("truncate") is False for kw in mcp.get_document_kwargs)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_non_identical_content_not_deleted():
    """Same metadata but different OCR content → NOT a duplicate, nothing deleted."""
    docs = [_doc(1), _doc(2)]
    contents = {1: "BODY ONE", 2: "TOTALLY DIFFERENT BODY"}
    mcp = _make_mcp(docs, contents)

    result = await paperless_dedupe({}, mcp_manager=mcp)

    assert result["success"] is True
    assert mcp.deleted == []
    assert result["data"]["groups"] == 0
    assert result["action_taken"] is False


@pytest.mark.unit
@pytest.mark.asyncio
async def test_missing_content_is_skipped_never_deleted():
    """A member without OCR text can't be proven identical → skipped, never deleted."""
    docs = [_doc(1), _doc(2)]
    contents = {1: "BODY", 2: None}
    mcp = _make_mcp(docs, contents)

    result = await paperless_dedupe({}, mcp_manager=mcp)

    assert mcp.deleted == []
    assert result["data"]["groups"] == 0
    assert result["data"]["skipped"] == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_dry_run_deletes_nothing_but_reports():
    docs = [_doc(1), _doc(2)]
    contents = {1: "SAME", 2: "SAME"}
    mcp = _make_mcp(docs, contents)

    result = await paperless_dedupe({"dry_run": True}, mcp_manager=mcp)

    assert mcp.deleted == []  # delete_document never called
    assert result["action_taken"] is False
    assert result["data"]["dry_run"] is True
    assert result["data"]["deleted"] == 1  # would-delete count
    assert result["data"]["deleted_ids"] == [2]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_unique_metadata_makes_no_content_fetches():
    """Distinct metadata → no candidate groups → get_document/delete never called."""
    docs = [_doc(1, title="Rechnung Jan"), _doc(2, title="Vertrag Feb", document_type="Vertrag")]
    fetched: list[int] = []

    class _MCP:
        deleted: list[int] = []

        async def execute_tool(self, tool, params, **_kw):
            if tool == "mcp.paperless.search_documents":
                return {"success": True, "message": json.dumps({"results": docs})}
            if tool == "mcp.paperless.get_document":
                fetched.append(params["document_id"])
                return {"success": True, "message": json.dumps({"content": "x"})}
            return {"success": True, "message": "{}"}

    result = await paperless_dedupe({}, mcp_manager=_MCP())

    assert fetched == []  # no content fetched for singletons
    assert result["data"]["groups"] == 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_permission_denied_without_admin(monkeypatch):
    from utils.config import settings

    monkeypatch.setattr(settings, "auth_enabled", True)
    docs = [_doc(1), _doc(2)]
    mcp = _make_mcp(docs, {1: "SAME", 2: "SAME"})

    result = await paperless_dedupe({}, mcp_manager=mcp, user_permissions=["ha.read"])

    assert result["success"] is False
    assert result["action_taken"] is False
    assert mcp.deleted == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_permission_denied_for_unidentified_turn_when_auth_on(monkeypatch):
    """Fail-closed: auth ON + user_permissions=None (device/unrecognized-voice) is
    DENIED for this destructive tool — must not trash documents without ADMIN."""
    from utils.config import settings

    monkeypatch.setattr(settings, "auth_enabled", True)
    mcp = _make_mcp([_doc(1), _doc(2)], {1: "SAME", 2: "SAME"})

    result = await paperless_dedupe({}, mcp_manager=mcp, user_permissions=None)

    assert result["success"] is False
    assert result["action_taken"] is False
    assert mcp.deleted == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_no_mcp_returns_error():
    result = await paperless_dedupe({}, mcp_manager=None)
    assert result["success"] is False
    assert result["action_taken"] is False
