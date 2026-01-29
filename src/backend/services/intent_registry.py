"""
Intent Registry — Central registry for all available intents.

Dynamically manages intents based on enabled integrations.
Core integrations (Home Assistant, Frigate, n8n, RAG) register their intents here.
Plugins and MCP tools also contribute their intents.

Usage:
    from services.intent_registry import intent_registry

    # Get prompt text for all enabled intents
    prompt_text = await intent_registry.build_intent_prompt(lang="de")

    # Check if an intent is available
    if intent_registry.is_intent_available("homeassistant.turn_on"):
        ...
"""
from dataclasses import dataclass, field
from typing import Dict, List, Optional, TYPE_CHECKING
from loguru import logger

from utils.config import settings
from services.prompt_manager import prompt_manager

if TYPE_CHECKING:
    from integrations.core.plugin_registry import PluginRegistry


@dataclass
class IntentParam:
    """Parameter definition for an intent."""
    name: str
    description: str
    required: bool = False
    param_type: str = "string"  # string, integer, float, boolean
    enum: Optional[List[str]] = None  # Valid values


@dataclass
class IntentDef:
    """Definition of a single intent."""
    name: str  # e.g., "homeassistant.turn_on"
    description_de: str
    description_en: str
    parameters: List[IntentParam] = field(default_factory=list)
    examples_de: List[str] = field(default_factory=list)
    examples_en: List[str] = field(default_factory=list)

    def get_description(self, lang: str = "de") -> str:
        """Get description in specified language."""
        return self.description_en if lang == "en" else self.description_de

    def get_examples(self, lang: str = "de") -> List[str]:
        """Get examples in specified language."""
        return self.examples_en if lang == "en" else self.examples_de


@dataclass
class IntegrationIntents:
    """Collection of intents for an integration."""
    integration_name: str
    title_de: str
    title_en: str
    intents: List[IntentDef]
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
    - Plugins (via PluginRegistry)
    - MCP servers (dynamically discovered)
    """

    def __init__(self):
        self._plugin_registry: Optional["PluginRegistry"] = None
        self._mcp_tools: List[Dict] = []

    def set_plugin_registry(self, registry: "PluginRegistry") -> None:
        """Set the plugin registry for plugin intent access."""
        self._plugin_registry = registry

    def set_mcp_tools(self, tools: List[Dict]) -> None:
        """Set available MCP tools for intent prompt."""
        self._mcp_tools = tools

    def get_enabled_integrations(self) -> List[IntegrationIntents]:
        """Get list of enabled core integrations."""
        return [i for i in CORE_INTEGRATIONS if i.is_enabled_func()]

    def is_intent_available(self, intent_name: str) -> bool:
        """Check if an intent is currently available."""
        # Check core integrations
        for integration in self.get_enabled_integrations():
            for intent in integration.intents:
                if intent.name == intent_name:
                    return True

        # Check plugins
        if self._plugin_registry:
            if self._plugin_registry.get_plugin_for_intent(intent_name):
                return True

        # Check MCP tools
        for tool in self._mcp_tools:
            if tool.get("intent") == intent_name:
                return True

        return False

    def get_intent_definition(self, intent_name: str) -> Optional[IntentDef]:
        """Get definition of an intent by name."""
        for integration in self.get_enabled_integrations():
            for intent in integration.intents:
                if intent.name == intent_name:
                    return intent
        return None

    def build_intent_prompt(self, lang: str = "de") -> str:
        """
        Build the dynamic intent prompt section based on enabled integrations.

        Args:
            lang: Language for descriptions (de/en)

        Returns:
            Formatted prompt text listing all available intents
        """
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

        # Plugins (if registry available)
        if self._plugin_registry and settings.plugins_enabled:
            plugin_intents = self._plugin_registry.get_all_intents()
            if plugin_intents:
                title = "PLUGINS" if lang == "en" else "PLUGINS"
                section_lines = [f"=== {title} ==="]

                for intent_def in plugin_intents:
                    params_str = ""
                    if intent_def.parameters:
                        params = [
                            f"{p.name}{'*' if p.required else ''}"
                            for p in intent_def.parameters
                        ]
                        params_str = f" ({', '.join(params)})"

                    section_lines.append(f"- {intent_def.name}: {intent_def.description}{params_str}")

                sections.append("\n".join(section_lines))

        # MCP Tools (if enabled and tools available)
        if settings.mcp_enabled and self._mcp_tools:
            title = "MCP TOOLS" if lang == "en" else "MCP TOOLS"
            section_lines = [f"=== {title} ==="]

            for tool in self._mcp_tools:
                intent_name = tool.get("intent", tool.get("name", "unknown"))
                description = tool.get("description", "")
                section_lines.append(f"- {intent_name}: {description}")

            sections.append("\n".join(section_lines))

        return "\n\n".join(sections)

    def build_examples_prompt(self, lang: str = "de", max_examples: int = 10) -> str:
        """
        Build examples section for the intent prompt.

        Args:
            lang: Language for examples (de/en)
            max_examples: Maximum number of examples to include

        Returns:
            Formatted examples text
        """
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

        if not examples:
            return ""

        header = "EXAMPLES:" if lang == "en" else "BEISPIELE:"
        lines = [header]

        for i, (example, intent_name) in enumerate(examples, 1):
            # Simplified JSON representation
            lines.append(f'{i}. "{example}" → {{"intent":"{intent_name}",...}}')

        return "\n".join(lines)

    def get_status(self) -> Dict:
        """Get status of all integrations and their intents."""
        status = {
            "enabled_integrations": [],
            "disabled_integrations": [],
            "total_intents": 0,
            "plugins": 0,
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

        if self._plugin_registry:
            plugin_intents = self._plugin_registry.get_all_intents()
            status["plugins"] = len(plugin_intents)
            status["total_intents"] += len(plugin_intents)

        status["mcp_tools"] = len(self._mcp_tools)
        status["total_intents"] += len(self._mcp_tools)

        return status


# Global singleton instance
intent_registry = IntentRegistry()
