"""
Presence Detection API Routes

Endpoints for room occupancy, user presence, BLE device management,
and presence analytics (heatmap, predictions).
"""

import hashlib
import time
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.database import User
from models.permissions import Permission
from services.auth_service import require_permission
from services.database import get_db
from ha_glue.services.presence_service import get_presence_service
from utils.config import settings
from ha_glue.utils.config import ha_glue_settings

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
    return PresenceStatusResponse(enabled=ha_glue_settings.presence_enabled)


# --- Diagnostics: raw per-satellite RSSI sightings ---


def _redact_device_key(key: str) -> str:
    """Stable, non-reversible short id for a device sighting key so the
    diagnostic can tell devices apart WITHOUT leaking the raw MAC or the
    user-chosen `irk:<label>` nickname (both are device-fingerprinting PII).
    Prefixed so an admin still sees whether it was MAC- or IRK-resolved."""
    prefix = "irk" if key.startswith("irk:") else "mac"
    digest = hashlib.sha256(key.encode()).hexdigest()[:8]
    return f"{prefix}:{digest}"


@router.get("/debug/sightings")
async def get_debug_sightings(
    current_user: User = Depends(require_permission(Permission.ADMIN)),
):
    """ADMIN-only diagnostic: the in-memory recent BLE sightings per tracked
    device — every satellite's raw RSSI + timestamp — so a per-satellite
    RSSI-over-time chart can be built, plus each device's current per-room
    filtered RSSI (the value the switch margin compares) and resolved room.

    Exposes cross-user presence + movement traces, so it is gated on ADMIN like
    the device/IRK management endpoints in this router. The per-device `key` is
    a redacted hash (never the raw MAC / IRK label)."""
    if not ha_glue_settings.presence_enabled:
        return {"now": round(time.time(), 3), "devices": []}
    presence = get_presence_service()
    now = time.time()
    out = []
    for key, sightings in presence._sightings.items():
        uid = presence._user_for_key(key)
        cur = presence._presence.get(uid) if uid is not None else None
        out.append({
            "key": _redact_device_key(key),
            "user_id": uid,
            "user_name": presence.get_user_name(uid) if uid is not None else None,
            "current_room_id": cur.room_id if cur else None,
            "current_room_name": cur.room_name if cur else None,
            "current_confidence": round(cur.confidence, 3) if cur else None,
            "filtered_by_room": {
                (presence._room_names.get(rid, str(rid))): round(v, 2)
                for rid, v in (cur.room_rssi_filtered.items() if cur else {})
            },
            "sightings": [
                {
                    "satellite_id": s.satellite_id,
                    "room_id": s.room_id,
                    "room_name": (presence._room_names.get(s.room_id)
                                  if s.room_id is not None else None),
                    "rssi": s.rssi,
                    "timestamp": round(s.timestamp, 3),
                    "age_s": round(now - s.timestamp, 2),
                }
                for s in sorted(sightings, key=lambda x: x.timestamp)
            ],
        })
    return {"now": round(now, 3), "devices": out}


# --- Room occupancy ---

@router.get("/rooms", response_model=list[RoomOccupancyResponse])
async def get_rooms_presence():
    """Get all rooms with their current occupants."""
    if not ha_glue_settings.presence_enabled:
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
    if not ha_glue_settings.presence_enabled:
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
    if not ha_glue_settings.presence_enabled:
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


class BLEDeviceUpdate(BaseModel):
    detection_method: str = Field(..., max_length=20)
    mac_address: str | None = Field(None, max_length=17)


@router.patch("/devices/{device_id}", response_model=BLEDeviceResponse)
async def update_device(
    device_id: int,
    body: BLEDeviceUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_permission(Permission.ADMIN)),
):
    """Update a device's detection method and/or MAC address."""
    if body.detection_method not in VALID_DETECTION_METHODS:
        raise HTTPException(
            status_code=422,
            detail=f"Invalid detection_method. Must be one of: {', '.join(VALID_DETECTION_METHODS)}",
        )

    presence = get_presence_service()
    device = await presence.update_device(
        device_id, body.detection_method, db, mac_address=body.mac_address,
    )
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")

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


# --- Per-person BLE IRK store (resolve rotating RPAs → stable identity) ---

class BLEIrkCreate(BaseModel):
    user_id: int
    label: str = Field(..., min_length=1, max_length=100,
                       description="Globally-unique stable identity (e.g. 'eduard-iphone')")
    irk: str = Field(..., pattern=r"^[0-9A-Fa-f]{32}$",
                     description="16-byte IRK as 32 hex chars, MSO-first")


class BLEIrkResponse(BaseModel):
    id: int
    user_id: int
    label: str
    is_enabled: bool
    created_at: str | None = None


@router.get("/irks", response_model=list[BLEIrkResponse])
async def list_irks(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_permission(Permission.ADMIN)),
):
    """List registered BLE IRKs (metadata only — the key itself is never returned)."""
    from models.database import UserBleIrk
    rows = (await db.execute(select(UserBleIrk))).scalars().all()
    return [
        BLEIrkResponse(
            id=r.id, user_id=r.user_id, label=r.label, is_enabled=r.is_enabled,
            created_at=r.created_at.isoformat() if r.created_at else None,
        )
        for r in rows
    ]


@router.post("/irks", response_model=BLEIrkResponse, status_code=201)
async def create_irk(
    body: BLEIrkCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_permission(Permission.ADMIN)),
):
    """Register a per-person IRK (stored encrypted, pushed to satellites)."""
    from models.database import User as DBUser, UserBleIrk
    from services.secret_encryption import encrypt_secret

    if not (await db.execute(select(DBUser).where(DBUser.id == body.user_id))).scalar_one_or_none():
        raise HTTPException(status_code=404, detail="User not found")
    if (await db.execute(select(UserBleIrk).where(UserBleIrk.label == body.label))).scalar_one_or_none():
        raise HTTPException(status_code=409, detail="Label already exists")

    row = UserBleIrk(
        user_id=body.user_id,
        label=body.label,
        irk_encrypted=encrypt_secret(body.irk.lower()),
        is_enabled=True,
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)

    presence = get_presence_service()
    await presence.load_device_registry(db)   # reloads MAC + IRK caches
    await presence.push_macs_to_satellites()

    return BLEIrkResponse(
        id=row.id, user_id=row.user_id, label=row.label, is_enabled=row.is_enabled,
        created_at=row.created_at.isoformat() if row.created_at else None,
    )


class BLEIrkCaptureRequest(BaseModel):
    satellite_id: str
    user_id: int
    label: str = Field(..., min_length=1, max_length=100)
    window_seconds: int = Field(default=60, ge=10, le=180)


@router.post("/irks/capture", response_model=BLEIrkResponse, status_code=201)
async def capture_irk(
    body: BLEIrkCaptureRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_permission(Permission.ADMIN)),
):
    """Drive the UI pairing flow: open a one-time pairing window on a satellite,
    capture the phone's IRK when it bonds, and store it (encrypted) for `user_id`.
    The caller shows the user the 'pair to Renfield <room>' prompt meanwhile."""
    from models.database import User as DBUser, UserBleIrk
    from services.secret_encryption import encrypt_secret
    from ha_glue.services.satellite_manager import get_satellite_manager

    if not (await db.execute(select(DBUser).where(DBUser.id == body.user_id))).scalar_one_or_none():
        raise HTTPException(status_code=404, detail="User not found")
    if (await db.execute(select(UserBleIrk).where(UserBleIrk.label == body.label))).scalar_one_or_none():
        raise HTTPException(status_code=409, detail="Label already exists")

    manager = get_satellite_manager()
    res = await manager.request_irk_capture(body.satellite_id, body.label, body.window_seconds)
    if res.get("error"):
        raise HTTPException(status_code=502, detail=f"Capture failed: {res['error']}")
    irk = (res.get("irk") or "").lower()
    if len(irk) != 32 or any(c not in "0123456789abcdef" for c in irk):
        raise HTTPException(status_code=408, detail="No phone paired during the capture window")

    row = UserBleIrk(
        user_id=body.user_id, label=body.label,
        irk_encrypted=encrypt_secret(irk), is_enabled=True,
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)

    presence = get_presence_service()
    await presence.load_device_registry(db)
    await presence.push_macs_to_satellites()

    return BLEIrkResponse(
        id=row.id, user_id=row.user_id, label=row.label, is_enabled=row.is_enabled,
        created_at=row.created_at.isoformat() if row.created_at else None,
    )


class BLEIrkUpdate(BaseModel):
    is_enabled: bool


@router.patch("/irks/{irk_id}", response_model=BLEIrkResponse)
async def update_irk(
    irk_id: int,
    body: BLEIrkUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_permission(Permission.ADMIN)),
):
    """Enable/disable an IRK without deleting it (disabling stops resolution +
    re-pushes the reduced set to satellites)."""
    from models.database import UserBleIrk
    row = (await db.execute(select(UserBleIrk).where(UserBleIrk.id == irk_id))).scalar_one_or_none()
    if not row:
        raise HTTPException(status_code=404, detail="IRK not found")
    row.is_enabled = body.is_enabled
    await db.commit()
    await db.refresh(row)

    presence = get_presence_service()
    await presence.load_device_registry(db)
    await presence.push_macs_to_satellites()

    return BLEIrkResponse(
        id=row.id, user_id=row.user_id, label=row.label, is_enabled=row.is_enabled,
        created_at=row.created_at.isoformat() if row.created_at else None,
    )


@router.delete("/irks/{irk_id}", status_code=204)
async def delete_irk(
    irk_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_permission(Permission.ADMIN)),
):
    """Revoke a BLE IRK (deletes it + re-pushes the reduced set to satellites)."""
    from models.database import UserBleIrk
    row = (await db.execute(select(UserBleIrk).where(UserBleIrk.id == irk_id))).scalar_one_or_none()
    if not row:
        raise HTTPException(status_code=404, detail="IRK not found")
    await db.delete(row)
    await db.commit()

    presence = get_presence_service()
    await presence.load_device_registry(db)
    await presence.push_macs_to_satellites()


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


class PresenceEventResponse(BaseModel):
    id: int
    user_id: int
    room_id: int
    room_name: str | None = None
    event_type: str
    source: str | None = None
    confidence: float | None = None
    satellite_id: str | None = None
    created_at: datetime


@router.get("/analytics/heatmap", response_model=list[HeatmapCell])
async def get_heatmap(
    days: int = Query(default=30, ge=1, le=365),
    user_id: int | None = Query(default=None),
    db: AsyncSession = Depends(get_db),
):
    """Room x hour heatmap of enter events."""
    from ha_glue.services.presence_analytics import PresenceAnalyticsService

    service = PresenceAnalyticsService(db)
    return await service.get_heatmap(days=days, user_id=user_id)


@router.get("/analytics/predictions", response_model=list[PredictionEntry])
async def get_predictions(
    user_id: int = Query(...),
    days: int = Query(default=60, ge=7, le=365),
    db: AsyncSession = Depends(get_db),
):
    """Per-user room presence predictions by day-of-week and hour."""
    from ha_glue.services.presence_analytics import PresenceAnalyticsService

    service = PresenceAnalyticsService(db)
    return await service.get_predictions(user_id=user_id, days=days)


@router.get("/analytics/daily", response_model=list[DailySummary])
async def get_daily_summary(
    days: int = Query(default=7, ge=1, le=90),
    db: AsyncSession = Depends(get_db),
):
    """Daily enter/leave event counts."""
    from ha_glue.services.presence_analytics import PresenceAnalyticsService

    service = PresenceAnalyticsService(db)
    return await service.get_daily_summary(days=days)


# --- Persistent presence history (timeline / last-seen / room-window) ---


def _resolve_history_target(user_id: int | None, current_user: "User | None") -> int:
    """Resolve whose presence history to read, guarding against IDOR.

    Self-lookups and single-user mode (``current_user`` None, AUTH off) are
    unrestricted. Reading ANOTHER user's location history requires ROOMS_MANAGE
    — otherwise any ROOMS_READ user could enumerate where anyone in the
    household has been.
    """
    target = user_id if user_id is not None else (current_user.id if current_user else None)
    if target is None:
        raise HTTPException(status_code=400, detail="user_id is required")
    if (
        current_user is not None
        and target != current_user.id
        and not current_user.has_permission(Permission.ROOMS_MANAGE.value)
    ):
        raise HTTPException(
            status_code=403,
            detail="Reading another user's presence history requires ROOMS_MANAGE",
        )
    return target


@router.get("/analytics/timeline", response_model=list[PresenceEventResponse])
async def get_presence_timeline(
    user_id: int | None = Query(default=None),
    since: datetime | None = Query(default=None),
    until: datetime | None = Query(default=None),
    room_id: int | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    db: AsyncSession = Depends(get_db),
    current_user: User | None = Depends(require_permission(Permission.ROOMS_READ)),
):
    """Chronological presence-event timeline for a user.

    Defaults to the authenticated user when ``user_id`` is omitted. In
    single-user mode (AUTH_ENABLED=false) ``current_user`` is None.
    """
    if not ha_glue_settings.presence_history_enabled:
        raise HTTPException(status_code=404, detail="Presence history is disabled")

    target_user_id = _resolve_history_target(user_id, current_user)

    from ha_glue.services.presence_analytics import PresenceAnalyticsService

    service = PresenceAnalyticsService(db)
    return await service.get_timeline(
        user_id=target_user_id,
        since=since,
        until=until,
        room_id=room_id,
        limit=limit,
        offset=offset,
    )


class LastSeenByRoomEntry(BaseModel):
    room_id: int
    room_name: str | None = None
    last_seen: datetime


@router.get("/analytics/last-seen-by-room", response_model=list[LastSeenByRoomEntry])
async def get_last_seen_by_room(
    user_id: int | None = Query(default=None),
    db: AsyncSession = Depends(get_db),
    current_user: User | None = Depends(require_permission(Permission.ROOMS_READ)),
):
    """Per-room most-recent 'enter' time for a user."""
    if not ha_glue_settings.presence_history_enabled:
        raise HTTPException(status_code=404, detail="Presence history is disabled")

    target_user_id = _resolve_history_target(user_id, current_user)

    from ha_glue.services.presence_analytics import PresenceAnalyticsService

    service = PresenceAnalyticsService(db)
    return await service.get_last_seen_by_room(user_id=target_user_id)


@router.get("/analytics/room-window", response_model=list[PresenceEventResponse])
async def get_room_window(
    room_id: int = Query(...),
    since: datetime | None = Query(default=None),
    until: datetime | None = Query(default=None),
    db: AsyncSession = Depends(get_db),
    current_user: User | None = Depends(require_permission(Permission.ROOMS_MANAGE)),
):
    """All users' enter/leave events for a room within a window (admin).

    ``since``/``until`` default to the last 24 hours when omitted.
    """
    if not ha_glue_settings.presence_history_enabled:
        raise HTTPException(status_code=404, detail="Presence history is disabled")

    now = datetime.now(UTC).replace(tzinfo=None)
    if until is None:
        until = now
    if since is None:
        since = until - timedelta(hours=24)

    from ha_glue.services.presence_analytics import PresenceAnalyticsService

    service = PresenceAnalyticsService(db)
    return await service.get_room_occupancy_window(room_id=room_id, since=since, until=until)
