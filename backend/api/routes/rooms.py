"""
Room Management API Routes

Endpoints for room CRUD, satellite assignment, and Home Assistant area synchronization.
"""
from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel
from typing import Optional, List
from sqlalchemy.ext.asyncio import AsyncSession
from loguru import logger

from services.database import get_db
from services.room_service import RoomService
from integrations.homeassistant import HomeAssistantClient


router = APIRouter()


# --- Pydantic Models ---

class RoomCreate(BaseModel):
    name: str
    icon: Optional[str] = None
    ha_area_id: Optional[str] = None


class RoomUpdate(BaseModel):
    name: Optional[str] = None
    icon: Optional[str] = None


class RoomResponse(BaseModel):
    id: int
    name: str
    alias: str
    ha_area_id: Optional[str]
    source: str
    icon: Optional[str]
    created_at: Optional[str]
    updated_at: Optional[str]
    last_synced_at: Optional[str]
    satellite_count: int
    satellites: List[dict]

    class Config:
        from_attributes = True


class HAAreaResponse(BaseModel):
    area_id: str
    name: str
    icon: Optional[str]
    is_linked: bool
    linked_room_id: Optional[int]
    linked_room_name: Optional[str]


class HAImportRequest(BaseModel):
    conflict_resolution: str = "skip"  # skip, link, overwrite


class HAImportResponse(BaseModel):
    imported: int
    linked: int
    skipped: int
    errors: List[str]


class HAExportResponse(BaseModel):
    exported: int
    linked: int
    errors: List[str]


class SyncResponse(BaseModel):
    import_results: HAImportResponse
    export_results: HAExportResponse


class LinkHAAreaRequest(BaseModel):
    ha_area_id: str


class SatelliteAssignRequest(BaseModel):
    satellite_id: str


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


@router.get("", response_model=List[RoomResponse])
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

@router.get("/ha/areas", response_model=List[HAAreaResponse])
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
            detail=f"Failed to connect to Home Assistant: {str(e)}"
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
            detail=f"Failed to connect to Home Assistant: {str(e)}"
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
            detail=f"Failed to connect to Home Assistant: {str(e)}"
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
            results["errors"].append(f"{room.name}: {str(e)}")

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
            detail=f"Failed to connect to Home Assistant: {str(e)}"
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
            export_results["errors"].append(f"{room.name}: {str(e)}")

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
