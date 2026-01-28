"""
Agent Tool Registry — Wraps existing infrastructure as tool descriptions for the LLM.

Generates compact tool descriptions from:
- Core tools (Home Assistant actions)
- Plugin tools (YAML-based plugins)

These descriptions are included in the Agent Loop prompt so the LLM knows
which tools it can call.
"""
from typing import Dict, List, Optional, TYPE_CHECKING
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
    """Registry of all tools available to the Agent Loop."""

    # Core Home Assistant tools (always available if HA is configured)
    CORE_HA_TOOLS: List[ToolDefinition] = [
        ToolDefinition(
            name="homeassistant.turn_on",
            description="Gerät einschalten (Licht, Schalter, etc.)",
            parameters={"entity_id": "Home Assistant Entity ID (z.B. light.wohnzimmer)"},
        ),
        ToolDefinition(
            name="homeassistant.turn_off",
            description="Gerät ausschalten",
            parameters={"entity_id": "Home Assistant Entity ID"},
        ),
        ToolDefinition(
            name="homeassistant.toggle",
            description="Gerät umschalten (ein/aus wechseln)",
            parameters={"entity_id": "Home Assistant Entity ID"},
        ),
        ToolDefinition(
            name="homeassistant.get_state",
            description="Status eines Geräts oder Sensors abfragen",
            parameters={"entity_id": "Home Assistant Entity ID"},
        ),
        ToolDefinition(
            name="homeassistant.set_value",
            description="Wert setzen (z.B. Temperatur, Helligkeit)",
            parameters={
                "entity_id": "Home Assistant Entity ID",
                "value": "Zielwert",
                "attribute": "Attribut (z.B. temperature, brightness)",
            },
        ),
    ]

    def __init__(self, plugin_registry: Optional["PluginRegistry"] = None, ha_available: bool = True, mcp_manager: Optional["MCPManager"] = None):
        self._tools: Dict[str, ToolDefinition] = {}
        self._ha_available = ha_available

        # Register core tools
        if ha_available:
            for tool in self.CORE_HA_TOOLS:
                self._tools[tool.name] = tool

        # Register plugin tools
        if plugin_registry:
            self._register_plugin_tools(plugin_registry)

        # Register MCP tools
        if mcp_manager:
            self._register_mcp_tools(mcp_manager)

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

    def _register_mcp_tools(self, mcp_manager: "MCPManager") -> None:
        """Register all MCP tools as agent tools."""
        for mcp_tool in mcp_manager.get_all_tools():
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

    def build_tools_prompt(self) -> str:
        """
        Build a compact text description of all available tools for the LLM prompt.

        Returns:
            Formatted string listing all tools with parameters.
        """
        if not self._tools:
            return "KEINE TOOLS VERFÜGBAR."

        lines = ["VERFÜGBARE TOOLS:"]

        for tool in self._tools.values():
            if tool.parameters:
                params_str = ", ".join(
                    f"{name}: {desc}" for name, desc in tool.parameters.items()
                )
                lines.append(f"- {tool.name}: {tool.description} | Parameter: {{{params_str}}}")
            else:
                lines.append(f"- {tool.name}: {tool.description}")

        return "\n".join(lines)
