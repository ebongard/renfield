"""
Agent Tool Registry — Wraps existing infrastructure as tool descriptions for the LLM.

Generates compact tool descriptions from:
- Core tools (Home Assistant actions)
- Plugin tools (YAML-based plugins)

These descriptions are included in the Agent Loop prompt so the LLM knows
which tools it can call.

Includes keyword-based tool relevance filtering to reduce prompt size
for small LLMs by selecting only tools relevant to the user's query.
"""
import re
from typing import Dict, List, Optional, Set, TYPE_CHECKING
from dataclasses import dataclass, field
from loguru import logger

if TYPE_CHECKING:
    from integrations.core.plugin_registry import PluginRegistry
    from services.mcp_client import MCPManager


@dataclass
class ToolDefinition:
    """Definition of a tool available to the Agent."""
    name: str
    description: str
    parameters: Dict[str, str] = field(default_factory=dict)  # param_name -> description


class AgentToolRegistry:
    """Registry of all tools available to the Agent Loop.

    Tools are registered dynamically from:
    - MCP servers (Home Assistant, n8n, weather, search, etc.)
    - Plugins (legacy YAML-based plugins)
    """

    def __init__(
        self,
        plugin_registry: Optional["PluginRegistry"] = None,
        mcp_manager: Optional["MCPManager"] = None,
        server_filter: Optional[List[str]] = None,
        internal_filter: Optional[List[str]] = None,
    ):
        """Initialize the tool registry.

        Args:
            plugin_registry: Legacy plugin registry
            mcp_manager: MCP server manager
            server_filter: If set, only include MCP tools from these server names.
                          None means include all servers.
            internal_filter: If set, only include these internal tool names.
                            None means include all internal tools.
        """
        self._tools: Dict[str, ToolDefinition] = {}

        # Register plugin tools
        if plugin_registry:
            self._register_plugin_tools(plugin_registry)

        # Register MCP tools (includes HA, n8n, weather, search, etc.)
        if mcp_manager:
            self._register_mcp_tools(mcp_manager, server_filter=server_filter)

        # Register internal agent tools (room resolution, media playback)
        self._register_internal_tools(internal_filter=internal_filter)

    def _register_plugin_tools(self, plugin_registry: "PluginRegistry") -> None:
        """Register all plugin intents as agent tools."""
        for intent_def in plugin_registry.get_all_intents():
            params = {}
            for p in intent_def.parameters:
                req_marker = " (required)" if p.required else ""
                params[p.name] = f"{p.description}{req_marker}"

            tool = ToolDefinition(
                name=intent_def.name,
                description=intent_def.description,
                parameters=params,
            )
            self._tools[tool.name] = tool
            logger.debug(f"🔧 Agent tool registered: {tool.name}")

    def _register_internal_tools(self, internal_filter: Optional[List[str]] = None) -> None:
        """Register internal agent tools (room resolution, media playback).

        Args:
            internal_filter: If set, only register these tool names. None = all.
        """
        from services.internal_tools import InternalToolService

        for name, definition in InternalToolService.TOOLS.items():
            if internal_filter is not None and name not in internal_filter:
                continue

            params = {}
            for param_name, param_desc in definition.get("parameters", {}).items():
                params[param_name] = param_desc

            tool = ToolDefinition(
                name=name,
                description=definition["description"],
                parameters=params,
            )
            self._tools[tool.name] = tool
            logger.debug(f"Internal agent tool registered: {tool.name}")

    def _register_mcp_tools(self, mcp_manager: "MCPManager", server_filter: Optional[List[str]] = None) -> None:
        """Register MCP tools as agent tools.

        Args:
            mcp_manager: MCP server manager
            server_filter: If set, only include tools from these server names. None = all.
        """
        for mcp_tool in mcp_manager.get_all_tools():
            if server_filter is not None and mcp_tool.server_name not in server_filter:
                continue
            params = {}
            schema_props = mcp_tool.input_schema.get("properties", {})
            required_params = mcp_tool.input_schema.get("required", [])
            for param_name, param_schema in schema_props.items():
                desc = param_schema.get("description", param_schema.get("type", ""))
                if param_name in required_params:
                    desc += " (required)"
                params[param_name] = desc

            tool = ToolDefinition(
                name=mcp_tool.namespaced_name,
                description=mcp_tool.description,
                parameters=params,
            )
            self._tools[tool.name] = tool
            logger.debug(f"MCP agent tool registered: {tool.name}")

    def get_tool(self, name: str) -> Optional[ToolDefinition]:
        """Get a tool definition by name."""
        return self._tools.get(name)

    def get_tool_names(self) -> List[str]:
        """Get list of all registered tool names."""
        return list(self._tools.keys())

    def is_valid_tool(self, name: str) -> bool:
        """Check if a tool name is valid."""
        return name in self._tools

    def build_tools_prompt(self, tools: Optional[Dict[str, "ToolDefinition"]] = None) -> str:
        """
        Build a compact text description of tools for the LLM prompt.

        Args:
            tools: Optional dict of tools to include. If None, uses all registered tools.

        Returns:
            Formatted string listing tools with parameters.
        """
        tool_set = tools if tools is not None else self._tools
        if not tool_set:
            return "KEINE TOOLS VERFÜGBAR."

        lines = ["VERFÜGBARE TOOLS:"]

        for tool in tool_set.values():
            if tool.parameters:
                params_str = ", ".join(
                    f"{name}: {desc}" for name, desc in tool.parameters.items()
                )
                lines.append(f"- {tool.name}: {tool.description} | Parameter: {{{params_str}}}")
            else:
                lines.append(f"- {tool.name}: {tool.description}")

        return "\n".join(lines)

    # Keyword mapping: query keywords -> MCP server prefixes / tool name fragments
    # Each entry: set of query keywords -> set of server prefixes to include
    _SERVER_KEYWORD_MAP: Dict[str, List[str]] = {
        # Document management
        "paperless": ["mcp.paperless"],
        "dokument": ["mcp.paperless"],
        "document": ["mcp.paperless"],
        "rechnung": ["mcp.paperless"],
        "invoice": ["mcp.paperless"],
        "suche": ["mcp.paperless"],
        "search": ["mcp.paperless"],
        # Email
        "email": ["mcp.email"],
        "mail": ["mcp.email"],
        "schick": ["mcp.email"],
        "send": ["mcp.email"],
        "postfach": ["mcp.email"],
        "inbox": ["mcp.email"],
        # Weather
        "wetter": ["mcp.weather"],
        "weather": ["mcp.weather"],
        "temperatur": ["mcp.weather"],
        "regen": ["mcp.weather"],
        "wind": ["mcp.weather"],
        # Smart home
        "licht": ["mcp.homeassistant"],
        "light": ["mcp.homeassistant"],
        "schalte": ["mcp.homeassistant"],
        "turn": ["mcp.homeassistant"],
        "heizung": ["mcp.homeassistant"],
        "thermostat": ["mcp.homeassistant"],
        "sensor": ["mcp.homeassistant"],
        # Media
        "jellyfin": ["mcp.jellyfin"],
        "film": ["mcp.jellyfin"],
        "movie": ["mcp.jellyfin"],
        "serie": ["mcp.jellyfin"],
        "musik": ["mcp.jellyfin"],
        "music": ["mcp.jellyfin"],
        # News
        "news": ["mcp.news"],
        "nachrichten": ["mcp.news"],
        "artikel": ["mcp.news"],
        # Workflow
        "n8n": ["mcp.n8n"],
        "workflow": ["mcp.n8n"],
        # Web search
        "web": ["mcp.search"],
        "google": ["mcp.search"],
        "internet": ["mcp.search"],
    }

    def select_relevant_tools(self, query: str, max_tools: int = 20) -> Dict[str, "ToolDefinition"]:
        """
        Select tools relevant to the user's query using keyword matching.

        Matches query words against known keyword-to-server mappings to select
        only the MCP server tool groups that are likely needed. Falls back to
        all tools if no keywords match.

        Args:
            query: The user's original message
            max_tools: Maximum number of tools to include

        Returns:
            Dict of relevant ToolDefinitions
        """
        if not self._tools:
            return {}

        query_lower = query.lower()
        # Extract words from query
        query_words = set(re.findall(r'[a-zäöüß]+', query_lower))

        # Find matching server prefixes
        matched_prefixes: Set[str] = set()
        for keyword, prefixes in self._SERVER_KEYWORD_MAP.items():
            # Match if keyword appears as a word or substring in query
            if keyword in query_words or keyword in query_lower:
                matched_prefixes.update(prefixes)

        if not matched_prefixes:
            # No keyword matches — return all tools (fallback)
            logger.debug(f"🔧 No keyword matches for '{query[:50]}', using all {len(self._tools)} tools")
            return self._tools

        # Select tools whose names start with any matched prefix
        selected: Dict[str, ToolDefinition] = {}
        for name, tool in self._tools.items():
            if any(name.startswith(prefix) for prefix in matched_prefixes):
                selected[name] = tool

        # Always include internal tools (minimal token cost, LLM decides usage)
        for name, tool in self._tools.items():
            if name.startswith("internal."):
                selected[name] = tool

        if not selected:
            # Safety: if matching produced empty set, return all tools
            logger.debug(f"🔧 Prefix matching produced no tools, using all {len(self._tools)}")
            return self._tools

        # Cap at max_tools
        if len(selected) > max_tools:
            # Prioritize tools with names containing query words
            scored = []
            for name, tool in selected.items():
                score = sum(1 for w in query_words if w in name.lower() or w in tool.description.lower())
                scored.append((score, name, tool))
            scored.sort(key=lambda x: x[0], reverse=True)
            selected = {name: tool for _, name, tool in scored[:max_tools]}

        logger.info(
            f"🔧 Agent tool filter: {len(selected)}/{len(self._tools)} tools selected "
            f"(prefixes: {sorted(matched_prefixes)})"
        )
        return selected
