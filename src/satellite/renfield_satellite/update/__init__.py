"""
OTA Update module for Renfield Satellite

Provides functionality for downloading, installing, and rolling back
satellite software updates.
"""

from .update_manager import UpdateManager, UpdateStage, UpdateError

__all__ = ["UpdateManager", "UpdateStage", "UpdateError"]
