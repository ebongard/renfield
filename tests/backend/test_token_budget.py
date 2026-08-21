"""Tests for Token Budget Enforcement -- progressive prompt reduction."""

import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Stub 'ollama' only if genuinely absent — in the real test container it
# IS installed, and stubbing it poisons later tests that import it.
if "ollama" not in sys.modules:
    try:
        import ollama  # noqa: F401
    except Exception:  # noqa: BLE001
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

        with patch("services.agent_service.settings") as s, \
             patch("services.agent_service.effective_agent_num_ctx", return_value=32768):
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

        with patch("services.agent_service.settings") as s, \
             patch("services.agent_service.effective_agent_num_ctx", return_value=32768):
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

        with patch("services.agent_service.settings") as s, \
             patch("services.agent_service.effective_agent_num_ctx", return_value=32768):
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


class TestTokenBudgetLogLine:
    """Reva-compat: `_enforce_token_budget` must emit a `Token budget: N/M (X%)`
    log line on entry. Reva's `test_token_budget_logged` E2E asserts on this
    substring; subsequent "Budget pass N (...)" lines are observability and
    not part of the contract.
    """

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_emits_canonical_log_line(self):
        agent = _make_agent()
        ctx = AgentContext(original_message="test")
        prompt = "Hello " * 100

        captured: list[str] = []
        with patch("services.agent_service.settings") as s, \
             patch("services.agent_service.effective_agent_num_ctx", return_value=32768), \
             patch("services.agent_service.logger") as log:
            s.ollama_num_ctx = 32768
            s.agent_default_num_predict = 2048
            s.agent_budget_threshold = 0.85
            log.info.side_effect = lambda msg, *a, **kw: captured.append(msg)

            await agent._enforce_token_budget(
                prompt, ctx, "test", None,
                memory_context="", document_context="", lang="de",
            )

        # At least one log line must start with "Token budget:" — that's the
        # contract Reva relies on. Format: "Token budget: <used>/<max> (<%>)".
        canonical = [m for m in captured if m.startswith("Token budget:")]
        assert canonical, f"No canonical log line found. Captured: {captured}"
        # Sanity-check the format: should contain a slash, a percentage sign, parens.
        assert "/" in canonical[0]
        assert "%)" in canonical[0]


class TestConfigurableContentCaps:
    """The former hardcoded content caps must follow their settings so a
    large-context deployment can raise them via ConfigMap."""

    @pytest.mark.unit
    def test_compress_history_message_follows_setting(self, monkeypatch):
        from services.agent_service import _compress_history_message
        monkeypatch.setattr(
            "services.agent_service.settings.agent_history_message_max_chars", 2000
        )
        content = "y" * 3000
        out = _compress_history_message(content)
        assert len(out) == 2003  # 2000 chars + "..."
        # Explicit max_chars argument still wins over the setting
        assert len(_compress_history_message(content, max_chars=150)) == 153

    @pytest.mark.unit
    def test_tool_result_text_cap_follows_setting(self, monkeypatch):
        monkeypatch.setattr(
            "services.agent_service.settings.agent_tool_result_text_max_chars", 12000
        )
        ctx = AgentContext(original_message="test")
        ctx.steps = [
            AgentStep(step_number=1, step_type="tool_result",
                      content="z" * 20000, tool="t"),
        ]
        prompt = ctx.build_history_prompt(lang="de")
        assert "z" * 12000 in prompt
        assert "z" * 12001 not in prompt


class TestBackendAwareBudget:
    """The budget must follow the effective serving backend: when the agent
    tier routes to the OpenAI-compat llama-server and its --ctx-size is
    declared (llm_openai_num_ctx), a prompt far over the Ollama window must
    pass through un-reduced."""

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_large_ctx_prompt_passes_untouched(self):
        agent = _make_agent()
        ctx = AgentContext(original_message="test")
        # ~30k tokens — over 85% of 32768, but far under 262144.
        large_prompt = "x" * 120000

        with patch("services.agent_service.settings") as s, \
             patch("services.agent_service.effective_agent_num_ctx", return_value=262144):
            s.agent_default_num_predict = 2048
            s.agent_budget_threshold = 0.85

            prompt, mem, doc, _hist = await agent._enforce_token_budget(
                large_prompt, ctx, "test", None,
                memory_context="mem", document_context="doc", lang="de",
            )
            assert prompt == large_prompt
            assert mem == "mem"
            assert doc == "doc"

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_same_prompt_reduced_on_ollama_ctx(self):
        """Control: identical prompt IS reduced when the effective window is
        the Ollama one — proves the backend switch is what widens the budget."""
        agent = _make_agent()
        ctx = AgentContext(original_message="test")
        large_prompt = "x" * 120000

        async def mock_build(*args, **kwargs):
            return "x" * 50000

        agent._build_agent_prompt = mock_build

        with patch("services.agent_service.settings") as s, \
             patch("services.agent_service.effective_agent_num_ctx", return_value=32768):
            s.agent_default_num_predict = 2048
            s.agent_budget_threshold = 0.85

            prompt, _, _, _ = await agent._enforce_token_budget(
                large_prompt, ctx, "test", None,
                memory_context="mem", document_context="doc", lang="de",
            )
            assert len(prompt) < len(large_prompt)
