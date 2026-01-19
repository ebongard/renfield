"""
Room Service - Business Logic for Room Management

Handles:
- Room CRUD operations
- Room name normalization for voice commands
- Home Assistant Area synchronization
- Satellite-Room assignment
"""

import re
import unicodedata
from datetime import datetime
from typing import List, Optional, Dict, Any
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from loguru import logger

from models.database import Room, RoomSatellite


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
            source: Origin of room (renfield, homeassistant, satellite)
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
        await self.db.refresh(room)

        logger.info(f"Created room: {name} (alias: {alias}, source: {source})")
        return room

    async def get_room(self, room_id: int) -> Optional[Room]:
        """Get room by ID with satellites loaded"""
        result = await self.db.execute(
            select(Room)
            .options(selectinload(Room.satellites))
            .where(Room.id == room_id)
        )
        return result.scalar_one_or_none()

    async def get_room_by_name(self, name: str) -> Optional[Room]:
        """Get room by exact name"""
        result = await self.db.execute(
            select(Room)
            .options(selectinload(Room.satellites))
            .where(Room.name == name)
        )
        return result.scalar_one_or_none()

    async def get_room_by_alias(self, alias: str) -> Optional[Room]:
        """Get room by normalized alias (for voice commands)"""
        normalized = normalize_room_name(alias)
        result = await self.db.execute(
            select(Room)
            .options(selectinload(Room.satellites))
            .where(Room.alias == normalized)
        )
        return result.scalar_one_or_none()

    async def get_room_by_ha_area_id(self, ha_area_id: str) -> Optional[Room]:
        """Get room by Home Assistant area ID"""
        result = await self.db.execute(
            select(Room)
            .options(selectinload(Room.satellites))
            .where(Room.ha_area_id == ha_area_id)
        )
        return result.scalar_one_or_none()

    async def get_all_rooms(self) -> List[Room]:
        """Get all rooms with satellites loaded"""
        result = await self.db.execute(
            select(Room)
            .options(selectinload(Room.satellites))
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
        await self.db.refresh(room)

        logger.info(f"Updated room {room_id}: {room.name}")
        return room

    async def delete_room(self, room_id: int) -> bool:
        """
        Delete a room and all associated satellite assignments.

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
        await self.db.refresh(room)

        logger.info(f"Linked room {room.name} to HA area {ha_area_id}")
        return room

    async def unlink_from_ha(self, room_id: int) -> Optional[Room]:
        """Remove Home Assistant area link from room"""
        room = await self.get_room(room_id)
        if not room:
            return None

        room.ha_area_id = None
        room.last_synced_at = None
        await self.db.commit()
        await self.db.refresh(room)

        logger.info(f"Unlinked room {room.name} from HA")
        return room

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

    # --- Satellite Operations ---

    async def assign_satellite(
        self,
        room_id: int,
        satellite_id: str
    ) -> Optional[RoomSatellite]:
        """
        Assign a satellite to a room.

        If satellite is already assigned to another room, reassign it.

        Args:
            room_id: Room ID to assign to
            satellite_id: Satellite identifier

        Returns:
            RoomSatellite record or None if room not found
        """
        room = await self.get_room(room_id)
        if not room:
            return None

        # Check if satellite already assigned
        result = await self.db.execute(
            select(RoomSatellite).where(RoomSatellite.satellite_id == satellite_id)
        )
        existing = result.scalar_one_or_none()

        if existing:
            if existing.room_id == room_id:
                # Already assigned to this room
                return existing
            # Reassign to new room
            existing.room_id = room_id
            existing.last_connected_at = datetime.utcnow()
            await self.db.commit()
            await self.db.refresh(existing)
            logger.info(f"Reassigned satellite {satellite_id} to room {room.name}")
            return existing

        # Create new assignment
        assignment = RoomSatellite(
            room_id=room_id,
            satellite_id=satellite_id,
            last_connected_at=datetime.utcnow()
        )
        self.db.add(assignment)
        await self.db.commit()
        await self.db.refresh(assignment)

        logger.info(f"Assigned satellite {satellite_id} to room {room.name}")
        return assignment

    async def get_or_create_room_for_satellite(
        self,
        satellite_id: str,
        room_name: str,
        auto_create: bool = True
    ) -> Optional[Room]:
        """
        Get or create room for a satellite registration.

        Args:
            satellite_id: Satellite identifier
            room_name: Room name from satellite config
            auto_create: Whether to create room if not exists

        Returns:
            Room object or None if not found and auto_create=False
        """
        # First check if satellite already has an assignment
        result = await self.db.execute(
            select(RoomSatellite)
            .options(selectinload(RoomSatellite.room))
            .where(RoomSatellite.satellite_id == satellite_id)
        )
        existing_assignment = result.scalar_one_or_none()

        if existing_assignment and existing_assignment.room:
            # Update connection timestamp
            existing_assignment.is_online = True
            existing_assignment.last_connected_at = datetime.utcnow()
            await self.db.commit()
            return existing_assignment.room

        # Try to find room by name
        room = await self.get_room_by_name(room_name)

        if not room and auto_create:
            # Create new room from satellite
            room = await self.create_room(
                name=room_name,
                source="satellite"
            )

        if room:
            # Assign satellite to room
            await self.assign_satellite(room.id, satellite_id)

        return room

    async def set_satellite_online(self, satellite_id: str, is_online: bool):
        """Update satellite online status"""
        await self.db.execute(
            update(RoomSatellite)
            .where(RoomSatellite.satellite_id == satellite_id)
            .values(
                is_online=is_online,
                last_connected_at=datetime.utcnow() if is_online else RoomSatellite.last_connected_at
            )
        )
        await self.db.commit()

    async def get_satellite_assignment(self, satellite_id: str) -> Optional[RoomSatellite]:
        """Get satellite assignment with room loaded"""
        result = await self.db.execute(
            select(RoomSatellite)
            .options(selectinload(RoomSatellite.room))
            .where(RoomSatellite.satellite_id == satellite_id)
        )
        return result.scalar_one_or_none()

    async def unassign_satellite(self, satellite_id: str) -> bool:
        """Remove satellite from its room"""
        result = await self.db.execute(
            select(RoomSatellite).where(RoomSatellite.satellite_id == satellite_id)
        )
        assignment = result.scalar_one_or_none()

        if assignment:
            await self.db.delete(assignment)
            await self.db.commit()
            logger.info(f"Unassigned satellite {satellite_id}")
            return True
        return False

    # --- Helper Methods ---

    def room_to_dict(self, room: Room) -> Dict[str, Any]:
        """Convert Room to dictionary for API responses"""
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
            "satellite_count": len(room.satellites) if room.satellites else 0,
            "satellites": [
                {
                    "satellite_id": sat.satellite_id,
                    "is_online": sat.is_online,
                    "last_connected_at": sat.last_connected_at.isoformat() if sat.last_connected_at else None
                }
                for sat in (room.satellites or [])
            ]
        }
