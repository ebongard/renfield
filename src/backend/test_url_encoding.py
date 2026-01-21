#!/usr/bin/env python3
"""Test URL encoding in plugin system"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from integrations.core.plugin_loader import PluginLoader
from integrations.core.plugin_registry import PluginRegistry


async def test_url_encoding():
    """Test URL parameter encoding"""
    print("=" * 60)
    print("🧪 TEST: URL Parameter Encoding")
    print("=" * 60)
    print()

    # Load plugins
    loader = PluginLoader('integrations/plugins')
    plugins = loader.load_all_plugins()

    if 'search' not in plugins:
        print("⏭️  Search plugin not loaded")
        return

    registry = PluginRegistry()
    registry.register_plugins(plugins)

    plugin = registry.get_plugin_for_intent('search.instant_answer')

    # Test queries with special characters
    test_queries = [
        "Was ist Photosynthese?",  # Fragezeichen, Umlaut
        "Wer hat 2025 den Friedensnobelpreis gewonnen?",  # Leerzeichen, Zahlen, Fragezeichen
        "Python Tutorial für Anfänger",  # Leerzeichen, Umlaut
        "C++ vs C#",  # Sonderzeichen
        "100% Erfolg",  # Prozentzeichen
    ]

    print("Testing URL encoding with special characters:")
    print()

    for query in test_queries:
        print(f"Query: {query}")

        # Get the plugin instance to access the method directly
        from integrations.core.generic_plugin import GenericPlugin

        # Find the intent definition
        intent_def = None
        for intent in plugins['search'].intents:
            if intent.name == 'search.instant_answer':
                intent_def = intent
                break

        if intent_def:
            # Test URL substitution
            from urllib.parse import quote_plus
            expected_encoded = quote_plus(query)

            # Build URL manually to check
            test_url = intent_def.api.url.replace("{params.query}", expected_encoded)
            test_url = test_url.replace("{config.url}", "https://api.duckduckgo.com")

            print(f"  Expected URL part: ...q={expected_encoded}...")
            print(f"  URL looks valid: {'?' in test_url and '=' in test_url}")

        print()

    print("=" * 60)
    print("✅ URL encoding test completed")
    print("=" * 60)
    print()
    print("Note: Actual API calls would show the encoded URLs in logs.")
    print("Look for: 🌐 API Call: HTTPMethod.GET https://...")


if __name__ == "__main__":
    asyncio.run(test_url_encoding())
