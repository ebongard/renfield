"""Tests for Context Variable Extraction -- entity extraction from MCP results."""

import json
from pathlib import Path

import pytest

from services import context_extractor
from services.context_extractor import (
    _extraction_rules,
    _resolve_path,
    extract_context_vars,
    load_extraction_config,
)

_CONFIG_DIR = Path(__file__).resolve().parents[2] / "src" / "backend" / "config"

# One response per rule in the shipped YAML, built from each MCP's source
# (invented values, no user data), with the variables the rule must extract.
RECORDED_SHAPES = {
    # renfield-mcp-paperless 1.13.0 server.py search_documents
    "mcp.paperless.search_documents": (
        {
            "summary": {"total_matching": 2, "returned": 2},
            "results": [
                {
                    "id": 42, "title": "Rechnung März", "created": "2026-03-01",
                    "correspondent": "Telekom", "document_type": "Rechnung",
                    "tags": ["Haushalt"], "page_count": 2, "snippet": None,
                    "storage_path": None,
                },
                {"id": 43, "title": "Vertrag", "created": "2026-02-01", "correspondent": None},
            ],
        },
        {
            "last_document_id": 42,
            "last_document_title": "Rechnung März",
            "last_document_correspondent": "Telekom",
        },
    ),
    # renfield-mcp-jellyfin server.py search_media (Movie field set)
    "mcp.jellyfin.search_media": (
        {"total": 1, "items": [{"id": "abc123", "name": "Inception", "year": 2010, "genre": "Sci-Fi"}]},
        {"last_media_id": "abc123", "last_media_name": "Inception"},
    ),
    # n8n-mcp 2.94.3 handlers-n8n-manager.js handleGetWorkflow
    "mcp.n8n.n8n_get_workflow": (
        {"success": True, "data": {"id": "wf-1", "name": "Backup", "active": True, "nodes": []}},
        {"current_workflow_id": "wf-1", "current_workflow_name": "Backup"},
    ),
    # n8n-mcp 2.94.3 handlers-n8n-manager.js handleListWorkflows
    "mcp.n8n.n8n_list_workflows": (
        {
            "success": True,
            "data": {
                "workflows": [{"id": "wf-1", "name": "Backup"}, {"id": "wf-2", "name": "Sync"}],
                "returned": 2,
                "nextCursor": None,
                "hasMore": False,
            },
        },
        {"workflow_count": 2},
    ),
    # renfield-mcp-mail server.py _search_emails_sync + _format_compact_email
    "mcp.email.search_emails": (
        {
            "account": "personal", "folder": "INBOX", "query": "unseen", "count": 1,
            "results": [{
                "id": "17", "from": "Shop <shop@example.com>", "subject": "Ihre Bestellung",
                "date": "2026-03-01T10:00:00+01:00", "folder": "INBOX", "flags": [],
            }],
        },
        {
            "last_email_id": "17",
            "last_email_subject": "Ihre Bestellung",
            "last_email_from": "Shop <shop@example.com>",
        },
    ),
}


@pytest.fixture(autouse=True)
def _load_config():
    """Load extraction config before each test."""
    load_extraction_config()
    yield
    _extraction_rules.clear()


class TestResolvePath:

    @pytest.mark.unit
    def test_simple_field(self):
        assert _resolve_path({"name": "test"}, "name") == "test"

    @pytest.mark.unit
    def test_nested_field(self):
        assert _resolve_path({"a": {"b": "val"}}, "a.b") == "val"

    @pytest.mark.unit
    def test_array_index(self):
        data = [{"id": 1}, {"id": 2}]
        assert _resolve_path(data, "[0].id") == 1

    @pytest.mark.unit
    def test_array_collect(self):
        data = [{"id": 1}, {"id": 2}, {"id": 3}]
        assert _resolve_path(data, "[].id") == [1, 2, 3]

    @pytest.mark.unit
    def test_length(self):
        assert _resolve_path([1, 2, 3], "__length__") == 3

    @pytest.mark.unit
    def test_missing_field(self):
        assert _resolve_path({"a": 1}, "b") is None

    @pytest.mark.unit
    def test_index_out_of_range(self):
        assert _resolve_path([{"a": 1}], "[5].a") is None

    @pytest.mark.unit
    def test_non_list_with_index(self):
        assert _resolve_path({"a": 1}, "[0].a") is None


class TestExtractContextVars:

    @pytest.mark.unit
    def test_non_mcp_tool_ignored(self):
        result = {"success": True, "message": '{"id": 1}'}
        assert extract_context_vars("internal.some_tool", result) == {}

    @pytest.mark.unit
    def test_failed_result_ignored(self):
        result = {"success": False, "message": "error"}
        assert extract_context_vars("mcp.paperless.search_documents", result) == {}

    @pytest.mark.unit
    def test_unknown_server_ignored(self):
        result = {"success": True, "message": '{"id": 1}'}
        assert extract_context_vars("mcp.unknown.tool", result) == {}

    @pytest.mark.unit
    @pytest.mark.parametrize("tool_name", sorted(RECORDED_SHAPES))
    def test_shipped_rule_extracts_from_its_recorded_shape(self, tool_name):
        """Every rule of the shipped YAML yields ALL its variables from the real shape."""
        payload, expected = RECORDED_SHAPES[tool_name]
        result = {"success": True, "message": json.dumps(payload)}
        assert extract_context_vars(tool_name, result) == expected

    @pytest.mark.unit
    def test_every_shipped_rule_has_a_recorded_shape(self):
        # load_extraction_config REBINDS the module global — read it from the module.
        configured = {
            f"mcp.{server}.{tool}"
            for server, tools in context_extractor._extraction_rules.items()
            for tool in tools
        }
        assert configured, "the shipped YAML loaded no rules"
        assert configured == set(RECORDED_SHAPES)

    @pytest.mark.unit
    def test_paperless_search_after_compaction(self, monkeypatch):
        """The extractor reads the COMPACTED result — the two YAMLs must agree."""
        from services import mcp_compact

        monkeypatch.setattr(mcp_compact, "_compact_rules", {})
        mcp_compact.load_compact_config(str(_CONFIG_DIR / "mcp_compact.yaml"))
        payload, expected = RECORDED_SHAPES["mcp.paperless.search_documents"]
        compacted = mcp_compact.compact_mcp_result(
            "mcp.paperless.search_documents",
            {"success": True, "message": json.dumps(payload), "data": None},
        )
        assert extract_context_vars("mcp.paperless.search_documents", compacted) == expected

    @pytest.mark.unit
    def test_non_json_message_ignored(self):
        result = {"success": True, "message": "plain text response"}
        assert extract_context_vars("mcp.paperless.search_documents", result) == {}

    @pytest.mark.unit
    def test_empty_result_ignored(self):
        result = {"success": True, "message": json.dumps({"summary": {"returned": 0}, "results": []})}
        extracted = extract_context_vars("mcp.paperless.search_documents", result)
        # results.[0].id on an empty list returns None — no variables extracted
        assert extracted == {}
