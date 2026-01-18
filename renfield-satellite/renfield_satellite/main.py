"""
Renfield Satellite - Main Entry Point

Starts the satellite voice assistant service.
"""

import asyncio
import signal
import sys
from typing import Optional

from .config import load_config, Config
from .satellite import Satellite


# Global satellite instance for signal handling
_satellite: Optional[Satellite] = None
_loop: Optional[asyncio.AbstractEventLoop] = None


def signal_handler(signum, frame):
    """Handle shutdown signals"""
    print(f"\nReceived signal {signum}")
    if _satellite and _loop and _loop.is_running():
        # Schedule stop in the event loop (thread-safe)
        _loop.call_soon_threadsafe(lambda: asyncio.create_task(_satellite.stop()))


async def main(config_path: Optional[str] = None):
    """
    Main entry point.

    Args:
        config_path: Optional path to configuration file
    """
    global _satellite, _loop

    # Store event loop for signal handler
    _loop = asyncio.get_running_loop()

    print("=" * 50)
    print("  Renfield Satellite Voice Assistant")
    print("=" * 50)

    # Load configuration
    config = load_config(config_path)

    print(f"Satellite ID: {config.satellite.id}")
    print(f"Room: {config.satellite.room}")
    print(f"Server: {config.server.url}")
    print(f"Wake word: {config.wakeword.model}")
    print("=" * 50)

    # Create satellite
    _satellite = Satellite(config)

    # Setup signal handlers
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    try:
        # Start satellite
        await _satellite.start()
    except KeyboardInterrupt:
        print("\nInterrupted by user")
    except Exception as e:
        print(f"Fatal error: {e}")
        import traceback
        traceback.print_exc()
    finally:
        await _satellite.stop()


def run():
    """Entry point for console script"""
    config_path = None

    # Check for config path argument
    if len(sys.argv) > 1:
        config_path = sys.argv[1]

    # Run async main
    asyncio.run(main(config_path))


if __name__ == "__main__":
    run()
