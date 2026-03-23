"""Tests for MCP Response Compaction Engine."""

import json

import pytest

from services.mcp_compact import (
    _compact_rules,
    _extract_fields,
    _resolve_tool_name,
    compact_mcp_result,
)


# ============================================================================
# _resolve_tool_name
# ============================================================================


class TestResolveToolName:

    @pytest.mark.unit
    def test_mcp_namespaced(self):
        assert _resolve_tool_name("mcp.homeassistant.get_states") == (
            "homeassistant",
            "get_states",
        )

    @pytest.mark.unit
    def test_mcp_with_dots_in_tool(self):
        assert _resolve_tool_name("mcp.server.some.tool") == ("server", "some.tool")

    @pytest.mark.unit
    def test_internal_tool(self):
        assert _resolve_tool_name("internal.play_in_room") is None

    @pytest.mark.unit
    def test_short_name(self):
        assert _resolve_tool_name("get_states") is None

    @pytest.mark.unit
    def test_mcp_prefix_only(self):
        assert _resolve_tool_name("mcp.server") is None


# ============================================================================
# _extract_fields
# ============================================================================


class TestExtractFields:

    @pytest.mark.unit
    def test_simple_fields(self):
        data = {"a": 1, "b": 2, "c": 3}
        result = _extract_fields(data, ["a", "c"])
        assert result == {"a": 1, "c": 3}

    @pytest.mark.unit
    def test_nested_fields(self):
        data = {"attr": {"name": "light", "color": "red", "brightness": 80}}
        result = _extract_fields(data, ["attr.name", "attr.brightness"])
        assert result == {"attr": {"name": "light", "brightness": 80}}

    @pytest.mark.unit
    def test_array_fields(self):
        data = {
            "items": [
                {"id": 1, "name": "a", "extra": "x"},
                {"id": 2, "name": "b", "extra": "y"},
            ]
        }
        result = _extract_fields(data, ["items[].id", "items[].name"])
        assert result == {"items": [{"id": 1, "name": "a"}, {"id": 2, "name": "b"}]}

    @pytest.mark.unit
    def test_missing_field_skipped(self):
        data = {"a": 1}
        result = _extract_fields(data, ["a", "b"])
        assert result == {"a": 1}

    @pytest.mark.unit
    def test_list_input(self):
        data = [{"a": 1, "b": 2}, {"a": 3, "b": 4}]
        result = _extract_fields(data, ["a"])
        assert result == [{"a": 1}, {"a": 3}]

    @pytest.mark.unit
    def test_deeply_nested(self):
        data = {"level1": {"level2": {"level3": "value", "other": "skip"}}}
        result = _extract_fields(data, ["level1.level2.level3"])
        assert result == {"level1": {"level2": {"level3": "value"}}}

    @pytest.mark.unit
    def test_nested_array_fields(self):
        data = {
            "phases": [
                {
                    "name": "build",
                    "tasks": [
                        {"id": 1, "status": "done", "log": "..."},
                        {"id": 2, "status": "running", "log": "..."},
                    ],
                }
            ]
        }
        result = _extract_fields(data, ["phases[].name", "phases[].tasks[].status"])
        assert result == {
            "phases": [{"name": "build", "tasks": [{"status": "done"}, {"status": "running"}]}]
        }

    @pytest.mark.unit
    def test_empty_dict(self):
        assert _extract_fields({}, ["a"]) == {}

    @pytest.mark.unit
    def test_scalar_input(self):
        assert _extract_fields(42, ["a"]) == 42

    @pytest.mark.unit
    def test_empty_list(self):
        assert _extract_fields([], ["a"]) == []


# ============================================================================
# compact_mcp_result
# ============================================================================


class TestCompactMcpResult:

    @pytest.mark.unit
    def test_no_rules_passes_through(self):
        result = {"success": True, "message": '{"x": 1}', "data": None}
        compacted = compact_mcp_result("mcp.unknown.tool", result)
        assert compacted == result

    @pytest.mark.unit
    def test_non_mcp_tool_passes_through(self):
        result = {"success": True, "message": "hello"}
        compacted = compact_mcp_result("internal.some_tool", result)
        assert compacted == result

    @pytest.mark.unit
    def test_with_matching_rule(self):
        _compact_rules["testserver"] = {"test_tool": ["id", "name"]}
        try:
            result = {
                "success": True,
                "message": json.dumps(
                    {"id": 1, "name": "doc", "content": "very long text", "metadata": {"a": 1}}
                ),
                "data": [
                    {
                        "type": "text",
                        "text": json.dumps(
                            {"id": 1, "name": "doc", "content": "very long text"}
                        ),
                    }
                ],
            }
            compacted = compact_mcp_result("mcp.testserver.test_tool", result)

            msg = json.loads(compacted["message"])
            assert msg == {"id": 1, "name": "doc"}
            assert "very long" not in compacted["message"]

            data_text = json.loads(compacted["data"][0]["text"])
            assert data_text == {"id": 1, "name": "doc"}
        finally:
            del _compact_rules["testserver"]

    @pytest.mark.unit
    def test_non_json_message_passes_through(self):
        _compact_rules["srv"] = {"tool": ["id"]}
        try:
            result = {"success": True, "message": "plain text response", "data": None}
            compacted = compact_mcp_result("mcp.srv.tool", result)
            assert compacted["message"] == "plain text response"
        finally:
            del _compact_rules["srv"]

    @pytest.mark.unit
    def test_list_result_compaction(self):
        """Test compaction when the MCP result is a JSON list."""
        _compact_rules["ha"] = {"get_states": ["entity_id", "state"]}
        try:
            states = [
                {"entity_id": "light.living", "state": "on", "attributes": {"brightness": 255}},
                {"entity_id": "sensor.temp", "state": "21.5", "attributes": {"unit": "C"}},
            ]
            result = {
                "success": True,
                "message": json.dumps(states),
                "data": None,
            }
            compacted = compact_mcp_result("mcp.ha.get_states", result)
            parsed = json.loads(compacted["message"])
            assert parsed == [
                {"entity_id": "light.living", "state": "on"},
                {"entity_id": "sensor.temp", "state": "21.5"},
            ]
        finally:
            del _compact_rules["ha"]

    @pytest.mark.unit
    def test_original_result_not_mutated(self):
        """Ensure compaction returns a new dict, not mutating the original."""
        _compact_rules["srv"] = {"tool": ["id"]}
        try:
            original_msg = json.dumps({"id": 1, "extra": "big"})
            result = {"success": True, "message": original_msg, "data": None}
            compacted = compact_mcp_result("mcp.srv.tool", result)
            assert result["message"] == original_msg  # original unchanged
            assert compacted["message"] != original_msg
        finally:
            del _compact_rules["srv"]
