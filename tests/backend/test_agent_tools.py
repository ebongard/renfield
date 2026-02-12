"""
Tests for AgentToolRegistry — Tool descriptions for the Agent Loop.

Tools are registered dynamically from MCP servers.
"""

from unittest.mock import MagicMock

import pytest

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

    @pytest.mark.unit
    def test_resolve_tool_name_exact(self):
        """Exact match returns the full namespaced name."""
        mock_mcp = MagicMock()
        mock_tool = MagicMock()
        mock_tool.namespaced_name = "mcp.homeassistant.GetLiveContext"
        mock_tool.description = "Get live context"
        mock_tool.input_schema = {"properties": {}, "required": []}
        mock_mcp.get_all_tools.return_value = [mock_tool]

        registry = AgentToolRegistry(mcp_manager=mock_mcp)
        assert registry.resolve_tool_name("mcp.homeassistant.GetLiveContext") == "mcp.homeassistant.GetLiveContext"

    @pytest.mark.unit
    def test_resolve_tool_name_short(self):
        """Short name without namespace resolves to full name."""
        mock_mcp = MagicMock()
        mock_tool = MagicMock()
        mock_tool.namespaced_name = "mcp.homeassistant.GetLiveContext"
        mock_tool.description = "Get live context"
        mock_tool.input_schema = {"properties": {}, "required": []}
        mock_mcp.get_all_tools.return_value = [mock_tool]

        registry = AgentToolRegistry(mcp_manager=mock_mcp)
        assert registry.resolve_tool_name("GetLiveContext") == "mcp.homeassistant.GetLiveContext"

    @pytest.mark.unit
    def test_resolve_tool_name_ambiguous(self):
        """Ambiguous short name (matches multiple tools) returns None."""
        mock_mcp = MagicMock()
        tool1 = MagicMock()
        tool1.namespaced_name = "mcp.server1.search"
        tool1.description = "Search 1"
        tool1.input_schema = {"properties": {}, "required": []}
        tool2 = MagicMock()
        tool2.namespaced_name = "mcp.server2.search"
        tool2.description = "Search 2"
        tool2.input_schema = {"properties": {}, "required": []}
        mock_mcp.get_all_tools.return_value = [tool1, tool2]

        registry = AgentToolRegistry(mcp_manager=mock_mcp)
        assert registry.resolve_tool_name("search") is None

    @pytest.mark.unit
    def test_resolve_tool_name_unknown(self):
        """Unknown tool name returns None."""
        mock_mcp = MagicMock()
        mock_mcp.get_all_tools.return_value = []

        registry = AgentToolRegistry(mcp_manager=mock_mcp)
        assert registry.resolve_tool_name("nonexistent") is None

    @pytest.mark.unit
    def test_is_valid_tool_accepts_short_name(self):
        """is_valid_tool should accept short names via resolve_tool_name."""
        mock_mcp = MagicMock()
        mock_tool = MagicMock()
        mock_tool.namespaced_name = "mcp.homeassistant.HassTurnOn"
        mock_tool.description = "Turn on"
        mock_tool.input_schema = {"properties": {}, "required": []}
        mock_mcp.get_all_tools.return_value = [mock_tool]

        registry = AgentToolRegistry(mcp_manager=mock_mcp)
        assert registry.is_valid_tool("HassTurnOn") is True
        assert registry.is_valid_tool("mcp.homeassistant.HassTurnOn") is True
        assert registry.is_valid_tool("NonExistent") is False


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


def _build_multi_server_registry():
    """Helper: Create a registry with tools from multiple MCP servers."""
    mock_mcp = MagicMock()
    tools = []
    tool_defs = [
        ("mcp.paperless.search_documents", "Search documents in Paperless-NGX"),
        ("mcp.paperless.download_document", "Download a document from Paperless-NGX"),
        ("mcp.email.send_email", "Send an email"),
        ("mcp.email.list_emails", "List emails from inbox"),
        ("mcp.email.read_email", "Read an email"),
        ("mcp.weather.weather_forecast", "Get weather forecast"),
        ("mcp.weather.air_quality", "Get air quality"),
        ("mcp.homeassistant.turn_on", "Turn on a device"),
        ("mcp.homeassistant.turn_off", "Turn off a device"),
        ("mcp.homeassistant.get_states", "Get entity states"),
        ("mcp.n8n.list_workflows", "List n8n workflows"),
        ("mcp.n8n.execute_workflow", "Execute an n8n workflow"),
        ("mcp.jellyfin.search_media", "Search media in Jellyfin"),
        ("mcp.jellyfin.get_libraries", "Get Jellyfin libraries"),
        ("mcp.search.web_search", "Search the web"),
        ("mcp.news.search_articles", "Search news articles"),
    ]
    for name, desc in tool_defs:
        mock_tool = MagicMock()
        mock_tool.namespaced_name = name
        mock_tool.description = desc
        mock_tool.input_schema = {"properties": {"q": {"type": "string", "description": "Query"}}, "required": []}
        tools.append(mock_tool)
    mock_mcp.get_all_tools.return_value = tools
    return AgentToolRegistry(mcp_manager=mock_mcp)


class TestSelectRelevantTools:
    """Test keyword-based tool relevance filtering."""

    @pytest.mark.unit
    def test_paperless_and_email_for_invoice_query(self):
        """German invoice+email query should select paperless and email tools."""
        registry = _build_multi_server_registry()
        selected = registry.select_relevant_tools(
            "Suche mir die letzten beiden Rechnungen von IONOS heraus und schicke sie an test@example.com"
        )
        names = set(selected.keys())
        assert "mcp.paperless.search_documents" in names
        assert "mcp.paperless.download_document" in names
        assert "mcp.email.send_email" in names
        # Should NOT include weather, HA, n8n, jellyfin
        assert not any(n.startswith("mcp.weather") for n in names)
        assert not any(n.startswith("mcp.homeassistant") for n in names)
        assert not any(n.startswith("mcp.n8n") for n in names)
        assert not any(n.startswith("mcp.jellyfin") for n in names)

    @pytest.mark.unit
    def test_weather_query_selects_weather_tools(self):
        """Weather query should select only weather tools."""
        registry = _build_multi_server_registry()
        selected = registry.select_relevant_tools("Wie ist das Wetter morgen?")
        names = set(selected.keys())
        assert any(n.startswith("mcp.weather") for n in names)
        assert not any(n.startswith("mcp.paperless") for n in names)

    @pytest.mark.unit
    def test_smart_home_query_selects_ha_tools(self):
        """Smart home query should select HA tools."""
        registry = _build_multi_server_registry()
        selected = registry.select_relevant_tools("Schalte das Licht im Wohnzimmer ein")
        names = set(selected.keys())
        assert any(n.startswith("mcp.homeassistant") for n in names)

    @pytest.mark.unit
    def test_no_keywords_returns_all_tools(self):
        """Query with no matching keywords should fall back to all tools."""
        registry = _build_multi_server_registry()
        selected = registry.select_relevant_tools("Erzähl mir einen Witz")
        assert len(selected) == len(registry._tools)

    @pytest.mark.unit
    def test_empty_registry_returns_empty(self):
        """Empty registry should return empty dict."""
        registry = AgentToolRegistry()
        selected = registry.select_relevant_tools("Test query")
        assert selected == {}

    @pytest.mark.unit
    def test_filtered_tools_prompt_is_smaller(self):
        """Filtered tools prompt should be significantly smaller than full prompt."""
        registry = _build_multi_server_registry()
        full_prompt = registry.build_tools_prompt()
        selected = registry.select_relevant_tools(
            "Suche Rechnungen und schicke sie per Email"
        )
        filtered_prompt = registry.build_tools_prompt(tools=selected)
        assert len(filtered_prompt) < len(full_prompt)

    @pytest.mark.unit
    def test_search_keyword_includes_paperless(self):
        """'suche' keyword should include paperless tools."""
        registry = _build_multi_server_registry()
        selected = registry.select_relevant_tools("Suche nach Dokumenten")
        names = set(selected.keys())
        assert any(n.startswith("mcp.paperless") for n in names)

    @pytest.mark.unit
    def test_web_keyword_includes_search(self):
        """'web' keyword should include web search tools."""
        registry = _build_multi_server_registry()
        selected = registry.select_relevant_tools("Suche im Web nach Informationen")
        names = set(selected.keys())
        assert any(n.startswith("mcp.search") for n in names)

    @pytest.mark.unit
    def test_multi_keyword_combines_servers(self):
        """Query with multiple keywords should combine server tool groups."""
        registry = _build_multi_server_registry()
        selected = registry.select_relevant_tools(
            "Suche die Rechnung und schicke sie per Mail, dann schalte das Licht aus"
        )
        names = set(selected.keys())
        assert any(n.startswith("mcp.paperless") for n in names)
        assert any(n.startswith("mcp.email") for n in names)
        assert any(n.startswith("mcp.homeassistant") for n in names)
