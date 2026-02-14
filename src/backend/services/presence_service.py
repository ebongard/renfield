"""
Presence Service — BLE-based room-level presence detection.

In-memory state management for tracking which users are in which rooms,
based on BLE scan reports from satellites. Aggregates RSSI from multiple
satellites for robust room assignment with hysteresis to prevent room flicker.
"""

import time
from dataclasses import dataclass

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from utils.config import settings


@dataclass
class DeviceSighting:
    """A single BLE scan result from a satellite."""
    satellite_id: str
    room_id: int | None
    rssi: int
    timestamp: float


@dataclass
class UserPresence:
    """Current presence state of a user."""
    user_id: int
    room_id: int | None = None
    room_name: str | None = None
    satellite_id: str | None = None
    confidence: float = 0.0
    last_seen: float = 0.0
    consecutive_room_count: int = 0  # for hysteresis


class PresenceService:
    """
    In-memory presence tracking service.

    Processes BLE scan reports from satellites and maintains a map of
    user_id → current room. Uses "strongest RSSI wins" with hysteresis
    to avoid room flicker.
    """

    def __init__(self):
        self._mac_to_user: dict[str, int] = {}          # MAC → user_id cache
        self._presence: dict[int, UserPresence] = {}     # user_id → presence
        self._sightings: dict[str, list[DeviceSighting]] = {}  # MAC → recent sightings
        self._hysteresis_threshold: int = settings.presence_hysteresis_scans
        self._stale_timeout: float = float(settings.presence_stale_timeout)
        self._rssi_threshold: int = settings.presence_rssi_threshold
        self._room_names: dict[int, str] = {}            # room_id → name cache
        self._user_names: dict[int, str] = {}            # user_id → username

    async def load_device_registry(self, db: AsyncSession):
        """Load UserBleDevice table into MAC → user_id cache."""
        from models.database import User, UserBleDevice

        result = await db.execute(
            select(UserBleDevice).where(UserBleDevice.is_enabled == True)  # noqa: E712
        )
        devices = result.scalars().all()

        self._mac_to_user = {
            d.mac_address.upper(): d.user_id for d in devices
        }

        # Cache user names for frontend display
        user_result = await db.execute(select(User))
        self._user_names = {u.id: u.username for u in user_result.scalars().all()}

        logger.info(f"Presence: loaded {len(self._mac_to_user)} BLE devices")

    def set_room_name(self, room_id: int, name: str):
        """Cache a room name for display."""
        self._room_names[room_id] = name

    def get_user_name(self, user_id: int) -> str | None:
        """Get cached username for a user_id."""
        return self._user_names.get(user_id)

    def process_ble_report(
        self,
        satellite_id: str,
        room_id: int | None,
        devices: list[dict],
        room_name: str | None = None,
    ):
        """
        Process a BLE scan report from a satellite.

        Args:
            satellite_id: ID of the reporting satellite
            room_id: Room where the satellite is located
            devices: List of {mac, rssi} dicts
            room_name: Optional room name for display
        """
        if room_name and room_id:
            self._room_names[room_id] = room_name

        now = time.time()

        for device in devices:
            mac = device.get("mac", "").upper()
            rssi = device.get("rssi", -100)

            # Only track known devices
            if mac not in self._mac_to_user:
                continue

            sighting = DeviceSighting(
                satellite_id=satellite_id,
                room_id=room_id,
                rssi=rssi,
                timestamp=now,
            )

            # Keep only recent sightings (last 2 minutes)
            if mac not in self._sightings:
                self._sightings[mac] = []
            self._sightings[mac] = [
                s for s in self._sightings[mac]
                if now - s.timestamp < self._stale_timeout
            ]
            self._sightings[mac].append(sighting)

            self._assign_room(mac)

        # Clean up stale presence
        self._cleanup_stale(now)

    def _assign_room(self, mac: str):
        """Assign a user to a room based on multi-satellite RSSI aggregation with hysteresis."""
        user_id = self._mac_to_user.get(mac)
        if user_id is None:
            return

        sightings = self._sightings.get(mac, [])
        if not sightings:
            return

        # Filter by RSSI threshold
        valid = [s for s in sightings if s.rssi >= self._rssi_threshold]
        if not valid:
            return

        # Group by room — keep latest sighting per (room, satellite) pair
        latest_per_key: dict[tuple[int, str], DeviceSighting] = {}
        for s in valid:
            if s.room_id is None:
                continue
            key = (s.room_id, s.satellite_id)
            if key not in latest_per_key or s.timestamp > latest_per_key[key].timestamp:
                latest_per_key[key] = s

        # Collect RSSI values per room
        room_rssi: dict[int, list[int]] = {}
        for (room_id, _), s in latest_per_key.items():
            room_rssi.setdefault(room_id, []).append(s.rssi)

        if not room_rssi:
            return

        # Score each room: mean RSSI + multi-satellite bonus (5 dBm per extra satellite)
        best_room_id = None
        best_score = float("-inf")
        best_rssi_values: list[int] = []

        for room_id, rssi_values in room_rssi.items():
            mean_rssi = sum(rssi_values) / len(rssi_values)
            score = mean_rssi + 5 * (len(rssi_values) - 1)
            if score > best_score:
                best_score = score
                best_room_id = room_id
                best_rssi_values = rssi_values

        # Find strongest satellite_id in the winning room
        best_satellite_id = None
        best_sat_rssi = float("-inf")
        best_timestamp = 0.0
        for (room_id, sat_id), s in latest_per_key.items():
            if room_id == best_room_id:
                if s.rssi > best_sat_rssi:
                    best_sat_rssi = s.rssi
                    best_satellite_id = sat_id
                if s.timestamp > best_timestamp:
                    best_timestamp = s.timestamp

        current = self._presence.get(user_id)
        if current is None:
            current = UserPresence(user_id=user_id)
            self._presence[user_id] = current

        current.last_seen = best_timestamp

        # Confidence: RSSI component (70%) + satellite count component (30%)
        mean_rssi = sum(best_rssi_values) / len(best_rssi_values)
        rssi_conf = max(0.0, min(1.0, (mean_rssi + 90) / 60.0))
        sat_factor = min(1.0, len(best_rssi_values) / 3.0)
        confidence = rssi_conf * 0.7 + sat_factor * 0.3

        current.confidence = confidence

        if best_room_id == current.room_id:
            # Same room — reinforce
            current.consecutive_room_count += 1
            current.satellite_id = best_satellite_id
        else:
            # Different room — apply hysteresis
            current.consecutive_room_count += 1
            if current.room_id is None or current.consecutive_room_count >= self._hysteresis_threshold:
                old_room = current.room_name or current.room_id
                current.room_id = best_room_id
                current.room_name = self._room_names.get(best_room_id) if best_room_id else None
                current.satellite_id = best_satellite_id
                current.consecutive_room_count = 1
                new_room = current.room_name or current.room_id
                logger.debug(f"Presence: user {user_id} moved {old_room} → {new_room}")
            # else: not enough consecutive scans, keep current room

    def _cleanup_stale(self, now: float):
        """Mark users as absent if not seen recently."""
        stale_users = []
        for user_id, presence in self._presence.items():
            if now - presence.last_seen > self._stale_timeout:
                stale_users.append(user_id)

        for user_id in stale_users:
            old = self._presence.pop(user_id)
            logger.debug(f"Presence: user {user_id} marked absent (was in {old.room_name or old.room_id})")

    def get_room_occupants(self, room_id: int) -> list[UserPresence]:
        """Get all users currently in a room."""
        return [
            p for p in self._presence.values()
            if p.room_id == room_id
        ]

    def get_user_presence(self, user_id: int) -> UserPresence | None:
        """Get presence info for a specific user."""
        return self._presence.get(user_id)

    def get_all_presence(self) -> dict[int, UserPresence]:
        """Get all current presence data."""
        return dict(self._presence)

    def is_user_alone_in_room(self, user_id: int) -> bool | None:
        """
        Check if user is the only person in their room.

        Returns:
            True if alone, False if others present, None if user not tracked.
        """
        presence = self._presence.get(user_id)
        if presence is None or presence.room_id is None:
            return None

        occupants = self.get_room_occupants(presence.room_id)
        return len(occupants) == 1

    def get_known_macs(self) -> set[str]:
        """Get all known MAC addresses for pushing to satellites."""
        return set(self._mac_to_user.keys())

    async def add_device(
        self,
        user_id: int,
        mac: str,
        name: str,
        device_type: str,
        db: AsyncSession,
    ):
        """Add a BLE device to the registry and DB."""
        from models.database import UserBleDevice

        mac = mac.upper()
        device = UserBleDevice(
            user_id=user_id,
            mac_address=mac,
            device_name=name,
            device_type=device_type,
        )
        db.add(device)
        await db.commit()
        await db.refresh(device)

        # Update cache
        self._mac_to_user[mac] = user_id
        logger.info(f"Presence: registered BLE device {mac} for user {user_id}")
        return device

    async def remove_device(self, device_id: int, db: AsyncSession):
        """Remove a BLE device from the registry and DB."""
        from models.database import UserBleDevice

        result = await db.execute(
            select(UserBleDevice).where(UserBleDevice.id == device_id)
        )
        device = result.scalar_one_or_none()
        if device:
            mac = device.mac_address.upper()
            self._mac_to_user.pop(mac, None)
            self._sightings.pop(mac, None)
            await db.delete(device)
            await db.commit()
            logger.info(f"Presence: removed BLE device {mac}")
            return True
        return False


# Singleton instance
_presence_service: PresenceService | None = None


def get_presence_service() -> PresenceService:
    """Get the singleton PresenceService instance."""
    global _presence_service
    if _presence_service is None:
        _presence_service = PresenceService()
    return _presence_service
