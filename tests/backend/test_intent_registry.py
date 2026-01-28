"""
Tests for IntentRegistry — Dynamic intent management.
"""
import pytest
from unittest.mock import patch, MagicMock

from services.intent_registry import (
    IntentRegistry,
    IntentDef,
    IntentParam,
    IntegrationIntents,
    CORE_INTEGRATIONS,
    HOME_ASSISTANT_INTENTS,
    KNOWLEDGE_INTENTS,
    CAMERA_INTENTS,
    N8N_INTENTS,
    GENERAL_INTENTS,
    intent_registry,
)


class TestIntentDef:
    """Tests for IntentDef dataclass."""

    def test_get_description_german(self):
        """Test getting German description."""
        intent = IntentDef(
            name="test.intent",
            description_de="Deutsche Beschreibung",
            description_en="English description",
        )
        assert intent.get_description("de") == "Deutsche Beschreibung"

    def test_get_description_english(self):
        """Test getting English description."""
        intent = IntentDef(
            name="test.intent",
            description_de="Deutsche Beschreibung",
            description_en="English description",
        )
        assert intent.get_description("en") == "English description"

    def test_get_description_default_german(self):
        """Test default language is German."""
        intent = IntentDef(
            name="test.intent",
            description_de="Deutsche Beschreibung",
            description_en="English description",
        )
        assert intent.get_description() == "Deutsche Beschreibung"

    def test_get_examples(self):
        """Test getting examples in different languages."""
        intent = IntentDef(
            name="test.intent",
            description_de="Test",
            description_en="Test",
            examples_de=["Beispiel 1", "Beispiel 2"],
            examples_en=["Example 1", "Example 2"],
        )
        assert intent.get_examples("de") == ["Beispiel 1", "Beispiel 2"]
        assert intent.get_examples("en") == ["Example 1", "Example 2"]


class TestIntegrationIntents:
    """Tests for IntegrationIntents dataclass."""

    def test_get_title_german(self):
        """Test getting German title."""
        integration = IntegrationIntents(
            integration_name="test",
            title_de="Test Integration",
            title_en="Test Integration EN",
            intents=[],
            is_enabled_func=lambda: True,
        )
        assert integration.get_title("de") == "Test Integration"

    def test_get_title_english(self):
        """Test getting English title."""
        integration = IntegrationIntents(
            integration_name="test",
            title_de="Test Integration",
            title_en="Test Integration EN",
            intents=[],
            is_enabled_func=lambda: True,
        )
        assert integration.get_title("en") == "Test Integration EN"


class TestCoreIntegrationDefinitions:
    """Tests for core integration definitions."""

    def test_home_assistant_intents_defined(self):
        """Test Home Assistant intents are properly defined."""
        assert HOME_ASSISTANT_INTENTS.integration_name == "homeassistant"
        assert len(HOME_ASSISTANT_INTENTS.intents) > 0

        # Check some key intents exist
        intent_names = [i.name for i in HOME_ASSISTANT_INTENTS.intents]
        assert "homeassistant.turn_on" in intent_names
        assert "homeassistant.turn_off" in intent_names
        assert "homeassistant.get_state" in intent_names

    def test_knowledge_intents_defined(self):
        """Test Knowledge/RAG intents are properly defined."""
        assert KNOWLEDGE_INTENTS.integration_name == "knowledge"

        intent_names = [i.name for i in KNOWLEDGE_INTENTS.intents]
        assert "knowledge.search" in intent_names
        assert "knowledge.ask" in intent_names

    def test_camera_intents_defined(self):
        """Test Camera/Frigate intents are properly defined."""
        assert CAMERA_INTENTS.integration_name == "camera"

        intent_names = [i.name for i in CAMERA_INTENTS.intents]
        assert "camera.get_events" in intent_names
        assert "camera.list_cameras" in intent_names

    def test_n8n_intents_defined(self):
        """Test n8n workflow intents are properly defined."""
        assert N8N_INTENTS.integration_name == "n8n"

        intent_names = [i.name for i in N8N_INTENTS.intents]
        assert "n8n.trigger" in intent_names

    def test_general_intents_always_enabled(self):
        """Test general intents are always enabled."""
        assert GENERAL_INTENTS.is_enabled_func() is True

        intent_names = [i.name for i in GENERAL_INTENTS.intents]
        assert "general.conversation" in intent_names

    def test_all_core_integrations_registered(self):
        """Test all core integrations are in CORE_INTEGRATIONS."""
        integration_names = [i.integration_name for i in CORE_INTEGRATIONS]
        assert "homeassistant" in integration_names
        assert "knowledge" in integration_names
        assert "camera" in integration_names
        assert "n8n" in integration_names
        assert "general" in integration_names


class TestIntentRegistry:
    """Tests for IntentRegistry class."""

    def test_singleton_instance(self):
        """Test global singleton instance exists."""
        assert intent_registry is not None
        assert isinstance(intent_registry, IntentRegistry)

    @patch("services.intent_registry.settings")
    def test_get_enabled_integrations_all_disabled(self, mock_settings):
        """Test no integrations enabled returns only general."""
        mock_settings.home_assistant_url = None
        mock_settings.home_assistant_token = None
        mock_settings.rag_enabled = False
        mock_settings.frigate_url = None
        mock_settings.n8n_webhook_url = None

        registry = IntentRegistry()
        enabled = registry.get_enabled_integrations()

        # Only general.conversation should be enabled
        integration_names = [i.integration_name for i in enabled]
        assert "general" in integration_names
        assert "homeassistant" not in integration_names
        assert "knowledge" not in integration_names

    @patch("services.intent_registry.settings")
    def test_get_enabled_integrations_ha_enabled(self, mock_settings):
        """Test Home Assistant enabled when URL and token set."""
        mock_settings.home_assistant_url = "http://ha.local:8123"
        mock_settings.home_assistant_token = "test-token"
        mock_settings.rag_enabled = False
        mock_settings.frigate_url = None
        mock_settings.n8n_webhook_url = None

        registry = IntentRegistry()
        enabled = registry.get_enabled_integrations()

        integration_names = [i.integration_name for i in enabled]
        assert "homeassistant" in integration_names
        assert "general" in integration_names

    @patch("services.intent_registry.settings")
    def test_get_enabled_integrations_rag_enabled(self, mock_settings):
        """Test RAG/Knowledge enabled when rag_enabled is True."""
        mock_settings.home_assistant_url = None
        mock_settings.home_assistant_token = None
        mock_settings.rag_enabled = True
        mock_settings.frigate_url = None
        mock_settings.n8n_webhook_url = None

        registry = IntentRegistry()
        enabled = registry.get_enabled_integrations()

        integration_names = [i.integration_name for i in enabled]
        assert "knowledge" in integration_names

    @patch("services.intent_registry.settings")
    def test_is_intent_available(self, mock_settings):
        """Test checking if intent is available."""
        mock_settings.home_assistant_url = "http://ha.local"
        mock_settings.home_assistant_token = "token"
        mock_settings.rag_enabled = True
        mock_settings.frigate_url = None
        mock_settings.n8n_webhook_url = None

        registry = IntentRegistry()

        # Available intents
        assert registry.is_intent_available("homeassistant.turn_on") is True
        assert registry.is_intent_available("knowledge.search") is True
        assert registry.is_intent_available("general.conversation") is True

        # Unavailable intent (Frigate not configured)
        assert registry.is_intent_available("camera.get_events") is False

    @patch("services.intent_registry.settings")
    def test_get_intent_definition(self, mock_settings):
        """Test getting intent definition by name."""
        mock_settings.home_assistant_url = "http://ha.local"
        mock_settings.home_assistant_token = "token"
        mock_settings.rag_enabled = True
        mock_settings.frigate_url = None
        mock_settings.n8n_webhook_url = None

        registry = IntentRegistry()

        intent = registry.get_intent_definition("homeassistant.turn_on")
        assert intent is not None
        assert intent.name == "homeassistant.turn_on"
        assert len(intent.parameters) > 0

        # Non-existent intent
        assert registry.get_intent_definition("nonexistent.intent") is None

    @patch("services.intent_registry.settings")
    def test_build_intent_prompt_german(self, mock_settings):
        """Test building intent prompt in German."""
        mock_settings.home_assistant_url = "http://ha.local"
        mock_settings.home_assistant_token = "token"
        mock_settings.rag_enabled = True
        mock_settings.frigate_url = None
        mock_settings.n8n_webhook_url = None
        mock_settings.plugins_enabled = False
        mock_settings.mcp_enabled = False

        registry = IntentRegistry()
        prompt = registry.build_intent_prompt(lang="de")

        # Check German content
        assert "SMART HOME (Home Assistant)" in prompt
        assert "WISSENSDATENBANK (RAG)" in prompt
        assert "homeassistant.turn_on" in prompt
        assert "knowledge.search" in prompt

    @patch("services.intent_registry.settings")
    def test_build_intent_prompt_english(self, mock_settings):
        """Test building intent prompt in English."""
        mock_settings.home_assistant_url = "http://ha.local"
        mock_settings.home_assistant_token = "token"
        mock_settings.rag_enabled = True
        mock_settings.frigate_url = None
        mock_settings.n8n_webhook_url = None
        mock_settings.plugins_enabled = False
        mock_settings.mcp_enabled = False

        registry = IntentRegistry()
        prompt = registry.build_intent_prompt(lang="en")

        # Check English content
        assert "SMART HOME (Home Assistant)" in prompt
        assert "KNOWLEDGE BASE (RAG)" in prompt
        assert "Turn device on" in prompt

    @patch("services.intent_registry.settings")
    def test_build_intent_prompt_excludes_disabled(self, mock_settings):
        """Test that disabled integrations are excluded from prompt."""
        mock_settings.home_assistant_url = None  # HA disabled
        mock_settings.home_assistant_token = None
        mock_settings.rag_enabled = True
        mock_settings.frigate_url = None
        mock_settings.n8n_webhook_url = None
        mock_settings.plugins_enabled = False
        mock_settings.mcp_enabled = False

        registry = IntentRegistry()
        prompt = registry.build_intent_prompt(lang="de")

        # HA should not be in prompt
        assert "homeassistant.turn_on" not in prompt
        # RAG should be in prompt
        assert "knowledge.search" in prompt

    @patch("services.intent_registry.settings")
    def test_build_examples_prompt(self, mock_settings):
        """Test building examples prompt."""
        mock_settings.home_assistant_url = "http://ha.local"
        mock_settings.home_assistant_token = "token"
        mock_settings.rag_enabled = True
        mock_settings.frigate_url = None
        mock_settings.n8n_webhook_url = None

        registry = IntentRegistry()
        examples = registry.build_examples_prompt(lang="de", max_examples=5)

        assert "BEISPIELE:" in examples
        # Should have some examples
        assert "homeassistant" in examples or "knowledge" in examples

    @patch("services.intent_registry.settings")
    def test_get_status(self, mock_settings):
        """Test getting registry status."""
        mock_settings.home_assistant_url = "http://ha.local"
        mock_settings.home_assistant_token = "token"
        mock_settings.rag_enabled = True
        mock_settings.frigate_url = None
        mock_settings.n8n_webhook_url = None

        registry = IntentRegistry()
        status = registry.get_status()

        assert "enabled_integrations" in status
        assert "disabled_integrations" in status
        assert "total_intents" in status

        enabled_names = [i["name"] for i in status["enabled_integrations"]]
        assert "homeassistant" in enabled_names
        assert "knowledge" in enabled_names

        assert "camera" in status["disabled_integrations"]
        assert "n8n" in status["disabled_integrations"]

    def test_set_plugin_registry(self):
        """Test setting plugin registry."""
        registry = IntentRegistry()
        mock_plugin_registry = MagicMock()

        registry.set_plugin_registry(mock_plugin_registry)

        assert registry._plugin_registry == mock_plugin_registry

    def test_set_mcp_tools(self):
        """Test setting MCP tools."""
        registry = IntentRegistry()
        mcp_tools = [
            {"intent": "mcp.server.tool1", "description": "Tool 1"},
            {"intent": "mcp.server.tool2", "description": "Tool 2"},
        ]

        registry.set_mcp_tools(mcp_tools)

        assert registry._mcp_tools == mcp_tools

    @patch("services.intent_registry.settings")
    def test_is_intent_available_with_mcp(self, mock_settings):
        """Test MCP tools are checked for intent availability."""
        mock_settings.home_assistant_url = None
        mock_settings.home_assistant_token = None
        mock_settings.rag_enabled = False
        mock_settings.frigate_url = None
        mock_settings.n8n_webhook_url = None

        registry = IntentRegistry()
        registry.set_mcp_tools([
            {"intent": "mcp.weather.get_forecast", "description": "Get weather"}
        ])

        assert registry.is_intent_available("mcp.weather.get_forecast") is True
        assert registry.is_intent_available("mcp.nonexistent.tool") is False


class TestIntentParameters:
    """Tests for intent parameter definitions."""

    def test_required_parameters_marked(self):
        """Test required parameters are properly marked."""
        turn_on = None
        for intent in HOME_ASSISTANT_INTENTS.intents:
            if intent.name == "homeassistant.turn_on":
                turn_on = intent
                break

        assert turn_on is not None
        assert len(turn_on.parameters) > 0

        entity_id_param = turn_on.parameters[0]
        assert entity_id_param.name == "entity_id"
        assert entity_id_param.required is True

    def test_optional_parameters(self):
        """Test optional parameters exist."""
        get_events = None
        for intent in CAMERA_INTENTS.intents:
            if intent.name == "camera.get_events":
                get_events = intent
                break

        assert get_events is not None
        # camera.get_events has optional parameters
        has_optional = any(not p.required for p in get_events.parameters)
        assert has_optional is True
