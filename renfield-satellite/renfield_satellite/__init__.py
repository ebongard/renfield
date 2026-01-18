"""
Renfield Satellite - Raspberry Pi Voice Assistant

A headless voice assistant satellite for the Renfield ecosystem.
Designed for Raspberry Pi Zero 2 W with ReSpeaker 2-Mics Pi HAT.
"""

__version__ = "1.0.0"
__author__ = "Renfield Team"

from .satellite import Satellite
from .config import Config, load_config

__all__ = ["Satellite", "Config", "load_config", "__version__"]
