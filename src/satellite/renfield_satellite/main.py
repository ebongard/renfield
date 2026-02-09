"""
Renfield Satellite - Main Entry Point

Starts the satellite voice assistant service.
"""

import asyncio
import os
import signal
import sys
import threading
from typing import Optional

from .config import load_config, Config
from .satellite import Satellite


# Global satellite instance for signal handling
_satellite: Optional[Satellite] = None
_loop: Optional[asyncio.AbstractEventLoop] = None


def signal_handler(signum, frame):
    """Handle shutdown signals.

    Uses os._exit() as a safety net after 5 seconds. On the ReSpeaker 4-Mic
    Array (AC108 codec), any PyAudio cleanup that touches the driver can crash
    the kernel. os._exit() bypasses Python's atexit handlers and object
    finalizers, letting the OS release resources safely.
    """
    print(f"\nReceived signal {signum}, shutting down...")

    # Safety net: force-exit after 5 seconds so the process never hangs.
    # This prevents systemd from sending SIGKILL after TimeoutStopSec,
    # which could leave the AC108 driver in a dirty state.
    def _force_exit():
        print("Graceful shutdown timed out, forcing exit")
        os._exit(0)

    timer = threading.Timer(5.0, _force_exit)
    timer.daemon = True
    timer.start()

    if _satellite and _loop and _loop.is_running():
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
        # Exit immediately after graceful stop. Python's normal shutdown
        # sequence runs atexit handlers and object finalizers which can
        # trigger pa.terminate() and crash the AC108 kernel driver.
        print("Shutdown complete")
        os._exit(0)


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
