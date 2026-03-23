"""Tests for Token Budget Enforcement -- progressive prompt reduction."""

import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Ensure 'ollama' module is available
if "ollama" not in sys.modules:
    sys.modules["ollama"] = MagicMock()

from services.agent_service import AgentContext, AgentService, AgentStep
from services.agent_tools import AgentToolRegistry


def _make_agent() -> AgentService:
    """Create an AgentService with mock registry."""
    registry = MagicMock(spec=AgentToolRegistry)
    registry._tools = {}
    registry.get_tool_names.return_value = []
    registry.build_tools_prompt.return_value = "TOOLS: none"
    role = MagicMock()
    role.max_steps = 5
    role.prompt_key = "agent_prompt"
    role.model = None
    role.ollama_url = None
    role.name = "general"
    return AgentService(registry, role=role)


class TestAgentContextTruncateHistory:

    @pytest.mark.unit
    def test_truncates_long_results(self):
        ctx = AgentContext(original_message="test")
        ctx.steps = [
            AgentStep(step_number=1, step_type="tool_call", content="call", tool="t"),
            AgentStep(step_number=1, step_type="tool_result", content="x" * 2000, tool="t"),
        ]
        ctx.truncate_history_results(max_chars=100)
        assert len(ctx.steps[1].content) < 200
        assert ctx.steps[1].content.endswith("...[truncated]")

    @pytest.mark.unit
    def test_leaves_short_results(self):
        ctx = AgentContext(original_message="test")
        ctx.steps = [
            AgentStep(step_number=1, step_type="tool_result", content="short", tool="t"),
        ]
        ctx.truncate_history_results(max_chars=100)
        assert ctx.steps[0].content == "short"

    @pytest.mark.unit
    def test_only_truncates_tool_results(self):
        ctx = AgentContext(original_message="test")
        ctx.steps = [
            AgentStep(step_number=1, step_type="tool_call", content="x" * 2000, tool="t"),
        ]
        ctx.truncate_history_results(max_chars=100)
        assert len(ctx.steps[0].content) == 2000  # tool_call not truncated


class TestEnforceTokenBudget:

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_under_budget_no_change(self):
        """Prompt under threshold passes through unchanged."""
        agent = _make_agent()
        ctx = AgentContext(original_message="test")
        short_prompt = "Hello " * 100  # ~600 chars ~ 150 tokens

        with patch("services.agent_service.settings") as s:
            s.ollama_num_ctx = 32768
            s.agent_default_num_predict = 2048
            s.agent_budget_threshold = 0.85

            result = await agent._enforce_token_budget(
                short_prompt, ctx, "test", None,
                memory_context="", document_context="", lang="de",
            )
            prompt, mem, doc, hist = result
            assert prompt == short_prompt
            assert mem == ""
            assert doc == ""

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_over_budget_drops_memory(self):
        """Large prompt triggers memory context removal."""
        agent = _make_agent()
        ctx = AgentContext(original_message="test")
        # Simulate a large prompt
        large_prompt = "x" * 120000  # ~30k tokens with 4 chars/token > 85% of 32k

        # Make _build_agent_prompt return progressively smaller prompts
        call_count = 0

        async def mock_build(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            mem = kwargs.get("memory_context", "")
            doc = kwargs.get("document_context", "")
            # Each removed section saves ~20000 chars
            size = 120000
            if not mem:
                size -= 40000
            if not doc:
                size -= 20000
            return "x" * size

        agent._build_agent_prompt = mock_build

        with patch("services.agent_service.settings") as s:
            s.ollama_num_ctx = 32768
            s.agent_default_num_predict = 2048
            s.agent_budget_threshold = 0.85

            result = await agent._enforce_token_budget(
                large_prompt, ctx, "test", None,
                memory_context="big memory " * 1000,
                document_context="big doc " * 1000,
                lang="de",
            )
            prompt, mem, doc, hist = result
            # Memory or document context should have been dropped
            assert len(prompt) < len(large_prompt)

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_conversation_history_halved(self):
        """Over-budget triggers conversation history reduction."""
        agent = _make_agent()
        ctx = AgentContext(original_message="test")
        large_prompt = "x" * 120000

        full_history = [{"role": "user", "content": f"msg {i}"} for i in range(20)]
        received_history = None

        async def mock_build(*args, **kwargs):
            nonlocal received_history
            hist = args[2] if len(args) > 2 else kwargs.get("conversation_history")
            received_history = hist
            # Simulate prompt getting smaller with less history
            return "x" * 50000  # Still under budget after first pass

        agent._build_agent_prompt = mock_build

        with patch("services.agent_service.settings") as s:
            s.ollama_num_ctx = 32768
            s.agent_default_num_predict = 2048
            s.agent_budget_threshold = 0.85

            result = await agent._enforce_token_budget(
                large_prompt, ctx, "test", full_history,
                memory_context="", document_context="", lang="de",
            )
            _, _, _, returned_hist = result
            # History should have been reduced to last 3
            assert returned_hist is not None
            assert len(returned_hist) <= 3
