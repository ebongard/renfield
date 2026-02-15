"""
Presence Detection API Routes

Endpoints for room occupancy, user presence, BLE device management,
and presence analytics (heatmap, predictions).
"""

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.database import User
from models.permissions import Permission
from services.auth_service import require_permission
from services.database import get_db
from services.presence_service import get_presence_service
from utils.config import settings

router = APIRouter(prefix="/api/presence")


# --- Schemas ---

class UserPresenceResponse(BaseModel):
    user_id: int
    user_name: str | None = None
    room_id: int | None = None
    room_name: str | None = None
    satellite_id: str | None = None
    confidence: float = 0.0
    last_seen: float = 0.0
    alone: bool | None = None


class RoomOccupancyResponse(BaseModel):
    room_id: int
    room_name: str | None = None
    occupants: list[UserPresenceResponse] = []


class BLEDeviceResponse(BaseModel):
    id: int
    user_id: int
    mac_address: str
    device_name: str
    device_type: str
    detection_method: str = "ble"
    is_enabled: bool


VALID_DETECTION_METHODS = {"ble", "classic_bt"}


class BLEDeviceCreate(BaseModel):
    user_id: int
    mac_address: str = Field(..., pattern=r"^([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}$")
    device_name: str = Field(..., min_length=1, max_length=100)
    device_type: str = Field(default="phone", max_length=50)
    detection_method: str = Field(default="ble", max_length=20)


# --- Status ---


class PresenceStatusResponse(BaseModel):
    enabled: bool


@router.get("/status", response_model=PresenceStatusResponse)
async def get_presence_status():
    """Check whether presence detection is enabled."""
    return PresenceStatusResponse(enabled=settings.presence_enabled)


# --- Room occupancy ---

@router.get("/rooms", response_model=list[RoomOccupancyResponse])
async def get_rooms_presence():
    """Get all rooms with their current occupants."""
    if not settings.presence_enabled:
        return []

    presence = get_presence_service()
    all_presence = presence.get_all_presence()

    # Group by room
    rooms: dict[int, RoomOccupancyResponse] = {}
    for _uid, p in all_presence.items():
        if p.room_id is None:
            continue
        if p.room_id not in rooms:
            rooms[p.room_id] = RoomOccupancyResponse(
                room_id=p.room_id,
                room_name=p.room_name,
            )
        rooms[p.room_id].occupants.append(UserPresenceResponse(
            user_id=p.user_id,
            user_name=presence.get_user_name(p.user_id),
            room_id=p.room_id,
            room_name=p.room_name,
            satellite_id=p.satellite_id,
            confidence=round(p.confidence, 2),
            last_seen=p.last_seen,
        ))

    return list(rooms.values())


@router.get("/room/{room_id}", response_model=RoomOccupancyResponse)
async def get_room_presence(room_id: int):
    """Get occupants of a specific room."""
    if not settings.presence_enabled:
        return RoomOccupancyResponse(room_id=room_id)

    presence = get_presence_service()
    occupants = presence.get_room_occupants(room_id)

    room_name = None
    items = []
    for p in occupants:
        room_name = room_name or p.room_name
        items.append(UserPresenceResponse(
            user_id=p.user_id,
            user_name=presence.get_user_name(p.user_id),
            room_id=p.room_id,
            room_name=p.room_name,
            satellite_id=p.satellite_id,
            confidence=round(p.confidence, 2),
            last_seen=p.last_seen,
        ))

    return RoomOccupancyResponse(
        room_id=room_id,
        room_name=room_name,
        occupants=items,
    )


@router.get("/user/{user_id}", response_model=UserPresenceResponse)
async def get_user_presence(user_id: int):
    """Get current room and alone-status for a user."""
    if not settings.presence_enabled:
        return UserPresenceResponse(user_id=user_id)

    presence = get_presence_service()
    p = presence.get_user_presence(user_id)
    if p is None:
        return UserPresenceResponse(user_id=user_id)

    return UserPresenceResponse(
        user_id=p.user_id,
        user_name=presence.get_user_name(p.user_id),
        room_id=p.room_id,
        room_name=p.room_name,
        satellite_id=p.satellite_id,
        confidence=round(p.confidence, 2),
        last_seen=p.last_seen,
        alone=presence.is_user_alone_in_room(user_id),
    )


# --- BLE Device management (admin only) ---

@router.get("/devices", response_model=list[BLEDeviceResponse])
async def list_devices(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_permission(Permission.ADMIN)),
):
    """List all registered BLE devices."""
    from models.database import UserBleDevice

    result = await db.execute(select(UserBleDevice))
    devices = result.scalars().all()

    return [
        BLEDeviceResponse(
            id=d.id,
            user_id=d.user_id,
            mac_address=d.mac_address,
            device_name=d.device_name,
            device_type=d.device_type,
            detection_method=d.detection_method or "ble",
            is_enabled=d.is_enabled,
        )
        for d in devices
    ]


@router.post("/devices", response_model=BLEDeviceResponse, status_code=201)
async def register_device(
    body: BLEDeviceCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_permission(Permission.ADMIN)),
):
    """Register a new BLE device for presence tracking."""
    from models.database import UserBleDevice

    # Validate detection_method
    if body.detection_method not in VALID_DETECTION_METHODS:
        raise HTTPException(
            status_code=422,
            detail=f"Invalid detection_method. Must be one of: {', '.join(VALID_DETECTION_METHODS)}",
        )

    # Check for duplicate MAC
    mac = body.mac_address.upper()
    existing = await db.execute(
        select(UserBleDevice).where(UserBleDevice.mac_address == mac)
    )
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=409, detail=f"MAC address {mac} already registered")

    presence = get_presence_service()
    device = await presence.add_device(
        user_id=body.user_id,
        mac=mac,
        name=body.device_name,
        device_type=body.device_type,
        db=db,
        detection_method=body.detection_method,
    )

    return BLEDeviceResponse(
        id=device.id,
        user_id=device.user_id,
        mac_address=device.mac_address,
        device_name=device.device_name,
        device_type=device.device_type,
        detection_method=device.detection_method or "ble",
        is_enabled=device.is_enabled,
    )


@router.delete("/devices/{device_id}", status_code=204)
async def delete_device(
    device_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_permission(Permission.ADMIN)),
):
    """Remove a BLE device from presence tracking."""
    presence = get_presence_service()
    removed = await presence.remove_device(device_id, db)
    if not removed:
        raise HTTPException(status_code=404, detail="Device not found")


# --- Analytics ---

class HeatmapCell(BaseModel):
    room_id: int
    room_name: str
    hour: int
    count: int


class PredictionEntry(BaseModel):
    room_id: int
    room_name: str
    day_of_week: int
    hour: int
    probability: float


class DailySummary(BaseModel):
    date: str
    enter_count: int
    leave_count: int


@router.get("/analytics/heatmap", response_model=list[HeatmapCell])
async def get_heatmap(
    days: int = Query(default=30, ge=1, le=365),
    user_id: int | None = Query(default=None),
    db: AsyncSession = Depends(get_db),
):
    """Room x hour heatmap of enter events."""
    from services.presence_analytics import PresenceAnalyticsService

    service = PresenceAnalyticsService(db)
    return await service.get_heatmap(days=days, user_id=user_id)


@router.get("/analytics/predictions", response_model=list[PredictionEntry])
async def get_predictions(
    user_id: int = Query(...),
    days: int = Query(default=60, ge=7, le=365),
    db: AsyncSession = Depends(get_db),
):
    """Per-user room presence predictions by day-of-week and hour."""
    from services.presence_analytics import PresenceAnalyticsService

    service = PresenceAnalyticsService(db)
    return await service.get_predictions(user_id=user_id, days=days)


@router.get("/analytics/daily", response_model=list[DailySummary])
async def get_daily_summary(
    days: int = Query(default=7, ge=1, le=90),
    db: AsyncSession = Depends(get_db),
):
    """Daily enter/leave event counts."""
    from services.presence_analytics import PresenceAnalyticsService

    service = PresenceAnalyticsService(db)
    return await service.get_daily_summary(days=days)
