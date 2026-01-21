"""
Plugin registry - manages loaded plugins and intent routing
"""
from typing import Dict, List, Optional
from loguru import logger
from .plugin_schema import PluginDefinition, IntentDefinition


class PluginRegistry:
    """Central registry for all loaded plugins"""

    def __init__(self):
        self.plugins: Dict[str, PluginDefinition] = {}
        self.intent_map: Dict[str, 'GenericPlugin'] = {}  # Maps intent name to plugin instance
        self.plugin_instances: Dict[str, 'GenericPlugin'] = {}

    def register_plugins(self, plugins: Dict[str, PluginDefinition]):
        """
        Register all loaded plugins

        Args:
            plugins: Dict mapping plugin name to PluginDefinition
        """
        from .generic_plugin import GenericPlugin

        self.plugins = plugins

        # Create plugin instances and build intent map
        for plugin_name, plugin_def in plugins.items():
            # Create plugin instance
            plugin_instance = GenericPlugin(plugin_def)
            self.plugin_instances[plugin_name] = plugin_instance

            # Map all intents to this plugin
            for intent_def in plugin_def.intents:
                intent_name = intent_def.name
                if intent_name in self.intent_map:
                    logger.warning(
                        f"⚠️  Intent conflict! '{intent_name}' already registered by another plugin. "
                        f"Overwriting with plugin '{plugin_name}'"
                    )
                self.intent_map[intent_name] = plugin_instance
                logger.debug(f"📝 Registered intent: {intent_name} → {plugin_name}")

        logger.info(f"📋 Registry: {len(self.plugins)} plugins, {len(self.intent_map)} intents")

    def get_plugin_for_intent(self, intent_name: str) -> Optional['GenericPlugin']:
        """Get plugin instance that handles given intent"""
        return self.intent_map.get(intent_name)

    def get_all_intents(self) -> List[IntentDefinition]:
        """Get all registered intents for LLM prompt generation"""
        all_intents = []
        for plugin_def in self.plugins.values():
            all_intents.extend(plugin_def.intents)
        return all_intents

    def get_intent_definition(self, intent_name: str) -> Optional[IntentDefinition]:
        """Get intent definition by name"""
        for plugin_def in self.plugins.values():
            for intent_def in plugin_def.intents:
                if intent_def.name == intent_name:
                    return intent_def
        return None

    def list_plugins(self) -> List[Dict]:
        """List all loaded plugins with metadata"""
        return [
            {
                "name": plugin_def.metadata.name,
                "version": plugin_def.metadata.version,
                "description": plugin_def.metadata.description,
                "intents": [i.name for i in plugin_def.intents]
            }
            for plugin_def in self.plugins.values()
        ]
