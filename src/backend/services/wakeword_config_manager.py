"""
Wake Word Configuration Manager

Manages centralized wake word settings and pushes configuration changes
to all connected devices (satellites, web panels, browsers).

Features:
- Stores/retrieves settings from database
- Falls back to environment variables when DB settings don't exist
- Broadcasts config changes to all subscribers (satellites, web devices)
- Tracks device sync status (which devices have applied the config)
- Provides model availability info for satellites
"""
from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Optional, Dict, Any
from fastapi import WebSocket
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from loguru import logger

from utils.config import settings
from models.database import (
    SystemSetting,
    SETTING_WAKEWORD_KEYWORD,
    SETTING_WAKEWORD_THRESHOLD,
    SETTING_WAKEWORD_COOLDOWN_MS,
)


# Available wake word keywords with their metadata
AVAILABLE_KEYWORDS = [
    {
        "id": "alexa",
        "label": "Alexa",
        "description": "Pre-trained wake word (32-bit ONNX, recommended)"
    },
    {
        "id": "hey_jarvis",
        "label": "Hey Jarvis",
        "description": "Pre-trained wake word"
    },
    {
        "id": "hey_mycroft",
        "label": "Hey Mycroft",
        "description": "Pre-trained wake word"
    },
]

# Valid keyword IDs for validation
VALID_KEYWORDS = [kw["id"] for kw in AVAILABLE_KEYWORDS]


@dataclass
class DeviceSyncStatus:
    """Tracks a device's synchronization status with current config"""
    device_id: str
    device_type: str  # "satellite" or "web_device"
    synced: bool = False
    active_keywords: List[str] = field(default_factory=list)
    failed_keywords: List[str] = field(default_factory=list)
    last_ack_time: Optional[datetime] = None
    error: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "device_id": self.device_id,
            "device_type": self.device_type,
            "synced": self.synced,
            "active_keywords": self.active_keywords,
            "failed_keywords": self.failed_keywords,
            "last_ack_time": self.last_ack_time.isoformat() if self.last_ack_time else None,
            "error": self.error,
        }


@dataclass
class WakeWordConfig:
    """Wake word configuration data"""
    keyword: str
    threshold: float
    cooldown_ms: int
    enabled: bool = True

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for JSON serialization"""
        return {
            "keyword": self.keyword,
            "threshold": self.threshold,
            "cooldown_ms": self.cooldown_ms,
            "enabled": self.enabled,
            "wake_words": [self.keyword],  # For backward compatibility with satellites
        }

    def to_satellite_config(self) -> Dict[str, Any]:
        """Convert to satellite config format"""
        return {
            "wake_words": [self.keyword],
            "threshold": self.threshold,
            "cooldown_ms": self.cooldown_ms,
        }


class WakeWordConfigManager:
    """
    Manages wake word configuration and broadcasts changes to subscribers.

    This is a singleton that:
    - Stores/retrieves settings from database
    - Falls back to environment variables when DB settings don't exist
    - Maintains a list of WebSocket subscribers (satellites, web devices)
    - Broadcasts config changes to all subscribers
    - Tracks device sync status (which devices have applied the config)
    """

    def __init__(self):
        self._subscribers: List[WebSocket] = []
        self._subscriber_info: Dict[WebSocket, Dict[str, str]] = {}  # ws -> {device_id, device_type}
        self._device_sync_status: Dict[str, DeviceSyncStatus] = {}  # device_id -> sync status
        self._pending_config_version: int = 0  # Incremented on each config update
        logger.info("WakeWordConfigManager initialized")

    async def get_config(self, db: AsyncSession) -> WakeWordConfig:
        """
        Load current config from database with fallback to environment variables.

        Args:
            db: Database session

        Returns:
            WakeWordConfig with current settings
        """
        # Try to load from database first
        keyword = await self._get_setting(db, SETTING_WAKEWORD_KEYWORD)
        threshold = await self._get_setting(db, SETTING_WAKEWORD_THRESHOLD)
        cooldown_ms = await self._get_setting(db, SETTING_WAKEWORD_COOLDOWN_MS)

        # Fall back to environment variables if not set in DB
        if keyword is None:
            keyword = settings.wake_word_default
        if threshold is None:
            threshold = settings.wake_word_threshold
        else:
            threshold = float(threshold)
        if cooldown_ms is None:
            cooldown_ms = settings.wake_word_cooldown_ms
        else:
            cooldown_ms = int(cooldown_ms)

        return WakeWordConfig(
            keyword=keyword,
            threshold=threshold,
            cooldown_ms=cooldown_ms,
            enabled=settings.wake_word_enabled,
        )

    async def update_config(
        self,
        db: AsyncSession,
        keyword: Optional[str] = None,
        threshold: Optional[float] = None,
        cooldown_ms: Optional[int] = None,
        updated_by: Optional[int] = None,
    ) -> WakeWordConfig:
        """
        Update wake word configuration in database and broadcast to all subscribers.

        Args:
            db: Database session
            keyword: New wake word keyword (must be in VALID_KEYWORDS)
            threshold: New threshold (0.1 - 1.0)
            cooldown_ms: New cooldown in milliseconds (500 - 10000)
            updated_by: User ID who made the change

        Returns:
            Updated WakeWordConfig

        Raises:
            ValueError: If keyword is invalid or values out of range
        """
        # Validate keyword if provided
        if keyword is not None and keyword not in VALID_KEYWORDS:
            raise ValueError(f"Invalid keyword: {keyword}. Must be one of: {VALID_KEYWORDS}")

        # Validate threshold if provided
        if threshold is not None and not (0.1 <= threshold <= 1.0):
            raise ValueError(f"Invalid threshold: {threshold}. Must be between 0.1 and 1.0")

        # Validate cooldown if provided
        if cooldown_ms is not None and not (500 <= cooldown_ms <= 10000):
            raise ValueError(f"Invalid cooldown: {cooldown_ms}. Must be between 500 and 10000")

        # Update settings in database
        if keyword is not None:
            await self._set_setting(db, SETTING_WAKEWORD_KEYWORD, keyword, updated_by)
            logger.info(f"Wake word keyword updated to: {keyword}")

        if threshold is not None:
            await self._set_setting(db, SETTING_WAKEWORD_THRESHOLD, str(threshold), updated_by)
            logger.info(f"Wake word threshold updated to: {threshold}")

        if cooldown_ms is not None:
            await self._set_setting(db, SETTING_WAKEWORD_COOLDOWN_MS, str(cooldown_ms), updated_by)
            logger.info(f"Wake word cooldown updated to: {cooldown_ms}ms")

        await db.commit()

        # Get updated config
        config = await self.get_config(db)

        # Broadcast to all subscribers
        await self.broadcast_config(config)

        return config

    async def _get_setting(self, db: AsyncSession, key: str) -> Optional[str]:
        """Get a single setting value from database"""
        result = await db.execute(
            select(SystemSetting).where(SystemSetting.key == key)
        )
        setting = result.scalar_one_or_none()
        return setting.value if setting else None

    async def _set_setting(
        self,
        db: AsyncSession,
        key: str,
        value: str,
        updated_by: Optional[int] = None
    ):
        """Set a single setting value in database (upsert)"""
        result = await db.execute(
            select(SystemSetting).where(SystemSetting.key == key)
        )
        setting = result.scalar_one_or_none()

        if setting:
            setting.value = value
            setting.updated_by = updated_by
        else:
            setting = SystemSetting(
                key=key,
                value=value,
                updated_by=updated_by
            )
            db.add(setting)

    def subscribe(
        self,
        websocket: WebSocket,
        device_id: Optional[str] = None,
        device_type: Optional[str] = None
    ):
        """
        Register a WebSocket connection to receive config updates.

        Args:
            websocket: WebSocket connection to add
            device_id: Optional device identifier (e.g., "satellite-living-room")
            device_type: Optional device type ("satellite" or "web_device")
        """
        if websocket not in self._subscribers:
            self._subscribers.append(websocket)
            if device_id:
                self._subscriber_info[websocket] = {
                    "device_id": device_id,
                    "device_type": device_type or "unknown"
                }
                # Initialize pending sync status for this device
                if device_id not in self._device_sync_status:
                    self._device_sync_status[device_id] = DeviceSyncStatus(
                        device_id=device_id,
                        device_type=device_type or "unknown",
                        synced=False
                    )
            logger.debug(f"WebSocket subscribed to wake word config (total: {len(self._subscribers)}, device: {device_id})")

    def unsubscribe(self, websocket: WebSocket):
        """
        Remove a WebSocket connection from subscribers.

        Args:
            websocket: WebSocket connection to remove
        """
        if websocket in self._subscribers:
            self._subscribers.remove(websocket)
            # Clean up subscriber info but keep sync status for history
            if websocket in self._subscriber_info:
                del self._subscriber_info[websocket]
            logger.debug(f"WebSocket unsubscribed from wake word config (total: {len(self._subscribers)})")

    async def broadcast_config(self, config: WakeWordConfig):
        """
        Broadcast configuration update to all subscribers.

        Args:
            config: Current wake word configuration
        """
        if not self._subscribers:
            logger.debug("No subscribers to broadcast config update to")
            return

        # Increment config version for tracking acknowledgments
        self._pending_config_version += 1

        # Mark all devices as pending sync
        for device_id in self._device_sync_status:
            self._device_sync_status[device_id].synced = False

        message = {
            "type": "config_update",
            "config": config.to_satellite_config(),
            "config_version": self._pending_config_version,
        }

        # Send to all subscribers, removing any that fail
        failed_subscribers = []
        for ws in self._subscribers:
            try:
                await ws.send_json(message)
            except Exception as e:
                logger.warning(f"Failed to send config update to subscriber: {e}")
                failed_subscribers.append(ws)

        # Remove failed subscribers
        for ws in failed_subscribers:
            self._subscribers.remove(ws)
            if ws in self._subscriber_info:
                del self._subscriber_info[ws]

        if self._subscribers:
            logger.info(f"Broadcast config update v{self._pending_config_version} to {len(self._subscribers)} subscribers")

    def handle_config_ack(
        self,
        device_id: str,
        success: bool,
        active_keywords: Optional[List[str]] = None,
        failed_keywords: Optional[List[str]] = None,
        error: Optional[str] = None,
    ) -> DeviceSyncStatus:
        """
        Handle config acknowledgment from a device.

        Called when a device (satellite or web) sends a config_ack message
        after receiving and applying (or failing to apply) a config_update.

        Args:
            device_id: The device identifier
            success: Whether the device successfully applied the config
            active_keywords: List of keywords the device has active
            failed_keywords: List of keywords the device could not load
            error: Error message if something went wrong

        Returns:
            Updated DeviceSyncStatus for the device
        """
        if device_id not in self._device_sync_status:
            # Create new status entry for unknown device
            self._device_sync_status[device_id] = DeviceSyncStatus(
                device_id=device_id,
                device_type="unknown"
            )

        status = self._device_sync_status[device_id]
        status.synced = success
        status.active_keywords = active_keywords or []
        status.failed_keywords = failed_keywords or []
        status.last_ack_time = datetime.utcnow()
        status.error = error

        if success:
            logger.info(f"✅ Device {device_id} synced: active keywords = {active_keywords}")
        else:
            logger.warning(f"⚠️ Device {device_id} sync failed: {error or 'unknown error'}, failed keywords = {failed_keywords}")

        return status

    def get_subscriber_count(self) -> int:
        """Get number of active subscribers"""
        return len(self._subscribers)

    def get_available_keywords(self) -> List[Dict[str, Any]]:
        """Get list of available wake word keywords"""
        return AVAILABLE_KEYWORDS.copy()

    def get_device_sync_status(self, device_id: Optional[str] = None) -> Dict[str, Any]:
        """
        Get sync status for devices.

        Args:
            device_id: Optional specific device ID to check

        Returns:
            Dict with sync status information:
            - config_version: Current config version
            - devices: List of DeviceSyncStatus dicts
            - all_synced: True if all devices are synced
            - synced_count: Number of synced devices
            - pending_count: Number of pending devices
        """
        if device_id:
            status = self._device_sync_status.get(device_id)
            if status:
                return status.to_dict()
            return {"error": f"Device {device_id} not found"}

        devices = [s.to_dict() for s in self._device_sync_status.values()]
        synced_count = sum(1 for s in self._device_sync_status.values() if s.synced)
        pending_count = len(self._device_sync_status) - synced_count

        return {
            "config_version": self._pending_config_version,
            "devices": devices,
            "all_synced": pending_count == 0 and len(self._device_sync_status) > 0,
            "synced_count": synced_count,
            "pending_count": pending_count,
        }

    def get_device_by_websocket(self, websocket: WebSocket) -> Optional[str]:
        """Get device ID for a websocket connection"""
        info = self._subscriber_info.get(websocket)
        return info.get("device_id") if info else None


# Global singleton instance
_wakeword_config_manager: Optional[WakeWordConfigManager] = None


def get_wakeword_config_manager() -> WakeWordConfigManager:
    """Get or create the global WakeWordConfigManager instance"""
    global _wakeword_config_manager
    if _wakeword_config_manager is None:
        _wakeword_config_manager = WakeWordConfigManager()
    return _wakeword_config_manager
