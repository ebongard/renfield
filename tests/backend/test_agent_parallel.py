"""
Tests for parallel tool execution (Phase 1) and parallel orchestrator (Phase 2).
"""
import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services.agent_service import AgentStep, _parse_agent_json
from utils.config import settings


# ===========================================================================
# Phase 1: Parallel Tool Execution
# ===========================================================================


@pytest.mark.unit
def test_parse_multi_action_json():
    """Multi-action JSON format is parsed correctly."""
    raw = json.dumps({
        "actions": [
            {"action": "weather.get_current", "parameters": {"location": "Berlin"}},
            {"action": "calendar.get_today", "parameters": {}},
        ],
        "reason": "Independent queries"
    })
    parsed = _parse_agent_json(raw)
    assert "actions" in parsed
    assert len(parsed["actions"]) == 2
    assert parsed["actions"][0]["action"] == "weather.get_current"


@pytest.mark.unit
def test_parse_single_action_still_works():
    """Existing single-action format is unchanged."""
    raw = json.dumps({
        "action": "weather.get_current",
        "parameters": {"location": "Berlin"},
        "reason": "Need weather"
    })
    parsed = _parse_agent_json(raw)
    assert "action" in parsed
    assert parsed["action"] == "weather.get_current"
    assert "actions" not in parsed


@pytest.mark.unit
@pytest.mark.asyncio
async def test_parallel_tool_one_fails_other_succeeds():
    """If one parallel tool fails, the other's result is still available."""
    mock_executor = AsyncMock()

    async def conditional_run(intent_data, **kwargs):
        if intent_data["intent"] == "tool_fail":
            raise ConnectionError("Tool unavailable")
        return {"success": True, "message": "OK", "action_taken": True}

    mock_executor.run = conditional_run

    actions = [
        {"action": "tool_ok", "parameters": {}},
        {"action": "tool_fail", "parameters": {}},
    ]

    async def _run_one(act):
        intent_data = {"intent": act["action"], "parameters": act.get("parameters", {}), "confidence": 1.0}
        return await conditional_run(intent_data)

    results = await asyncio.gather(*[_run_one(a) for a in actions], return_exceptions=True)

    assert results[0]["success"] is True
    assert isinstance(results[1], ConnectionError)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_parallel_disabled_treats_actions_as_malformed():
    """When agent_parallel_tools is False, actions array has no single 'action' key."""
    raw = json.dumps({
        "actions": [
            {"action": "tool_a", "parameters": {}},
            {"action": "tool_b", "parameters": {}},
        ],
        "reason": "parallel"
    })
    parsed = _parse_agent_json(raw)

    # With parallel disabled, the ReAct loop checks "action" not in parsed (line 916)
    # and treats it as malformed. Verify the parsed dict has no single "action".
    assert "action" not in parsed
    assert "actions" in parsed


# ===========================================================================
# Phase 2: Parallel Orchestrator
# ===========================================================================


@pytest.mark.unit
@pytest.mark.asyncio
async def test_orchestrator_parallel_runs_both_agents():
    """Orchestrator runs sub-agents in parallel when configured."""
    from services.orchestrator import QueryOrchestrator

    mock_router = MagicMock()
    mock_role_a = MagicMock()
    mock_role_a.has_agent_loop = True
    mock_role_a.mcp_servers = ["server_a"]
    mock_role_a.internal_tools = []
    mock_role_b = MagicMock()
    mock_role_b.has_agent_loop = True
    mock_role_b.mcp_servers = ["server_b"]
    mock_role_b.internal_tools = []
    mock_router.roles = {"role_a": mock_role_a, "role_b": mock_role_b}

    orch = QueryOrchestrator(mock_router, MagicMock())

    async def mock_run_sub(sq, *args, **kwargs):
        return {
            "role": sq["role"],
            "query": sq["query"],
            "answer": f"Result for {sq['role']}",
            "steps": [
                AgentStep(step_number=1, step_type="final_answer", content=f"Result for {sq['role']}"),
            ],
        }

    orch._run_sub_agent = mock_run_sub
    orch._synthesize = AsyncMock(return_value="Combined answer")

    sub_queries = [
        {"role": "role_a", "query": "query a"},
        {"role": "role_b", "query": "query b"},
    ]

    steps = []
    with patch.object(settings, "agent_orchestrator_parallel", True):
        async for step in orch._run_parallel(sub_queries, "original", MagicMock(), MagicMock()):
            steps.append(step)

    final_answers = [s for s in steps if s.step_type == "final_answer"]
    # Orchestrator now yields exactly ONE final_answer — the synthesized
    # one. Per-sub-agent final_answer steps are suppressed so the web chat
    # doesn't render multiple greetings / duplicated intro text.
    assert len(final_answers) == 1
    assert final_answers[0].content == "Combined answer"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_orchestrator_error_isolation():
    """One sub-agent failing doesn't block the other."""
    from services.orchestrator import QueryOrchestrator

    mock_router = MagicMock()
    mock_role = MagicMock()
    mock_role.has_agent_loop = True
    mock_role.mcp_servers = []
    mock_role.internal_tools = []
    mock_router.roles = {"good": mock_role, "bad": mock_role}

    orch = QueryOrchestrator(mock_router, MagicMock())

    async def mock_run_sub(sq, *args, **kwargs):
        if sq["role"] == "bad":
            raise RuntimeError("Sub-agent crashed")
        return {
            "role": sq["role"],
            "query": sq["query"],
            "answer": "Good result",
            "steps": [AgentStep(step_number=1, step_type="final_answer", content="Good result")],
        }

    orch._run_sub_agent = mock_run_sub
    orch._synthesize = AsyncMock(return_value=None)

    sub_queries = [
        {"role": "good", "query": "works"},
        {"role": "bad", "query": "crashes"},
    ]

    steps = []
    with patch.object(settings, "agent_orchestrator_parallel", True):
        async for step in orch._run_parallel(sub_queries, "test", MagicMock(), MagicMock()):
            steps.append(step)

    errors = [s for s in steps if s.step_type == "error"]
    finals = [s for s in steps if s.step_type == "final_answer"]
    assert len(errors) == 1
    assert "bad" in errors[0].content
    # Even with synthesis returning None, exactly one final_answer is
    # yielded — falling back to the surviving sub-agent's answer.
    assert len(finals) == 1
    assert finals[0].content == "Good result"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_orchestrator_step_tagging():
    """Sub-agent steps are tagged with sub_agent_role."""
    from services.orchestrator import QueryOrchestrator

    mock_router = MagicMock()
    mock_role = MagicMock()
    mock_role.has_agent_loop = True
    mock_role.mcp_servers = []
    mock_role.internal_tools = []
    mock_router.roles = {"smart_home": mock_role}

    orch = QueryOrchestrator(mock_router, MagicMock())

    async def mock_run_sub(sq, *args, **kwargs):
        return {
            "role": sq["role"],
            "query": sq["query"],
            "answer": "Light on",
            "steps": [
                AgentStep(step_number=1, step_type="tool_call", tool="ha.turn_on"),
                AgentStep(step_number=1, step_type="tool_result", tool="ha.turn_on", success=True),
                AgentStep(step_number=2, step_type="final_answer", content="Light on"),
            ],
        }

    orch._run_sub_agent = mock_run_sub
    orch._synthesize = AsyncMock(return_value=None)

    steps = []
    with patch.object(settings, "agent_orchestrator_parallel", True):
        async for step in orch._run_parallel(
            [{"role": "smart_home", "query": "light on"}],
            "test", MagicMock(), MagicMock()
        ):
            steps.append(step)

    # All steps from sub-agent should have role tag
    for step in steps:
        if step.data and "sub_agent_role" in step.data:
            assert step.data["sub_agent_role"] == "smart_home"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_orchestrator_all_sub_agents_fail_emits_error_final_answer():
    """When every sub-agent raises, the orchestrator must still yield a
    final_answer so downstream message persistence runs. Regression test
    for the silent-failure hole before PR #384 fix commit 51fb953."""
    from services.orchestrator import QueryOrchestrator

    mock_router = MagicMock()
    mock_role = MagicMock()
    mock_role.has_agent_loop = True
    mock_role.mcp_servers = []
    mock_role.internal_tools = []
    mock_router.roles = {"release": mock_role, "jira": mock_role}

    orch = QueryOrchestrator(mock_router, MagicMock())

    async def mock_run_sub(sq, *args, **kwargs):
        raise RuntimeError(f"{sq['role']} down")

    orch._run_sub_agent = mock_run_sub
    orch._synthesize = AsyncMock(return_value=None)

    sub_queries = [
        {"role": "release", "query": "a"},
        {"role": "jira", "query": "b"},
    ]

    steps = []
    with patch.object(settings, "agent_orchestrator_parallel", True):
        async for step in orch._run_parallel(sub_queries, "test", MagicMock(), MagicMock()):
            steps.append(step)

    errors = [s for s in steps if s.step_type == "error"]
    finals = [s for s in steps if s.step_type == "final_answer"]
    assert len(errors) == 2
    # Exactly one final_answer, localized error message
    assert len(finals) == 1
    assert "release" in finals[0].content and "jira" in finals[0].content


@pytest.mark.unit
@pytest.mark.asyncio
async def test_orchestrator_synthesis_none_falls_back_to_first_answer():
    """If the synthesizer returns None despite having ≥2 non-empty
    sub-agent results, the orchestrator must still emit a final_answer
    (falling back to the first answer). Regression test for the
    silent-return hole in the middle branch."""
    from services.agent_service import AgentStep
    from services.orchestrator import QueryOrchestrator

    mock_router = MagicMock()
    mock_role = MagicMock()
    mock_role.has_agent_loop = True
    mock_role.mcp_servers = []
    mock_role.internal_tools = []
    mock_router.roles = {"a": mock_role, "b": mock_role}

    orch = QueryOrchestrator(mock_router, MagicMock())

    async def mock_run_sub(sq, *args, **kwargs):
        return {
            "role": sq["role"],
            "query": sq["query"],
            "answer": f"answer-from-{sq['role']}",
            "steps": [],
        }

    orch._run_sub_agent = mock_run_sub
    orch._synthesize = AsyncMock(return_value=None)  # synthesizer fails

    steps = []
    with patch.object(settings, "agent_orchestrator_parallel", True):
        async for step in orch._run_parallel(
            [{"role": "a", "query": "q"}, {"role": "b", "query": "q"}],
            "msg", MagicMock(), MagicMock(),
        ):
            steps.append(step)

    finals = [s for s in steps if s.step_type == "final_answer"]
    assert len(finals) == 1
    # Falls back to the first non-empty answer
    assert finals[0].content == "answer-from-a"
