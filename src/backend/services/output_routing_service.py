"""
Output Routing Service for Renfield

Handles intelligent routing of TTS audio output to the best available device
in a room, based on priority configuration and device availability.

Routing Algorithm:
1. Get all configured output devices for room/type, sorted by priority
2. For each device (by priority):
   a. Check availability via HA API / DeviceManager
   b. If available → use it
   c. If busy AND allow_interruption=True → use it
   d. If busy AND allow_interruption=False → try next
   e. If off/unreachable → try next
3. If no configured device available → fallback to input device
4. If nothing available → return None
"""

from dataclasses import dataclass
from enum import Enum

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from integrations.homeassistant import HomeAssistantClient
from models.database import OUTPUT_TYPE_AUDIO, OUTPUT_TYPE_VISUAL, RoomDevice, RoomOutputDevice


class DeviceAvailability(str, Enum):
    """Device availability status"""
    AVAILABLE = "available"      # Ready (idle, paused, standby)
    BUSY = "busy"                # Playing (playing, buffering)
    OFF = "off"                  # Turned off
    UNAVAILABLE = "unavailable"  # Not reachable


@dataclass
class OutputDecision:
    """Result of output routing decision"""
    output_device: RoomOutputDevice | None
    target_id: str
    target_type: str  # "renfield" or "homeassistant"
    availability: DeviceAvailability
    fallback_to_input: bool
    reason: str


class OutputRoutingService:
    """
    Service for routing TTS/visual output to the best available device.
    """

    def __init__(self, db_session: AsyncSession):
        self.db = db_session
        self.ha_client = HomeAssistantClient()

    async def get_audio_output_for_room(
        self,
        room_id: int,
        input_device_id: str | None = None
    ) -> OutputDecision:
        """
        Get the best available audio output device for a room.

        Args:
            room_id: Room ID to get output for
            input_device_id: The input device ID (for fallback)

        Returns:
            OutputDecision with the selected device or fallback info
        """
        return await self._get_output_for_room(
            room_id=room_id,
            output_type=OUTPUT_TYPE_AUDIO,
            input_device_id=input_device_id
        )

    async def get_visual_output_for_room(
        self,
        room_id: int,
        input_device_id: str | None = None
    ) -> OutputDecision:
        """
        Get the best available visual output device for a room.

        Args:
            room_id: Room ID to get output for
            input_device_id: The input device ID (for fallback)

        Returns:
            OutputDecision with the selected device or fallback info
        """
        return await self._get_output_for_room(
            room_id=room_id,
            output_type=OUTPUT_TYPE_VISUAL,
            input_device_id=input_device_id
        )

    async def _get_output_for_room(
        self,
        room_id: int,
        output_type: str,
        input_device_id: str | None = None
    ) -> OutputDecision:
        """
        Core routing logic to find the best output device.
        """
        # Get all configured output devices for this room/type, sorted by priority
        output_devices = await self._get_output_devices(room_id, output_type)

        if not output_devices:
            logger.debug(f"No {output_type} output devices configured for room {room_id}")
            return OutputDecision(
                output_device=None,
                target_id=input_device_id or "",
                target_type="renfield",
                availability=DeviceAvailability.AVAILABLE,
                fallback_to_input=True,
                reason="no_output_devices_configured"
            )

        # Try each device in priority order
        for device in output_devices:
            if not device.is_enabled:
                continue

            # Check availability
            availability = await self._check_device_availability(device)

            # Decision logic
            if availability == DeviceAvailability.AVAILABLE:
                logger.info(f"Selected output device: {device.device_name or device.target_id} (available)")
                return OutputDecision(
                    output_device=device,
                    target_id=device.target_id,
                    target_type="renfield" if device.is_renfield_device else "homeassistant",
                    availability=availability,
                    fallback_to_input=False,
                    reason="device_available"
                )

            elif availability == DeviceAvailability.BUSY:
                if device.allow_interruption:
                    logger.info(f"Selected output device: {device.device_name or device.target_id} (busy, interrupting)")
                    return OutputDecision(
                        output_device=device,
                        target_id=device.target_id,
                        target_type="renfield" if device.is_renfield_device else "homeassistant",
                        availability=availability,
                        fallback_to_input=False,
                        reason="device_busy_allowing_interruption"
                    )
                else:
                    logger.debug(f"Skipping device {device.device_name or device.target_id}: busy, no interruption allowed")
                    continue

            else:
                # OFF or UNAVAILABLE - try next device
                logger.debug(f"Skipping device {device.device_name or device.target_id}: {availability.value}")
                continue

        # No suitable device found - fallback to input device
        logger.info(f"No suitable output device found, falling back to input device: {input_device_id}")
        return OutputDecision(
            output_device=None,
            target_id=input_device_id or "",
            target_type="renfield",
            availability=DeviceAvailability.AVAILABLE,
            fallback_to_input=True,
            reason="all_devices_unavailable"
        )

    async def _get_output_devices(
        self,
        room_id: int,
        output_type: str
    ) -> list[RoomOutputDevice]:
        """Get all configured output devices for a room, sorted by priority."""
        stmt = (
            select(RoomOutputDevice)
            .where(RoomOutputDevice.room_id == room_id)
            .where(RoomOutputDevice.output_type == output_type)
            .order_by(RoomOutputDevice.priority)
        )
        result = await self.db.execute(stmt)
        return list(result.scalars().all())

    async def _check_device_availability(
        self,
        output_device: RoomOutputDevice
    ) -> DeviceAvailability:
        """
        Check if an output device is available for playback.
        """
        if output_device.is_renfield_device:
            return await self._check_renfield_device_availability(output_device.renfield_device_id)
        else:
            return await self._check_ha_device_availability(output_device.ha_entity_id)

    async def _check_renfield_device_availability(
        self,
        device_id: str
    ) -> DeviceAvailability:
        """
        Check if a Renfield device is available (connected and idle).
        """
        from services.device_manager import DeviceState, get_device_manager

        device_manager = get_device_manager()
        device = device_manager.get_device(device_id)

        if not device:
            return DeviceAvailability.UNAVAILABLE

        if not device.capabilities.has_speaker:
            return DeviceAvailability.UNAVAILABLE

        # Check device state
        if device.state == DeviceState.SPEAKING:
            return DeviceAvailability.BUSY
        elif device.state in [DeviceState.IDLE, DeviceState.PROCESSING, DeviceState.LISTENING]:
            return DeviceAvailability.AVAILABLE
        else:
            return DeviceAvailability.UNAVAILABLE

    async def _check_ha_device_availability(
        self,
        entity_id: str
    ) -> DeviceAvailability:
        """
        Check if a Home Assistant media player is available.

        Maps HA states to our availability status:
        - idle, paused, standby, on → AVAILABLE
        - playing, buffering → BUSY
        - off → OFF
        - unavailable, unknown → UNAVAILABLE
        """
        try:
            state = await self.ha_client.get_state(entity_id)

            if not state:
                return DeviceAvailability.UNAVAILABLE

            ha_state = state.get("state", "unknown").lower()

            # Map HA states to availability
            if ha_state in ["idle", "paused", "standby", "on"]:
                return DeviceAvailability.AVAILABLE
            elif ha_state in ["playing", "buffering"]:
                return DeviceAvailability.BUSY
            elif ha_state == "off":
                return DeviceAvailability.OFF
            else:
                return DeviceAvailability.UNAVAILABLE

        except Exception as e:
            logger.error(f"Failed to check HA device availability for {entity_id}: {e}")
            return DeviceAvailability.UNAVAILABLE

    # --- CRUD Operations ---

    async def get_output_devices_for_room(self, room_id: int) -> list[RoomOutputDevice]:
        """Get all output devices for a room."""
        stmt = (
            select(RoomOutputDevice)
            .where(RoomOutputDevice.room_id == room_id)
            .order_by(RoomOutputDevice.output_type, RoomOutputDevice.priority)
        )
        result = await self.db.execute(stmt)
        return list(result.scalars().all())

    async def add_output_device(
        self,
        room_id: int,
        output_type: str,
        renfield_device_id: str | None = None,
        ha_entity_id: str | None = None,
        priority: int = 1,
        allow_interruption: bool = False,
        tts_volume: float | None = 0.5,
        device_name: str | None = None
    ) -> RoomOutputDevice:
        """
        Add a new output device to a room.

        Either renfield_device_id or ha_entity_id must be provided.
        """
        if not renfield_device_id and not ha_entity_id:
            raise ValueError("Either renfield_device_id or ha_entity_id must be provided")

        if renfield_device_id and ha_entity_id:
            raise ValueError("Only one of renfield_device_id or ha_entity_id can be provided")

        # Auto-generate device name if not provided
        if not device_name:
            if renfield_device_id:
                device_name = renfield_device_id
            else:
                # Try to get friendly name from HA
                try:
                    state = await self.ha_client.get_state(ha_entity_id)
                    device_name = state.get("attributes", {}).get("friendly_name", ha_entity_id)
                except Exception:
                    device_name = ha_entity_id  # Fallback if HA unavailable

        output_device = RoomOutputDevice(
            room_id=room_id,
            output_type=output_type,
            renfield_device_id=renfield_device_id,
            ha_entity_id=ha_entity_id,
            priority=priority,
            allow_interruption=allow_interruption,
            tts_volume=tts_volume,
            device_name=device_name,
            is_enabled=True
        )

        self.db.add(output_device)
        await self.db.commit()
        await self.db.refresh(output_device)

        logger.info(f"Added output device '{device_name}' to room {room_id}")
        return output_device

    async def update_output_device(
        self,
        device_id: int,
        priority: int | None = None,
        allow_interruption: bool | None = None,
        tts_volume: float | None = None,
        is_enabled: bool | None = None,
        device_name: str | None = None
    ) -> RoomOutputDevice | None:
        """Update an existing output device."""
        stmt = select(RoomOutputDevice).where(RoomOutputDevice.id == device_id)
        result = await self.db.execute(stmt)
        device = result.scalar_one_or_none()

        if not device:
            return None

        if priority is not None:
            device.priority = priority
        if allow_interruption is not None:
            device.allow_interruption = allow_interruption
        if tts_volume is not None:
            device.tts_volume = tts_volume
        if is_enabled is not None:
            device.is_enabled = is_enabled
        if device_name is not None:
            device.device_name = device_name

        await self.db.commit()
        await self.db.refresh(device)

        logger.info(f"Updated output device {device_id}")
        return device

    async def delete_output_device(self, device_id: int) -> bool:
        """Delete an output device."""
        stmt = select(RoomOutputDevice).where(RoomOutputDevice.id == device_id)
        result = await self.db.execute(stmt)
        device = result.scalar_one_or_none()

        if not device:
            return False

        await self.db.delete(device)
        await self.db.commit()

        logger.info(f"Deleted output device {device_id}")
        return True

    async def reorder_output_devices(
        self,
        room_id: int,
        output_type: str,
        device_ids: list[int]
    ) -> list[RoomOutputDevice]:
        """
        Reorder output devices by setting new priorities.

        Args:
            room_id: Room ID
            output_type: "audio" or "visual"
            device_ids: List of device IDs in new priority order (first = highest priority)

        Returns:
            Updated list of devices
        """
        for priority, device_id in enumerate(device_ids, start=1):
            stmt = select(RoomOutputDevice).where(
                RoomOutputDevice.id == device_id,
                RoomOutputDevice.room_id == room_id,
                RoomOutputDevice.output_type == output_type
            )
            result = await self.db.execute(stmt)
            device = result.scalar_one_or_none()

            if device:
                device.priority = priority

        await self.db.commit()

        # Return updated list
        return await self._get_output_devices(room_id, output_type)

    async def get_available_ha_media_players(self) -> list[dict]:
        """
        Get all available Home Assistant media_player entities.

        Returns list of dicts with entity_id, friendly_name, state.
        """
        try:
            entities = await self.ha_client.get_entities_by_domain("media_player")
            return entities
        except Exception as e:
            logger.error(f"Failed to get HA media players: {e}")
            return []

    async def get_available_renfield_devices(self, room_id: int) -> list[RoomDevice]:
        """
        Get all Renfield devices in a room that have speaker capability.
        """
        stmt = select(RoomDevice).where(RoomDevice.room_id == room_id)
        result = await self.db.execute(stmt)
        devices = result.scalars().all()

        # Filter to devices with speaker capability
        return [d for d in devices if d.capabilities and d.capabilities.get("has_speaker", False)]
