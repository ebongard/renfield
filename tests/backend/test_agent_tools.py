"""
Tests for AgentToolRegistry — Tool descriptions for the Agent Loop.

Tools are registered dynamically from MCP servers and plugins.
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


class TestAgentToolRegistryMCPTools:
    """Test MCP tool registration."""

    @pytest.mark.unit
    def test_mcp_tools_registered(self):
        """MCP tools should be registered as agent tools."""
        mock_mcp = MagicMock()

        mock_tool = MagicMock()
        mock_tool.namespaced_name = "mcp.homeassistant.turn_on"
        mock_tool.description = "Turn on a device"
        mock_tool.input_schema = {
            "properties": {
                "entity_id": {"type": "string", "description": "HA Entity ID"}
            },
            "required": ["entity_id"]
        }

        mock_mcp.get_all_tools.return_value = [mock_tool]

        registry = AgentToolRegistry(mcp_manager=mock_mcp)
        assert registry.is_valid_tool("mcp.homeassistant.turn_on") is True

        tool = registry.get_tool("mcp.homeassistant.turn_on")
        assert tool.description == "Turn on a device"
        assert "entity_id" in tool.parameters
        assert "(required)" in tool.parameters["entity_id"]

    @pytest.mark.unit
    def test_multiple_mcp_tools(self):
        """Multiple MCP tools from different servers should all register."""
        mock_mcp = MagicMock()

        tools = []
        for name in ["mcp.homeassistant.turn_on", "mcp.weather.get_forecast", "mcp.n8n.list_workflows"]:
            mock_tool = MagicMock()
            mock_tool.namespaced_name = name
            mock_tool.description = f"Description for {name}"
            mock_tool.input_schema = {"properties": {}, "required": []}
            tools.append(mock_tool)

        mock_mcp.get_all_tools.return_value = tools

        registry = AgentToolRegistry(mcp_manager=mock_mcp)
        names = registry.get_tool_names()
        assert len(names) == 3
        assert "mcp.homeassistant.turn_on" in names
        assert "mcp.weather.get_forecast" in names
        assert "mcp.n8n.list_workflows" in names

    @pytest.mark.unit
    def test_empty_registry_no_mcp(self):
        """Without MCP or plugins, registry should be empty."""
        registry = AgentToolRegistry()
        assert len(registry.get_tool_names()) == 0

    @pytest.mark.unit
    def test_get_tool_returns_none_for_unknown(self):
        registry = AgentToolRegistry()
        tool = registry.get_tool("nonexistent.tool")
        assert tool is None

    @pytest.mark.unit
    def test_is_valid_tool(self):
        mock_mcp = MagicMock()
        mock_tool = MagicMock()
        mock_tool.namespaced_name = "mcp.test.tool"
        mock_tool.description = "Test"
        mock_tool.input_schema = {"properties": {}, "required": []}
        mock_mcp.get_all_tools.return_value = [mock_tool]

        registry = AgentToolRegistry(mcp_manager=mock_mcp)
        assert registry.is_valid_tool("mcp.test.tool") is True
        assert registry.is_valid_tool("nonexistent.tool") is False


class TestAgentToolRegistryPluginTools:
    """Test plugin tool registration."""

    @pytest.mark.unit
    def test_plugin_tools_registered(self):
        """Plugin intents should be registered as agent tools."""
        mock_registry = MagicMock()

        mock_param = MagicMock()
        mock_param.name = "location"
        mock_param.description = "City name"
        mock_param.required = True

        mock_intent = MagicMock()
        mock_intent.name = "weather.get_current"
        mock_intent.description = "Get current weather"
        mock_intent.parameters = [mock_param]

        mock_registry.get_all_intents.return_value = [mock_intent]

        registry = AgentToolRegistry(plugin_registry=mock_registry)
        assert registry.is_valid_tool("weather.get_current") is True

        tool = registry.get_tool("weather.get_current")
        assert tool.description == "Get current weather"
        assert "location" in tool.parameters
        assert "(required)" in tool.parameters["location"]

    @pytest.mark.unit
    def test_plugin_and_mcp_tools_coexist(self):
        """Both MCP tools and plugin tools should be available."""
        mock_plugin_registry = MagicMock()
        mock_intent = MagicMock()
        mock_intent.name = "weather.get_current"
        mock_intent.description = "Get weather"
        mock_intent.parameters = []
        mock_plugin_registry.get_all_intents.return_value = [mock_intent]

        mock_mcp = MagicMock()
        mock_tool = MagicMock()
        mock_tool.namespaced_name = "mcp.homeassistant.turn_on"
        mock_tool.description = "Turn on"
        mock_tool.input_schema = {"properties": {}, "required": []}
        mock_mcp.get_all_tools.return_value = [mock_tool]

        registry = AgentToolRegistry(plugin_registry=mock_plugin_registry, mcp_manager=mock_mcp)
        assert registry.is_valid_tool("mcp.homeassistant.turn_on") is True
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

        registry = AgentToolRegistry(plugin_registry=mock_registry)
        tool = registry.get_tool("weather.get_current")
        assert "(required)" not in tool.parameters["units"]


class TestAgentToolRegistryPrompt:
    """Test build_tools_prompt() — generates LLM prompt text."""

    @pytest.mark.unit
    def test_empty_registry_prompt(self):
        registry = AgentToolRegistry()
        prompt = registry.build_tools_prompt()
        assert "KEINE TOOLS" in prompt

    @pytest.mark.unit
    def test_prompt_contains_mcp_tool_names(self):
        mock_mcp = MagicMock()
        tools = []
        for name, desc in [
            ("mcp.homeassistant.turn_on", "Turn on device"),
            ("mcp.weather.get_forecast", "Get weather forecast"),
        ]:
            mock_tool = MagicMock()
            mock_tool.namespaced_name = name
            mock_tool.description = desc
            mock_tool.input_schema = {
                "properties": {"param": {"type": "string", "description": "A param"}},
                "required": []
            }
            tools.append(mock_tool)
        mock_mcp.get_all_tools.return_value = tools

        registry = AgentToolRegistry(mcp_manager=mock_mcp)
        prompt = registry.build_tools_prompt()
        assert "mcp.homeassistant.turn_on" in prompt
        assert "mcp.weather.get_forecast" in prompt
        assert "VERFÜGBARE TOOLS:" in prompt

    @pytest.mark.unit
    def test_prompt_contains_descriptions(self):
        mock_mcp = MagicMock()
        mock_tool = MagicMock()
        mock_tool.namespaced_name = "mcp.test.tool"
        mock_tool.description = "A very specific description"
        mock_tool.input_schema = {"properties": {}, "required": []}
        mock_mcp.get_all_tools.return_value = [mock_tool]

        registry = AgentToolRegistry(mcp_manager=mock_mcp)
        prompt = registry.build_tools_prompt()
        assert "A very specific description" in prompt

    @pytest.mark.unit
    def test_prompt_contains_parameters(self):
        mock_mcp = MagicMock()
        mock_tool = MagicMock()
        mock_tool.namespaced_name = "mcp.ha.turn_on"
        mock_tool.description = "Turn on"
        mock_tool.input_schema = {
            "properties": {"entity_id": {"type": "string", "description": "Entity ID"}},
            "required": ["entity_id"]
        }
        mock_mcp.get_all_tools.return_value = [mock_tool]

        registry = AgentToolRegistry(mcp_manager=mock_mcp)
        prompt = registry.build_tools_prompt()
        assert "entity_id" in prompt
