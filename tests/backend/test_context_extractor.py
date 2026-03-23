"""Tests for Context Variable Extraction -- entity extraction from MCP results."""

import json

import pytest

from services.context_extractor import (
    _extraction_rules,
    _resolve_path,
    extract_context_vars,
    load_extraction_config,
)


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
    def test_paperless_search(self):
        """Extracts document ID and title from paperless search results."""
        docs = [
            {"id": 42, "title": "Rechnung März", "correspondent": "Telekom"},
            {"id": 43, "title": "Vertrag"},
        ]
        result = {"success": True, "message": json.dumps(docs)}
        extracted = extract_context_vars("mcp.paperless.search_documents", result)
        assert extracted["last_document_id"] == 42
        assert extracted["last_document_title"] == "Rechnung März"
        assert extracted["last_document_correspondent"] == "Telekom"

    @pytest.mark.unit
    def test_jellyfin_search(self):
        """Extracts media ID, name, type from Jellyfin search."""
        items = [{"Id": "abc123", "Name": "Inception", "Type": "Movie", "Overview": "..."}]
        result = {"success": True, "message": json.dumps(items)}
        extracted = extract_context_vars("mcp.jellyfin.search", result)
        assert extracted["last_media_id"] == "abc123"
        assert extracted["last_media_name"] == "Inception"
        assert extracted["last_media_type"] == "Movie"

    @pytest.mark.unit
    def test_homeassistant_entities(self):
        """Extracts entity IDs from HA states."""
        states = [
            {"entity_id": "light.living", "state": "on"},
            {"entity_id": "sensor.temp", "state": "21.5"},
        ]
        result = {"success": True, "message": json.dumps(states)}
        extracted = extract_context_vars("mcp.homeassistant.get_states", result)
        assert extracted["queried_entities"] == ["light.living", "sensor.temp"]

    @pytest.mark.unit
    def test_non_json_message_ignored(self):
        result = {"success": True, "message": "plain text response"}
        assert extract_context_vars("mcp.paperless.search_documents", result) == {}

    @pytest.mark.unit
    def test_empty_result_ignored(self):
        result = {"success": True, "message": "[]"}
        extracted = extract_context_vars("mcp.paperless.search_documents", result)
        # [0].id on empty list returns None — no variables extracted
        assert extracted == {}

    @pytest.mark.unit
    def test_n8n_workflow_count(self):
        """Extracts workflow count using __length__ path."""
        workflows = [{"id": 1, "name": "backup"}, {"id": 2, "name": "sync"}]
        result = {"success": True, "message": json.dumps(workflows)}
        extracted = extract_context_vars("mcp.n8n.n8n_list_workflows", result)
        assert extracted["workflow_count"] == 2
