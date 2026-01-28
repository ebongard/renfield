"""
Tests for AgentToolRegistry — Tool descriptions for the Agent Loop.
"""

import pytest
from unittest.mock import MagicMock
from services.agent_tools import AgentToolRegistry, ToolDefinition


class TestToolDefinition:
    """Test ToolDefinition dataclass."""

    @pytest.mark.unit
    def test_create_basic(self):
        tool = ToolDefinition(name="test.tool", description="A test tool")
        assert tool.name == "test.tool"
        assert tool.description == "A test tool"
        assert tool.parameters == {}

    @pytest.mark.unit
    def test_create_with_params(self):
        tool = ToolDefinition(
            name="test.tool",
            description="A test tool",
            parameters={"entity_id": "The entity ID"}
        )
        assert tool.parameters == {"entity_id": "The entity ID"}


class TestAgentToolRegistryCoreTools:
    """Test core HA tool registration."""

    @pytest.mark.unit
    def test_ha_tools_registered_when_available(self):
        registry = AgentToolRegistry(ha_available=True)
        names = registry.get_tool_names()
        assert "homeassistant.turn_on" in names
        assert "homeassistant.turn_off" in names
        assert "homeassistant.toggle" in names
        assert "homeassistant.get_state" in names
        assert "homeassistant.set_value" in names

    @pytest.mark.unit
    def test_ha_tools_not_registered_when_unavailable(self):
        registry = AgentToolRegistry(ha_available=False)
        names = registry.get_tool_names()
        assert "homeassistant.turn_on" not in names
        assert len(names) == 0

    @pytest.mark.unit
    def test_get_tool_returns_definition(self):
        registry = AgentToolRegistry(ha_available=True)
        tool = registry.get_tool("homeassistant.turn_on")
        assert tool is not None
        assert tool.name == "homeassistant.turn_on"
        assert "entity_id" in tool.parameters

    @pytest.mark.unit
    def test_get_tool_returns_none_for_unknown(self):
        registry = AgentToolRegistry(ha_available=True)
        tool = registry.get_tool("nonexistent.tool")
        assert tool is None

    @pytest.mark.unit
    def test_is_valid_tool(self):
        registry = AgentToolRegistry(ha_available=True)
        assert registry.is_valid_tool("homeassistant.turn_on") is True
        assert registry.is_valid_tool("nonexistent.tool") is False


class TestAgentToolRegistryPluginTools:
    """Test plugin tool registration."""

    @pytest.mark.unit
    def test_plugin_tools_registered(self):
        """Plugin intents should be registered as agent tools."""
        mock_registry = MagicMock()

        # Create mock intent definitions
        mock_param = MagicMock()
        mock_param.name = "location"
        mock_param.description = "City name"
        mock_param.required = True

        mock_intent = MagicMock()
        mock_intent.name = "weather.get_current"
        mock_intent.description = "Get current weather"
        mock_intent.parameters = [mock_param]

        mock_registry.get_all_intents.return_value = [mock_intent]

        registry = AgentToolRegistry(plugin_registry=mock_registry, ha_available=False)
        assert registry.is_valid_tool("weather.get_current") is True

        tool = registry.get_tool("weather.get_current")
        assert tool.description == "Get current weather"
        assert "location" in tool.parameters
        assert "(required)" in tool.parameters["location"]

    @pytest.mark.unit
    def test_plugin_and_ha_tools_coexist(self):
        """Both HA core tools and plugin tools should be available."""
        mock_registry = MagicMock()

        mock_intent = MagicMock()
        mock_intent.name = "weather.get_current"
        mock_intent.description = "Get weather"
        mock_intent.parameters = []

        mock_registry.get_all_intents.return_value = [mock_intent]

        registry = AgentToolRegistry(plugin_registry=mock_registry, ha_available=True)
        assert registry.is_valid_tool("homeassistant.turn_on") is True
        assert registry.is_valid_tool("weather.get_current") is True

    @pytest.mark.unit
    def test_optional_parameter_no_required_marker(self):
        """Optional parameters should not have (required) marker."""
        mock_registry = MagicMock()

        mock_param = MagicMock()
        mock_param.name = "units"
        mock_param.description = "Temperature units"
        mock_param.required = False

        mock_intent = MagicMock()
        mock_intent.name = "weather.get_current"
        mock_intent.description = "Get weather"
        mock_intent.parameters = [mock_param]

        mock_registry.get_all_intents.return_value = [mock_intent]

        registry = AgentToolRegistry(plugin_registry=mock_registry, ha_available=False)
        tool = registry.get_tool("weather.get_current")
        assert "(required)" not in tool.parameters["units"]


class TestAgentToolRegistryPrompt:
    """Test build_tools_prompt() — generates LLM prompt text."""

    @pytest.mark.unit
    def test_empty_registry_prompt(self):
        registry = AgentToolRegistry(ha_available=False)
        prompt = registry.build_tools_prompt()
        assert "KEINE TOOLS" in prompt

    @pytest.mark.unit
    def test_prompt_contains_tool_names(self):
        registry = AgentToolRegistry(ha_available=True)
        prompt = registry.build_tools_prompt()
        assert "homeassistant.turn_on" in prompt
        assert "homeassistant.turn_off" in prompt
        assert "VERFÜGBARE TOOLS:" in prompt

    @pytest.mark.unit
    def test_prompt_contains_descriptions(self):
        registry = AgentToolRegistry(ha_available=True)
        prompt = registry.build_tools_prompt()
        assert "einschalten" in prompt.lower() or "Gerät einschalten" in prompt

    @pytest.mark.unit
    def test_prompt_contains_parameters(self):
        registry = AgentToolRegistry(ha_available=True)
        prompt = registry.build_tools_prompt()
        assert "entity_id" in prompt
