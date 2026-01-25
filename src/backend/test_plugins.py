#!/usr/bin/env python3
"""
CLI Tool: Plugin System Tester

Interactive command-line tool for testing and debugging the plugin system.
Run directly: python test_plugins.py

Note: Uses print() for CLI output (not logger) as this is an interactive tool.
"""
import asyncio
import sys
from pathlib import Path

# Add backend to path
sys.path.insert(0, str(Path(__file__).parent))

from integrations.core.plugin_loader import PluginLoader
from integrations.core.plugin_registry import PluginRegistry
from services.ollama_service import OllamaService


async def test_plugin_loading():
    """Test plugin loading and registration"""
    print("=" * 60)
    print("🧪 TESTING PLUGIN SYSTEM")
    print("=" * 60)
    print()

    # Load plugins
    print("📂 Loading plugins...")
    loader = PluginLoader('integrations/plugins')
    plugins = loader.load_all_plugins()

    print(f"✅ Loaded {len(plugins)} plugin(s)")
    print()

    # Register plugins
    print("📋 Registering plugins...")
    registry = PluginRegistry()
    registry.register_plugins(plugins)

    # Get all intents
    all_intents = registry.get_all_intents()
    print(f"✅ Registered {len(all_intents)} intent(s)")
    print()

    # Display intents
    print("=" * 60)
    print("📊 REGISTERED INTENTS")
    print("=" * 60)
    print()

    for intent_def in all_intents:
        params = ', '.join([
            f"{p.name}{'*' if p.required else ''}"
            for p in intent_def.parameters
        ])

        print(f"Intent: {intent_def.name}")
        print(f"  Description: {intent_def.description}")
        print(f"  Parameters: {params or 'none'}")

        if intent_def.examples:
            print(f"  Examples:")
            for example in intent_def.examples[:2]:
                print(f"    - \"{example}\"")
        print()

    # Test LLM prompt generation
    print("=" * 60)
    print("🤖 LLM PROMPT GENERATION TEST")
    print("=" * 60)
    print()

    ollama = OllamaService()
    plugin_context = ollama._build_plugin_context(registry)

    print("Generated Plugin Context:")
    print("-" * 60)
    print(plugin_context)
    print("-" * 60)
    print()

    print("✅ All tests completed successfully!")


if __name__ == "__main__":
    asyncio.run(test_plugin_loading())
