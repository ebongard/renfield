#!/usr/bin/env python3
"""Test error handling in plugin system"""
import asyncio
import sys
from pathlib import Path
import tempfile
import os

# Add backend to path
sys.path.insert(0, str(Path(__file__).parent))

from integrations.core.plugin_loader import PluginLoader
from integrations.core.plugin_registry import PluginRegistry
from integrations.core.generic_plugin import GenericPlugin
from integrations.core.plugin_schema import PluginDefinition
import yaml


def test_invalid_yaml():
    """Test 1: Invalid YAML syntax"""
    print("=" * 60)
    print("🧪 TEST 1: Invalid YAML Syntax")
    print("=" * 60)

    with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
        f.write("name: test\n")
        f.write("  invalid: indentation\n")
        f.write("no: [proper, structure\n")
        temp_file = f.name

    try:
        loader = PluginLoader(os.path.dirname(temp_file))
        plugins = loader.load_all_plugins()
        print("✅ Invalid YAML handled gracefully (plugin skipped)")
    except Exception as e:
        print(f"❌ Unhandled exception: {e}")
    finally:
        os.unlink(temp_file)

    print()


def test_missing_required_fields():
    """Test 2: Missing required fields in YAML"""
    print("=" * 60)
    print("🧪 TEST 2: Missing Required Fields")
    print("=" * 60)

    invalid_plugin = """
name: test_plugin
version: 1.0.0
# Missing: description, enabled_var, config, intents
"""

    with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False, dir='/tmp') as f:
        f.write(invalid_plugin)
        temp_file = f.name

    try:
        with open(temp_file, 'r') as f:
            data = yaml.safe_load(f)
            plugin_def = PluginDefinition(**data)
        print("❌ Should have failed validation")
    except Exception as e:
        print(f"✅ Validation error caught: {type(e).__name__}")
        print(f"   Message: {str(e)[:100]}")
    finally:
        os.unlink(temp_file)

    print()


async def test_missing_parameters():
    """Test 3: Missing required parameters when executing"""
    print("=" * 60)
    print("🧪 TEST 3: Missing Required Parameters")
    print("=" * 60)

    # Use weather plugin (already loaded)
    loader = PluginLoader('integrations/plugins')
    plugins = loader.load_all_plugins()

    if 'weather' in plugins:
        registry = PluginRegistry()
        registry.register_plugins(plugins)

        plugin = registry.get_plugin_for_intent('weather.get_current')

        # Try to execute without required 'location' parameter
        result = await plugin.execute('weather.get_current', {})

        if not result['success']:
            print(f"✅ Missing parameter error caught")
            print(f"   Message: {result['message']}")
        else:
            print(f"❌ Should have failed with missing parameter")
    else:
        print("⏭️  Weather plugin not loaded (skipped)")

    print()


async def test_invalid_parameter_type():
    """Test 4: Invalid parameter type"""
    print("=" * 60)
    print("🧪 TEST 4: Invalid Parameter Type")
    print("=" * 60)

    loader = PluginLoader('integrations/plugins')
    plugins = loader.load_all_plugins()

    if 'weather' in plugins:
        registry = PluginRegistry()
        registry.register_plugins(plugins)

        plugin = registry.get_plugin_for_intent('weather.get_forecast')

        # Try with invalid type for 'days' parameter (should be integer)
        result = await plugin.execute('weather.get_forecast', {
            'location': 'Berlin',
            'days': 'not_a_number'  # Wrong type
        })

        if not result['success']:
            print(f"✅ Type validation error caught")
            print(f"   Message: {result['message']}")
        else:
            print(f"❌ Should have failed with type error")
    else:
        print("⏭️  Weather plugin not loaded (skipped)")

    print()


async def test_rate_limiting():
    """Test 5: Rate limiting"""
    print("=" * 60)
    print("🧪 TEST 5: Rate Limiting")
    print("=" * 60)

    loader = PluginLoader('integrations/plugins')
    plugins = loader.load_all_plugins()

    if 'weather' in plugins:
        registry = PluginRegistry()
        registry.register_plugins(plugins)

        plugin = registry.get_plugin_for_intent('weather.get_current')

        # Make many rapid requests to trigger rate limit
        # Weather plugin has rate_limit: 60 per minute
        success_count = 0
        rate_limited = False

        for i in range(65):  # Exceed rate limit
            result = await plugin.execute('weather.get_current', {'location': 'Berlin'})
            if result['success']:
                success_count += 1
            else:
                if 'rate' in result.get('message', '').lower():
                    rate_limited = True
                    print(f"✅ Rate limit triggered after {success_count} requests")
                    print(f"   Message: {result['message']}")
                    break

        if not rate_limited:
            print(f"⚠️  Rate limit not triggered (made {success_count} requests)")
    else:
        print("⏭️  Weather plugin not loaded (skipped)")

    print()


async def test_api_errors():
    """Test 6: API error handling"""
    print("=" * 60)
    print("🧪 TEST 6: API Error Handling")
    print("=" * 60)

    loader = PluginLoader('integrations/plugins')
    plugins = loader.load_all_plugins()

    if 'weather' in plugins:
        registry = PluginRegistry()
        registry.register_plugins(plugins)

        plugin = registry.get_plugin_for_intent('weather.get_current')

        # Try with invalid city name to trigger 404
        result = await plugin.execute('weather.get_current', {
            'location': 'ThisCityDoesNotExist12345'
        })

        if not result['success']:
            print(f"✅ API error handled gracefully")
            print(f"   Message: {result['message']}")
        else:
            print(f"⚠️  API returned success for invalid city")
    else:
        print("⏭️  Weather plugin not loaded (skipped)")

    print()


async def main():
    """Run all error handling tests"""
    print("\n")
    print("🔥" * 30)
    print("ERROR HANDLING TEST SUITE")
    print("🔥" * 30)
    print("\n")

    # Synchronous tests
    test_invalid_yaml()
    test_missing_required_fields()

    # Async tests
    await test_missing_parameters()
    await test_invalid_parameter_type()
    await test_rate_limiting()
    await test_api_errors()

    print("=" * 60)
    print("✅ ERROR HANDLING TESTS COMPLETED")
    print("=" * 60)
    print()


if __name__ == "__main__":
    asyncio.run(main())
