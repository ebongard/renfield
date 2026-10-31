"""Tests for MCP Response Compaction Engine."""

import json
from pathlib import Path

import pytest
from loguru import logger as loguru_logger

from services import mcp_compact
from services.mcp_compact import (
    _compact_rules,
    _extract_fields,
    _resolve_tool_name,
    compact_mcp_result,
    load_compact_config,
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


# ============================================================================
# The shipped rules against the real response shapes
# ============================================================================

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SHIPPED_RULES = _REPO_ROOT / "src" / "backend" / "config" / "mcp_compact.yaml"

# The response of renfield-mcp-paperless 1.13.0 search_documents, built from its
# source (server.py: search_documents -> _build_summary + _resolve_document).
# Invented documents, no user data. `content` is not part of the shape (the MCP
# turns it into `snippet`); storage_path is resolved to a name.
PAPERLESS_SEARCH_DOCUMENTS = {
    "summary": {
        "total_matching": 3,
        "returned": 2,
        "filters": {"correspondent": "Stadtwerke"},
        "note": "Showing first 2 of 3 matches.",
        "top_correspondents": [{"name": "Stadtwerke", "count": 2}],
        "top_document_types": [{"name": "Rechnung", "count": 2}],
    },
    "results": [
        {
            "id": 4711,
            "title": "Jahresabrechnung Strom 2025",
            "created": "2026-01-15",
            "correspondent": "Stadtwerke",
            "document_type": "Rechnung",
            "tags": ["Haushalt", "Energie"],
            "page_count": 3,
            "snippet": "...die Jahresabrechnung für den Zeitraum...",
            "storage_path": "Haushalt/Energie",
        },
        {
            "id": 4712,
            "title": "Abschlagsplan 2026",
            "created": "2026-01-20",
            "correspondent": "Stadtwerke",
            "document_type": "Rechnung",
            "tags": None,
            "page_count": 1,
            "snippet": None,
            "storage_path": None,
        },
    ],
}

# The response of n8n-mcp 2.94.3 (pinned in src/backend/Dockerfile)
# n8n_list_workflows, built from dist/mcp/handlers-n8n-manager.js
# handleListWorkflows.
N8N_LIST_WORKFLOWS = {
    "success": True,
    "data": {
        "workflows": [
            {
                "id": "wf-1",
                "name": "Backup",
                "active": True,
                "isArchived": False,
                "createdAt": "2026-01-01T00:00:00.000Z",
                "updatedAt": "2026-02-01T00:00:00.000Z",
                "tags": [],
                "nodeCount": 7,
            },
        ],
        "returned": 1,
        "nextCursor": None,
        "hasMore": False,
    },
}

#: One recorded response per rule in the shipped YAML; a new rule needs a shape.
RECORDED_SHAPES = {
    "mcp.paperless.search_documents": PAPERLESS_SEARCH_DOCUMENTS,
    "mcp.n8n.n8n_list_workflows": N8N_LIST_WORKFLOWS,
}


@pytest.fixture
def shipped_rules(monkeypatch):
    """Load the real config/mcp_compact.yaml; the module's rule table is restored afterwards.

    load_compact_config REBINDS the module global, so the table imported at the top
    of this file is not the one it fills — read it from the module.
    """
    monkeypatch.setattr(mcp_compact, "_compact_rules", mcp_compact._compact_rules)
    load_compact_config(str(_SHIPPED_RULES))
    return mcp_compact._compact_rules


def _as_mcp_result(payload) -> dict:
    """Wrap a payload the way MCPManager hands a tool result to the agent."""
    text = json.dumps(payload)
    return {"success": True, "message": text, "data": [{"type": "text", "text": text}]}


@pytest.fixture
def warnings_logged():
    captured: list[str] = []
    sink = loguru_logger.add(lambda m: captured.append(str(m)), level="WARNING")
    yield captured
    loguru_logger.remove(sink)


class TestShippedRules:

    @pytest.mark.unit
    def test_every_shipped_rule_has_a_recorded_shape(self, shipped_rules):
        configured = {
            f"mcp.{server}.{tool}" for server, tools in shipped_rules.items() for tool in tools
        }
        assert configured, "the shipped YAML loaded no rules"
        assert configured == set(RECORDED_SHAPES)

    @pytest.mark.unit
    @pytest.mark.parametrize("tool_name", sorted(RECORDED_SHAPES))
    def test_shipped_rule_matches_its_recorded_shape(self, shipped_rules, warnings_logged, tool_name):
        payload = RECORDED_SHAPES[tool_name]
        compacted = compact_mcp_result(tool_name, _as_mcp_result(payload))

        message = json.loads(compacted["message"])
        assert message, f"{tool_name}: the rule kept nothing of its recorded shape"
        assert message != payload, f"{tool_name}: the rule removed nothing"
        assert json.loads(compacted["data"][0]["text"]) == message
        assert not warnings_logged

    @pytest.mark.unit
    def test_paperless_search_keeps_summary_ids_and_titles(self, shipped_rules):
        compacted = compact_mcp_result(
            "mcp.paperless.search_documents", _as_mcp_result(PAPERLESS_SEARCH_DOCUMENTS)
        )
        message = json.loads(compacted["message"])

        assert message["summary"] == PAPERLESS_SEARCH_DOCUMENTS["summary"]
        assert [doc["id"] for doc in message["results"]] == [4711, 4712]
        assert [doc["title"] for doc in message["results"]] == [
            "Jahresabrechnung Strom 2025",
            "Abschlagsplan 2026",
        ]
        first = message["results"][0]
        assert first["correspondent"] == "Stadtwerke"
        assert first["created"] == "2026-01-15"
        assert first["tags"] == ["Haushalt", "Energie"]
        assert first["snippet"].startswith("...die Jahresabrechnung")
        assert "storage_path" not in first

    @pytest.mark.unit
    def test_paperless_error_envelope_survives(self, shipped_rules, warnings_logged):
        error = {"error": "Unknown correspondent: 'Nobody'"}
        compacted = compact_mcp_result("mcp.paperless.search_documents", _as_mcp_result(error))
        assert json.loads(compacted["message"]) == error
        assert not warnings_logged

    @pytest.mark.unit
    def test_n8n_list_keeps_workflow_ids_and_names(self, shipped_rules):
        compacted = compact_mcp_result(
            "mcp.n8n.n8n_list_workflows", _as_mcp_result(N8N_LIST_WORKFLOWS)
        )
        message = json.loads(compacted["message"])

        assert message["success"] is True
        assert message["data"]["workflows"] == [
            {"id": "wf-1", "name": "Backup", "active": True, "updatedAt": "2026-02-01T00:00:00.000Z"}
        ]
        assert message["data"]["returned"] == 1

    @pytest.mark.unit
    def test_n8n_error_envelope_survives(self, shipped_rules):
        error = {"success": False, "error": "Workflow not found", "code": "NOT_FOUND"}
        compacted = compact_mcp_result("mcp.n8n.n8n_list_workflows", _as_mcp_result(error))
        assert json.loads(compacted["message"]) == {"success": False, "error": "Workflow not found"}


# ============================================================================
# A rule that keeps nothing is a shape mismatch, not an empty answer
# ============================================================================


class TestHollowCompactionFallsBack:

    @pytest.fixture(autouse=True)
    def _stale_rule(self, monkeypatch):
        # The pre-fix paperless rule: written for a bare list of documents.
        monkeypatch.setitem(mcp_compact._compact_rules, "stale", {"tool": ["id", "title", "tags[].name"]})

    @pytest.mark.unit
    def test_wrapped_payload_passes_through_uncompacted(self, warnings_logged):
        result = _as_mcp_result(PAPERLESS_SEARCH_DOCUMENTS)
        compacted = compact_mcp_result("mcp.stale.tool", result)

        assert compacted["message"] == result["message"]
        assert compacted["data"] == result["data"]
        assert "Jahresabrechnung Strom 2025" in compacted["message"]

    @pytest.mark.unit
    def test_mismatch_logs_a_warning_with_keys_but_no_content(self, warnings_logged):
        compact_mcp_result("mcp.stale.tool", _as_mcp_result(PAPERLESS_SEARCH_DOCUMENTS))

        assert len(warnings_logged) == 2  # message + data text
        line = warnings_logged[0]
        assert "mcp.stale.tool" in line
        assert "'results'" in line and "'summary'" in line
        assert "Jahresabrechnung" not in line

    @pytest.mark.unit
    def test_list_of_unmatched_items_passes_through(self, warnings_logged):
        payload = [{"entity": "a"}, {"entity": "b"}]
        result = {"success": True, "message": json.dumps(payload), "data": None}
        compacted = compact_mcp_result("mcp.stale.tool", result)

        assert json.loads(compacted["message"]) == payload
        assert len(warnings_logged) == 1

    @pytest.mark.unit
    def test_partial_match_is_still_compacted(self, warnings_logged):
        payload = [{"id": 1, "body": "x"}, {"other": 2}]
        result = {"success": True, "message": json.dumps(payload), "data": None}
        compacted = compact_mcp_result("mcp.stale.tool", result)

        assert json.loads(compacted["message"]) == [{"id": 1}, {}]
        assert not warnings_logged

    @pytest.mark.unit
    @pytest.mark.parametrize("payload", [[], {}])
    def test_empty_payload_is_an_empty_answer_without_warning(self, warnings_logged, payload):
        result = {"success": True, "message": json.dumps(payload), "data": None}
        compacted = compact_mcp_result("mcp.stale.tool", result)

        assert json.loads(compacted["message"]) == payload
        assert not warnings_logged


# ============================================================================
# The MCP client's truncation note survives every rule
# ============================================================================


class TestTruncationNoteSurvives:

    @pytest.mark.unit
    @pytest.mark.parametrize("tool_name", sorted(RECORDED_SHAPES))
    def test_truncation_note_is_kept_by_every_shipped_rule(self, shipped_rules, tool_name):
        """_truncate_response embeds `_truncation` when it cut the largest array;
        a rule that drops it makes a cut list read as the complete answer."""
        note = {"showing": 1, "total": 2, "note": "Showing 1 of 2 results"}
        payload = {**RECORDED_SHAPES[tool_name], "_truncation": note}
        compacted = compact_mcp_result(tool_name, _as_mcp_result(payload))

        assert json.loads(compacted["message"])["_truncation"] == note
        assert json.loads(compacted["data"][0]["text"])["_truncation"] == note

    @pytest.mark.unit
    def test_payload_cut_by_the_real_client_keeps_its_note(self, shipped_rules):
        from services.mcp_client import _truncate_response

        many = dict(PAPERLESS_SEARCH_DOCUMENTS)
        many["results"] = [
            {**PAPERLESS_SEARCH_DOCUMENTS["results"][0], "id": i, "snippet": "x" * 400}
            for i in range(50)
        ]
        cut = _truncate_response(json.dumps(many), max_size=4096)
        assert "_truncation" in json.loads(cut)  # negative control: the client did cut

        compacted = compact_mcp_result(
            "mcp.paperless.search_documents", {"success": True, "message": cut, "data": None}
        )
        message = json.loads(compacted["message"])
        assert message["_truncation"]["total"] == 50
        assert message["_truncation"]["showing"] == len(message["results"]) < 50

    @pytest.mark.unit
    def test_a_note_alone_is_still_a_hollow_compaction(self, monkeypatch, warnings_logged):
        monkeypatch.setitem(mcp_compact._compact_rules, "stale", {"tool": ["id"]})
        payload = {"results": [{"title": "a"}], "_truncation": {"showing": 1, "total": 9}}
        result = {"success": True, "message": json.dumps(payload), "data": None}
        compacted = compact_mcp_result("mcp.stale.tool", result)

        assert compacted["message"] == result["message"]
        assert len(warnings_logged) == 1
