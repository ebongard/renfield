"""
Tests für ActionExecutor

Testet:
- Intent Routing
- Home Assistant Aktionen
- n8n Workflow Ausführung
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
    async def test_route_homeassistant_intent(self, action_executor, mock_ha_client):
        """Test: HomeAssistant Intent wird korrekt geroutet"""
        intent_data = {
            "intent": "homeassistant.turn_on",
            "parameters": {"entity_id": "light.wohnzimmer"},
            "confidence": 0.95
        }

        result = await action_executor.execute(intent_data)

        assert result["success"] is True
        assert result["action_taken"] is True
        mock_ha_client.turn_on.assert_called_once_with("light.wohnzimmer")

    @pytest.mark.unit
    async def test_route_n8n_intent(self, action_executor, mock_n8n_client):
        """Test: n8n Intent wird korrekt geroutet"""
        intent_data = {
            "intent": "n8n.trigger_workflow",
            "parameters": {"workflow_id": "test-workflow"},
            "confidence": 0.9
        }

        result = await action_executor.execute(intent_data)

        assert result["success"] is True
        assert result["action_taken"] is True
        mock_n8n_client.trigger_workflow.assert_called_once()

    @pytest.mark.unit
    async def test_route_camera_intent(self, action_executor):
        """Test: Camera Intent wird korrekt geroutet"""
        intent_data = {
            "intent": "camera.get_snapshot",
            "parameters": {"camera": "front_door"},
            "confidence": 0.85
        }

        result = await action_executor.execute(intent_data)

        # Camera actions are currently placeholders
        assert result["action_taken"] is False

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


# ============================================================================
# ActionExecutor Home Assistant Tests
# ============================================================================

class TestActionExecutorHomeAssistant:
    """Tests für Home Assistant Aktionen"""

    @pytest.mark.unit
    async def test_turn_on(self, action_executor, mock_ha_client):
        """Test: turn_on Aktion"""
        intent_data = {
            "intent": "homeassistant.turn_on",
            "parameters": {"entity_id": "light.wohnzimmer"},
            "confidence": 0.95
        }

        result = await action_executor.execute(intent_data)

        assert result["success"] is True
        assert result["entity_id"] == "light.wohnzimmer"
        assert "eingeschaltet" in result["message"]

    @pytest.mark.unit
    async def test_turn_off(self, action_executor, mock_ha_client):
        """Test: turn_off Aktion"""
        intent_data = {
            "intent": "homeassistant.turn_off",
            "parameters": {"entity_id": "switch.fernseher"},
            "confidence": 0.92
        }

        result = await action_executor.execute(intent_data)

        assert result["success"] is True
        mock_ha_client.turn_off.assert_called_once_with("switch.fernseher")
        assert "ausgeschaltet" in result["message"]

    @pytest.mark.unit
    async def test_toggle(self, action_executor, mock_ha_client):
        """Test: toggle Aktion"""
        intent_data = {
            "intent": "homeassistant.toggle",
            "parameters": {"entity_id": "light.flur"},
            "confidence": 0.88
        }

        result = await action_executor.execute(intent_data)

        assert result["success"] is True
        mock_ha_client.toggle.assert_called_once_with("light.flur")
        assert "umgeschaltet" in result["message"]

    @pytest.mark.unit
    async def test_get_state(self, action_executor, mock_ha_client):
        """Test: get_state Aktion"""
        mock_ha_client.get_state.return_value = {
            "state": "on",
            "attributes": {"friendly_name": "Küchen Licht"}
        }

        intent_data = {
            "intent": "homeassistant.get_state",
            "parameters": {"entity_id": "light.kueche"},
            "confidence": 0.9
        }

        result = await action_executor.execute(intent_data)

        assert result["success"] is True
        assert result["state"] == "on"
        assert "eingeschaltet" in result["message"]

    @pytest.mark.unit
    async def test_check_state_alias(self, action_executor, mock_ha_client):
        """Test: check_state wird wie get_state behandelt"""
        mock_ha_client.get_state.return_value = {
            "state": "off",
            "attributes": {"friendly_name": "Test Light"}
        }

        intent_data = {
            "intent": "homeassistant.check_state",
            "parameters": {"entity_id": "light.test"},
            "confidence": 0.85
        }

        result = await action_executor.execute(intent_data)

        assert result["success"] is True

    @pytest.mark.unit
    async def test_set_value(self, action_executor, mock_ha_client):
        """Test: set_value Aktion"""
        mock_ha_client.set_value.return_value = True

        intent_data = {
            "intent": "homeassistant.set_value",
            "parameters": {
                "entity_id": "light.wohnzimmer",
                "value": 50,
                "attribute": "brightness"
            },
            "confidence": 0.9
        }

        result = await action_executor.execute(intent_data)

        assert result["success"] is True
        mock_ha_client.set_value.assert_called_once()

    @pytest.mark.unit
    async def test_entity_search_by_name(self, action_executor, mock_ha_client):
        """Test: Entity wird nach Namen gesucht wenn keine entity_id"""
        mock_ha_client.search_entities.return_value = [
            {"entity_id": "light.wohnzimmer", "friendly_name": "Wohnzimmer Licht"}
        ]

        intent_data = {
            "intent": "homeassistant.turn_on",
            "parameters": {"name": "Wohnzimmer Licht"},
            "confidence": 0.9
        }

        result = await action_executor.execute(intent_data)

        assert result["success"] is True
        mock_ha_client.search_entities.assert_called_once_with("Wohnzimmer Licht")
        mock_ha_client.turn_on.assert_called_once_with("light.wohnzimmer")

    @pytest.mark.unit
    async def test_entity_not_found(self, action_executor, mock_ha_client):
        """Test: Fehler wenn Entity nicht gefunden"""
        mock_ha_client.search_entities.return_value = []

        intent_data = {
            "intent": "homeassistant.turn_on",
            "parameters": {"name": "Nicht Existierendes Gerät"},
            "confidence": 0.9
        }

        result = await action_executor.execute(intent_data)

        assert result["success"] is False
        assert "konnte kein gerät" in result["message"].lower()

    @pytest.mark.unit
    async def test_ha_exception_handling(self, action_executor, mock_ha_client):
        """Test: Exceptions werden abgefangen"""
        mock_ha_client.turn_on.side_effect = Exception("Connection failed")

        intent_data = {
            "intent": "homeassistant.turn_on",
            "parameters": {"entity_id": "light.test"},
            "confidence": 0.9
        }

        result = await action_executor.execute(intent_data)

        assert result["success"] is False
        assert "fehler" in result["message"].lower()


# ============================================================================
# ActionExecutor n8n Tests
# ============================================================================

class TestActionExecutorN8N:
    """Tests für n8n Workflow Ausführung"""

    @pytest.mark.unit
    async def test_trigger_workflow_by_id(self, action_executor, mock_n8n_client):
        """Test: Workflow nach ID triggern"""
        mock_n8n_client.trigger_workflow.return_value = {
            "success": True,
            "executionId": "exec-123"
        }

        intent_data = {
            "intent": "n8n.trigger",
            "parameters": {
                "workflow_id": "workflow-123",
                "data": {"param": "value"}
            },
            "confidence": 0.9
        }

        result = await action_executor.execute(intent_data)

        assert result["success"] is True
        assert result["action_taken"] is True
        mock_n8n_client.trigger_workflow.assert_called_once_with(
            "workflow-123",
            {"param": "value"}
        )

    @pytest.mark.unit
    async def test_trigger_workflow_by_name(self, action_executor, mock_n8n_client):
        """Test: Workflow nach Name triggern"""
        mock_n8n_client.trigger_workflow.return_value = {"success": True}

        intent_data = {
            "intent": "n8n.trigger",
            "parameters": {"workflow_name": "backup-workflow"},
            "confidence": 0.85
        }

        result = await action_executor.execute(intent_data)

        assert result["success"] is True

    @pytest.mark.unit
    async def test_n8n_exception_handling(self, action_executor, mock_n8n_client):
        """Test: n8n Fehler werden abgefangen"""
        mock_n8n_client.trigger_workflow.side_effect = Exception("Webhook failed")

        intent_data = {
            "intent": "n8n.trigger",
            "parameters": {"workflow_id": "failing-workflow"},
            "confidence": 0.9
        }

        result = await action_executor.execute(intent_data)

        assert result["success"] is False
        assert "fehler" in result["message"].lower()


# ============================================================================
# ActionExecutor State Translation Tests
# ============================================================================

class TestActionExecutorStateTranslation:
    """Tests für State-Übersetzung"""

    @pytest.mark.unit
    def test_translate_on_state(self, action_executor):
        """Test: 'on' wird zu 'eingeschaltet'"""
        result = action_executor._translate_state("on")
        assert result == "eingeschaltet"

    @pytest.mark.unit
    def test_translate_off_state(self, action_executor):
        """Test: 'off' wird zu 'ausgeschaltet'"""
        result = action_executor._translate_state("off")
        assert result == "ausgeschaltet"

    @pytest.mark.unit
    def test_translate_open_state(self, action_executor):
        """Test: 'open' wird zu 'offen'"""
        result = action_executor._translate_state("open")
        assert result == "offen"

    @pytest.mark.unit
    def test_translate_closed_state(self, action_executor):
        """Test: 'closed' wird zu 'geschlossen'"""
        result = action_executor._translate_state("closed")
        assert result == "geschlossen"

    @pytest.mark.unit
    def test_translate_playing_state(self, action_executor):
        """Test: 'playing' wird zu 'läuft'"""
        result = action_executor._translate_state("playing")
        assert result == "läuft"

    @pytest.mark.unit
    def test_translate_unknown_state(self, action_executor):
        """Test: Unbekannte States werden unverändert zurückgegeben"""
        result = action_executor._translate_state("custom_state")
        assert result == "custom_state"

    @pytest.mark.unit
    def test_translate_case_insensitive(self, action_executor):
        """Test: Übersetzung ist case-insensitive"""
        assert action_executor._translate_state("ON") == "eingeschaltet"
        assert action_executor._translate_state("Off") == "ausgeschaltet"
        assert action_executor._translate_state("PLAYING") == "läuft"


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
    async def test_empty_parameters(self, action_executor, mock_ha_client):
        """Test: Leere Parameter bei HA Action"""
        intent_data = {
            "intent": "homeassistant.turn_on",
            "parameters": {},
            "confidence": 0.9
        }

        result = await action_executor.execute(intent_data)

        # Action executor tries to turn_on with None entity_id
        # The mock succeeds, so the result is success
        assert result["success"] is True
        mock_ha_client.turn_on.assert_called_once_with(None)

    @pytest.mark.unit
    async def test_low_confidence_still_executes(self, action_executor, mock_ha_client):
        """Test: Auch niedrige Confidence führt Aktion aus"""
        intent_data = {
            "intent": "homeassistant.turn_on",
            "parameters": {"entity_id": "light.test"},
            "confidence": 0.3
        }

        result = await action_executor.execute(intent_data)

        # ActionExecutor prüft Confidence nicht
        assert result["success"] is True
        mock_ha_client.turn_on.assert_called_once()

    @pytest.mark.unit
    async def test_unknown_ha_intent(self, action_executor):
        """Test: Unbekannter HA Intent"""
        intent_data = {
            "intent": "homeassistant.unknown_action",
            "parameters": {"entity_id": "light.test"},
            "confidence": 0.9
        }

        result = await action_executor.execute(intent_data)

        assert result["success"] is False
        assert "unbekannter" in result["message"].lower()


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
    async def test_no_plugin_registry(self, mock_ha_client, mock_n8n_client):
        """Test: ActionExecutor ohne Plugin Registry"""
        from services.action_executor import ActionExecutor

        executor = ActionExecutor(plugin_registry=None)
        executor.ha_client = mock_ha_client
        executor.n8n_client = mock_n8n_client

        intent_data = {
            "intent": "custom.action",
            "parameters": {},
            "confidence": 0.9
        }

        result = await executor.execute(intent_data)

        assert result["success"] is False
