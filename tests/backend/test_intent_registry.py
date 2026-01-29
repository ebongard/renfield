"""
Tests for IntentRegistry — Dynamic intent management.

Core integrations are now limited to Knowledge (RAG) and General.
Home Assistant, n8n, and Camera intents are provided via MCP servers.
"""
import pytest
from unittest.mock import patch, MagicMock

from services.intent_registry import (
    IntentRegistry,
    IntentDef,
    IntentParam,
    IntegrationIntents,
    CORE_INTEGRATIONS,
    KNOWLEDGE_INTENTS,
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

    def test_knowledge_intents_defined(self):
        """Test Knowledge/RAG intents are properly defined."""
        assert KNOWLEDGE_INTENTS.integration_name == "knowledge"

        intent_names = [i.name for i in KNOWLEDGE_INTENTS.intents]
        assert "knowledge.search" in intent_names
        assert "knowledge.ask" in intent_names

    def test_general_intents_always_enabled(self):
        """Test general intents are always enabled."""
        assert GENERAL_INTENTS.is_enabled_func() is True

        intent_names = [i.name for i in GENERAL_INTENTS.intents]
        assert "general.conversation" in intent_names

    def test_core_integrations_only_knowledge_and_general(self):
        """Test CORE_INTEGRATIONS contains only internal intents (HA/n8n/camera are MCP-only)."""
        integration_names = [i.integration_name for i in CORE_INTEGRATIONS]
        assert "knowledge" in integration_names
        assert "general" in integration_names
        assert len(integration_names) == 2

        # HA, n8n, camera are no longer in core — they're MCP-only
        assert "homeassistant" not in integration_names
        assert "n8n" not in integration_names
        assert "camera" not in integration_names


class TestIntentRegistry:
    """Tests for IntentRegistry class."""

    def test_singleton_instance(self):
        """Test global singleton instance exists."""
        assert intent_registry is not None
        assert isinstance(intent_registry, IntentRegistry)

    @patch("services.intent_registry.settings")
    def test_get_enabled_integrations_all_disabled(self, mock_settings):
        """Test no integrations enabled returns only general."""
        mock_settings.rag_enabled = False

        registry = IntentRegistry()
        enabled = registry.get_enabled_integrations()

        integration_names = [i.integration_name for i in enabled]
        assert "general" in integration_names
        assert "knowledge" not in integration_names

    @patch("services.intent_registry.settings")
    def test_get_enabled_integrations_rag_enabled(self, mock_settings):
        """Test RAG/Knowledge enabled when rag_enabled is True."""
        mock_settings.rag_enabled = True

        registry = IntentRegistry()
        enabled = registry.get_enabled_integrations()

        integration_names = [i.integration_name for i in enabled]
        assert "knowledge" in integration_names
        assert "general" in integration_names

    @patch("services.intent_registry.settings")
    def test_is_intent_available_core(self, mock_settings):
        """Test checking if core intent is available."""
        mock_settings.rag_enabled = True

        registry = IntentRegistry()

        assert registry.is_intent_available("knowledge.search") is True
        assert registry.is_intent_available("general.conversation") is True

    @patch("services.intent_registry.settings")
    def test_is_intent_available_mcp(self, mock_settings):
        """Test MCP tools are checked for intent availability."""
        mock_settings.rag_enabled = False

        registry = IntentRegistry()
        registry.set_mcp_tools([
            {"intent": "mcp.homeassistant.turn_on", "description": "Turn on"},
            {"intent": "mcp.weather.get_forecast", "description": "Get weather"},
        ])

        assert registry.is_intent_available("mcp.homeassistant.turn_on") is True
        assert registry.is_intent_available("mcp.weather.get_forecast") is True
        assert registry.is_intent_available("mcp.nonexistent.tool") is False

    @patch("services.intent_registry.settings")
    def test_get_intent_definition_core(self, mock_settings):
        """Test getting core intent definition by name."""
        mock_settings.rag_enabled = True

        registry = IntentRegistry()

        intent = registry.get_intent_definition("knowledge.search")
        assert intent is not None
        assert intent.name == "knowledge.search"
        assert len(intent.parameters) > 0

        # Non-existent intent
        assert registry.get_intent_definition("nonexistent.intent") is None

    @patch("services.intent_registry.settings")
    def test_build_intent_prompt_german(self, mock_settings):
        """Test building intent prompt in German."""
        mock_settings.rag_enabled = True
        mock_settings.plugins_enabled = False
        mock_settings.mcp_enabled = False

        registry = IntentRegistry()
        prompt = registry.build_intent_prompt(lang="de")

        assert "WISSENSDATENBANK (RAG)" in prompt
        assert "knowledge.search" in prompt

    @patch("services.intent_registry.settings")
    def test_build_intent_prompt_english(self, mock_settings):
        """Test building intent prompt in English."""
        mock_settings.rag_enabled = True
        mock_settings.plugins_enabled = False
        mock_settings.mcp_enabled = False

        registry = IntentRegistry()
        prompt = registry.build_intent_prompt(lang="en")

        assert "KNOWLEDGE BASE (RAG)" in prompt

    @patch("services.intent_registry.settings")
    def test_build_intent_prompt_with_mcp(self, mock_settings):
        """Test MCP tools appear in prompt when enabled."""
        mock_settings.rag_enabled = False
        mock_settings.plugins_enabled = False
        mock_settings.mcp_enabled = True

        registry = IntentRegistry()
        registry.set_mcp_tools([
            {"intent": "mcp.homeassistant.turn_on", "description": "Turn on device"},
            {"intent": "mcp.weather.get_forecast", "description": "Get weather"},
        ])
        prompt = registry.build_intent_prompt(lang="de")

        assert "MCP TOOLS" in prompt
        assert "mcp.homeassistant.turn_on" in prompt
        assert "mcp.weather.get_forecast" in prompt

    @patch("services.intent_registry.settings")
    def test_build_intent_prompt_excludes_disabled(self, mock_settings):
        """Test that disabled integrations are excluded from prompt."""
        mock_settings.rag_enabled = False  # Knowledge disabled
        mock_settings.plugins_enabled = False
        mock_settings.mcp_enabled = False

        registry = IntentRegistry()
        prompt = registry.build_intent_prompt(lang="de")

        # Knowledge should not be in prompt
        assert "knowledge.search" not in prompt

    @patch("services.intent_registry.settings")
    def test_build_examples_prompt(self, mock_settings):
        """Test building examples prompt."""
        mock_settings.rag_enabled = True
        mock_settings.mcp_enabled = False

        registry = IntentRegistry()
        examples = registry.build_examples_prompt(lang="de", max_examples=5)

        assert "BEISPIELE:" in examples
        assert "knowledge" in examples

    @patch("services.intent_registry.settings")
    def test_build_examples_prompt_with_mcp(self, mock_settings):
        """Test MCP tools contribute examples to the prompt from YAML config."""
        mock_settings.rag_enabled = False
        mock_settings.mcp_enabled = True

        registry = IntentRegistry()
        registry.set_mcp_tools([
            {"intent": "mcp.paperless.search_documents", "description": "Search documents", "server": "paperless"},
            {"intent": "mcp.paperless.get_document", "description": "Get document", "server": "paperless"},
            {"intent": "mcp.weather.get_forecast", "description": "Get forecast", "server": "weather"},
        ])
        registry.set_mcp_examples({
            "paperless": {
                "de": ["Zeige meine Dokumente in Paperless"],
                "en": ["Show my documents in Paperless"],
            },
            "weather": {
                "de": ["Wie wird das Wetter morgen?"],
                "en": ["What's the weather tomorrow?"],
            },
        })
        examples = registry.build_examples_prompt(lang="de", max_examples=20)

        assert "BEISPIELE:" in examples
        assert "mcp.paperless" in examples
        assert "mcp.weather" in examples

    @patch("services.intent_registry.settings")
    def test_build_mcp_examples_deduplicates_servers(self, mock_settings):
        """Test that MCP examples are deduplicated per server (1 example per server)."""
        mock_settings.mcp_enabled = True

        registry = IntentRegistry()
        registry.set_mcp_tools([
            {"intent": "mcp.paperless.search", "description": "Search", "server": "paperless"},
            {"intent": "mcp.paperless.get", "description": "Get", "server": "paperless"},
            {"intent": "mcp.paperless.delete", "description": "Delete", "server": "paperless"},
        ])
        registry.set_mcp_examples({
            "paperless": {"de": ["Zeige Dokumente"], "en": ["Show documents"]},
        })
        examples = registry._build_mcp_examples(lang="de")

        # Should only have 1 example for paperless (deduplicated by server)
        assert len(examples) == 1
        assert "mcp.paperless.search" in examples[0][1]

    @patch("services.intent_registry.settings")
    def test_build_mcp_examples_unknown_server(self, mock_settings):
        """Test MCP examples for server without configured examples returns empty."""
        mock_settings.mcp_enabled = True

        registry = IntentRegistry()
        registry.set_mcp_tools([
            {"intent": "mcp.unknown.tool", "description": "Unknown tool", "server": "unknown_server"},
        ])
        # No set_mcp_examples() — server has no configured examples
        examples = registry._build_mcp_examples(lang="de")

        assert len(examples) == 0

    @patch("services.intent_registry.settings")
    def test_build_mcp_examples_from_config(self, mock_settings):
        """Test MCP examples are read from YAML-configured examples with language support."""
        mock_settings.mcp_enabled = True

        registry = IntentRegistry()
        registry.set_mcp_tools([
            {"intent": "mcp.weather.get_forecast", "description": "Get forecast", "server": "weather"},
        ])
        registry.set_mcp_examples({
            "weather": {
                "de": ["Wie wird das Wetter?"],
                "en": ["What's the weather?"],
            },
        })

        examples_de = registry._build_mcp_examples(lang="de")
        assert len(examples_de) == 1
        assert examples_de[0][0] == "Wie wird das Wetter?"
        assert examples_de[0][1] == "mcp.weather.get_forecast"

        examples_en = registry._build_mcp_examples(lang="en")
        assert len(examples_en) == 1
        assert examples_en[0][0] == "What's the weather?"

    @patch("services.intent_registry.settings")
    def test_build_mcp_examples_falls_back_to_german(self, mock_settings):
        """Test MCP examples fall back to German when requested language is missing."""
        mock_settings.mcp_enabled = True

        registry = IntentRegistry()
        registry.set_mcp_tools([
            {"intent": "mcp.paperless.search", "description": "Search", "server": "paperless"},
        ])
        registry.set_mcp_examples({
            "paperless": {"de": ["Zeige Dokumente"]},  # No "en" key
        })

        examples_en = registry._build_mcp_examples(lang="en")
        assert len(examples_en) == 1
        assert examples_en[0][0] == "Zeige Dokumente"  # Falls back to German

    def test_set_mcp_examples(self):
        """Test setting MCP examples."""
        registry = IntentRegistry()
        examples = {"weather": {"de": ["Wetter?"], "en": ["Weather?"]}}
        registry.set_mcp_examples(examples)
        assert registry._mcp_examples == examples

    @patch("services.intent_registry.settings")
    def test_get_status(self, mock_settings):
        """Test getting registry status."""
        mock_settings.rag_enabled = True

        registry = IntentRegistry()
        status = registry.get_status()

        assert "enabled_integrations" in status
        assert "disabled_integrations" in status
        assert "total_intents" in status

        enabled_names = [i["name"] for i in status["enabled_integrations"]]
        assert "knowledge" in enabled_names
        assert "general" in enabled_names

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


class TestIntentParameters:
    """Tests for intent parameter definitions."""

    def test_knowledge_required_parameters(self):
        """Test required parameters are properly marked on knowledge intents."""
        search = None
        for intent in KNOWLEDGE_INTENTS.intents:
            if intent.name == "knowledge.search":
                search = intent
                break

        assert search is not None
        assert len(search.parameters) > 0

        query_param = search.parameters[0]
        assert query_param.name == "query"
        assert query_param.required is True
