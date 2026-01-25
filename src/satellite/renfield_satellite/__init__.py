"""
Renfield Satellite - Raspberry Pi Voice Assistant

A headless voice assistant satellite for the Renfield ecosystem.
Designed for Raspberry Pi Zero 2 W with ReSpeaker 2-Mics Pi HAT.
"""

__version__ = "1.0.0"
__author__ = "Renfield Team"

# Lazy imports to allow CLI tools to run without all dependencies
def __getattr__(name):
    if name == "Satellite":
        from .satellite import Satellite
        return Satellite
    elif name == "Config":
        from .config import Config
        return Config
    elif name == "load_config":
        from .config import load_config
        return load_config
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

__all__ = ["Satellite", "Config", "load_config", "__version__"]
