"""
Agent Tool Registry — Wraps existing infrastructure as tool descriptions for the LLM.

Generates compact tool descriptions from:
- MCP servers (Home Assistant, n8n, weather, search, etc.)
- Internal tools (room resolution, media playback)

These descriptions are included in the Agent Loop prompt so the LLM knows
which tools it can call.

Tool filtering is handled by AgentRouter which classifies messages into
roles (smart_home, research, documents, etc.) with pre-defined MCP server lists.
"""
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Optional

from loguru import logger

if TYPE_CHECKING:
    from services.mcp_client import MCPManager


# --- Tool name sanitization ------------------------------------------------
# OpenAI / Ollama / most native-FC backends require tool names to match
# ^[a-zA-Z0-9_-]+$. MCP namespaces use dots (e.g. "mcp.release.list_releases"),
# which would be rejected. We round-trip by swapping dots for double
# underscores during the request and reversing on the response.
_SANITIZED_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]+$")


def sanitize_tool_name(name: str) -> str:
    """Convert a dotted tool name to a native-FC-compatible identifier.

    `mcp.release.list_releases` → `mcp__release__list_releases`.
    Already-valid names pass through unchanged.
    """
    if _SANITIZED_NAME_RE.match(name):
        return name
    return name.replace(".", "__")


def unsanitize_tool_name(name: str) -> str:
    """Reverse of :func:`sanitize_tool_name`.

    Collapses `__` back to `.`. Safe to call on names that were never
    sanitized (idempotent for names without `__`).
    """
    return name.replace("__", ".")


@dataclass
class ToolDefinition:
    """Definition of a tool available to the Agent.

    `parameters` is the flattened {param_name: description_str} form used by
    the ReAct prompt builder. `input_schema` is the full JSON Schema from the
    underlying tool source (MCP's input_schema, internal tool declarations,
    plugin-registered tools). It is preserved so native function-calling
    (OpenAI-style `tools=[]`) can emit proper JSON-Schema to the LLM without
    a lossy round-trip through the flattened form.
    """
    name: str
    description: str
    parameters: dict[str, str] = field(default_factory=dict)  # param_name -> description
    input_schema: dict | None = None  # full JSON Schema, for native function calling


class AgentToolRegistry:
    """Registry of all tools available to the Agent Loop.

    Tools are registered dynamically from:
    - MCP servers (Home Assistant, n8n, weather, search, etc.)
    - Internal tools (room resolution, media playback)
    """

    def __init__(
        self,
        mcp_manager: Optional["MCPManager"] = None,
        server_filter: list[str] | None = None,
        internal_filter: list[str] | None = None,
        _init_only: bool = False,
    ):
        """Initialize the tool registry with MCP + platform-internal tools.

        Direct construction is restricted: ``__init__`` does NOT run the
        ``register_tools`` hook, so plugin tools (Reva's
        ``resolve_team_members`` etc.) are absent. Production code must use
        :meth:`AgentToolRegistry.create` instead — it runs the hook in-line
        and returns a fully populated registry. Tests that don't need
        plugin tools may opt into direct construction by passing
        ``_init_only=True``.

        The guard exists because TD-13 (Reva P0.5) was a silent-failure
        regression caused by exactly this gap — a caller that constructed
        the registry without awaiting the plugin hook lost plugin tools
        with no signal in the logs. We trade a flagged ``RuntimeError`` at
        the wrong call site for that class of mistake.

        Args:
            mcp_manager: MCP server manager
            server_filter: If set, only include MCP tools from these server names.
                          None means include all servers.
            internal_filter: If set, only include these internal tool names.
                            None means include all internal tools.
            _init_only: Test-only escape hatch. Set to True to construct a
                registry without plugin tools. Production code must not
                pass this — use :meth:`create` instead.

        Raises:
            RuntimeError: if ``_init_only`` is False (the default).
        """
        if not _init_only:
            raise RuntimeError(
                "Direct AgentToolRegistry(...) construction is restricted "
                "because it skips plugin-tool registration "
                "(the register_tools hook). Production code must use "
                "`await AgentToolRegistry.create(...)`. Tests that don't "
                "need plugin tools may pass `_init_only=True`."
            )

        self._tools: dict[str, ToolDefinition] = {}

        # Expose the construction filters as public attributes so the
        # register_tools hook (and other plugins) can scope their additions
        # the same way the built-in MCP/internal registration does.
        # Without this, plugins have no way to know which sub-set of tools
        # the caller intended for this registry — they would over-register.
        self.server_filter = server_filter
        self.internal_filter = internal_filter

        # Register MCP tools (includes HA, n8n, weather, search, etc.)
        if mcp_manager:
            self._register_mcp_tools(mcp_manager, server_filter=server_filter)

        # Register internal agent tools (room resolution, media playback)
        self._register_internal_tools(internal_filter=internal_filter)

    @classmethod
    async def create(
        cls,
        mcp_manager: Optional["MCPManager"] = None,
        server_filter: list[str] | None = None,
        internal_filter: list[str] | None = None,
    ) -> "AgentToolRegistry":
        """Async constructor — builds the registry AND awaits plugin tools.

        Replaces the previous pattern where ``__init__`` scheduled a
        background task and every call site had to remember
        ``await tool_registry._hook_task``. With ``create()`` the registry
        returned to the caller is fully populated.

        Plugin hooks are awaited directly here (not via ``run_hooks``) so
        that a failing handler propagates as a real exception. Caller code
        — notably the orchestrator — relies on this to fail fast with a
        diagnosable error instead of silently using a half-populated
        registry. ``run_hooks`` swallows handler exceptions for fire-and-
        forget event sites; ``register_tools`` is fail-loud.
        """
        from utils.hooks import _hooks

        registry = cls(
            mcp_manager=mcp_manager,
            server_filter=server_filter,
            internal_filter=internal_filter,
            _init_only=True,
        )
        for hook in _hooks.get("register_tools", []):
            await hook(registry=registry)
        return registry

    def _register_internal_tools(self, internal_filter: list[str] | None = None) -> None:
        """Register platform-owned internal agent tools.

        The only `internal.*` tool the platform ships is `knowledge_search`
        (pure RAG, no ha_glue deps). Every other `internal.*` tool is
        registered by ha_glue via the `register_tools` hook. On platform-
        only deploys without ha_glue, the agent loop simply never sees
        media/room/presence tools — which is correct.

        Args:
            internal_filter: If set, only register these tool names. None = all.
        """
        from services.chat_upload_tool import CHAT_UPLOAD_TOOLS
        from services.knowledge_tool import KNOWLEDGE_TOOL

        platform_tools: dict = {**KNOWLEDGE_TOOL, **CHAT_UPLOAD_TOOLS}

        for name, definition in platform_tools.items():
            if internal_filter is not None and name not in internal_filter:
                continue

            params = dict(definition.get("parameters", {}))
            tool = ToolDefinition(
                name=name,
                description=definition["description"],
                parameters=params,
            )
            self._tools[tool.name] = tool
            logger.debug(f"Internal agent tool registered: {tool.name}")

    def _register_mcp_tools(self, mcp_manager: "MCPManager", server_filter: list[str] | None = None) -> None:
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
                input_schema=mcp_tool.input_schema,
            )
            self._tools[tool.name] = tool
            logger.debug(f"MCP agent tool registered: {tool.name}")

    def get_tool(self, name: str) -> ToolDefinition | None:
        """Get a tool definition by name."""
        return self._tools.get(name)

    def get_tool_names(self) -> list[str]:
        """Get list of all registered tool names."""
        return list(self._tools.keys())

    def resolve_tool_name(self, name: str) -> str | None:
        """Resolve a tool name, supporting short names without namespace prefix.

        Small LLMs sometimes emit the bare tool name instead of the fully
        qualified `mcp.<server>.<tool>` form. This tries exact match
        first, then falls back to suffix match (must be unambiguous).
        """
        if name in self._tools:
            return name
        # Suffix match: find tools ending with '.<name>'
        suffix = f".{name}"
        matches = [k for k in self._tools if k.endswith(suffix)]
        if len(matches) == 1:
            return matches[0]
        return None

    def is_valid_tool(self, name: str) -> bool:
        """Check if a tool name is valid."""
        return self.resolve_tool_name(name) is not None

    def build_tools_prompt(self, tools: dict[str, "ToolDefinition"] | None = None) -> str:
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

    def build_tools_schema(
        self,
        tools: dict[str, "ToolDefinition"] | None = None,
    ) -> list[dict]:
        """Build an OpenAI-format tools array for native function calling.

        Produces the shape expected by OpenAI `/v1/chat/completions`, Ollama
        `/api/chat`, llama.cpp `/v1/chat/completions`, vLLM, and Anthropic
        (which is normalised to the same shape by AnthropicClient). Tool
        names are sanitized via :func:`sanitize_tool_name` so they match
        `^[a-zA-Z0-9_-]+$`. The caller is responsible for un-sanitizing
        names on the response side before dispatching to the executor.

        Falls back to a synthesised JSON Schema from the flattened
        `parameters` dict when `input_schema` is absent (older tools
        registered before the schema was preserved).

        Args:
            tools: Optional subset of registered tools to expose. None means
                all registered tools.

        Returns:
            List of `{"type": "function", "function": {name, description, parameters}}`
            dicts. Empty list if no tools are registered or selected.
        """
        tool_set = tools if tools is not None else self._tools
        result: list[dict] = []
        for tool in tool_set.values():
            if tool.input_schema is not None:
                schema = tool.input_schema
            else:
                # Synthesize a minimal schema from the flattened parameters.
                # All params typed as string because we lost the original
                # type info; the LLM still gets enough to call sanely.
                schema = {
                    "type": "object",
                    "properties": {
                        param: {"type": "string", "description": desc}
                        for param, desc in tool.parameters.items()
                    },
                }
            result.append({
                "type": "function",
                "function": {
                    "name": sanitize_tool_name(tool.name),
                    "description": tool.description,
                    "parameters": schema,
                },
            })
        return result
