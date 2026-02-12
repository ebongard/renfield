"""
Intent Registry — Central registry for all available intents.

Dynamically manages intents based on enabled integrations.
Core integrations (Home Assistant, Frigate, n8n, RAG) register their intents here.
MCP tools also contribute their intents.

Usage:
    from services.intent_registry import intent_registry

    # Get prompt text for all enabled intents
    prompt_text = await intent_registry.build_intent_prompt(lang="de")

    # Check if an intent is available
    if intent_registry.is_intent_available("homeassistant.turn_on"):
        ...
"""
from dataclasses import dataclass, field

from utils.config import settings


@dataclass
class IntentParam:
    """Parameter definition for an intent."""
    name: str
    description: str
    required: bool = False
    param_type: str = "string"  # string, integer, float, boolean
    enum: list[str] | None = None  # Valid values


@dataclass
class IntentDef:
    """Definition of a single intent."""
    name: str  # e.g., "homeassistant.turn_on"
    description_de: str
    description_en: str
    parameters: list[IntentParam] = field(default_factory=list)
    examples_de: list[str] = field(default_factory=list)
    examples_en: list[str] = field(default_factory=list)

    def get_description(self, lang: str = "de") -> str:
        """Get description in specified language."""
        return self.description_en if lang == "en" else self.description_de

    def get_examples(self, lang: str = "de") -> list[str]:
        """Get examples in specified language."""
        return self.examples_en if lang == "en" else self.examples_de


@dataclass
class IntegrationIntents:
    """Collection of intents for an integration."""
    integration_name: str
    title_de: str
    title_en: str
    intents: list[IntentDef]
    is_enabled_func: callable  # Function that returns True if integration is enabled

    def get_title(self, lang: str = "de") -> str:
        """Get section title in specified language."""
        return self.title_en if lang == "en" else self.title_de


# =============================================================================
# Core Integration Intent Definitions
# =============================================================================

KNOWLEDGE_INTENTS = IntegrationIntents(
    integration_name="knowledge",
    title_de="WISSENSDATENBANK (RAG)",
    title_en="KNOWLEDGE BASE (RAG)",
    is_enabled_func=lambda: settings.rag_enabled,
    intents=[
        IntentDef(
            name="knowledge.search",
            description_de="Suche in der Wissensdatenbank",
            description_en="Search knowledge base",
            parameters=[IntentParam("query", "Suchanfrage", required=True)],
            examples_de=["Suche nach Rezepten", "Finde Informationen über Python"],
            examples_en=["Search for recipes", "Find information about Python"],
        ),
        IntentDef(
            name="knowledge.ask",
            description_de="Frage an die Wissensdatenbank",
            description_en="Ask knowledge base",
            parameters=[IntentParam("question", "Die Frage", required=True)],
            examples_de=["Was steht in meinen Notizen über Docker?"],
            examples_en=["What do my notes say about Docker?"],
        ),
    ],
)

GENERAL_INTENTS = IntegrationIntents(
    integration_name="general",
    title_de="ALLGEMEIN",
    title_en="GENERAL",
    is_enabled_func=lambda: True,  # Always enabled
    intents=[
        IntentDef(
            name="general.conversation",
            description_de="Normale Konversation (kein spezifischer Intent)",
            description_en="Normal conversation (no specific intent)",
            examples_de=["Wie geht es dir?", "Erzähl mir einen Witz"],
            examples_en=["How are you?", "Tell me a joke"],
        ),
    ],
)

# All core integrations (HA, n8n, camera are now MCP-only)
CORE_INTEGRATIONS = [
    KNOWLEDGE_INTENTS,
    GENERAL_INTENTS,
]


class IntentRegistry:
    """
    Central registry for all available intents.

    Manages intents from:
    - Core integrations (Home Assistant, Frigate, n8n, RAG)
    - MCP servers (dynamically discovered)
    """

    def __init__(self):
        self._mcp_tools: list[dict] = []
        self._mcp_examples: dict[str, dict[str, list[str]]] = {}  # server_name → {"de": [...], "en": [...]}
        self._mcp_prompt_tools: dict[str, list[str]] = {}  # server_name → [tool_name, ...]
        # Prompt cache: invalidated when tools/plugins change
        self._prompt_cache: dict[str, str] = {}  # key → cached output
        self._examples_cache: dict[str, str] = {}  # key → cached output

    def _invalidate_prompt_cache(self) -> None:
        """Clear cached prompt outputs when configuration changes."""
        self._prompt_cache.clear()
        self._examples_cache.clear()

    def set_mcp_tools(self, tools: list[dict]) -> None:
        """Set available MCP tools for intent prompt."""
        self._mcp_tools = tools
        self._invalidate_prompt_cache()

    def set_mcp_examples(self, examples: dict[str, dict[str, list[str]]]) -> None:
        """Set MCP server examples from YAML config.

        Args:
            examples: Dict mapping server name to {"de": [...], "en": [...]}
        """
        self._mcp_examples = examples
        self._invalidate_prompt_cache()

    def set_mcp_prompt_tools(self, prompt_tools: dict[str, list[str]]) -> None:
        """Set per-server prompt_tools filter from YAML config.

        Args:
            prompt_tools: Dict mapping server name to list of tool base names
                          to include in the LLM intent prompt.
                          Servers without an entry show all their tools.
        """
        self._mcp_prompt_tools = prompt_tools
        self._invalidate_prompt_cache()

    def get_enabled_integrations(self) -> list[IntegrationIntents]:
        """Get list of enabled core integrations."""
        return [i for i in CORE_INTEGRATIONS if i.is_enabled_func()]

    def is_intent_available(self, intent_name: str) -> bool:
        """Check if an intent is currently available."""
        # Check core integrations
        for integration in self.get_enabled_integrations():
            for intent in integration.intents:
                if intent.name == intent_name:
                    return True

        # Check MCP tools
        return any(tool.get("intent") == intent_name for tool in self._mcp_tools)

    def get_intent_definition(self, intent_name: str) -> IntentDef | None:
        """Get definition of an intent by name."""
        for integration in self.get_enabled_integrations():
            for intent in integration.intents:
                if intent.name == intent_name:
                    return intent
        return None

    def build_intent_prompt(self, lang: str = "de") -> str:
        """
        Build the dynamic intent prompt section based on enabled integrations.

        Uses an in-memory cache that is invalidated when tools/plugins change.

        Args:
            lang: Language for descriptions (de/en)

        Returns:
            Formatted prompt text listing all available intents
        """
        cache_key = f"intent_prompt_{lang}"
        if cache_key in self._prompt_cache:
            return self._prompt_cache[cache_key]

        sections = []

        # Core integrations
        for integration in self.get_enabled_integrations():
            section_lines = [f"=== {integration.get_title(lang)} ==="]

            for intent in integration.intents:
                # Format parameters
                params_str = ""
                if intent.parameters:
                    params = [
                        f"{p.name}{'*' if p.required else ''}"
                        for p in intent.parameters
                    ]
                    params_str = f" ({', '.join(params)})"

                section_lines.append(f"- {intent.name}: {intent.get_description(lang)}{params_str}")

            sections.append("\n".join(section_lines))

        # MCP Tools (if enabled and tools available)
        # Only tools listed in prompt_tools YAML config are shown to avoid
        # overwhelming the LLM. All tools remain available for execution.
        if settings.mcp_enabled and self._mcp_tools:
            title = "MCP TOOLS"
            section_lines = [f"=== {title} ==="]

            for tool in self._mcp_tools:
                server = tool.get("server", "unknown")
                intent_name = tool.get("intent", tool.get("name", "unknown"))

                # Filter: only show tools listed in prompt_tools config
                if self._mcp_prompt_tools:
                    allowed = self._mcp_prompt_tools.get(server)
                    if allowed is not None:
                        # Extract tool's base name (after "mcp.<server>.")
                        base_name = intent_name.split(".")[-1] if "." in intent_name else intent_name
                        if base_name not in allowed:
                            continue
                    else:
                        # Server has no prompt_tools config → show all its tools
                        pass

                description = tool.get("description", "")
                # Include parameter names from input_schema so LLM uses correct params
                schema = tool.get("input_schema", {})
                params = list(schema.get("properties", {}).keys())

                # Simplify lat/lon tools: show only "location" so the LLM provides a city name
                # (auto-geocoded at execution time, other params auto-filled)
                if "latitude" in params and "longitude" in params:
                    params = ["location"]

                if params:
                    params_str = ", ".join(params)
                    section_lines.append(f"- {intent_name}: {description} (parameters: {params_str})")
                else:
                    section_lines.append(f"- {intent_name}: {description}")

            sections.append("\n".join(section_lines))

        result = "\n\n".join(sections)
        self._prompt_cache[cache_key] = result
        return result

    def build_examples_prompt(self, lang: str = "de", max_examples: int = 15) -> str:
        """
        Build examples section for the intent prompt.

        Uses an in-memory cache that is invalidated when tools/plugins change.

        Args:
            lang: Language for examples (de/en)
            max_examples: Maximum number of examples to include

        Returns:
            Formatted examples text
        """
        cache_key = f"examples_{lang}_{max_examples}"
        if cache_key in self._examples_cache:
            return self._examples_cache[cache_key]

        examples = []

        for integration in self.get_enabled_integrations():
            for intent in integration.intents:
                intent_examples = intent.get_examples(lang)
                for example in intent_examples[:2]:  # Max 2 per intent
                    examples.append((example, intent.name))
                    if len(examples) >= max_examples:
                        break
            if len(examples) >= max_examples:
                break

        # MCP tool examples (auto-generated from tool descriptions)
        if settings.mcp_enabled and self._mcp_tools and len(examples) < max_examples:
            mcp_examples = self._build_mcp_examples(lang)
            for example, intent_name in mcp_examples:
                examples.append((example, intent_name))
                if len(examples) >= max_examples:
                    break

        if not examples:
            self._examples_cache[cache_key] = ""
            return ""

        header = "EXAMPLES:" if lang == "en" else "BEISPIELE:"
        lines = [header]

        for i, (example, intent_name) in enumerate(examples, 1):
            # Simplified JSON representation
            lines.append(f'{i}. "{example}" → {{"intent":"{intent_name}",...}}')

        result = "\n".join(lines)
        self._examples_cache[cache_key] = result
        return result

    def _build_mcp_examples(self, lang: str = "de") -> list[tuple]:
        """Generate example queries for MCP tools from YAML-configured examples.

        Examples are read from self._mcp_examples (populated via set_mcp_examples()
        from mcp_servers.yaml). Returns 1 example per server.
        Uses example_intent from YAML config if available, otherwise falls back
        to the first tool of that server.
        """
        examples = []
        seen_servers = set()

        for tool in self._mcp_tools:
            server = tool.get("server", "")
            if server in seen_servers:
                continue
            seen_servers.add(server)

            server_examples = self._mcp_examples.get(server, {})
            # Use configured example_intent, or fall back to first tool of server
            intent_name = server_examples.get("_example_intent") or tool.get("intent", tool.get("name", "unknown"))
            lang_examples = server_examples.get(lang, server_examples.get("de", []))
            for ex in lang_examples[:1]:  # 1 example per server
                examples.append((ex, intent_name))

        return examples

    def get_status(self) -> dict:
        """Get status of all integrations and their intents."""
        status = {
            "enabled_integrations": [],
            "disabled_integrations": [],
            "total_intents": 0,
            "mcp_tools": 0,
        }

        for integration in CORE_INTEGRATIONS:
            if integration.is_enabled_func():
                status["enabled_integrations"].append({
                    "name": integration.integration_name,
                    "title": integration.title_en,
                    "intents": len(integration.intents),
                })
                status["total_intents"] += len(integration.intents)
            else:
                status["disabled_integrations"].append(integration.integration_name)

        status["mcp_tools"] = len(self._mcp_tools)
        status["total_intents"] += len(self._mcp_tools)

        return status


# Global singleton instance
intent_registry = IntentRegistry()
