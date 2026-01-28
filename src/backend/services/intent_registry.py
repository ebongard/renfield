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

HOME_ASSISTANT_INTENTS = IntegrationIntents(
    integration_name="homeassistant",
    title_de="SMART HOME (Home Assistant)",
    title_en="SMART HOME (Home Assistant)",
    is_enabled_func=lambda: bool(settings.home_assistant_url and settings.home_assistant_token),
    intents=[
        IntentDef(
            name="homeassistant.turn_on",
            description_de="Gerät einschalten",
            description_en="Turn device on",
            parameters=[IntentParam("entity_id", "Entity ID des Geräts", required=True)],
            examples_de=["Schalte das Licht ein", "Mach die Lampe an"],
            examples_en=["Turn on the light", "Switch on the lamp"],
        ),
        IntentDef(
            name="homeassistant.turn_off",
            description_de="Gerät ausschalten",
            description_en="Turn device off",
            parameters=[IntentParam("entity_id", "Entity ID des Geräts", required=True)],
            examples_de=["Schalte das Licht aus", "Mach die Lampe aus"],
            examples_en=["Turn off the light", "Switch off the lamp"],
        ),
        IntentDef(
            name="homeassistant.toggle",
            description_de="Gerät umschalten",
            description_en="Toggle device",
            parameters=[IntentParam("entity_id", "Entity ID des Geräts", required=True)],
        ),
        IntentDef(
            name="homeassistant.get_state",
            description_de="Status eines Geräts abfragen",
            description_en="Get device state",
            parameters=[IntentParam("entity_id", "Entity ID des Geräts", required=True)],
            examples_de=["Ist das Fenster offen?", "Wie ist der Status der Heizung?"],
            examples_en=["Is the window open?", "What's the status of the heater?"],
        ),
        IntentDef(
            name="homeassistant.set_brightness",
            description_de="Helligkeit setzen (0-255)",
            description_en="Set brightness (0-255)",
            parameters=[
                IntentParam("entity_id", "Entity ID der Lampe", required=True),
                IntentParam("brightness", "Helligkeit 0-255", required=True, param_type="integer"),
            ],
            examples_de=["Dimme das Licht auf 50%"],
            examples_en=["Dim the light to 50%"],
        ),
        IntentDef(
            name="homeassistant.set_temperature",
            description_de="Temperatur setzen (Grad)",
            description_en="Set temperature (degrees)",
            parameters=[
                IntentParam("entity_id", "Entity ID des Thermostats", required=True),
                IntentParam("temperature", "Zieltemperatur", required=True, param_type="float"),
            ],
            examples_de=["Stelle die Heizung auf 21 Grad"],
            examples_en=["Set the heating to 21 degrees"],
        ),
        IntentDef(
            name="homeassistant.set_color",
            description_de="Farbe einer Lampe setzen",
            description_en="Set light color",
            parameters=[
                IntentParam("entity_id", "Entity ID der Lampe", required=True),
                IntentParam("color", "Farbe (red, blue, green, etc.)", required=True),
            ],
        ),
        IntentDef(
            name="homeassistant.media_play",
            description_de="Medien abspielen",
            description_en="Play media",
            parameters=[IntentParam("entity_id", "Entity ID des Media Players", required=True)],
        ),
        IntentDef(
            name="homeassistant.media_pause",
            description_de="Medien pausieren",
            description_en="Pause media",
            parameters=[IntentParam("entity_id", "Entity ID des Media Players", required=True)],
        ),
        IntentDef(
            name="homeassistant.media_stop",
            description_de="Medien stoppen",
            description_en="Stop media",
            parameters=[IntentParam("entity_id", "Entity ID des Media Players", required=True)],
        ),
        IntentDef(
            name="homeassistant.media_next",
            description_de="Nächster Titel",
            description_en="Next track",
            parameters=[IntentParam("entity_id", "Entity ID des Media Players", required=True)],
        ),
        IntentDef(
            name="homeassistant.media_previous",
            description_de="Vorheriger Titel",
            description_en="Previous track",
            parameters=[IntentParam("entity_id", "Entity ID des Media Players", required=True)],
        ),
        IntentDef(
            name="homeassistant.set_volume",
            description_de="Lautstärke setzen (0-100)",
            description_en="Set volume (0-100)",
            parameters=[
                IntentParam("entity_id", "Entity ID des Media Players", required=True),
                IntentParam("volume", "Lautstärke 0-100", required=True, param_type="integer"),
            ],
        ),
    ],
)

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

CAMERA_INTENTS = IntegrationIntents(
    integration_name="camera",
    title_de="KAMERA (Frigate)",
    title_en="CAMERA (Frigate)",
    is_enabled_func=lambda: bool(settings.frigate_url),
    intents=[
        IntentDef(
            name="camera.get_events",
            description_de="Kamera-Events abrufen",
            description_en="Get camera events",
            parameters=[
                IntentParam("camera", "Kameraname (optional)", required=False),
                IntentParam("label", "Objekttyp: person, car, etc. (optional)", required=False),
            ],
            examples_de=["Zeige Kamera-Events", "Was hat die Kamera aufgezeichnet?"],
            examples_en=["Show camera events", "What did the camera record?"],
        ),
        IntentDef(
            name="camera.get_snapshot",
            description_de="Aktuelles Kamerabild abrufen",
            description_en="Get current camera snapshot",
            parameters=[IntentParam("camera", "Kameraname", required=True)],
            examples_de=["Zeige Bild von der Haustür-Kamera"],
            examples_en=["Show image from the front door camera"],
        ),
        IntentDef(
            name="camera.list_cameras",
            description_de="Verfügbare Kameras auflisten",
            description_en="List available cameras",
            examples_de=["Welche Kameras gibt es?"],
            examples_en=["Which cameras are available?"],
        ),
    ],
)

N8N_INTENTS = IntegrationIntents(
    integration_name="n8n",
    title_de="WORKFLOWS (n8n)",
    title_en="WORKFLOWS (n8n)",
    is_enabled_func=lambda: bool(settings.n8n_webhook_url),
    intents=[
        IntentDef(
            name="n8n.trigger",
            description_de="n8n Workflow auslösen",
            description_en="Trigger n8n workflow",
            parameters=[
                IntentParam("workflow", "Workflow-Name oder ID", required=True),
                IntentParam("data", "Optionale Daten für den Workflow", required=False),
            ],
            examples_de=["Starte den Backup-Workflow", "Führe Automatisierung aus"],
            examples_en=["Start the backup workflow", "Run automation"],
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

# All core integrations
CORE_INTEGRATIONS = [
    HOME_ASSISTANT_INTENTS,
    KNOWLEDGE_INTENTS,
    CAMERA_INTENTS,
    N8N_INTENTS,
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
