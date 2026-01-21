"""
Room Service - Business Logic for Room Management

Handles:
- Room CRUD operations
- Room name normalization for voice commands
- Home Assistant Area synchronization
- Device-Room assignment (satellites, web clients, tablets)
"""

import re
import unicodedata
from datetime import datetime
from typing import List, Optional, Dict, Any
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from loguru import logger

from models.database import (
    Room, RoomDevice,
    DEVICE_TYPE_SATELLITE, DEVICE_TYPE_WEB_BROWSER, DEVICE_TYPE_WEB_PANEL,
    DEVICE_TYPE_WEB_TABLET, DEVICE_TYPE_WEB_KIOSK, DEVICE_TYPES,
    DEFAULT_CAPABILITIES
)


def normalize_room_name(name: str) -> str:
    """
    Normalize room name for voice command matching.

    - Lowercase
    - Remove umlauts (ä → ae, ö → oe, ü → ue, ß → ss)
    - Remove special characters
    - Replace spaces with nothing

    Example: "Wohnzimmer" → "wohnzimmer"
             "Gästezimmer" → "gaestezimmer"
    """
    if not name:
        return ""

    # Lowercase
    result = name.lower().strip()

    # German umlaut replacements
    umlaut_map = {
        'ä': 'ae',
        'ö': 'oe',
        'ü': 'ue',
        'ß': 'ss',
        'Ä': 'ae',
        'Ö': 'oe',
        'Ü': 'ue',
    }

    for umlaut, replacement in umlaut_map.items():
        result = result.replace(umlaut, replacement)

    # Remove accents from other characters
    result = unicodedata.normalize('NFKD', result)
    result = ''.join(c for c in result if not unicodedata.combining(c))

    # Keep only alphanumeric characters
    result = re.sub(r'[^a-z0-9]', '', result)

    return result


def generate_device_id(device_type: str, room_name: str, suffix: str = None) -> str:
    """
    Generate a unique device ID.

    Format: {type_prefix}-{room_alias}-{suffix}
    Example: "web-wohnzimmer-ipad1", "sat-kueche-main"
    """
    type_prefixes = {
        DEVICE_TYPE_SATELLITE: "sat",
        DEVICE_TYPE_WEB_PANEL: "panel",
        DEVICE_TYPE_WEB_TABLET: "tablet",
        DEVICE_TYPE_WEB_BROWSER: "web",
        DEVICE_TYPE_WEB_KIOSK: "kiosk",
    }

    prefix = type_prefixes.get(device_type, "dev")
    room_alias = normalize_room_name(room_name)[:20]  # Truncate long room names

    if suffix:
        return f"{prefix}-{room_alias}-{suffix}"
    else:
        import uuid
        return f"{prefix}-{room_alias}-{uuid.uuid4().hex[:6]}"


class RoomService:
    """Service for Room Management operations"""

    def __init__(self, db: AsyncSession):
        self.db = db

    # --- CRUD Operations ---

    async def create_room(
        self,
        name: str,
        source: str = "renfield",
        ha_area_id: Optional[str] = None,
        icon: Optional[str] = None
    ) -> Room:
        """
        Create a new room.

        Args:
            name: Display name for the room
            source: Origin of room (renfield, homeassistant, satellite, device)
            ha_area_id: Optional Home Assistant area ID to link
            icon: Optional Material Design icon (e.g., "mdi:sofa")

        Returns:
            Created Room object
        """
        alias = normalize_room_name(name)

        room = Room(
            name=name,
            alias=alias,
            source=source,
            ha_area_id=ha_area_id,
            icon=icon
        )

        self.db.add(room)
        await self.db.commit()

        logger.info(f"Created room: {name} (alias: {alias}, source: {source})")
        # Re-fetch with eager loading to avoid lazy load issues
        return await self.get_room(room.id)

    async def get_room(self, room_id: int) -> Optional[Room]:
        """Get room by ID with devices loaded"""
        result = await self.db.execute(
            select(Room)
            .options(selectinload(Room.devices))
            .where(Room.id == room_id)
        )
        return result.scalar_one_or_none()

    async def get_room_by_name(self, name: str) -> Optional[Room]:
        """Get room by exact name"""
        result = await self.db.execute(
            select(Room)
            .options(selectinload(Room.devices))
            .where(Room.name == name)
        )
        return result.scalar_one_or_none()

    async def get_room_by_alias(self, alias: str) -> Optional[Room]:
        """Get room by normalized alias (for voice commands)"""
        normalized = normalize_room_name(alias)
        result = await self.db.execute(
            select(Room)
            .options(selectinload(Room.devices))
            .where(Room.alias == normalized)
        )
        return result.scalar_one_or_none()

    async def get_room_by_ha_area_id(self, ha_area_id: str) -> Optional[Room]:
        """Get room by Home Assistant area ID"""
        result = await self.db.execute(
            select(Room)
            .options(selectinload(Room.devices))
            .where(Room.ha_area_id == ha_area_id)
        )
        return result.scalar_one_or_none()

    async def get_all_rooms(self) -> List[Room]:
        """Get all rooms with devices loaded"""
        result = await self.db.execute(
            select(Room)
            .options(selectinload(Room.devices))
            .order_by(Room.name)
        )
        return list(result.scalars().all())

    async def update_room(
        self,
        room_id: int,
        name: Optional[str] = None,
        icon: Optional[str] = None,
        ha_area_id: Optional[str] = None
    ) -> Optional[Room]:
        """
        Update room details.

        Args:
            room_id: Room ID to update
            name: New display name (alias will be recalculated)
            icon: New icon
            ha_area_id: New HA area ID (pass empty string to unlink)

        Returns:
            Updated Room or None if not found
        """
        room = await self.get_room(room_id)
        if not room:
            return None

        if name is not None:
            room.name = name
            room.alias = normalize_room_name(name)

        if icon is not None:
            room.icon = icon if icon else None

        if ha_area_id is not None:
            room.ha_area_id = ha_area_id if ha_area_id else None

        room.updated_at = datetime.utcnow()
        await self.db.commit()

        logger.info(f"Updated room {room_id}: {room.name}")
        # Re-fetch with eager loading to avoid lazy load issues
        return await self.get_room(room_id)

    async def delete_room(self, room_id: int) -> bool:
        """
        Delete a room and all associated device assignments.

        Args:
            room_id: Room ID to delete

        Returns:
            True if deleted, False if not found
        """
        room = await self.get_room(room_id)
        if not room:
            return False

        name = room.name
        await self.db.delete(room)
        await self.db.commit()

        logger.info(f"Deleted room: {name}")
        return True

    # --- Home Assistant Sync Operations ---

    async def link_to_ha_area(self, room_id: int, ha_area_id: str) -> Optional[Room]:
        """Link a room to a Home Assistant area"""
        room = await self.get_room(room_id)
        if not room:
            return None

        room.ha_area_id = ha_area_id
        room.last_synced_at = datetime.utcnow()
        await self.db.commit()

        logger.info(f"Linked room {room.name} to HA area {ha_area_id}")
        # Re-fetch with eager loading to avoid lazy load issues
        return await self.get_room(room_id)

    async def unlink_from_ha(self, room_id: int) -> Optional[Room]:
        """Remove Home Assistant area link from room"""
        room = await self.get_room(room_id)
        if not room:
            return None

        room.ha_area_id = None
        room.last_synced_at = None
        await self.db.commit()

        logger.info(f"Unlinked room {room.name} from HA")
        # Re-fetch with eager loading to avoid lazy load issues
        return await self.get_room(room_id)

    async def import_ha_areas(
        self,
        ha_areas: List[Dict[str, Any]],
        conflict_resolution: str = "skip"
    ) -> Dict[str, Any]:
        """
        Import areas from Home Assistant.

        Args:
            ha_areas: List of HA areas [{"area_id": str, "name": str, "icon": str}, ...]
            conflict_resolution: How to handle existing rooms
                - "skip": Skip if room with same name exists
                - "link": Link existing room with same name to HA area
                - "overwrite": Update existing room's HA link

        Returns:
            Summary of import results
        """
        results = {
            "imported": 0,
            "linked": 0,
            "skipped": 0,
            "errors": []
        }

        for area in ha_areas:
            area_id = area.get("area_id")
            area_name = area.get("name")
            area_icon = area.get("icon")

            if not area_id or not area_name:
                results["errors"].append(f"Invalid area data: {area}")
                continue

            try:
                # Check if already linked
                existing_linked = await self.get_room_by_ha_area_id(area_id)
                if existing_linked:
                    # Update name if changed
                    if existing_linked.name != area_name:
                        existing_linked.name = area_name
                        existing_linked.alias = normalize_room_name(area_name)
                        existing_linked.last_synced_at = datetime.utcnow()
                        await self.db.commit()
                    results["skipped"] += 1
                    continue

                # Check for name match
                existing_by_name = await self.get_room_by_name(area_name)

                if existing_by_name:
                    if conflict_resolution == "skip":
                        results["skipped"] += 1
                        continue
                    elif conflict_resolution in ["link", "overwrite"]:
                        existing_by_name.ha_area_id = area_id
                        existing_by_name.last_synced_at = datetime.utcnow()
                        if area_icon:
                            existing_by_name.icon = area_icon
                        await self.db.commit()
                        results["linked"] += 1
                        continue

                # Create new room
                await self.create_room(
                    name=area_name,
                    source="homeassistant",
                    ha_area_id=area_id,
                    icon=area_icon
                )
                results["imported"] += 1

            except Exception as e:
                logger.error(f"Error importing area {area_name}: {e}")
                results["errors"].append(f"{area_name}: {str(e)}")

        logger.info(f"HA Import complete: {results}")
        return results

    async def get_rooms_for_export(self) -> List[Room]:
        """Get rooms that can be exported to Home Assistant (no HA link yet)"""
        result = await self.db.execute(
            select(Room)
            .where(Room.ha_area_id.is_(None))
            .order_by(Room.name)
        )
        return list(result.scalars().all())

    # --- Device Operations ---

    async def register_device(
        self,
        device_id: str,
        room_name: str,
        device_type: str = DEVICE_TYPE_WEB_BROWSER,
        device_name: Optional[str] = None,
        capabilities: Optional[Dict[str, Any]] = None,
        is_stationary: bool = True,
        user_agent: Optional[str] = None,
        ip_address: Optional[str] = None,
        auto_create_room: bool = True
    ) -> Optional[RoomDevice]:
        """
        Register a device to a room.

        Creates the room if it doesn't exist (when auto_create_room=True).
        Updates existing device if device_id already exists.

        Args:
            device_id: Unique device identifier
            room_name: Room name to assign device to
            device_type: Type of device (satellite, web_panel, web_tablet, etc.)
            device_name: User-friendly device name
            capabilities: Device capabilities dict (merged with defaults)
            is_stationary: Whether device is stationary
            user_agent: Browser/client user agent string
            ip_address: Client IP address
            auto_create_room: Create room if it doesn't exist

        Returns:
            RoomDevice record or None if room not found and auto_create=False
        """
        # Validate device type
        if device_type not in DEVICE_TYPES:
            logger.warning(f"Unknown device type: {device_type}, defaulting to web_browser")
            device_type = DEVICE_TYPE_WEB_BROWSER

        # Get or create room
        room = await self.get_room_by_name(room_name)
        if not room:
            if auto_create_room:
                source = "satellite" if device_type == DEVICE_TYPE_SATELLITE else "device"
                room = await self.create_room(name=room_name, source=source)
            else:
                return None

        # Build capabilities (merge defaults with provided)
        default_caps = DEFAULT_CAPABILITIES.get(device_type, {}).copy()
        if capabilities:
            default_caps.update(capabilities)

        # Check if device already exists
        result = await self.db.execute(
            select(RoomDevice).where(RoomDevice.device_id == device_id)
        )
        existing = result.scalar_one_or_none()

        if existing:
            # Update existing device
            existing.room_id = room.id
            existing.device_type = device_type
            existing.device_name = device_name or existing.device_name
            existing.capabilities = default_caps
            existing.is_stationary = is_stationary
            existing.is_online = True
            existing.last_connected_at = datetime.utcnow()
            existing.user_agent = user_agent
            existing.ip_address = ip_address
            existing.updated_at = datetime.utcnow()

            await self.db.commit()
            await self.db.refresh(existing)

            logger.info(f"Updated device {device_id} in room {room.name}")
            return existing

        # Create new device
        device = RoomDevice(
            room_id=room.id,
            device_id=device_id,
            device_type=device_type,
            device_name=device_name,
            capabilities=default_caps,
            is_stationary=is_stationary,
            is_online=True,
            last_connected_at=datetime.utcnow(),
            user_agent=user_agent,
            ip_address=ip_address
        )

        self.db.add(device)
        await self.db.commit()
        await self.db.refresh(device)

        logger.info(f"Registered device {device_id} ({device_type}) to room {room.name}")
        return device

    async def get_device(self, device_id: str) -> Optional[RoomDevice]:
        """Get device by ID with room loaded"""
        result = await self.db.execute(
            select(RoomDevice)
            .options(selectinload(RoomDevice.room))
            .where(RoomDevice.device_id == device_id)
        )
        return result.scalar_one_or_none()

    async def get_device_by_id(self, db_id: int) -> Optional[RoomDevice]:
        """Get device by database ID"""
        result = await self.db.execute(
            select(RoomDevice)
            .options(selectinload(RoomDevice.room))
            .where(RoomDevice.id == db_id)
        )
        return result.scalar_one_or_none()

    async def get_devices_in_room(self, room_id: int) -> List[RoomDevice]:
        """Get all devices in a room"""
        result = await self.db.execute(
            select(RoomDevice)
            .where(RoomDevice.room_id == room_id)
            .order_by(RoomDevice.device_name)
        )
        return list(result.scalars().all())

    async def get_online_devices_in_room(self, room_id: int) -> List[RoomDevice]:
        """Get online devices in a room"""
        result = await self.db.execute(
            select(RoomDevice)
            .where(RoomDevice.room_id == room_id, RoomDevice.is_online == True)
        )
        return list(result.scalars().all())

    async def get_all_devices(self) -> List[RoomDevice]:
        """Get all devices with rooms loaded"""
        result = await self.db.execute(
            select(RoomDevice)
            .options(selectinload(RoomDevice.room))
            .order_by(RoomDevice.device_id)
        )
        return list(result.scalars().all())

    async def set_device_online(self, device_id: str, is_online: bool, ip_address: Optional[str] = None):
        """Update device online status and optionally IP address"""
        values = {
            "is_online": is_online,
            "updated_at": datetime.utcnow()
        }
        if is_online:
            values["last_connected_at"] = datetime.utcnow()
            # Update IP address if provided (Option 1: update on every connection)
            if ip_address:
                values["ip_address"] = ip_address

        await self.db.execute(
            update(RoomDevice)
            .where(RoomDevice.device_id == device_id)
            .values(**values)
        )
        await self.db.commit()

    async def update_device_ip(self, device_id: str, ip_address: str) -> Optional[RoomDevice]:
        """
        Update device IP address.

        Called on every connection to ensure IP is current.
        Logs a warning if a stationary device's IP changes.
        """
        device = await self.get_device(device_id)
        if not device:
            return None

        old_ip = device.ip_address
        if old_ip and old_ip != ip_address and device.is_stationary:
            logger.warning(
                f"Stationary device {device_id} IP changed: {old_ip} → {ip_address}"
            )

        device.ip_address = ip_address
        device.updated_at = datetime.utcnow()

        await self.db.commit()
        await self.db.refresh(device)

        return device

    async def get_stationary_device_by_ip(self, ip_address: str) -> Optional[RoomDevice]:
        """
        Find a stationary device by IP address.

        Used for automatic room detection: when a client connects,
        check if their IP matches a known stationary device to
        automatically determine the room context.

        Args:
            ip_address: Client IP address

        Returns:
            RoomDevice if found (stationary only), None otherwise
        """
        result = await self.db.execute(
            select(RoomDevice)
            .options(selectinload(RoomDevice.room))
            .where(
                RoomDevice.ip_address == ip_address,
                RoomDevice.is_stationary == True
            )
        )
        return result.scalar_one_or_none()

    async def get_room_context_by_ip(self, ip_address: str) -> Optional[Dict[str, Any]]:
        """
        Get room context for automatic room detection.

        Args:
            ip_address: Client IP address

        Returns:
            Room context dict if found, None otherwise
        """
        device = await self.get_stationary_device_by_ip(ip_address)
        if not device or not device.room:
            return None

        return {
            "room_name": device.room.name,
            "room_id": device.room.id,
            "device_id": device.device_id,
            "device_type": device.device_type,
            "device_name": device.device_name,
            "auto_detected": True,  # Flag to indicate this was auto-detected
        }

    async def update_device_capabilities(
        self,
        device_id: str,
        capabilities: Dict[str, Any]
    ) -> Optional[RoomDevice]:
        """Update device capabilities"""
        device = await self.get_device(device_id)
        if not device:
            return None

        # Merge with existing capabilities
        current_caps = device.capabilities or {}
        current_caps.update(capabilities)
        device.capabilities = current_caps
        device.updated_at = datetime.utcnow()

        await self.db.commit()
        await self.db.refresh(device)

        return device

    async def unregister_device(self, device_id: str) -> bool:
        """Remove a device"""
        result = await self.db.execute(
            select(RoomDevice).where(RoomDevice.device_id == device_id)
        )
        device = result.scalar_one_or_none()

        if device:
            await self.db.delete(device)
            await self.db.commit()
            logger.info(f"Unregistered device {device_id}")
            return True
        return False

    async def delete_device(self, device_id: str) -> bool:
        """Delete a device (alias for unregister_device)"""
        return await self.unregister_device(device_id)

    async def move_device_to_room(self, device_id: str, room_id: int) -> Optional[RoomDevice]:
        """
        Move a device to a different room.

        Args:
            device_id: Device ID to move
            room_id: Target room ID

        Returns:
            Updated device or None if device/room not found
        """
        device = await self.get_device(device_id)
        if not device:
            return None

        room = await self.get_room(room_id)
        if not room:
            return None

        device.room_id = room_id
        device.updated_at = datetime.utcnow()

        await self.db.commit()
        await self.db.refresh(device)

        logger.info(f"Moved device {device_id} to room {room.name}")
        return device

    # --- Legacy Compatibility Methods ---
    # These methods maintain backward compatibility with the old RoomSatellite API

    async def assign_satellite(
        self,
        room_id: int,
        satellite_id: str
    ) -> Optional[RoomDevice]:
        """Legacy method: Assign a satellite to a room."""
        room = await self.get_room(room_id)
        if not room:
            return None

        return await self.register_device(
            device_id=satellite_id,
            room_name=room.name,
            device_type=DEVICE_TYPE_SATELLITE,
            auto_create_room=False
        )

    async def get_or_create_room_for_satellite(
        self,
        satellite_id: str,
        room_name: str,
        auto_create: bool = True
    ) -> Optional[Room]:
        """Legacy method: Get or create room for a satellite registration."""
        device = await self.register_device(
            device_id=satellite_id,
            room_name=room_name,
            device_type=DEVICE_TYPE_SATELLITE,
            auto_create_room=auto_create
        )

        if device:
            return await self.get_room(device.room_id)
        return None

    async def set_satellite_online(self, satellite_id: str, is_online: bool):
        """Legacy method: Update satellite online status"""
        await self.set_device_online(satellite_id, is_online)

    async def get_satellite_assignment(self, satellite_id: str) -> Optional[RoomDevice]:
        """Legacy method: Get satellite assignment with room loaded"""
        return await self.get_device(satellite_id)

    async def unassign_satellite(self, satellite_id: str) -> bool:
        """Legacy method: Remove satellite from its room"""
        return await self.unregister_device(satellite_id)

    # --- Helper Methods ---

    def room_to_dict(self, room: Room) -> Dict[str, Any]:
        """Convert Room to dictionary for API responses"""
        devices = room.devices if room.devices else []

        return {
            "id": room.id,
            "name": room.name,
            "alias": room.alias,
            "ha_area_id": room.ha_area_id,
            "source": room.source,
            "icon": room.icon,
            "created_at": room.created_at.isoformat() if room.created_at else None,
            "updated_at": room.updated_at.isoformat() if room.updated_at else None,
            "last_synced_at": room.last_synced_at.isoformat() if room.last_synced_at else None,
            "device_count": len(devices),
            "satellite_count": len([d for d in devices if d.device_type == DEVICE_TYPE_SATELLITE]),
            "online_count": len([d for d in devices if d.is_online]),
            "devices": [self.device_to_dict(d) for d in devices],
            # Legacy compatibility
            "satellites": [
                {
                    "satellite_id": d.device_id,
                    "is_online": d.is_online,
                    "last_connected_at": d.last_connected_at.isoformat() if d.last_connected_at else None
                }
                for d in devices if d.device_type == DEVICE_TYPE_SATELLITE
            ]
        }

    def device_to_dict(self, device: RoomDevice) -> Dict[str, Any]:
        """Convert RoomDevice to dictionary for API responses"""
        return {
            "id": device.id,
            "device_id": device.device_id,
            "device_type": device.device_type,
            "device_name": device.device_name,
            "room_id": device.room_id,
            "room_name": device.room.name if device.room else None,
            "capabilities": device.capabilities,
            "is_online": device.is_online,
            "is_stationary": device.is_stationary,
            "last_connected_at": device.last_connected_at.isoformat() if device.last_connected_at else None,
            "created_at": device.created_at.isoformat() if device.created_at else None,
            "updated_at": device.updated_at.isoformat() if device.updated_at else None,
        }
