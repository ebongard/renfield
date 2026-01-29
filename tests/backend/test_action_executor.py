"""
Tests für ActionExecutor

Testet:
- Intent Routing (MCP, Knowledge, Plugin, General)
- MCP Tool Execution
- Plugin Dispatch
- Fehlerbehandlung
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch


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
            {"entity_id": "light.wohnzimmer"}
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
            {}
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
            {"location": "Berlin"}
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
    async def test_route_plugin_intent(self, action_executor, mock_plugin_registry):
        """Test: Plugin Intent wird an Plugin Registry weitergeleitet"""
        mock_plugin = AsyncMock()
        mock_plugin.execute.return_value = {
            "success": True,
            "message": "Weather data fetched",
            "action_taken": True
        }
        mock_plugin_registry.get_plugin_for_intent.return_value = mock_plugin

        intent_data = {
            "intent": "weather.get_current",
            "parameters": {"location": "Berlin"},
            "confidence": 0.9
        }

        result = await action_executor.execute(intent_data)

        assert result["success"] is True
        mock_plugin.execute.assert_called_once_with(
            "weather.get_current",
            {"location": "Berlin"}
        )

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

        executor = ActionExecutor(plugin_registry=None, mcp_manager=None)

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
# ActionExecutor Plugin Integration Tests
# ============================================================================

class TestActionExecutorPluginIntegration:
    """Tests für Plugin Integration"""

    @pytest.mark.unit
    async def test_plugin_not_found_returns_unknown(
        self, action_executor, mock_plugin_registry
    ):
        """Test: Nicht gefundenes Plugin führt zu unknown intent"""
        mock_plugin_registry.get_plugin_for_intent.return_value = None

        intent_data = {
            "intent": "custom.unregistered_action",
            "parameters": {},
            "confidence": 0.8
        }

        result = await action_executor.execute(intent_data)

        assert result["success"] is False
        assert "unknown intent" in result["message"].lower()

    @pytest.mark.unit
    async def test_plugin_execution_error(
        self, action_executor, mock_plugin_registry
    ):
        """Test: Plugin Execution Fehler werden propagiert"""
        mock_plugin = AsyncMock()
        mock_plugin.execute.return_value = {
            "success": False,
            "message": "API rate limited",
            "action_taken": False
        }
        mock_plugin_registry.get_plugin_for_intent.return_value = mock_plugin

        intent_data = {
            "intent": "plugin.rate_limited",
            "parameters": {},
            "confidence": 0.9
        }

        result = await action_executor.execute(intent_data)

        assert result["success"] is False
        assert result["message"] == "API rate limited"

    @pytest.mark.unit
    async def test_no_plugin_registry(self):
        """Test: ActionExecutor ohne Plugin Registry"""
        from services.action_executor import ActionExecutor

        executor = ActionExecutor(plugin_registry=None, mcp_manager=None)

        intent_data = {
            "intent": "custom.action",
            "parameters": {},
            "confidence": 0.9
        }

        result = await executor.execute(intent_data)

        assert result["success"] is False
