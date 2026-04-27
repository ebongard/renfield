"""
Tests für ActionExecutor

Testet:
- Intent Routing (MCP, Knowledge, General)
- MCP Tool Execution
- Fehlerbehandlung
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ============================================================================
# ActionExecutor Intent Routing Tests
# ============================================================================

class TestActionExecutorRouting:
    """Tests für Intent Routing"""

    @pytest.mark.unit
    async def test_route_mcp_intent(self, action_executor):
        """Test: MCP Intent wird an mcp_manager weitergeleitet"""
        intent_data = {
            "intent": "mcp.homeassistant.turn_on",
            "parameters": {"entity_id": "light.wohnzimmer"},
            "confidence": 0.95
        }

        result = await action_executor.execute(intent_data)

        assert result["success"] is True
        action_executor.mcp_manager.execute_tool.assert_called_once_with(
            "mcp.homeassistant.turn_on",
            {"entity_id": "light.wohnzimmer"},
            user_permissions=None,
            user_id=None,
            progress_sink=None,
        )

    @pytest.mark.unit
    async def test_route_mcp_n8n_intent(self, action_executor):
        """Test: MCP n8n Intent wird korrekt geroutet"""
        intent_data = {
            "intent": "mcp.n8n.n8n_list_workflows",
            "parameters": {},
            "confidence": 0.9
        }

        result = await action_executor.execute(intent_data)

        assert result["success"] is True
        action_executor.mcp_manager.execute_tool.assert_called_once_with(
            "mcp.n8n.n8n_list_workflows",
            {},
            user_permissions=None,
            user_id=None,
            progress_sink=None,
        )

    @pytest.mark.unit
    async def test_route_mcp_weather_intent(self, action_executor):
        """Test: MCP Weather Intent wird korrekt geroutet"""
        intent_data = {
            "intent": "mcp.weather.get_current_weather",
            "parameters": {"location": "Berlin"},
            "confidence": 0.92
        }

        result = await action_executor.execute(intent_data)

        assert result["success"] is True
        action_executor.mcp_manager.execute_tool.assert_called_once_with(
            "mcp.weather.get_current_weather",
            {"location": "Berlin"},
            user_permissions=None,
            user_id=None,
            progress_sink=None,
        )

    @pytest.mark.unit
    async def test_route_general_conversation(self, action_executor):
        """Test: Conversation Intent führt keine Aktion aus"""
        intent_data = {
            "intent": "general.conversation",
            "parameters": {},
            "confidence": 0.7
        }

        result = await action_executor.execute(intent_data)

        assert result["success"] is True
        assert result["action_taken"] is False
        assert "no action needed" in result["message"].lower()

    @pytest.mark.unit
    async def test_route_unknown_intent(self, action_executor):
        """Test: Unbekannter Intent gibt Fehler zurück"""
        intent_data = {
            "intent": "unknown.action",
            "parameters": {},
            "confidence": 0.5
        }

        result = await action_executor.execute(intent_data)

        assert result["success"] is False
        assert result["action_taken"] is False
        assert "unknown intent" in result["message"].lower()

    @pytest.mark.unit
    async def test_route_knowledge_intent(self, action_executor):
        """Test: Knowledge Intent wird an RAG-Service geroutet"""
        intent_data = {
            "intent": "knowledge.search",
            "parameters": {"query": "Docker Anleitung"},
            "confidence": 0.85
        }

        with patch("services.database.AsyncSessionLocal") as mock_session:
            mock_db = AsyncMock()
            mock_session.return_value.__aenter__ = AsyncMock(return_value=mock_db)
            mock_session.return_value.__aexit__ = AsyncMock(return_value=False)

            with patch("services.rag_service.RAGService") as mock_rag_cls:
                mock_rag = MagicMock()
                mock_rag.search = AsyncMock(return_value=[])
                mock_rag_cls.return_value = mock_rag

                result = await action_executor.execute(intent_data)

        assert result["success"] is True
        assert result["action_taken"] is True


# ============================================================================
# ActionExecutor MCP Tests
# ============================================================================

class TestActionExecutorMCP:
    """Tests für MCP Tool Execution"""

    @pytest.mark.unit
    async def test_mcp_not_available_returns_unknown(self):
        """Test: Ohne mcp_manager wird MCP Intent als unknown behandelt"""
        from services.action_executor import ActionExecutor

        executor = ActionExecutor(mcp_manager=None)

        intent_data = {
            "intent": "mcp.homeassistant.turn_on",
            "parameters": {"entity_id": "light.test"},
            "confidence": 0.9
        }

        result = await executor.execute(intent_data)

        assert result["success"] is False
        assert "unknown intent" in result["message"].lower()

    @pytest.mark.unit
    async def test_mcp_tool_failure_propagated(self, action_executor):
        """Test: MCP Tool Fehler werden propagiert"""
        action_executor.mcp_manager.execute_tool.return_value = {
            "success": False,
            "message": "Tool execution failed: rate limited",
            "action_taken": False
        }

        intent_data = {
            "intent": "mcp.weather.get_forecast",
            "parameters": {"location": "Berlin"},
            "confidence": 0.9
        }

        result = await action_executor.execute(intent_data)

        assert result["success"] is False
        assert "rate limited" in result["message"]

    @pytest.mark.unit
    async def test_mcp_user_id_not_injected_into_parameters(self, action_executor):
        """Regression: user_id must NOT land in the MCP tool's `parameters` dict.

        MCP tools have strict Pydantic schemas and reject unknown keys with
        "Unexpected keyword argument". user_id is passed as a separate
        kwarg to execute_tool() (for permission checks and audit), never
        as a tool parameter.

        This is a regression guard — the fix lived on feat/web-chat-v2
        (commit f45c98e, 2026-04-11) but never reached main, so every
        MCP call for authenticated users started failing silently again
        when JWT WS auth went live. Don't let the same mistake land
        twice.
        """
        intent_data = {
            "intent": "mcp.release.list_releases",
            "parameters": {"status": "active"},  # original user-provided params
            "confidence": 0.95,
        }

        await action_executor.execute(
            intent_data,
            user_permissions=["release.read"],
            user_id=7,  # authenticated user
        )

        action_executor.mcp_manager.execute_tool.assert_called_once()
        call_args = action_executor.mcp_manager.execute_tool.call_args
        # Positional: (intent, parameters)
        passed_intent, passed_parameters = call_args.args
        assert passed_intent == "mcp.release.list_releases"
        assert passed_parameters == {"status": "active"}, (
            f"user_id leaked into MCP parameters: {passed_parameters!r}. "
            f"It must stay out of `parameters` and only travel via the "
            f"execute_tool kwarg so the tool's schema validation passes."
        )
        # But user_id MUST still reach execute_tool as a kwarg — that's
        # what downstream permission checks read.
        assert call_args.kwargs.get("user_id") == 7


# ============================================================================
# pre_mcp_call hook Tests
# ============================================================================

class TestActionExecutorPreMCPCall:
    """Tests for the pre_mcp_call plugin hook."""

    @pytest.mark.unit
    async def test_pre_mcp_call_replaces_parameters(self, action_executor):
        """Hook handler returning a dict replaces parameters before execute_tool."""
        from utils.hooks import register_hook, _hooks

        async def rewrite(intent, parameters, user_id=None, **_):
            if intent == "mcp.release.get_release":
                return {"id": "Applications/Folder/Release1"}
            return None

        register_hook("pre_mcp_call", rewrite)
        try:
            await action_executor.execute({
                "intent": "mcp.release.get_release",
                "parameters": {"title": "Product A 1.3.5"},
                "confidence": 0.9,
            })
        finally:
            _hooks["pre_mcp_call"].remove(rewrite)

        action_executor.mcp_manager.execute_tool.assert_called_once()
        call_args = action_executor.mcp_manager.execute_tool.call_args
        assert call_args.args[1] == {"id": "Applications/Folder/Release1"}

    @pytest.mark.unit
    async def test_pre_mcp_call_none_leaves_parameters_unchanged(self, action_executor):
        """Hook returning None leaves the original parameters intact."""
        from utils.hooks import register_hook, _hooks

        async def noop(intent, parameters, user_id=None, **_):
            return None

        register_hook("pre_mcp_call", noop)
        try:
            await action_executor.execute({
                "intent": "mcp.release.get_release",
                "parameters": {"id": "Applications/Folder/Release1"},
                "confidence": 0.9,
            })
        finally:
            _hooks["pre_mcp_call"].remove(noop)

        call_args = action_executor.mcp_manager.execute_tool.call_args
        assert call_args.args[1] == {"id": "Applications/Folder/Release1"}

    @pytest.mark.unit
    async def test_pre_mcp_call_first_dict_wins(self, action_executor):
        """When multiple handlers return dicts, the first one wins."""
        from utils.hooks import register_hook, _hooks

        async def first(intent, parameters, user_id=None, **_):
            return {"id": "first"}

        async def second(intent, parameters, user_id=None, **_):
            return {"id": "second"}

        register_hook("pre_mcp_call", first)
        register_hook("pre_mcp_call", second)
        try:
            await action_executor.execute({
                "intent": "mcp.release.get_release",
                "parameters": {"title": "x"},
                "confidence": 0.9,
            })
        finally:
            _hooks["pre_mcp_call"].remove(first)
            _hooks["pre_mcp_call"].remove(second)

        call_args = action_executor.mcp_manager.execute_tool.call_args
        assert call_args.args[1] == {"id": "first"}


# ============================================================================
# ActionExecutor Edge Cases Tests
# ============================================================================

class TestActionExecutorEdgeCases:
    """Tests für Edge Cases"""

    @pytest.mark.unit
    async def test_missing_intent(self, action_executor):
        """Test: Fehlender Intent wird als conversation behandelt"""
        intent_data = {
            "parameters": {},
            "confidence": 0.5
        }

        result = await action_executor.execute(intent_data)

        assert result["success"] is True
        assert result["action_taken"] is False

    @pytest.mark.unit
    async def test_knowledge_intent_without_query(self, action_executor):
        """Test: Knowledge Intent ohne Query gibt Fehler"""
        intent_data = {
            "intent": "knowledge.search",
            "parameters": {},
            "confidence": 0.9
        }

        result = await action_executor.execute(intent_data)

        assert result["success"] is False
        assert "keine suchanfrage" in result["message"].lower()


# ============================================================================
# ActionExecutor Knowledge empty_result Tests
# ============================================================================

class TestActionExecutorKnowledgeEmptyResult:
    """Tests für empty_result Flag bei Knowledge-Intents"""

    @pytest.mark.unit
    async def test_knowledge_empty_result_flag(self):
        """Test: Knowledge Intent mit 0 Ergebnissen setzt empty_result=True"""
        from services.action_executor import ActionExecutor

        executor = ActionExecutor()

        intent_data = {
            "intent": "knowledge.search",
            "parameters": {"query": "nonexistent topic"},
            "confidence": 0.8
        }

        with patch("services.database.AsyncSessionLocal") as mock_session:
            mock_db = AsyncMock()
            mock_session.return_value.__aenter__ = AsyncMock(return_value=mock_db)
            mock_session.return_value.__aexit__ = AsyncMock(return_value=False)

            with patch("services.rag_service.RAGService") as mock_rag_cls:
                mock_rag = MagicMock()
                mock_rag.search = AsyncMock(return_value=[])
                mock_rag_cls.return_value = mock_rag

                result = await executor.execute(intent_data)

        assert result["success"] is True
        assert result["empty_result"] is True
        assert result["data"]["results_count"] == 0

    @pytest.mark.unit
    async def test_knowledge_with_results_no_empty_flag(self):
        """Test: Knowledge Intent mit Ergebnissen hat kein empty_result"""
        from services.action_executor import ActionExecutor

        executor = ActionExecutor()

        intent_data = {
            "intent": "knowledge.search",
            "parameters": {"query": "Docker"},
            "confidence": 0.8
        }

        with patch("services.database.AsyncSessionLocal") as mock_session:
            mock_db = AsyncMock()
            mock_session.return_value.__aenter__ = AsyncMock(return_value=mock_db)
            mock_session.return_value.__aexit__ = AsyncMock(return_value=False)

            with patch("services.rag_service.RAGService") as mock_rag_cls:
                mock_rag = MagicMock()
                mock_rag.search = AsyncMock(return_value=[
                    {"similarity": 0.8, "chunk": {"content": "Docker is..."}, "document": {"filename": "notes.md"}}
                ])
                mock_rag_cls.return_value = mock_rag

                result = await executor.execute(intent_data)

        assert result["success"] is True
        assert result.get("empty_result") is not True
        assert result["data"]["results_count"] == 1


# ============================================================================
# User-ID Propagation Tests
# ============================================================================

class TestActionExecutorUserIdPropagation:
    """Tests for user_id propagation to MCP tools."""

    @pytest.mark.unit
    async def test_user_id_injected_into_mcp_params(self, action_executor):
        """user_id is injected as _user_id into MCP tool parameters."""
        action_executor.mcp_manager.execute_tool.return_value = {
            "success": True,
            "message": "OK",
            "data": {},
        }

        intent_data = {
            "intent": "mcp.calendar.list_events",
            "parameters": {"calendar": "work"},
            "confidence": 0.9,
        }

        await action_executor.execute(intent_data, user_id=42)

        # Verify user_id was injected into the parameters
        call_args = action_executor.mcp_manager.execute_tool.call_args
        params = call_args.args[1]  # second positional arg = arguments
        assert params["user_id"] == 42
        assert params["calendar"] == "work"
        assert call_args.kwargs["user_id"] == 42

    @pytest.mark.unit
    async def test_no_user_id_means_no_injection(self, action_executor):
        """Without user_id, user_id is NOT added to parameters."""
        action_executor.mcp_manager.execute_tool.return_value = {
            "success": True,
            "message": "OK",
            "data": {},
        }

        intent_data = {
            "intent": "mcp.calendar.list_events",
            "parameters": {"calendar": "work"},
            "confidence": 0.9,
        }

        await action_executor.execute(intent_data)

        call_args = action_executor.mcp_manager.execute_tool.call_args
        params = call_args.args[1]
        assert "user_id" not in params

    @pytest.mark.unit
    async def test_user_id_none_means_no_injection(self, action_executor):
        """user_id=None explicitly does NOT inject _user_id."""
        action_executor.mcp_manager.execute_tool.return_value = {
            "success": True,
            "message": "OK",
            "data": {},
        }

        intent_data = {
            "intent": "mcp.weather.get_weather",
            "parameters": {"location": "Berlin"},
            "confidence": 0.9,
        }

        await action_executor.execute(intent_data, user_id=None)

        call_args = action_executor.mcp_manager.execute_tool.call_args
        params = call_args.args[1]
        assert "user_id" not in params

    @pytest.mark.unit
    async def test_user_id_not_injected_for_non_mcp(self):
        """user_id is ignored for non-MCP intents (knowledge, conversation)."""
        from services.action_executor import ActionExecutor

        executor = ActionExecutor(mcp_manager=None)

        intent_data = {
            "intent": "general.conversation",
            "parameters": {},
            "confidence": 0.9,
        }

        result = await executor.execute(intent_data, user_id=42)
        assert result["success"] is True
        assert result["action_taken"] is False
