#!/usr/bin/env python3
"""Performance testing for plugin system"""
import asyncio
import sys
import time
from pathlib import Path

# Add backend to path
sys.path.insert(0, str(Path(__file__).parent))

from integrations.core.plugin_loader import PluginLoader
from integrations.core.plugin_registry import PluginRegistry


def test_plugin_loading_time():
    """Test plugin loading performance"""
    print("=" * 60)
    print("⚡ PERFORMANCE TEST: Plugin Loading Time")
    print("=" * 60)
    print()

    # Measure cold start
    start_time = time.time()
    loader = PluginLoader('integrations/plugins')
    plugins = loader.load_all_plugins()
    cold_start_time = (time.time() - start_time) * 1000

    print(f"📂 Plugins found: {len(plugins)}")
    print(f"⏱️  Cold start time: {cold_start_time:.2f}ms")

    # Measure registration time
    start_time = time.time()
    registry = PluginRegistry()
    registry.register_plugins(plugins)
    registration_time = (time.time() - start_time) * 1000

    print(f"⏱️  Registration time: {registration_time:.2f}ms")

    total_intents = len(registry.get_all_intents())
    print(f"📊 Total intents: {total_intents}")

    total_time = cold_start_time + registration_time
    print(f"⏱️  Total startup time: {total_time:.2f}ms")
    print()

    # Performance benchmarks
    if total_time < 100:
        print("✅ EXCELLENT: Startup time < 100ms")
    elif total_time < 500:
        print("✅ GOOD: Startup time < 500ms")
    elif total_time < 1000:
        print("⚠️  OK: Startup time < 1s")
    else:
        print("❌ SLOW: Startup time > 1s")

    print()
    return plugins, registry


async def test_api_call_latency(registry):
    """Test API call performance"""
    print("=" * 60)
    print("⚡ PERFORMANCE TEST: API Call Latency")
    print("=" * 60)
    print()

    plugin = registry.get_plugin_for_intent('weather.get_current')
    if not plugin:
        print("⏭️  Weather plugin not available")
        return

    # Warm-up request
    await plugin.execute('weather.get_current', {'location': 'Berlin'})

    # Measure 5 requests
    latencies = []
    for i in range(5):
        start_time = time.time()
        result = await plugin.execute('weather.get_current', {'location': 'Berlin'})
        latency = (time.time() - start_time) * 1000
        latencies.append(latency)

        status = "✅" if result['success'] else "❌"
        print(f"Request {i+1}: {status} {latency:.2f}ms")

    print()
    avg_latency = sum(latencies) / len(latencies)
    min_latency = min(latencies)
    max_latency = max(latencies)

    print(f"📊 Average latency: {avg_latency:.2f}ms")
    print(f"📊 Min latency: {min_latency:.2f}ms")
    print(f"📊 Max latency: {max_latency:.2f}ms")
    print()

    # Performance benchmarks
    if avg_latency < 200:
        print("✅ EXCELLENT: Average latency < 200ms")
    elif avg_latency < 500:
        print("✅ GOOD: Average latency < 500ms")
    elif avg_latency < 1000:
        print("⚠️  OK: Average latency < 1s")
    else:
        print("❌ SLOW: Average latency > 1s")

    print()


async def test_concurrent_requests(registry):
    """Test concurrent API calls"""
    print("=" * 60)
    print("⚡ PERFORMANCE TEST: Concurrent Requests")
    print("=" * 60)
    print()

    plugin = registry.get_plugin_for_intent('weather.get_current')
    if not plugin:
        print("⏭️  Weather plugin not available")
        return

    # Make 10 concurrent requests
    num_requests = 10
    cities = ['Berlin', 'Munich', 'Hamburg', 'Frankfurt', 'Cologne',
              'Stuttgart', 'Düsseldorf', 'Dortmund', 'Essen', 'Leipzig']

    start_time = time.time()

    tasks = [
        plugin.execute('weather.get_current', {'location': city})
        for city in cities
    ]

    results = await asyncio.gather(*tasks)

    total_time = (time.time() - start_time) * 1000

    success_count = sum(1 for r in results if r['success'])

    print(f"📊 Concurrent requests: {num_requests}")
    print(f"✅ Successful: {success_count}/{num_requests}")
    print(f"⏱️  Total time: {total_time:.2f}ms")
    print(f"⏱️  Average per request: {total_time/num_requests:.2f}ms")
    print()

    # Check if concurrent execution was beneficial
    estimated_sequential = num_requests * 300  # Assume 300ms per request
    speedup = estimated_sequential / total_time

    print(f"📈 Estimated speedup: {speedup:.2f}x")

    if speedup > 5:
        print("✅ EXCELLENT: High concurrency benefit")
    elif speedup > 2:
        print("✅ GOOD: Concurrency working well")
    else:
        print("⚠️  OK: Limited concurrency benefit")

    print()


async def test_memory_usage():
    """Test memory footprint of plugin system"""
    print("=" * 60)
    print("⚡ PERFORMANCE TEST: Memory Usage")
    print("=" * 60)
    print()

    try:
        import os

        import psutil

        process = psutil.Process(os.getpid())

        # Measure before loading
        mem_before = process.memory_info().rss / 1024 / 1024  # MB

        # Load plugins
        loader = PluginLoader('integrations/plugins')
        plugins = loader.load_all_plugins()

        registry = PluginRegistry()
        registry.register_plugins(plugins)

        # Measure after loading
        mem_after = process.memory_info().rss / 1024 / 1024  # MB

        mem_increase = mem_after - mem_before

        print(f"📊 Memory before: {mem_before:.2f} MB")
        print(f"📊 Memory after: {mem_after:.2f} MB")
        print(f"📊 Memory increase: {mem_increase:.2f} MB")
        print()

        if mem_increase < 5:
            print("✅ EXCELLENT: Memory overhead < 5 MB")
        elif mem_increase < 20:
            print("✅ GOOD: Memory overhead < 20 MB")
        else:
            print("⚠️  HIGH: Memory overhead > 20 MB")

    except ImportError:
        print("⏭️  psutil not available - skipping memory test")

    print()


async def main():
    """Run all performance tests"""
    print("\n")
    print("⚡" * 30)
    print("PERFORMANCE TEST SUITE")
    print("⚡" * 30)
    print("\n")

    # Test 1: Plugin loading time
    _plugins, registry = test_plugin_loading_time()

    # Test 2: Memory usage
    await test_memory_usage()

    # Test 3: API call latency
    await test_api_call_latency(registry)

    # Test 4: Concurrent requests
    await test_concurrent_requests(registry)

    print("=" * 60)
    print("✅ PERFORMANCE TESTS COMPLETED")
    print("=" * 60)
    print("\n")


if __name__ == "__main__":
    asyncio.run(main())
