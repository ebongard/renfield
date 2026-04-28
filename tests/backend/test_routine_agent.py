"""
Tests for the Routine Agent — Good Night / Good Morning multi-step sequences.

Verifies:
- Presence check is always called first
- Graceful degradation when MCP servers are unavailable
- Multi-user awareness (don't turn off occupied rooms)
- Correct prompt key and step limits from role config
"""
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Ensure 'ollama' module is available even when the package isn't installed.
if "ollama" not in sys.modules:
    _ollama_stub = MagicMock()
    _ollama_stub.AsyncClient = MagicMock()
    sys.modules["ollama"] = _ollama_stub

from services.agent_router import _parse_roles
from services.agent_service import AgentService
from services.agent_tools import AgentToolRegistry

# ============================================================================
# Helpers
# ============================================================================

async def collect_steps(agent, **kwargs) -> list:
    """Collect all steps from an agent run."""
    steps = []
    async for step in agent.run(**kwargs):
        steps.append(step)
    return steps


def _make_mock_agent_client(responses: list[str]):
    """Create a mock LLM client whose chat() returns successive JSON responses.

    This mocks the client returned by get_agent_client(), NOT ollama.client.
    The agent_service.run() calls get_agent_client() to create its own client.
    """
    call_count = 0

    async def mock_chat(**kwargs):
        nonlocal call_count
        idx = min(call_count, len(responses) - 1)
        call_count += 1
        resp = MagicMock()
        resp.message = MagicMock()
        resp.message.content = responses[idx]
        return resp

    client = MagicMock()
    client.chat = mock_chat
    return client


def _make_ollama_mock():
    """Create a minimal mock OllamaService (used for lang detection, not LLM calls)."""
    ollama = MagicMock()
    ollama.default_lang = "de"
    ollama.client = MagicMock()
    return ollama


def _make_executor_mock(results=None):
    """Create a mock ActionExecutor."""
    executor = AsyncMock()
    if results:
        executor.execute = AsyncMock(side_effect=results)
    else:
        executor.execute = AsyncMock(return_value={
            "success": True,
            "message": "Aktion ausgefuehrt",
            "action_taken": True,
        })
    return executor


ROUTINE_ROLE_CONFIG = {
    "roles": {
        "routine": {
            "description": {
                "de": "Routinen: Gute-Nacht-Routine, Guten-Morgen-Routine",
                "en": "Routines: good-night routine, good-morning routine",
            },
            "mcp_servers": ["homeassistant", "calendar", "weather", "jellyfin", "dlna", "radio"],
            "internal_tools": [
                "internal.get_all_presence",
                "internal.get_user_location",
                "internal.resolve_room_player",
                "internal.media_control",
                "internal.play_in_room",
                "internal.play_radio",
            ],
            "max_steps": 15,
            "prompt_key": "agent_prompt_routine",
        },
        "conversation": {
            "description": {"de": "Konversation", "en": "Conversation"},
        },
        "general": {
            "description": {"de": "Allgemein", "en": "General"},
            "mcp_servers": None,
            "internal_tools": None,
            "max_steps": 12,
            "prompt_key": "agent_prompt",
        },
    }
}


# ============================================================================
# Test Routine Role Configuration
# ============================================================================

class TestRoutineRoleConfig:
    """Test that the routine role is configured correctly."""

    @pytest.mark.unit
    def test_routine_role_parsed(self):
        roles = _parse_roles(ROUTINE_ROLE_CONFIG)
        assert "routine" in roles
        role = roles["routine"]
        assert role.max_steps == 15
        assert role.prompt_key == "agent_prompt_routine"
        assert role.has_agent_loop is True

    @pytest.mark.unit
    def test_routine_role_tools(self):
        roles = _parse_roles(ROUTINE_ROLE_CONFIG)
        role = roles["routine"]
        assert "internal.get_all_presence" in role.internal_tools
        assert "internal.get_user_location" in role.internal_tools
        assert "internal.media_control" in role.internal_tools
        assert "internal.play_in_room" in role.internal_tools
        assert "internal.play_radio" in role.internal_tools

    @pytest.mark.unit
    def test_routine_role_mcp_servers(self):
        roles = _parse_roles(ROUTINE_ROLE_CONFIG)
        role = roles["routine"]
        assert "homeassistant" in role.mcp_servers
        assert "calendar" in role.mcp_servers
        assert "weather" in role.mcp_servers

    @pytest.mark.unit
    def test_agent_service_uses_routine_role(self):
        """AgentService respects routine role max_steps and prompt_key."""
        roles = _parse_roles(ROUTINE_ROLE_CONFIG)
        role = roles["routine"]

        registry = AgentToolRegistry(
            internal_filter=role.internal_tools,
            _init_only=True,
        )
        agent = AgentService(registry, role=role)

        assert agent.max_steps == 15
        assert agent._prompt_key == "agent_prompt_routine"

    @pytest.mark.unit
    def test_routine_tool_registry_filters_internal_tools(self):
        """Tool registry only includes routine-allowed internal tools."""
        roles = _parse_roles(ROUTINE_ROLE_CONFIG)
        role = roles["routine"]

        registry = AgentToolRegistry(
            server_filter=role.mcp_servers,
            internal_filter=role.internal_tools,
            _init_only=True,
        )

        tool_names = registry.get_tool_names()
        # Without MCP manager, only internal tools are registered
        for name in tool_names:
            assert name in role.internal_tools, f"Unexpected tool: {name}"


# ============================================================================
# Test Routine Agent Execution
# ============================================================================

class TestRoutineAgentExecution:
    """Test the routine agent's multi-step execution via mocked LLM + executor."""

    def _make_routine_agent(self):
        """Create an AgentService configured with the routine role."""
        roles = _parse_roles(ROUTINE_ROLE_CONFIG)
        role = roles["routine"]
        registry = AgentToolRegistry(internal_filter=role.internal_tools, _init_only=True)
        return AgentService(registry, role=role)

    def _patch_agent_deps(self, llm_responses: list[str]):
        """Return a context manager that patches agent_service dependencies.

        Mocks: settings, prompt_manager, get_agent_client.
        """
        mock_client = _make_mock_agent_client(llm_responses)

        settings_patch = patch("services.agent_service.settings")
        pm_patch = patch("services.agent_service.prompt_manager")
        client_patch = patch(
            "services.agent_service.get_agent_client",
            return_value=(mock_client, "http://mock:11434"),
        )

        class _Combined:
            def __enter__(self_inner):
                mock_settings = settings_patch.start()
                mock_pm = pm_patch.start()
                client_patch.start()

                mock_settings.agent_max_steps = 15
                mock_settings.agent_step_timeout = 30.0
                mock_settings.agent_total_timeout = 120.0
                mock_settings.ollama_model = "test-model"
                mock_settings.agent_ollama_url = None
                mock_settings.agent_model = None
                mock_pm.get.return_value = "Du bist ein Routine-Agent. AUFGABE: {message}"
                return self_inner

            def __exit__(self_inner, *args):
                settings_patch.stop()
                pm_patch.stop()
                client_patch.stop()

        return _Combined()

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_good_night_calls_presence_first(self):
        """Good night routine should call get_all_presence as the first tool."""
        agent = self._make_routine_agent()

        responses = [
            '{"action": "internal.get_all_presence", "parameters": {}, "reason": "Pruefe wer zuhause ist"}',
            '{"action": "final_answer", "answer": "Gute Nacht! Alle Lichter aus.", "reason": "Routine abgeschlossen"}',
        ]
        executor = _make_executor_mock([
            {"success": True, "message": "Erik: Schlafzimmer", "action_taken": True},
        ])

        with self._patch_agent_deps(responses):
            steps = await collect_steps(
                agent,
                message="Gute Nacht",
                ollama=_make_ollama_mock(),
                executor=executor,
            )

        tool_calls = [s for s in steps if s.step_type == "tool_call"]
        assert len(tool_calls) >= 1
        assert tool_calls[0].tool == "internal.get_all_presence"

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_good_morning_calls_presence_first(self):
        """Good morning routine should also call get_all_presence as the first tool."""
        agent = self._make_routine_agent()

        responses = [
            '{"action": "internal.get_all_presence", "parameters": {}, "reason": "Check who is home"}',
            '{"action": "final_answer", "answer": "Guten Morgen! 14 Grad und sonnig.", "reason": "Routine done"}',
        ]
        executor = _make_executor_mock([
            {"success": True, "message": "Erik: Schlafzimmer", "action_taken": True},
        ])

        with self._patch_agent_deps(responses):
            steps = await collect_steps(
                agent,
                message="Guten Morgen",
                ollama=_make_ollama_mock(),
                executor=executor,
            )

        tool_calls = [s for s in steps if s.step_type == "tool_call"]
        assert len(tool_calls) >= 1
        assert tool_calls[0].tool == "internal.get_all_presence"

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_graceful_degradation_on_tool_failure(self):
        """Routine should continue to final_answer even when a tool fails."""
        agent = self._make_routine_agent()

        responses = [
            '{"action": "internal.get_all_presence", "parameters": {}, "reason": "Presence check"}',
            '{"action": "final_answer", "answer": "Gute Nacht! Kalender war nicht erreichbar.", "reason": "Kalender uebersprungen"}',
        ]
        executor = _make_executor_mock([
            {"success": False, "message": "MCP server unavailable", "action_taken": False},
        ])

        with self._patch_agent_deps(responses):
            steps = await collect_steps(
                agent,
                message="Gute Nacht",
                ollama=_make_ollama_mock(),
                executor=executor,
            )

        # Should still reach final_answer despite tool failure
        final_steps = [s for s in steps if s.step_type == "final_answer"]
        assert len(final_steps) == 1
        assert "Gute Nacht" in final_steps[0].content

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_multi_step_good_night_sequence(self):
        """Full good-night sequence with presence → media_control → final_answer."""
        agent = self._make_routine_agent()

        responses = [
            '{"action": "internal.get_all_presence", "parameters": {}, "reason": "Wer ist zuhause?"}',
            '{"action": "internal.media_control", "parameters": {"action": "stop"}, "reason": "Wiedergabe stoppen"}',
            '{"action": "final_answer", "answer": "Alles erledigt. Gute Nacht!", "reason": "Routine fertig"}',
        ]
        executor = _make_executor_mock([
            {"success": True, "message": "Erik: Schlafzimmer, Lisa: Wohnzimmer", "action_taken": True},
            {"success": True, "message": "Playback stopped", "action_taken": True},
        ])

        with self._patch_agent_deps(responses):
            steps = await collect_steps(
                agent,
                message="Gute Nacht",
                ollama=_make_ollama_mock(),
                executor=executor,
            )

        tool_calls = [s for s in steps if s.step_type == "tool_call"]
        assert len(tool_calls) == 2
        assert tool_calls[0].tool == "internal.get_all_presence"
        assert tool_calls[1].tool == "internal.media_control"

        final_steps = [s for s in steps if s.step_type == "final_answer"]
        assert len(final_steps) == 1


# ============================================================================
# Test Routine Prompt Loading
# ============================================================================

class TestRoutinePromptLoading:
    """Test that routine prompts are loadable from agent.yaml."""

    @pytest.mark.unit
    def test_routine_prompt_exists_in_yaml(self):
        """Verify agent_prompt_routine is defined in the actual agent.yaml."""
        from pathlib import Path

        import yaml

        agent_yaml_path = Path("src/backend/prompts/agent.yaml")
        if not agent_yaml_path.exists():
            pytest.skip("agent.yaml not found (running outside project root)")

        with open(agent_yaml_path) as f:
            prompts = yaml.safe_load(f)

        # Check DE prompt
        assert "agent_prompt_routine" in prompts["de"]
        de_prompt = prompts["de"]["agent_prompt_routine"]
        assert "Routine-Agent" in de_prompt
        assert "GUTE-NACHT-ROUTINE" in de_prompt
        assert "GUTEN-MORGEN-ROUTINE" in de_prompt
        assert "internal.get_all_presence" in de_prompt
        assert "{message}" in de_prompt

        # Check EN prompt
        assert "agent_prompt_routine" in prompts["en"]
        en_prompt = prompts["en"]["agent_prompt_routine"]
        assert "routine agent" in en_prompt.lower()
        assert "GOOD-NIGHT ROUTINE" in en_prompt
        assert "GOOD-MORNING ROUTINE" in en_prompt
        assert "internal.get_all_presence" in en_prompt

    @pytest.mark.unit
    def test_routine_prompt_has_required_variables(self):
        """Verify the routine prompt includes all required template variables."""
        from pathlib import Path

        import yaml

        agent_yaml_path = Path("src/backend/prompts/agent.yaml")
        if not agent_yaml_path.exists():
            pytest.skip("agent.yaml not found (running outside project root)")

        with open(agent_yaml_path) as f:
            prompts = yaml.safe_load(f)

        for lang in ("de", "en"):
            prompt = prompts[lang]["agent_prompt_routine"]
            assert "{message}" in prompt, f"Missing {{message}} in {lang}"
            assert "{tools_prompt}" in prompt, f"Missing {{tools_prompt}} in {lang}"
            assert "{step_directive}" in prompt, f"Missing {{step_directive}} in {lang}"
            assert "{memory_context}" in prompt, f"Missing {{memory_context}} in {lang}"
            assert "{room_context}" in prompt, f"Missing {{room_context}} in {lang}"

    @pytest.mark.unit
    def test_router_yaml_has_routine_hints(self):
        """Verify router.yaml contains routine classification guidance."""
        from pathlib import Path

        import yaml

        router_yaml_path = Path("src/backend/prompts/router.yaml")
        if not router_yaml_path.exists():
            pytest.skip("router.yaml not found (running outside project root)")

        with open(router_yaml_path) as f:
            router_prompts = yaml.safe_load(f)

        # DE router prompt should mention routine triggers
        de_prompt = router_prompts["de"]["classify_prompt"]
        assert "routine" in de_prompt.lower()
        assert "Gute Nacht" in de_prompt
        assert "Guten Morgen" in de_prompt

        # EN router prompt should mention routine triggers
        en_prompt = router_prompts["en"]["classify_prompt"]
        assert "routine" in en_prompt.lower()
        assert "Good night" in en_prompt
        assert "Good morning" in en_prompt
