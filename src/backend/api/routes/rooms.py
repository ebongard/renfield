"""
Room Management API Routes

Endpoints for room CRUD, satellite assignment, and Home Assistant area synchronization.
Pydantic schemas are defined in rooms_schemas.py.
"""

from fastapi import APIRouter, Depends, HTTPException
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from integrations.homeassistant import HomeAssistantClient
from services.database import get_db
from services.room_service import RoomService

# Import all schemas from separate file
from .rooms_schemas import (
    AvailableOutputResponse,
    ConnectedDeviceResponse,
    DeviceRegisterRequest,
    DeviceResponse,
    HAAreaResponse,
    HAExportResponse,
    HAImportRequest,
    HAImportResponse,
    OutputDeviceCreate,
    OutputDeviceReorderRequest,
    OutputDeviceResponse,
    OutputDeviceUpdate,
    RoomCreate,
    RoomResponse,
    RoomUpdate,
    SatelliteAssignRequest,
    SyncResponse,
)

router = APIRouter()


# --- Room CRUD Endpoints ---

@router.post("", response_model=RoomResponse)
async def create_room(
    room: RoomCreate,
    db: AsyncSession = Depends(get_db)
):
    """Create a new room"""
    service = RoomService(db)

    # Check if room with same name exists
    existing = await service.get_room_by_name(room.name)
    if existing:
        raise HTTPException(
            status_code=400,
            detail=f"Room with name '{room.name}' already exists"
        )

    new_room = await service.create_room(
        name=room.name,
        source="renfield",
        ha_area_id=room.ha_area_id,
        icon=room.icon
    )

    return service.room_to_dict(new_room)


@router.get("", response_model=list[RoomResponse])
async def list_rooms(db: AsyncSession = Depends(get_db)):
    """List all rooms with their satellites"""
    service = RoomService(db)
    rooms = await service.get_all_rooms()
    return [service.room_to_dict(r) for r in rooms]


@router.get("/{room_id}", response_model=RoomResponse)
async def get_room(
    room_id: int,
    db: AsyncSession = Depends(get_db)
):
    """Get a specific room with its satellites"""
    service = RoomService(db)
    room = await service.get_room(room_id)

    if not room:
        raise HTTPException(status_code=404, detail="Room not found")

    return service.room_to_dict(room)


@router.patch("/{room_id}", response_model=RoomResponse)
async def update_room(
    room_id: int,
    update: RoomUpdate,
    db: AsyncSession = Depends(get_db)
):
    """Update room details"""
    service = RoomService(db)

    # Check for name collision if updating name
    if update.name:
        existing = await service.get_room_by_name(update.name)
        if existing and existing.id != room_id:
            raise HTTPException(
                status_code=400,
                detail=f"Room with name '{update.name}' already exists"
            )

    room = await service.update_room(
        room_id=room_id,
        name=update.name,
        icon=update.icon
    )

    if not room:
        raise HTTPException(status_code=404, detail="Room not found")

    return service.room_to_dict(room)


@router.delete("/{room_id}")
async def delete_room(
    room_id: int,
    db: AsyncSession = Depends(get_db)
):
    """Delete a room and all satellite assignments"""
    service = RoomService(db)

    room = await service.get_room(room_id)
    if not room:
        raise HTTPException(status_code=404, detail="Room not found")

    room_name = room.name
    success = await service.delete_room(room_id)

    if success:
        return {"message": f"Room '{room_name}' deleted"}
    else:
        raise HTTPException(status_code=500, detail="Failed to delete room")


# --- Home Assistant Sync Endpoints ---

@router.get("/ha/areas", response_model=list[HAAreaResponse])
async def list_ha_areas(db: AsyncSession = Depends(get_db)):
    """
    List all Home Assistant areas with their link status.

    Shows which HA areas are linked to Renfield rooms.
    """
    service = RoomService(db)
    ha_client = HomeAssistantClient()

    try:
        areas = await ha_client.get_areas()
    except Exception as e:
        logger.error(f"Failed to fetch HA areas: {e}")
        raise HTTPException(
            status_code=503,
            detail=f"Failed to connect to Home Assistant: {e!s}"
        )

    # Get all rooms to check links
    rooms = await service.get_all_rooms()
    room_by_ha_id = {r.ha_area_id: r for r in rooms if r.ha_area_id}

    result = []
    for area in areas:
        area_id = area.get("area_id")
        linked_room = room_by_ha_id.get(area_id)

        result.append(HAAreaResponse(
            area_id=area_id,
            name=area.get("name", ""),
            icon=area.get("icon"),
            is_linked=linked_room is not None,
            linked_room_id=linked_room.id if linked_room else None,
            linked_room_name=linked_room.name if linked_room else None
        ))

    return result


@router.post("/ha/import", response_model=HAImportResponse)
async def import_ha_areas(
    request: HAImportRequest,
    db: AsyncSession = Depends(get_db)
):
    """
    Import areas from Home Assistant into Renfield.

    Conflict resolution options:
    - skip: Don't import if a room with the same name exists
    - link: Link existing room with same name to the HA area
    - overwrite: Update existing room's HA link (replaces old link)
    """
    service = RoomService(db)
    ha_client = HomeAssistantClient()

    try:
        areas = await ha_client.get_areas()
    except Exception as e:
        logger.error(f"Failed to fetch HA areas: {e}")
        raise HTTPException(
            status_code=503,
            detail=f"Failed to connect to Home Assistant: {e!s}"
        )

    results = await service.import_ha_areas(
        ha_areas=areas,
        conflict_resolution=request.conflict_resolution
    )

    return HAImportResponse(**results)


@router.post("/ha/export", response_model=HAExportResponse)
async def export_rooms_to_ha(db: AsyncSession = Depends(get_db)):
    """
    Export Renfield rooms to Home Assistant as areas.

    Only exports rooms that are not already linked to an HA area.
    If an HA area with the same name exists, links to it instead of creating.
    """
    service = RoomService(db)
    ha_client = HomeAssistantClient()

    # Get rooms without HA link
    rooms_to_export = await service.get_rooms_for_export()

    if not rooms_to_export:
        return HAExportResponse(exported=0, linked=0, errors=[])

    # Get existing HA areas for name matching
    try:
        existing_areas = await ha_client.get_areas()
    except Exception as e:
        logger.error(f"Failed to fetch HA areas: {e}")
        raise HTTPException(
            status_code=503,
            detail=f"Failed to connect to Home Assistant: {e!s}"
        )

    area_by_name = {a.get("name", "").lower(): a for a in existing_areas}

    results = {
        "exported": 0,
        "linked": 0,
        "errors": []
    }

    for room in rooms_to_export:
        try:
            # Check if HA area with same name exists
            existing = area_by_name.get(room.name.lower())

            if existing:
                # Link to existing area
                await service.link_to_ha_area(room.id, existing["area_id"])
                results["linked"] += 1
                logger.info(f"Linked room '{room.name}' to existing HA area")
            else:
                # Create new area in HA
                new_area = await ha_client.create_area(
                    name=room.name,
                    icon=room.icon
                )

                if new_area:
                    await service.link_to_ha_area(room.id, new_area["area_id"])
                    results["exported"] += 1
                    logger.info(f"Created HA area for room '{room.name}'")
                else:
                    results["errors"].append(f"Failed to create area for '{room.name}'")

        except Exception as e:
            results["errors"].append(f"{room.name}: {e!s}")

    return HAExportResponse(**results)


@router.post("/ha/sync", response_model=SyncResponse)
async def sync_with_ha(
    conflict_resolution: str = "link",
    db: AsyncSession = Depends(get_db)
):
    """
    Bidirectional sync with Home Assistant.

    1. Import: Creates Renfield rooms for new HA areas
    2. Export: Creates HA areas for Renfield rooms without HA link
    """
    service = RoomService(db)
    ha_client = HomeAssistantClient()

    # Fetch HA areas once
    try:
        areas = await ha_client.get_areas()
    except Exception as e:
        logger.error(f"Failed to fetch HA areas: {e}")
        raise HTTPException(
            status_code=503,
            detail=f"Failed to connect to Home Assistant: {e!s}"
        )

    # Import
    import_results = await service.import_ha_areas(
        ha_areas=areas,
        conflict_resolution=conflict_resolution
    )

    # Export (rooms without HA link)
    rooms_to_export = await service.get_rooms_for_export()
    area_by_name = {a.get("name", "").lower(): a for a in areas}

    export_results = {
        "exported": 0,
        "linked": 0,
        "errors": []
    }

    for room in rooms_to_export:
        try:
            existing = area_by_name.get(room.name.lower())

            if existing:
                await service.link_to_ha_area(room.id, existing["area_id"])
                export_results["linked"] += 1
            else:
                new_area = await ha_client.create_area(room.name, room.icon)
                if new_area:
                    await service.link_to_ha_area(room.id, new_area["area_id"])
                    export_results["exported"] += 1
                else:
                    export_results["errors"].append(f"Failed to create area for '{room.name}'")
        except Exception as e:
            export_results["errors"].append(f"{room.name}: {e!s}")

    return SyncResponse(
        import_results=HAImportResponse(**import_results),
        export_results=HAExportResponse(**export_results)
    )


@router.post("/{room_id}/link/{ha_area_id}", response_model=RoomResponse)
async def link_room_to_ha_area(
    room_id: int,
    ha_area_id: str,
    db: AsyncSession = Depends(get_db)
):
    """Link a room to a specific Home Assistant area"""
    service = RoomService(db)

    # Check if area is already linked to another room
    existing = await service.get_room_by_ha_area_id(ha_area_id)
    if existing and existing.id != room_id:
        raise HTTPException(
            status_code=400,
            detail=f"HA area is already linked to room '{existing.name}'"
        )

    room = await service.link_to_ha_area(room_id, ha_area_id)

    if not room:
        raise HTTPException(status_code=404, detail="Room not found")

    return service.room_to_dict(room)


@router.delete("/{room_id}/link", response_model=RoomResponse)
async def unlink_room_from_ha(
    room_id: int,
    db: AsyncSession = Depends(get_db)
):
    """Remove Home Assistant area link from a room"""
    service = RoomService(db)

    room = await service.unlink_from_ha(room_id)

    if not room:
        raise HTTPException(status_code=404, detail="Room not found")

    return service.room_to_dict(room)


# --- Satellite Assignment Endpoints ---

@router.get("/{room_id}/satellites")
async def get_room_satellites(
    room_id: int,
    db: AsyncSession = Depends(get_db)
):
    """Get all satellites assigned to a room"""
    service = RoomService(db)

    room = await service.get_room(room_id)
    if not room:
        raise HTTPException(status_code=404, detail="Room not found")

    return {
        "room_id": room.id,
        "room_name": room.name,
        "satellites": [
            {
                "satellite_id": sat.satellite_id,
                "is_online": sat.is_online,
                "last_connected_at": sat.last_connected_at.isoformat() if sat.last_connected_at else None
            }
            for sat in room.satellites
        ]
    }


@router.post("/{room_id}/satellites")
async def assign_satellite_to_room(
    room_id: int,
    request: SatelliteAssignRequest,
    db: AsyncSession = Depends(get_db)
):
    """Manually assign a satellite to a room"""
    service = RoomService(db)

    assignment = await service.assign_satellite(room_id, request.satellite_id)

    if not assignment:
        raise HTTPException(status_code=404, detail="Room not found")

    return {
        "message": f"Satellite '{request.satellite_id}' assigned to room",
        "room_id": room_id,
        "satellite_id": request.satellite_id
    }


@router.delete("/{room_id}/satellites/{satellite_id}")
async def unassign_satellite(
    room_id: int,
    satellite_id: str,
    db: AsyncSession = Depends(get_db)
):
    """Remove a satellite from a room"""
    service = RoomService(db)

    # Verify room exists and satellite is assigned to it
    assignment = await service.get_satellite_assignment(satellite_id)
    if not assignment or assignment.room_id != room_id:
        raise HTTPException(
            status_code=404,
            detail="Satellite not found in this room"
        )

    await service.unassign_satellite(satellite_id)

    return {
        "message": f"Satellite '{satellite_id}' removed from room",
        "room_id": room_id,
        "satellite_id": satellite_id
    }


# --- Device Endpoints ---

@router.get("/devices/connected", response_model=list[ConnectedDeviceResponse])
async def get_connected_devices():
    """
    Get all currently connected devices (via WebSocket).

    Returns real-time status of all connected satellites and web clients.
    """
    from services.device_manager import get_device_manager

    device_manager = get_device_manager()
    devices = device_manager.get_all_devices()

    return [ConnectedDeviceResponse(**d) for d in devices]


@router.get("/devices/connected/{room_id}", response_model=list[ConnectedDeviceResponse])
async def get_connected_devices_in_room(room_id: int):
    """Get all connected devices in a specific room"""
    from services.device_manager import get_device_manager

    device_manager = get_device_manager()
    devices = device_manager.get_devices_in_room_by_id(room_id)

    return [
        ConnectedDeviceResponse(
            device_id=d.device_id,
            device_type=d.device_type,
            device_name=d.device_name,
            room=d.room,
            room_id=d.room_id,
            state=d.state.value,
            connected_at=d.connected_at,
            last_heartbeat=d.last_heartbeat,
            has_active_session=d.current_session_id is not None,
            is_stationary=d.is_stationary,
            capabilities=d.capabilities.to_dict()
        )
        for d in devices
    ]


@router.get("/{room_id}/devices", response_model=list[DeviceResponse])
async def get_room_devices(
    room_id: int,
    db: AsyncSession = Depends(get_db)
):
    """
    Get all devices registered in a room (from database).

    This includes both online and offline devices.
    For real-time connected status, use /devices/connected endpoint.
    """
    service = RoomService(db)

    room = await service.get_room(room_id)
    if not room:
        raise HTTPException(status_code=404, detail="Room not found")

    devices = await service.get_devices_in_room(room_id)

    return [
        DeviceResponse(
            id=d.id,
            device_id=d.device_id,
            device_type=d.device_type,
            device_name=d.device_name,
            room_id=room_id,
            room_name=room.name,
            capabilities=d.capabilities or {},
            is_online=d.is_online,
            is_stationary=d.is_stationary,
            last_connected_at=d.last_connected_at.isoformat() if d.last_connected_at else None,
            user_agent=d.user_agent,
            ip_address=d.ip_address
        )
        for d in devices
    ]


@router.post("/{room_id}/devices", response_model=DeviceResponse)
async def register_device(
    room_id: int,
    request: DeviceRegisterRequest,
    db: AsyncSession = Depends(get_db)
):
    """
    Manually register a device to a room.

    Normally devices register themselves via WebSocket, but this endpoint
    allows pre-registering devices or registering devices manually.
    """
    service = RoomService(db)

    room = await service.get_room(room_id)
    if not room:
        raise HTTPException(status_code=404, detail="Room not found")

    # Check if device already exists
    existing = await service.get_device(request.device_id)
    if existing:
        raise HTTPException(
            status_code=400,
            detail=f"Device '{request.device_id}' already registered"
        )

    device = await service.register_device(
        device_id=request.device_id,
        room_name=room.name,
        device_type=request.device_type,
        device_name=request.device_name,
        capabilities=request.capabilities,
        is_stationary=request.is_stationary
    )

    return DeviceResponse(
        id=device.id,
        device_id=device.device_id,
        device_type=device.device_type,
        device_name=device.device_name,
        room_id=device.room_id,
        room_name=room.name,
        capabilities=device.capabilities or {},
        is_online=device.is_online,
        is_stationary=device.is_stationary,
        last_connected_at=device.last_connected_at.isoformat() if device.last_connected_at else None,
        user_agent=device.user_agent,
        ip_address=device.ip_address
    )


@router.get("/devices/{device_id}", response_model=DeviceResponse)
async def get_device(
    device_id: str,
    db: AsyncSession = Depends(get_db)
):
    """Get a specific device by ID"""
    service = RoomService(db)

    device = await service.get_device(device_id)
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")

    return DeviceResponse(
        id=device.id,
        device_id=device.device_id,
        device_type=device.device_type,
        device_name=device.device_name,
        room_id=device.room_id,
        room_name=device.room.name,
        capabilities=device.capabilities or {},
        is_online=device.is_online,
        is_stationary=device.is_stationary,
        last_connected_at=device.last_connected_at.isoformat() if device.last_connected_at else None,
        user_agent=device.user_agent,
        ip_address=device.ip_address
    )


@router.delete("/devices/{device_id}")
async def delete_device(
    device_id: str,
    db: AsyncSession = Depends(get_db)
):
    """Delete a device from the system"""
    service = RoomService(db)

    device = await service.get_device(device_id)
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")

    await service.delete_device(device_id)

    return {"message": f"Device '{device_id}' deleted"}


@router.patch("/devices/{device_id}/room/{room_id}")
async def move_device_to_room(
    device_id: str,
    room_id: int,
    db: AsyncSession = Depends(get_db)
):
    """Move a device to a different room"""
    service = RoomService(db)

    device = await service.get_device(device_id)
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")

    room = await service.get_room(room_id)
    if not room:
        raise HTTPException(status_code=404, detail="Room not found")

    await service.move_device_to_room(device_id, room_id)

    return {
        "message": f"Device '{device_id}' moved to room '{room.name}'",
        "device_id": device_id,
        "room_id": room_id,
        "room_name": room.name
    }


# --- Output Device Endpoints ---

def _output_device_to_response(device) -> OutputDeviceResponse:
    """Convert RoomOutputDevice model to response"""
    return OutputDeviceResponse(
        id=device.id,
        room_id=device.room_id,
        output_type=device.output_type,
        renfield_device_id=device.renfield_device_id,
        ha_entity_id=device.ha_entity_id,
        dlna_renderer_name=device.dlna_renderer_name,
        priority=device.priority,
        allow_interruption=device.allow_interruption,
        tts_volume=device.tts_volume,
        device_name=device.device_name,
        is_enabled=device.is_enabled,
        created_at=device.created_at.isoformat() if device.created_at else None,
        updated_at=device.updated_at.isoformat() if device.updated_at else None
    )


@router.get("/{room_id}/output-devices", response_model=list[OutputDeviceResponse])
async def get_room_output_devices(
    room_id: int,
    db: AsyncSession = Depends(get_db)
):
    """Get all output devices configured for a room"""
    from services.output_routing_service import OutputRoutingService

    service = RoomService(db)
    room = await service.get_room(room_id)
    if not room:
        raise HTTPException(status_code=404, detail="Room not found")

    routing_service = OutputRoutingService(db)
    devices = await routing_service.get_output_devices_for_room(room_id)

    return [_output_device_to_response(d) for d in devices]


@router.post("/{room_id}/output-devices", response_model=OutputDeviceResponse)
async def add_output_device(
    room_id: int,
    request: OutputDeviceCreate,
    db: AsyncSession = Depends(get_db)
):
    """Add an output device to a room"""
    from services.output_routing_service import OutputRoutingService

    service = RoomService(db)
    room = await service.get_room(room_id)
    if not room:
        raise HTTPException(status_code=404, detail="Room not found")

    # Validate that exactly one device identifier is provided
    identifiers = [request.renfield_device_id, request.ha_entity_id, request.dlna_renderer_name]
    set_count = sum(1 for v in identifiers if v)
    if set_count == 0:
        raise HTTPException(
            status_code=400,
            detail="One of renfield_device_id, ha_entity_id, or dlna_renderer_name must be provided"
        )
    if set_count > 1:
        raise HTTPException(
            status_code=400,
            detail="Only one of renfield_device_id, ha_entity_id, or dlna_renderer_name can be provided"
        )

    # Validate output_type
    from models.database import OUTPUT_TYPES
    if request.output_type not in OUTPUT_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid output_type. Must be one of: {OUTPUT_TYPES}"
        )

    routing_service = OutputRoutingService(db)

    try:
        device = await routing_service.add_output_device(
            room_id=room_id,
            output_type=request.output_type,
            renfield_device_id=request.renfield_device_id,
            ha_entity_id=request.ha_entity_id,
            dlna_renderer_name=request.dlna_renderer_name,
            priority=request.priority,
            allow_interruption=request.allow_interruption,
            tts_volume=request.tts_volume,
            device_name=request.device_name
        )
        return _output_device_to_response(device)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.patch("/output-devices/{device_id}", response_model=OutputDeviceResponse)
async def update_output_device(
    device_id: int,
    request: OutputDeviceUpdate,
    db: AsyncSession = Depends(get_db)
):
    """Update an output device"""
    from services.output_routing_service import OutputRoutingService

    routing_service = OutputRoutingService(db)

    device = await routing_service.update_output_device(
        device_id=device_id,
        priority=request.priority,
        allow_interruption=request.allow_interruption,
        tts_volume=request.tts_volume,
        is_enabled=request.is_enabled,
        device_name=request.device_name
    )

    if not device:
        raise HTTPException(status_code=404, detail="Output device not found")

    return _output_device_to_response(device)


@router.delete("/output-devices/{device_id}")
async def delete_output_device(
    device_id: int,
    db: AsyncSession = Depends(get_db)
):
    """Delete an output device"""
    from services.output_routing_service import OutputRoutingService

    routing_service = OutputRoutingService(db)
    success = await routing_service.delete_output_device(device_id)

    if not success:
        raise HTTPException(status_code=404, detail="Output device not found")

    return {"message": "Output device deleted", "id": device_id}


@router.post("/{room_id}/output-devices/reorder", response_model=list[OutputDeviceResponse])
async def reorder_output_devices(
    room_id: int,
    request: OutputDeviceReorderRequest,
    output_type: str = "audio",
    db: AsyncSession = Depends(get_db)
):
    """
    Reorder output devices by setting new priorities.

    The device_ids list should be in the desired order (first = highest priority).
    """
    from models.database import OUTPUT_TYPES
    from services.output_routing_service import OutputRoutingService

    service = RoomService(db)
    room = await service.get_room(room_id)
    if not room:
        raise HTTPException(status_code=404, detail="Room not found")

    if output_type not in OUTPUT_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid output_type. Must be one of: {OUTPUT_TYPES}"
        )

    routing_service = OutputRoutingService(db)
    devices = await routing_service.reorder_output_devices(
        room_id=room_id,
        output_type=output_type,
        device_ids=request.device_ids
    )

    return [_output_device_to_response(d) for d in devices]


@router.get("/{room_id}/available-outputs", response_model=AvailableOutputResponse)
async def get_available_outputs(
    room_id: int,
    db: AsyncSession = Depends(get_db)
):
    """
    Get all available output devices for a room.

    Returns both Renfield devices (with speaker capability) and
    Home Assistant media_player entities.
    """
    from services.output_routing_service import OutputRoutingService

    service = RoomService(db)
    room = await service.get_room(room_id)
    if not room:
        raise HTTPException(status_code=404, detail="Room not found")

    routing_service = OutputRoutingService(db)

    # Get Renfield devices with speaker capability
    renfield_devices = await routing_service.get_available_renfield_devices(room_id)
    renfield_list = [
        {
            "device_id": d.device_id,
            "device_name": d.device_name or d.device_id,
            "device_type": d.device_type,
            "is_online": d.is_online,
            "capabilities": d.capabilities
        }
        for d in renfield_devices
    ]

    # Get HA media players
    ha_media_players = await routing_service.get_available_ha_media_players()

    # Get DLNA renderers
    dlna_renderers = await routing_service.get_available_dlna_renderers()

    return AvailableOutputResponse(
        renfield_devices=renfield_list,
        ha_media_players=ha_media_players,
        dlna_renderers=dlna_renderers
    )
