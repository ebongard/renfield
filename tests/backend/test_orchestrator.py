"""Tests for the cross-MCP query orchestrator.

Covers: planner detection, JSON recovery, sub-agent execution, hooks,
synthesizer, post-processing, and integration points.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services.orchestrator import (
    OrchestrationResult,
    SubAgentResult,
    _build_recognition_rules,
    _strip_llm_source_line,
    _try_parse_planner_json,
    detect_multi_role,
    get_orchestrator_roles,
    run_orchestrated,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@dataclass
class FakeAgentRole:
    name: str
    description: dict
    mcp_servers: list | None = None
    internal_tools: list | None = None
    max_steps: int = 8
    prompt_key: str = "agent"
    has_agent_loop: bool = True
    model: str | None = "test-model"
    ollama_url: str | None = "http://localhost:11434"
    keyword_boost: list | None = None


@pytest.fixture
def roles():
    return {
        "release": FakeAgentRole(
            name="release",
            description={"en": "Release management", "de": "Release-Verwaltung"},
            mcp_servers=["release"],
            keyword_boost=["Release", "Phase", "Gate"],
        ),
        "jira": FakeAgentRole(
            name="jira",
            description={"en": "Jira issue tracking", "de": "Jira-Ticketverwaltung"},
            mcp_servers=["jira"],
            keyword_boost=["Jira", "Ticket", "Sprint"],
        ),
        "confluence": FakeAgentRole(
            name="confluence",
            description={"en": "Confluence documentation", "de": "Confluence-Dokumentation"},
            mcp_servers=["confluence"],
            keyword_boost=["Confluence", "Wiki", "Doku"],
        ),
        "conversation": FakeAgentRole(
            name="conversation",
            description={"en": "Conversation", "de": "Konversation"},
            mcp_servers=None,
            has_agent_loop=False,
        ),
    }


@pytest.fixture
def mock_llm_response():
    """Factory for mock LLM responses."""
    def _make(content: str):
        resp = MagicMock()
        resp.message.content = content
        return resp
    return _make


# ---------------------------------------------------------------------------
# _try_parse_planner_json
# ---------------------------------------------------------------------------

class TestPlannerJsonRecovery:
    def test_valid_json_parsed_directly(self):
        raw = '{"multi": true, "steps": [{"role": "jira", "query": "find issues"}]}'
        result = _try_parse_planner_json(raw)
        assert result is not None
        assert result["multi"] is True
        assert len(result["steps"]) == 1

    def test_truncated_json_recovered(self):
        raw = '{"multi": true, "steps": [{"role": "jira", "query": "find issues"}, {"role": "release", "query": "show rele'
        result = _try_parse_planner_json(raw)
        assert result is not None
        assert result["multi"] is True
        assert len(result["steps"]) == 1  # Only the complete step
        assert result["steps"][0]["role"] == "jira"

    def test_unrecoverable_json_returns_none(self):
        raw = '{"multi": true, "steps": [broken'
        result = _try_parse_planner_json(raw)
        assert result is None

    def test_backslash_in_query_field(self):
        raw = r'{"multi": true, "steps": [{"role": "jira", "query": "path\\to\\file"}]}'
        result = _try_parse_planner_json(raw)
        assert result is not None
        assert result["steps"][0]["query"] == "path\\to\\file"

    def test_no_steps_marker_returns_none(self):
        raw = '{"multi": true, "data": []}'
        result = _try_parse_planner_json(raw)
        # Valid JSON but no "steps" -- strict parse succeeds
        assert result is not None
        assert "steps" not in result


# ---------------------------------------------------------------------------
# _strip_llm_source_line
# ---------------------------------------------------------------------------

class TestStripSourceLine:
    def test_strips_german_quelle(self):
        text = "Some answer.\n_Quelle: Release, Jira_"
        assert "Quelle" not in _strip_llm_source_line(text)

    def test_strips_english_source(self):
        text = "Some answer.\n_Source: Release, Jira_"
        assert "Source" not in _strip_llm_source_line(text)

    def test_strips_bold_variants(self):
        text = "Answer.\n**Sources: Release, Jira**"
        assert "Sources" not in _strip_llm_source_line(text)

    def test_preserves_non_source_lines(self):
        text = "The source of truth is the database.\nQuelle Daten zeigen..."
        # "source" in the middle of a sentence should NOT be stripped
        result = _strip_llm_source_line(text)
        assert "source of truth" in result


# ---------------------------------------------------------------------------
# _build_recognition_rules
# ---------------------------------------------------------------------------

class TestBuildRecognitionRules:
    def test_builds_from_keyword_boost(self, roles):
        rules = _build_recognition_rules(roles, "en")
        assert "Release" in rules
        assert "Jira" in rules

    def test_empty_roles_returns_fallback(self):
        rules = _build_recognition_rules({}, "en")
        assert "no specific recognition rules" in rules


# ---------------------------------------------------------------------------
# detect_multi_role
# ---------------------------------------------------------------------------

class TestDetectMultiRole:
    @pytest.mark.asyncio
    async def test_multi_role_returns_plan(self, roles, mock_llm_response):
        response_json = json.dumps({
            "multi": True,
            "steps": [
                {"role": "jira", "query": "find REVA-1"},
                {"role": "release", "query": "show release status"},
            ],
        })
        mock_client = AsyncMock()
        mock_client.chat = AsyncMock(return_value=mock_llm_response(response_json))

        with patch("services.orchestrator.get_agent_client", return_value=(mock_client, "http://test")), \
             patch("services.orchestrator.prompt_manager") as mock_pm:
            mock_pm.get.return_value = "planner prompt {message} {role_descriptions} {recognition_rules}"
            plan = await detect_multi_role("Show REVA-1 and the release", roles["release"], roles, "test-req")
            assert plan is not None
            assert len(plan) == 2
            assert plan[0]["role"] == "jira"

    @pytest.mark.asyncio
    async def test_single_role_returns_none(self, roles, mock_llm_response):
        mock_client = AsyncMock()
        mock_client.chat = AsyncMock(return_value=mock_llm_response('{"multi": false}'))

        with patch("services.orchestrator.get_agent_client", return_value=(mock_client, "http://test")), \
             patch("services.orchestrator.prompt_manager") as mock_pm:
            mock_pm.get.return_value = "planner prompt {message} {role_descriptions} {recognition_rules}"
            plan = await detect_multi_role("Show all releases", roles["release"], roles, "test-req")
            assert plan is None

    @pytest.mark.asyncio
    async def test_fewer_than_2_valid_steps_returns_none(self, roles, mock_llm_response):
        response_json = json.dumps({
            "multi": True,
            "steps": [{"role": "jira", "query": "find issues"}],
        })
        mock_client = AsyncMock()
        mock_client.chat = AsyncMock(return_value=mock_llm_response(response_json))

        with patch("services.orchestrator.get_agent_client", return_value=(mock_client, "http://test")), \
             patch("services.orchestrator.prompt_manager") as mock_pm:
            mock_pm.get.return_value = "planner prompt {message} {role_descriptions} {recognition_rules}"
            plan = await detect_multi_role("Show REVA-1", roles["release"], roles, "test-req")
            assert plan is None

    @pytest.mark.asyncio
    async def test_timeout_returns_none(self, roles):
        mock_client = AsyncMock()
        mock_client.chat = AsyncMock(side_effect=asyncio.TimeoutError())

        with patch("services.orchestrator.get_agent_client", return_value=(mock_client, "http://test")), \
             patch("services.orchestrator.prompt_manager") as mock_pm:
            mock_pm.get.return_value = "planner prompt {message} {role_descriptions} {recognition_rules}"
            plan = await detect_multi_role("test", roles["release"], roles, "test-req")
            assert plan is None

    @pytest.mark.asyncio
    async def test_no_model_returns_none(self, roles):
        no_model_role = FakeAgentRole(name="release", description={"en": "test"}, model=None)
        plan = await detect_multi_role("test", no_model_role, roles, "test-req")
        assert plan is None

    @pytest.mark.asyncio
    async def test_malformed_json_returns_none(self, roles, mock_llm_response):
        mock_client = AsyncMock()
        mock_client.chat = AsyncMock(return_value=mock_llm_response("This is not JSON at all"))

        with patch("services.orchestrator.get_agent_client", return_value=(mock_client, "http://test")), \
             patch("services.orchestrator.prompt_manager") as mock_pm:
            mock_pm.get.return_value = "planner prompt {message} {role_descriptions} {recognition_rules}"
            plan = await detect_multi_role("test", roles["release"], roles, "test-req")
            assert plan is None

    @pytest.mark.asyncio
    async def test_invalid_role_in_steps_filtered(self, roles, mock_llm_response):
        response_json = json.dumps({
            "multi": True,
            "steps": [
                {"role": "jira", "query": "find issues"},
                {"role": "nonexistent", "query": "do something"},
                {"role": "release", "query": "show releases"},
            ],
        })
        mock_client = AsyncMock()
        mock_client.chat = AsyncMock(return_value=mock_llm_response(response_json))

        with patch("services.orchestrator.get_agent_client", return_value=(mock_client, "http://test")), \
             patch("services.orchestrator.prompt_manager") as mock_pm:
            mock_pm.get.return_value = "planner prompt {message} {role_descriptions} {recognition_rules}"
            plan = await detect_multi_role("test", roles["release"], roles, "test-req")
            assert plan is not None
            assert len(plan) == 2
            assert all(s["role"] in ("jira", "release") for s in plan)


# ---------------------------------------------------------------------------
# get_orchestrator_roles
# ---------------------------------------------------------------------------

class TestGetOrchestratorRoles:
    @pytest.mark.asyncio
    async def test_returns_mcp_backed_roles(self, roles):
        mock_router = MagicMock()
        mock_router.roles = roles

        with patch("services.orchestrator.run_hooks", new_callable=AsyncMock, return_value=[]):
            result = await get_orchestrator_roles(mock_router)
            assert "release" in result
            assert "jira" in result
            assert "confluence" in result
            assert "conversation" not in result

    @pytest.mark.asyncio
    async def test_extends_via_hook(self, roles):
        mock_router = MagicMock()
        mock_router.roles = roles

        with patch("services.orchestrator.run_hooks", new_callable=AsyncMock, return_value=[{"itsm"}]):
            result = await get_orchestrator_roles(mock_router)
            assert "itsm" in result


# ---------------------------------------------------------------------------
# run_orchestrated
# ---------------------------------------------------------------------------

class TestRunOrchestrated:
    @pytest.mark.asyncio
    async def test_two_sub_agents_synthesized(self, roles, mock_llm_response):
        plan = [
            {"role": "jira", "query": "find REVA-1"},
            {"role": "release", "query": "show release"},
        ]

        jira_result = SubAgentResult(
            role="jira", answer="REVA-1 is open", tool_summaries=[("jira_get_issue", "data")],
            tools_available=["jira_get_issue"],
        )
        release_result = SubAgentResult(
            role="release", answer="Release 1.3 is active", tool_summaries=[("get_release", "data")],
            tools_available=["get_release"],
        )

        mock_client = AsyncMock()
        mock_client.chat = AsyncMock(return_value=mock_llm_response("Combined answer about REVA-1 and Release 1.3"))

        with patch("services.orchestrator._run_sub_agent", side_effect=[jira_result, release_result]), \
             patch("services.orchestrator.get_agent_client", return_value=(mock_client, "http://test")), \
             patch("services.orchestrator.run_hooks", new_callable=AsyncMock, return_value=[]), \
             patch("services.orchestrator.prompt_manager") as mock_pm:
            mock_pm.get.return_value = "synthesizer {message} {collected_data}"
            result = await run_orchestrated(
                plan=plan, original_message="Show REVA-1 and release",
                mcp_manager=MagicMock(), roles=roles, lang="en", req_id="test",
                history=None, memory_context="", context_vars_text="",
                user_name="testuser", user_id=1,
            )
            assert isinstance(result, OrchestrationResult)
            assert len(result.sub_results) == 2
            assert "Combined answer" in result.synthesis

    @pytest.mark.asyncio
    async def test_partial_failure_populates_errors(self, roles, mock_llm_response):
        plan = [
            {"role": "jira", "query": "find issues"},
            {"role": "release", "query": "show release"},
        ]

        release_result = SubAgentResult(
            role="release", answer="Release 1.3 is active",
            tool_summaries=[], tools_available=[],
        )

        async def mock_sub_agent(step, **kwargs):
            if step["role"] == "jira":
                raise TimeoutError("jira timed out")
            return release_result

        with patch("services.orchestrator._run_sub_agent", side_effect=mock_sub_agent), \
             patch("services.orchestrator.run_hooks", new_callable=AsyncMock, return_value=[]):
            result = await run_orchestrated(
                plan=plan, original_message="test", mcp_manager=MagicMock(),
                roles=roles, lang="en", req_id="test", history=None,
                memory_context="", context_vars_text="", user_name="test", user_id=1,
            )
            assert len(result.errors) == 1
            assert result.errors[0]["role"] == "jira"
            assert len(result.sub_results) == 1

    @pytest.mark.asyncio
    async def test_all_fail_returns_error_message(self, roles):
        plan = [
            {"role": "jira", "query": "find issues"},
            {"role": "release", "query": "show release"},
        ]

        async def mock_sub(step, **kwargs):
            raise TimeoutError(f"{step['role']} timed out")

        with patch("services.orchestrator._run_sub_agent", side_effect=mock_sub), \
             patch("services.orchestrator.run_hooks", new_callable=AsyncMock, return_value=[]):
            result = await run_orchestrated(
                plan=plan, original_message="test", mcp_manager=MagicMock(),
                roles=roles, lang="en", req_id="test", history=None,
                memory_context="", context_vars_text="", user_name="test", user_id=1,
            )
            assert "could not process" in result.synthesis.lower()
            assert len(result.errors) == 2

    @pytest.mark.asyncio
    async def test_single_result_skips_synthesis(self, roles):
        plan = [
            {"role": "jira", "query": "find issues"},
            {"role": "release", "query": "show release"},
        ]

        jira_result = SubAgentResult(
            role="jira", answer="REVA-1 is open",
            tool_summaries=[], tools_available=[],
        )

        async def mock_sub(step, **kwargs):
            if step["role"] == "jira":
                return jira_result
            raise TimeoutError("release timed out")

        with patch("services.orchestrator._run_sub_agent", side_effect=mock_sub), \
             patch("services.orchestrator.run_hooks", new_callable=AsyncMock, return_value=[]):
            result = await run_orchestrated(
                plan=plan, original_message="test", mcp_manager=MagicMock(),
                roles=roles, lang="en", req_id="test", history=None,
                memory_context="", context_vars_text="", user_name="test", user_id=1,
            )
            assert result.synthesis == "REVA-1 is open"

    @pytest.mark.asyncio
    async def test_synthesis_failure_concatenates(self, roles):
        plan = [
            {"role": "jira", "query": "find issues"},
            {"role": "release", "query": "show release"},
        ]

        jira_result = SubAgentResult(role="jira", answer="Jira data", tool_summaries=[], tools_available=[])
        release_result = SubAgentResult(role="release", answer="Release data", tool_summaries=[], tools_available=[])

        mock_client = AsyncMock()
        mock_client.chat = AsyncMock(side_effect=Exception("LLM down"))

        with patch("services.orchestrator._run_sub_agent", side_effect=[jira_result, release_result]), \
             patch("services.orchestrator.get_agent_client", return_value=(mock_client, "http://test")), \
             patch("services.orchestrator.run_hooks", new_callable=AsyncMock, return_value=[]), \
             patch("services.orchestrator.prompt_manager") as mock_pm:
            mock_pm.get.return_value = "synthesizer {message} {collected_data}"
            result = await run_orchestrated(
                plan=plan, original_message="test", mcp_manager=MagicMock(),
                roles=roles, lang="en", req_id="test", history=None,
                memory_context="", context_vars_text="", user_name="test", user_id=1,
            )
            assert "Jira data" in result.synthesis
            assert "Release data" in result.synthesis

    @pytest.mark.asyncio
    async def test_typing_callback_called(self, roles):
        plan = [{"role": "jira", "query": "test"}]
        jira_result = SubAgentResult(role="jira", answer="data", tool_summaries=[], tools_available=[])
        typing_mock = AsyncMock()

        with patch("services.orchestrator._run_sub_agent", return_value=jira_result), \
             patch("services.orchestrator.run_hooks", new_callable=AsyncMock, return_value=[]):
            await run_orchestrated(
                plan=plan, original_message="test", mcp_manager=MagicMock(),
                roles=roles, lang="en", req_id="test", history=None,
                memory_context="", context_vars_text="", user_name="test", user_id=1,
                typing_callback=typing_mock,
            )
            typing_mock.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_post_orchestration_hook_fires(self, roles):
        plan = [{"role": "jira", "query": "test"}]
        jira_result = SubAgentResult(role="jira", answer="data", tool_summaries=[], tools_available=[])

        hook_calls = []

        async def mock_run_hooks(event, **kwargs):
            hook_calls.append(event)
            return []

        with patch("services.orchestrator._run_sub_agent", return_value=jira_result), \
             patch("services.orchestrator.run_hooks", side_effect=mock_run_hooks):
            await run_orchestrated(
                plan=plan, original_message="test", mcp_manager=MagicMock(),
                roles=roles, lang="en", req_id="test", history=None,
                memory_context="", context_vars_text="", user_name="test", user_id=1,
            )
            assert "post_orchestration" in hook_calls

    @pytest.mark.asyncio
    async def test_plugin_data_merged(self, roles, mock_llm_response):
        plan = [
            {"role": "jira", "query": "test"},
            {"role": "release", "query": "test"},
        ]

        jira_result = SubAgentResult(
            role="jira", answer="jira data", tool_summaries=[], tools_available=[],
            plugin_data={"contacts": ["alice"]},
        )
        release_result = SubAgentResult(
            role="release", answer="release data", tool_summaries=[], tools_available=[],
            plugin_data={"contacts": ["bob"]},
        )

        mock_client = AsyncMock()
        mock_client.chat = AsyncMock(return_value=mock_llm_response("combined"))

        with patch("services.orchestrator._run_sub_agent", side_effect=[jira_result, release_result]), \
             patch("services.orchestrator.get_agent_client", return_value=(mock_client, "http://test")), \
             patch("services.orchestrator.run_hooks", new_callable=AsyncMock, return_value=[]), \
             patch("services.orchestrator.prompt_manager") as mock_pm:
            mock_pm.get.return_value = "synthesizer {message} {collected_data}"
            result = await run_orchestrated(
                plan=plan, original_message="test", mcp_manager=MagicMock(),
                roles=roles, lang="en", req_id="test", history=None,
                memory_context="", context_vars_text="", user_name="test", user_id=1,
            )
            assert "alice" in result.plugin_data["contacts"]
            assert "bob" in result.plugin_data["contacts"]
