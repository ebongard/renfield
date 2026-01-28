"""
Tests for AgentService — ReAct Agent Loop for multi-step tool chaining.
"""

import pytest
import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch
from services.agent_service import (
    AgentService,
    AgentStep,
    AgentContext,
    _parse_agent_json,
    _truncate,
    step_to_ws_message,
)
from services.agent_tools import AgentToolRegistry


# ============================================================================
# Helper: collect all steps from the async generator
# ============================================================================

async def collect_steps(agent, **kwargs) -> list:
    steps = []
    async for step in agent.run(**kwargs):
        steps.append(step)
    return steps


# ============================================================================
# Test _parse_agent_json
# ============================================================================

class TestParseAgentJson:
    """Test robust JSON parsing from LLM output."""

    @pytest.mark.unit
    def test_clean_json(self):
        raw = '{"action": "final_answer", "answer": "Hello"}'
        result = _parse_agent_json(raw)
        assert result is not None
        assert result["action"] == "final_answer"

    @pytest.mark.unit
    def test_markdown_code_block(self):
        raw = '```json\n{"action": "final_answer", "answer": "Hello"}\n```'
        result = _parse_agent_json(raw)
        assert result is not None
        assert result["action"] == "final_answer"

    @pytest.mark.unit
    def test_json_with_surrounding_text(self):
        raw = 'Here is my response: {"action": "final_answer", "answer": "Hello"} end.'
        result = _parse_agent_json(raw)
        assert result is not None
        assert result["action"] == "final_answer"

    @pytest.mark.unit
    def test_invalid_json_returns_none(self):
        raw = "This is just plain text without JSON."
        result = _parse_agent_json(raw)
        assert result is None

    @pytest.mark.unit
    def test_nested_json(self):
        raw = '{"action": "weather.get_current", "parameters": {"location": "Berlin"}, "reason": "test"}'
        result = _parse_agent_json(raw)
        assert result is not None
        assert result["action"] == "weather.get_current"
        assert result["parameters"]["location"] == "Berlin"

    @pytest.mark.unit
    def test_json_with_trailing_text(self):
        raw = '{"action": "final_answer", "answer": "Done"} some trailing text'
        result = _parse_agent_json(raw)
        assert result is not None
        assert result["action"] == "final_answer"

    @pytest.mark.unit
    def test_empty_string(self):
        assert _parse_agent_json("") is None

    @pytest.mark.unit
    def test_whitespace_only(self):
        assert _parse_agent_json("   \n  ") is None


# ============================================================================
# Test _truncate
# ============================================================================

class TestTruncate:

    @pytest.mark.unit
    def test_short_text(self):
        assert _truncate("Hello", 300) == "Hello"

    @pytest.mark.unit
    def test_exact_length(self):
        text = "a" * 300
        assert _truncate(text, 300) == text

    @pytest.mark.unit
    def test_long_text(self):
        text = "a" * 500
        result = _truncate(text, 300)
        assert len(result) == 303  # 300 + "..."
        assert result.endswith("...")


# ============================================================================
# Test AgentContext
# ============================================================================

class TestAgentContext:

    @pytest.mark.unit
    def test_empty_history_prompt(self):
        ctx = AgentContext(original_message="test")
        assert ctx.build_history_prompt() == ""

    @pytest.mark.unit
    def test_history_prompt_with_steps(self):
        ctx = AgentContext(original_message="test")
        ctx.steps.append(AgentStep(
            step_number=1,
            step_type="tool_call",
            tool="weather.get_current",
            parameters={"location": "Berlin"}
        ))
        ctx.steps.append(AgentStep(
            step_number=1,
            step_type="tool_result",
            content="12°C, bewölkt",
            tool="weather.get_current",
            success=True,
        ))

        prompt = ctx.build_history_prompt()
        assert "BISHERIGE SCHRITTE" in prompt
        assert "weather.get_current" in prompt
        assert "Berlin" in prompt
        assert "12°C" in prompt

    @pytest.mark.unit
    def test_history_prompt_truncates_results(self):
        ctx = AgentContext(original_message="test")
        ctx.steps.append(AgentStep(
            step_number=1,
            step_type="tool_result",
            content="x" * 500,
            tool="test",
            success=True,
        ))

        prompt = ctx.build_history_prompt()
        # Result should be truncated to 300 chars in the prompt
        assert len(prompt) < 500


# ============================================================================
# Test step_to_ws_message
# ============================================================================

class TestStepToWsMessage:

    @pytest.mark.unit
    def test_thinking_step(self):
        step = AgentStep(step_number=0, step_type="thinking", content="Analysiere...")
        msg = step_to_ws_message(step)
        assert msg["type"] == "agent_thinking"
        assert msg["step"] == 0
        assert msg["content"] == "Analysiere..."

    @pytest.mark.unit
    def test_tool_call_step(self):
        step = AgentStep(
            step_number=1, step_type="tool_call",
            tool="weather.get_current",
            parameters={"location": "Berlin"},
            reason="Hole Wetterdaten"
        )
        msg = step_to_ws_message(step)
        assert msg["type"] == "agent_tool_call"
        assert msg["tool"] == "weather.get_current"
        assert msg["parameters"]["location"] == "Berlin"
        assert msg["reason"] == "Hole Wetterdaten"

    @pytest.mark.unit
    def test_tool_result_step(self):
        step = AgentStep(
            step_number=1, step_type="tool_result",
            tool="weather.get_current",
            content="12°C", success=True,
            data={"temperature": 12}
        )
        msg = step_to_ws_message(step)
        assert msg["type"] == "agent_tool_result"
        assert msg["success"] is True
        assert msg["message"] == "12°C"
        assert msg["data"]["temperature"] == 12

    @pytest.mark.unit
    def test_final_answer_step(self):
        step = AgentStep(step_number=2, step_type="final_answer", content="Es sind 12°C.")
        msg = step_to_ws_message(step)
        assert msg["type"] == "stream"
        assert msg["content"] == "Es sind 12°C."

    @pytest.mark.unit
    def test_error_step(self):
        step = AgentStep(step_number=1, step_type="error", content="Fehler!", tool="bad_tool")
        msg = step_to_ws_message(step)
        assert msg["type"] == "agent_tool_result"
        assert msg["success"] is False


# ============================================================================
# Test AgentService.run() — Core Agent Loop
# ============================================================================

class TestAgentServiceRun:
    """Test the Agent Loop with mocked OllamaService and ActionExecutor."""

    def _make_registry(self, tools=None):
        """Create a tool registry with optional custom tools."""
        registry = AgentToolRegistry(ha_available=True)
        return registry

    def _make_ollama_mock(self, responses):
        """
        Create a mock OllamaService whose client.chat() returns
        successive JSON responses.
        """
        ollama = MagicMock()
        ollama.client = MagicMock()

        call_count = 0

        async def mock_chat(**kwargs):
            nonlocal call_count
            idx = min(call_count, len(responses) - 1)
            call_count += 1
            resp = MagicMock()
            resp.message = MagicMock()
            resp.message.content = responses[idx]
            return resp

        ollama.client.chat = mock_chat
        return ollama

    def _make_executor_mock(self, results=None):
        """Create a mock ActionExecutor."""
        executor = AsyncMock()
        if results:
            executor.execute = AsyncMock(side_effect=results)
        else:
            executor.execute = AsyncMock(return_value={
                "success": True,
                "message": "Aktion ausgeführt",
                "action_taken": True,
            })
        return executor

    @pytest.mark.unit
    async def test_direct_final_answer(self):
        """LLM immediately returns final_answer — should yield thinking + final_answer."""
        registry = self._make_registry()
        ollama = self._make_ollama_mock([
            '{"action": "final_answer", "answer": "Hallo!", "reason": "Einfache Frage"}'
        ])
        executor = self._make_executor_mock()

        agent = AgentService(registry, max_steps=5)
        steps = await collect_steps(
            agent, message="Hallo", ollama=ollama, executor=executor
        )

        step_types = [s.step_type for s in steps]
        assert "thinking" in step_types
        assert "final_answer" in step_types
        assert steps[-1].content == "Hallo!"

    @pytest.mark.unit
    async def test_one_tool_then_answer(self):
        """LLM calls one tool, then gives final answer."""
        registry = self._make_registry()
        ollama = self._make_ollama_mock([
            '{"action": "homeassistant.get_state", "parameters": {"entity_id": "sensor.temp"}, "reason": "Check temp"}',
            '{"action": "final_answer", "answer": "Es sind 22°C.", "reason": "Done"}'
        ])
        executor = self._make_executor_mock([
            {"success": True, "message": "22°C", "action_taken": True}
        ])

        agent = AgentService(registry, max_steps=5)
        steps = await collect_steps(
            agent, message="Wie warm ist es?", ollama=ollama, executor=executor
        )

        step_types = [s.step_type for s in steps]
        assert "thinking" in step_types
        assert "tool_call" in step_types
        assert "tool_result" in step_types
        assert "final_answer" in step_types

        # Check tool call details
        tool_call = next(s for s in steps if s.step_type == "tool_call")
        assert tool_call.tool == "homeassistant.get_state"
        assert tool_call.parameters["entity_id"] == "sensor.temp"

        # Check final answer
        final = next(s for s in steps if s.step_type == "final_answer")
        assert "22°C" in final.content

    @pytest.mark.unit
    async def test_invalid_tool_continues_loop(self):
        """LLM calls an invalid tool — error is recorded, loop continues."""
        registry = self._make_registry()
        ollama = self._make_ollama_mock([
            '{"action": "nonexistent.tool", "parameters": {}, "reason": "test"}',
            '{"action": "final_answer", "answer": "Fallback answer", "reason": "Recovered"}'
        ])
        executor = self._make_executor_mock()

        agent = AgentService(registry, max_steps=5)
        steps = await collect_steps(
            agent, message="Test invalid tool", ollama=ollama, executor=executor
        )

        step_types = [s.step_type for s in steps]
        assert "error" in step_types
        assert "final_answer" in step_types

        error_step = next(s for s in steps if s.step_type == "error")
        assert "nonexistent.tool" in error_step.content

    @pytest.mark.unit
    async def test_json_parse_failure_returns_raw_text(self):
        """When LLM returns non-JSON, raw text becomes the final answer."""
        registry = self._make_registry()
        ollama = self._make_ollama_mock([
            "Ich weiß die Antwort nicht, hier ist etwas Text."
        ])
        executor = self._make_executor_mock()

        agent = AgentService(registry, max_steps=5)
        steps = await collect_steps(
            agent, message="Some question", ollama=ollama, executor=executor
        )

        final = steps[-1]
        assert final.step_type == "final_answer"
        assert "Antwort nicht" in final.content or len(final.content) > 0

    @pytest.mark.unit
    async def test_max_steps_reached(self):
        """When max steps are exhausted, LLM summarizes collected results."""
        registry = self._make_registry()

        # LLM keeps calling tools, then on the summary call returns natural language
        call_count = 0

        ollama = MagicMock()
        ollama.client = MagicMock()

        async def mock_chat(**kwargs):
            nonlocal call_count
            call_count += 1
            resp = MagicMock()
            resp.message = MagicMock()
            # First calls: tool actions. Later call: summary.
            messages = kwargs.get("messages", [])
            user_content = messages[-1].get("content", "") if messages else ""
            if "Fasse die Ergebnisse" in user_content:
                resp.message.content = "Die Temperatur beträgt 22°C."
            else:
                resp.message.content = '{"action": "homeassistant.get_state", "parameters": {"entity_id": "sensor.temp"}, "reason": "Check"}'
            return resp

        ollama.client.chat = mock_chat
        executor = self._make_executor_mock([
            {"success": True, "message": "22°C", "action_taken": True}
        ] * 10)

        agent = AgentService(registry, max_steps=2)
        steps = await collect_steps(
            agent, message="Infinite loop test", ollama=ollama, executor=executor
        )

        # Should end with a final_answer from LLM summary
        assert steps[-1].step_type == "final_answer"
        assert "22°C" in steps[-1].content

    @pytest.mark.unit
    async def test_tool_execution_error(self):
        """Tool execution raises exception — error is caught and loop continues."""
        registry = self._make_registry()
        ollama = self._make_ollama_mock([
            '{"action": "homeassistant.turn_on", "parameters": {"entity_id": "light.test"}, "reason": "Turn on"}',
            '{"action": "final_answer", "answer": "Fehler beim Einschalten.", "reason": "Error"}'
        ])
        executor = self._make_executor_mock()
        executor.execute = AsyncMock(side_effect=[
            Exception("Connection refused"),
            {"success": True, "message": "OK", "action_taken": True},
        ])

        agent = AgentService(registry, max_steps=5)
        steps = await collect_steps(
            agent, message="Schalte Licht ein", ollama=ollama, executor=executor
        )

        # Tool result should show failure
        tool_results = [s for s in steps if s.step_type == "tool_result"]
        assert len(tool_results) == 1
        assert tool_results[0].success is False
        assert "Connection refused" in tool_results[0].content

    @pytest.mark.unit
    async def test_llm_call_exception(self):
        """LLM call raises exception — error + fallback answer yielded."""
        registry = self._make_registry()
        ollama = MagicMock()
        ollama.client = MagicMock()

        async def failing_chat(**kwargs):
            raise RuntimeError("Model not loaded")

        ollama.client.chat = failing_chat
        executor = self._make_executor_mock()

        agent = AgentService(registry, max_steps=5)
        steps = await collect_steps(
            agent, message="Test error", ollama=ollama, executor=executor
        )

        step_types = [s.step_type for s in steps]
        assert "error" in step_types
        assert "final_answer" in step_types
        assert "Fehler" in steps[-1].content

    @pytest.mark.unit
    async def test_step_timeout(self):
        """Per-step timeout — should yield error + summary answer."""
        registry = self._make_registry()
        ollama = MagicMock()
        ollama.client = MagicMock()

        call_count = 0

        async def slow_then_fast_chat(**kwargs):
            nonlocal call_count
            call_count += 1
            # First call is slow (triggers timeout), summary call is fast
            messages = kwargs.get("messages", [])
            user_content = messages[-1].get("content", "") if messages else ""
            if "Fasse die Ergebnisse" in user_content or call_count > 1:
                resp = MagicMock()
                resp.message = MagicMock()
                resp.message.content = "Zusammenfassung."
                return resp
            await asyncio.sleep(10)  # Very slow — will timeout

        ollama.client.chat = slow_then_fast_chat
        executor = self._make_executor_mock()

        agent = AgentService(registry, max_steps=5, step_timeout=0.1)
        steps = await collect_steps(
            agent, message="Timeout test", ollama=ollama, executor=executor
        )

        step_types = [s.step_type for s in steps]
        assert "error" in step_types
        assert "final_answer" in step_types

    @pytest.mark.unit
    async def test_conversation_history_included(self):
        """Conversation history should be included in the agent prompt."""
        registry = self._make_registry()
        ollama = self._make_ollama_mock([
            '{"action": "final_answer", "answer": "OK", "reason": "Done"}'
        ])
        executor = self._make_executor_mock()

        history = [
            {"role": "user", "content": "Was ist das Wetter?"},
            {"role": "assistant", "content": "Es sind 15°C in Berlin."},
        ]

        agent = AgentService(registry, max_steps=5)

        # We need to verify the prompt contains history. Capture the chat call.
        chat_calls = []
        original_chat = ollama.client.chat

        async def capturing_chat(**kwargs):
            chat_calls.append(kwargs)
            return await original_chat(**kwargs)

        ollama.client.chat = capturing_chat

        steps = await collect_steps(
            agent, message="Und morgen?", ollama=ollama, executor=executor,
            conversation_history=history
        )

        assert len(chat_calls) >= 1
        prompt_content = chat_calls[0]["messages"][1]["content"]
        assert "KONVERSATIONS-KONTEXT" in prompt_content
        assert "Was ist das Wetter?" in prompt_content


# ============================================================================
# Test AgentService — Timeout and Safety
# ============================================================================

class TestAgentServiceSafety:

    @pytest.mark.unit
    async def test_total_timeout(self):
        """Total timeout should stop the agent even if steps are within per-step limit."""
        registry = AgentToolRegistry(ha_available=True)
        ollama = MagicMock()
        ollama.client = MagicMock()

        call_count = 0

        async def slow_but_within_step(**kwargs):
            nonlocal call_count
            call_count += 1
            resp = MagicMock()
            resp.message = MagicMock()
            # Check if this is the summary call
            messages = kwargs.get("messages", [])
            user_content = messages[-1].get("content", "") if messages else ""
            if "Fasse die Ergebnisse" in user_content:
                resp.message.content = "Zusammenfassung der Ergebnisse."
                return resp
            await asyncio.sleep(0.05)  # 50ms per step
            resp.message.content = '{"action": "homeassistant.get_state", "parameters": {"entity_id": "test"}, "reason": "loop"}'
            return resp

        ollama.client.chat = slow_but_within_step

        executor = AsyncMock()
        executor.execute = AsyncMock(return_value={
            "success": True, "message": "OK", "action_taken": True
        })

        # total_timeout=0.1s, step_timeout=1s, max_steps=100
        # Should hit total timeout before max steps
        agent = AgentService(registry, max_steps=100, step_timeout=1.0, total_timeout=0.15)

        steps = []
        async for step in agent.run(
            message="Long running", ollama=ollama, executor=executor
        ):
            steps.append(step)

        # Should have been stopped early
        assert steps[-1].step_type == "final_answer"
        assert call_count < 100  # Didn't exhaust all steps

    @pytest.mark.unit
    async def test_build_summary_answer_with_results(self):
        """Summary answer should call LLM to produce natural language from tool results."""
        registry = AgentToolRegistry(ha_available=True)
        agent = AgentService(registry)

        ctx = AgentContext(original_message="Wie ist das Wetter?")
        ctx.steps.append(AgentStep(
            step_number=1, step_type="tool_result",
            content="Wetter: 15°C, sonnig", success=True, tool="weather"
        ))
        ctx.steps.append(AgentStep(
            step_number=2, step_type="tool_result",
            content="Fehler", success=False, tool="search"
        ))

        # Mock LLM that returns a nice summary
        ollama = MagicMock()
        ollama.client = MagicMock()

        async def mock_chat(**kwargs):
            resp = MagicMock()
            resp.message = MagicMock()
            resp.message.content = "In Berlin sind es aktuell 15°C bei sonnigem Wetter."
            return resp

        ollama.client.chat = mock_chat

        answer = await agent._build_summary_answer(ctx, 3, "Wie ist das Wetter?", ollama, "test-model")
        assert answer.step_type == "final_answer"
        assert "15°C" in answer.content
        # Should be natural language, not raw tool output
        assert "Successfully executed" not in answer.content

    @pytest.mark.unit
    async def test_build_summary_answer_no_results(self):
        """Summary answer without any results should give generic message."""
        registry = AgentToolRegistry(ha_available=True)
        agent = AgentService(registry)

        ctx = AgentContext(original_message="test")
        ollama = MagicMock()

        answer = await agent._build_summary_answer(ctx, 1, "test", ollama, "test-model")
        assert "nicht vollständig" in answer.content or "Entschuldigung" in answer.content

    @pytest.mark.unit
    async def test_build_summary_answer_llm_failure_fallback(self):
        """When LLM summary fails, should return a fallback message."""
        registry = AgentToolRegistry(ha_available=True)
        agent = AgentService(registry)

        ctx = AgentContext(original_message="test")
        ctx.steps.append(AgentStep(
            step_number=1, step_type="tool_result",
            content="some data", success=True, tool="test"
        ))

        ollama = MagicMock()
        ollama.client = MagicMock()

        async def failing_chat(**kwargs):
            raise RuntimeError("Model crashed")

        ollama.client.chat = failing_chat

        answer = await agent._build_summary_answer(ctx, 2, "test", ollama, "test-model")
        assert answer.step_type == "final_answer"
        assert "Entschuldigung" in answer.content or "zusammenfassen" in answer.content

    @pytest.mark.unit
    def test_build_fallback_answer(self):
        """Fallback answer should include the error message."""
        registry = AgentToolRegistry(ha_available=True)
        agent = AgentService(registry)

        ctx = AgentContext(original_message="test")
        answer = agent._build_fallback_answer(ctx, 1, "Model crashed")
        assert "Model crashed" in answer.content
        assert answer.step_type == "final_answer"


# ============================================================================
# Test Empty Response Retry Mechanism
# ============================================================================

class TestAgentServiceRetry:
    """Test the retry-on-empty-response mechanism."""

    def _make_registry(self):
        return AgentToolRegistry(ha_available=True)

    def _make_executor_mock(self, results=None):
        executor = AsyncMock()
        if results:
            executor.execute = AsyncMock(side_effect=results)
        else:
            executor.execute = AsyncMock(return_value={
                "success": True,
                "message": "OK",
                "action_taken": True,
            })
        return executor

    @pytest.mark.unit
    async def test_empty_response_triggers_retry(self):
        """When LLM returns empty string, retry with nudge should be attempted."""
        registry = self._make_registry()
        ollama = MagicMock()
        ollama.client = MagicMock()

        call_count = 0

        async def mock_chat(**kwargs):
            nonlocal call_count
            call_count += 1
            resp = MagicMock()
            resp.message = MagicMock()
            if call_count == 1:
                # First call returns empty
                resp.message.content = ""
            else:
                # Retry returns valid JSON
                resp.message.content = '{"action": "final_answer", "answer": "Retried!", "reason": "OK"}'
            return resp

        ollama.client.chat = mock_chat
        executor = self._make_executor_mock()

        agent = AgentService(registry, max_steps=5)
        steps = await collect_steps(
            agent, message="Test retry", ollama=ollama, executor=executor
        )

        # Should have succeeded via retry
        final = steps[-1]
        assert final.step_type == "final_answer"
        assert "Retried!" in final.content
        # Two calls: original + retry
        assert call_count == 2

    @pytest.mark.unit
    async def test_empty_response_retry_also_empty(self):
        """When both original and retry return empty, fallback message is used."""
        registry = self._make_registry()
        ollama = MagicMock()
        ollama.client = MagicMock()

        async def mock_chat(**kwargs):
            resp = MagicMock()
            resp.message = MagicMock()
            resp.message.content = ""
            return resp

        ollama.client.chat = mock_chat
        executor = self._make_executor_mock()

        agent = AgentService(registry, max_steps=5)
        steps = await collect_steps(
            agent, message="Test double empty", ollama=ollama, executor=executor
        )

        final = steps[-1]
        assert final.step_type == "final_answer"
        assert "Entschuldigung" in final.content or "bearbeiten" in final.content

    @pytest.mark.unit
    async def test_retry_with_tool_call_after_empty(self):
        """Empty response followed by retry that returns a tool call."""
        registry = self._make_registry()
        ollama = MagicMock()
        ollama.client = MagicMock()

        call_count = 0

        async def mock_chat(**kwargs):
            nonlocal call_count
            call_count += 1
            resp = MagicMock()
            resp.message = MagicMock()
            if call_count == 1:
                resp.message.content = ""  # Empty
            elif call_count == 2:
                resp.message.content = '{"action": "homeassistant.get_state", "parameters": {"entity_id": "sensor.temp"}, "reason": "Check"}'
            else:
                resp.message.content = '{"action": "final_answer", "answer": "22 Grad", "reason": "Done"}'
            return resp

        ollama.client.chat = mock_chat
        executor = self._make_executor_mock([
            {"success": True, "message": "22°C", "action_taken": True, "data": {"temperature": 22}}
        ])

        agent = AgentService(registry, max_steps=5)
        steps = await collect_steps(
            agent, message="Test retry + tool", ollama=ollama, executor=executor
        )

        step_types = [s.step_type for s in steps]
        assert "tool_call" in step_types
        assert "tool_result" in step_types
        assert "final_answer" in step_types


# ============================================================================
# Test Tool Result Data Inclusion
# ============================================================================

class TestToolResultDataInclusion:
    """Test that tool results include actual data for LLM reasoning."""

    def _make_registry(self):
        return AgentToolRegistry(ha_available=True)

    @pytest.mark.unit
    async def test_tool_result_includes_data(self):
        """Tool result content should include actual data, not just 'Successfully executed'."""
        registry = self._make_registry()

        ollama = MagicMock()
        ollama.client = MagicMock()

        call_count = 0

        async def mock_chat(**kwargs):
            nonlocal call_count
            call_count += 1
            resp = MagicMock()
            resp.message = MagicMock()
            if call_count <= 1:
                resp.message.content = '{"action": "homeassistant.get_state", "parameters": {"entity_id": "sensor.temp"}, "reason": "Check"}'
            else:
                resp.message.content = '{"action": "final_answer", "answer": "Done", "reason": "OK"}'
            return resp

        ollama.client.chat = mock_chat

        executor = AsyncMock()
        executor.execute = AsyncMock(return_value={
            "success": True,
            "message": "Successfully executed homeassistant.get_state",
            "action_taken": True,
            "data": {"state": "22.5", "unit": "°C", "entity_id": "sensor.temp"},
        })

        agent = AgentService(registry, max_steps=5)
        steps = await collect_steps(
            agent, message="Wie warm?", ollama=ollama, executor=executor
        )

        tool_results = [s for s in steps if s.step_type == "tool_result"]
        assert len(tool_results) == 1
        # Content should include the actual data, not just "Successfully executed"
        assert "22.5" in tool_results[0].content
        assert "Daten:" in tool_results[0].content

    @pytest.mark.unit
    async def test_tool_result_without_data_uses_message(self):
        """Tool result without data should use just the message."""
        registry = self._make_registry()

        ollama = MagicMock()
        ollama.client = MagicMock()

        call_count = 0

        async def mock_chat(**kwargs):
            nonlocal call_count
            call_count += 1
            resp = MagicMock()
            resp.message = MagicMock()
            if call_count <= 1:
                resp.message.content = '{"action": "homeassistant.turn_on", "parameters": {"entity_id": "light.test"}, "reason": "On"}'
            else:
                resp.message.content = '{"action": "final_answer", "answer": "Done", "reason": "OK"}'
            return resp

        ollama.client.chat = mock_chat

        executor = AsyncMock()
        executor.execute = AsyncMock(return_value={
            "success": True,
            "message": "Licht eingeschaltet",
            "action_taken": True,
        })

        agent = AgentService(registry, max_steps=5)
        steps = await collect_steps(
            agent, message="Licht an", ollama=ollama, executor=executor
        )

        tool_results = [s for s in steps if s.step_type == "tool_result"]
        assert len(tool_results) == 1
        assert tool_results[0].content == "Licht eingeschaltet"
        assert "Daten:" not in tool_results[0].content
