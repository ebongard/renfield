"""
Tests for AgentToolRegistry — Tool descriptions for the Agent Loop.

Tools are registered dynamically from MCP servers.
"""

from unittest.mock import MagicMock

import pytest

from services.agent_tools import AgentToolRegistry, ToolDefinition

# The registry always registers the platform-owned internal tools in
# __init__ via _register_internal_tools(). There are three of them:
#   internal.knowledge_search
#   internal.forward_attachment_to_paperless
#   internal.paperless_commit_upload
# Tests that count tools or assert an "empty" registry must account for
# these baseline entries.
INTERNAL_TOOL_NAMES = {
    "internal.knowledge_search",
    "internal.forward_attachment_to_paperless",
    "internal.paperless_commit_upload",
}
NUM_INTERNAL_TOOLS = len(INTERNAL_TOOL_NAMES)


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


class TestAgentToolRegistryConstruction:
    """Test that construction parameters are exposed for plugins."""

    @pytest.mark.unit
    def test_server_filter_stored_as_attribute(self):
        """The server_filter parameter must be exposed so plugins (e.g. the
        register_tools hook) can scope their additions to the same set of
        servers the caller selected for MCP/internal tools."""
        registry = AgentToolRegistry(server_filter=["jira", "confluence"], _init_only=True)
        assert registry.server_filter == ["jira", "confluence"]

    @pytest.mark.unit
    def test_server_filter_default_none(self):
        """When no server_filter is passed, the attribute is None (= all servers)."""
        registry = AgentToolRegistry(_init_only=True)
        assert registry.server_filter is None

    @pytest.mark.unit
    def test_internal_filter_stored_as_attribute(self):
        """The internal_filter parameter is exposed for the same reason."""
        registry = AgentToolRegistry(internal_filter=["internal.knowledge_search"], _init_only=True)
        assert registry.internal_filter == ["internal.knowledge_search"]


class TestAgentToolRegistryAsyncCreate:
    """Test the async ``create()`` classmethod that runs ``register_tools`` in-line.

    This is the production constructor — it replaces the old pattern where
    ``__init__`` scheduled a background task and every call site had to
    remember ``await tool_registry._hook_task``. With ``create()`` the
    registry returned to the caller is fully populated, eliminating the
    race condition that caused intermittent missing-plugin-tool failures.
    """

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_create_awaits_register_tools_hook(self):
        """``create()`` must await the ``register_tools`` hook before returning,
        so plugin-registered tools are present in the registry the caller receives."""
        from utils.hooks import _hooks, register_hook

        # Save original hook state for cleanup
        original = list(_hooks.get("register_tools", []))
        try:
            registered_during_create: list[bool] = []

            async def _plugin_hook(registry, **_kw):
                # Add a plugin tool — caller must see this in the returned registry.
                registry._tools["plugin.test_tool"] = ToolDefinition(
                    name="plugin.test_tool",
                    description="Plugin-registered tool",
                )
                registered_during_create.append(True)

            register_hook("register_tools", _plugin_hook)

            registry = await AgentToolRegistry.create()

            # Hook ran synchronously during create() — not deferred.
            assert registered_during_create == [True]
            # Plugin tool is visible immediately on the returned registry.
            assert "plugin.test_tool" in registry._tools
        finally:
            _hooks["register_tools"] = original

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_create_propagates_hook_failure(self):
        """If a ``register_tools`` hook raises, ``create()`` must propagate the
        exception so callers (e.g. the orchestrator) can fail fast with a
        diagnosable error instead of silently using a half-populated registry.

        This is the contract that ``run_hooks``'s swallow-and-log semantics
        would break — ``create()`` deliberately bypasses ``run_hooks`` to
        keep ``register_tools`` fail-loud."""
        from utils.hooks import _hooks, register_hook

        original = list(_hooks.get("register_tools", []))
        try:
            async def _broken_hook(**_kw):
                raise RuntimeError("plugin tool registration failed")

            register_hook("register_tools", _broken_hook)

            with pytest.raises(RuntimeError, match="plugin tool registration failed"):
                await AgentToolRegistry.create()
        finally:
            _hooks["register_tools"] = original

    @pytest.mark.unit
    def test_direct_init_without_opt_in_raises(self):
        """Direct ``AgentToolRegistry(...)`` construction must raise — it
        skips the ``register_tools`` hook and would silently lose plugin
        tools (Reva's resolve_team_members, resolve_role, etc.). This is
        the structural guard that prevents a regression of TD-13."""
        with pytest.raises(RuntimeError, match="AgentToolRegistry.create"):
            AgentToolRegistry()

        with pytest.raises(RuntimeError, match="AgentToolRegistry.create"):
            AgentToolRegistry(server_filter=["jira"])

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_create_passes_filters_to_init(self):
        """``create()`` must thread ``server_filter`` / ``internal_filter`` through
        to the underlying ``__init__`` so plugins can scope their additions."""
        registry = await AgentToolRegistry.create(
            server_filter=["jira"],
            internal_filter=["internal.knowledge_search"],
        )
        assert registry.server_filter == ["jira"]
        assert registry.internal_filter == ["internal.knowledge_search"]


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

        registry = AgentToolRegistry(mcp_manager=mock_mcp, _init_only=True)
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

        registry = AgentToolRegistry(mcp_manager=mock_mcp, _init_only=True)
        names = registry.get_tool_names()
        # 3 MCP tools + the 3 baseline internal tools.
        assert len(names) == 3 + NUM_INTERNAL_TOOLS
        assert "mcp.homeassistant.turn_on" in names
        assert "mcp.weather.get_forecast" in names
        assert "mcp.n8n.list_workflows" in names

    @pytest.mark.unit
    def test_empty_registry_no_mcp(self):
        """Without MCP or plugins, registry holds only the internal tools."""
        registry = AgentToolRegistry(_init_only=True)
        assert set(registry.get_tool_names()) == INTERNAL_TOOL_NAMES

    @pytest.mark.unit
    def test_get_tool_returns_none_for_unknown(self):
        registry = AgentToolRegistry(_init_only=True)
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

        registry = AgentToolRegistry(mcp_manager=mock_mcp, _init_only=True)
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

        registry = AgentToolRegistry(mcp_manager=mock_mcp, _init_only=True)
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

        registry = AgentToolRegistry(mcp_manager=mock_mcp, _init_only=True)
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

        registry = AgentToolRegistry(mcp_manager=mock_mcp, _init_only=True)
        assert registry.resolve_tool_name("search") is None

    @pytest.mark.unit
    def test_resolve_tool_name_unknown(self):
        """Unknown tool name returns None."""
        mock_mcp = MagicMock()
        mock_mcp.get_all_tools.return_value = []

        registry = AgentToolRegistry(mcp_manager=mock_mcp, _init_only=True)
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

        registry = AgentToolRegistry(mcp_manager=mock_mcp, _init_only=True)
        assert registry.is_valid_tool("HassTurnOn") is True
        assert registry.is_valid_tool("mcp.homeassistant.HassTurnOn") is True
        assert registry.is_valid_tool("NonExistent") is False


class TestAgentToolRegistryPrompt:
    """Test build_tools_prompt() — generates LLM prompt text."""

    @pytest.mark.unit
    def test_empty_registry_prompt(self):
        """An explicitly empty tool set produces the no-tools sentinel."""
        registry = AgentToolRegistry(_init_only=True)
        prompt = registry.build_tools_prompt(tools={})
        assert "KEINE TOOLS" in prompt

    @pytest.mark.unit
    def test_internal_tools_registry_prompt(self):
        """A registry with no MCP server still lists the baseline internal tools."""
        registry = AgentToolRegistry(_init_only=True)
        prompt = registry.build_tools_prompt()
        assert "VERFÜGBARE TOOLS:" in prompt
        assert "internal.knowledge_search" in prompt

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

        registry = AgentToolRegistry(mcp_manager=mock_mcp, _init_only=True)
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

        registry = AgentToolRegistry(mcp_manager=mock_mcp, _init_only=True)
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

        registry = AgentToolRegistry(mcp_manager=mock_mcp, _init_only=True)
        prompt = registry.build_tools_prompt()
        assert "entity_id" in prompt


# NOTE: The keyword-based ``select_relevant_tools()`` method and its tests
# (formerly ``TestSelectRelevantTools``) were removed. Tool-scope filtering
# is now performed at construction time via the ``server_filter`` /
# ``internal_filter`` parameters — see ``TestAgentToolRegistryConstruction``
# and ``TestAgentToolRegistryAsyncCreate`` above. There is no longer a
# post-construction relevance filter to test.


# ============================================================================
# Native Function Calling plumbing — sanitize/unsanitize + build_tools_schema
# ============================================================================


class TestSanitizeToolName:
    """Round-trip between dotted MCP names and native-FC-compatible names."""

    @pytest.mark.unit
    def test_mcp_namespaced_name_is_sanitized(self):
        from services.agent_tools import sanitize_tool_name
        assert sanitize_tool_name("mcp.release.list_releases") == "mcp__release__list_releases"

    @pytest.mark.unit
    def test_already_valid_name_passes_through(self):
        """A bare name that already matches ^[a-zA-Z0-9_-]+$ is unchanged."""
        from services.agent_tools import sanitize_tool_name
        assert sanitize_tool_name("list_global_roles") == "list_global_roles"
        assert sanitize_tool_name("find-stuff") == "find-stuff"

    @pytest.mark.unit
    def test_unsanitize_reverses_sanitize(self):
        from services.agent_tools import sanitize_tool_name, unsanitize_tool_name
        orig = "mcp.jira.search_issues"
        assert unsanitize_tool_name(sanitize_tool_name(orig)) == orig

    @pytest.mark.unit
    def test_unsanitize_is_idempotent_for_bare_names(self):
        """Calling unsanitize on names without '__' is a no-op."""
        from services.agent_tools import unsanitize_tool_name
        assert unsanitize_tool_name("list_global_roles") == "list_global_roles"


class TestBuildToolsSchema:
    """OpenAI-format tools schema for native function calling."""

    def _make_mcp_tool(self, name: str, description: str, input_schema: dict) -> MagicMock:
        tool = MagicMock()
        tool.namespaced_name = name
        tool.server_name = name.split(".")[1] if name.startswith("mcp.") else name
        tool.description = description
        tool.input_schema = input_schema
        return tool

    def _make_registry_with_tools(self, tools):
        mcp_manager = MagicMock()
        mcp_manager.get_all_tools.return_value = tools
        # internal_filter=[] suppresses the always-on platform internal
        # tools (knowledge_search, chat-upload tools) so build_tools_schema
        # reflects exactly the MCP tools under test.
        return AgentToolRegistry(
            mcp_manager=mcp_manager, internal_filter=[], _init_only=True
        )

    @pytest.mark.unit
    def test_returns_openai_tools_format(self):
        """Schema entries have {type: function, function: {name, description, parameters}}."""
        tool = self._make_mcp_tool(
            "mcp.release.list_releases",
            "List releases",
            {"type": "object", "properties": {"active": {"type": "boolean"}}},
        )
        registry = self._make_registry_with_tools([tool])
        schema = registry.build_tools_schema()

        assert len(schema) == 1
        entry = schema[0]
        assert entry["type"] == "function"
        assert entry["function"]["name"] == "mcp__release__list_releases"
        assert entry["function"]["description"] == "List releases"
        assert entry["function"]["parameters"]["properties"]["active"]["type"] == "boolean"

    @pytest.mark.unit
    def test_empty_registry_returns_empty_list(self):
        registry = self._make_registry_with_tools([])
        assert registry.build_tools_schema() == []

    @pytest.mark.unit
    def test_preselection_filters_output(self):
        """Passing a subset of tools scopes the schema to just those."""
        t1 = self._make_mcp_tool("mcp.a.foo", "foo", {"type": "object", "properties": {}})
        t2 = self._make_mcp_tool("mcp.a.bar", "bar", {"type": "object", "properties": {}})
        registry = self._make_registry_with_tools([t1, t2])
        preselected = {"mcp.a.foo": registry.get_tool("mcp.a.foo")}
        schema = registry.build_tools_schema(preselected)
        assert len(schema) == 1
        assert schema[0]["function"]["name"] == "mcp__a__foo"

    @pytest.mark.unit
    def test_full_input_schema_is_preserved(self):
        """Nested schemas with required fields and typed properties pass through intact."""
        input_schema = {
            "type": "object",
            "properties": {
                "release_id": {"type": "string", "description": "Full release ID"},
                "limit": {"type": "integer", "minimum": 1},
            },
            "required": ["release_id"],
        }
        tool = self._make_mcp_tool("mcp.release.get_release", "Get release", input_schema)
        registry = self._make_registry_with_tools([tool])
        schema = registry.build_tools_schema()
        assert schema[0]["function"]["parameters"] == input_schema

    @pytest.mark.unit
    def test_tool_without_input_schema_gets_synthesised_schema(self):
        """ToolDefinition with only flattened parameters gets a minimal fallback."""
        # internal_filter=[] keeps the registry empty so schema[0] is the
        # plugin tool added below, not an always-on internal tool.
        registry = AgentToolRegistry(internal_filter=[], _init_only=True)
        registry._tools["plugin.custom"] = ToolDefinition(
            name="plugin.custom",
            description="Plugin-registered tool",
            parameters={"x": "description of x", "y": "description of y"},
            input_schema=None,
        )
        schema = registry.build_tools_schema()
        assert schema[0]["function"]["name"] == "plugin__custom"
        params = schema[0]["function"]["parameters"]
        assert params["type"] == "object"
        assert params["properties"]["x"]["type"] == "string"
        assert params["properties"]["x"]["description"] == "description of x"
        assert params["properties"]["y"]["type"] == "string"


class TestToolDefinitionInputSchema:
    """input_schema preservation through registration."""

    @pytest.mark.unit
    def test_mcp_registration_preserves_input_schema(self):
        """When an MCP tool is registered, the full JSON Schema is retained on
        the ToolDefinition so build_tools_schema can emit it verbatim."""
        input_schema = {
            "type": "object",
            "properties": {"entity_id": {"type": "string"}},
            "required": ["entity_id"],
        }
        mcp_tool = MagicMock()
        mcp_tool.namespaced_name = "mcp.homeassistant.turn_on"
        mcp_tool.server_name = "homeassistant"
        mcp_tool.description = "Turn on a device"
        mcp_tool.input_schema = input_schema

        mcp_manager = MagicMock()
        mcp_manager.get_all_tools.return_value = [mcp_tool]
        registry = AgentToolRegistry(mcp_manager=mcp_manager, _init_only=True)

        tool = registry.get_tool("mcp.homeassistant.turn_on")
        assert tool is not None
        assert tool.input_schema == input_schema
