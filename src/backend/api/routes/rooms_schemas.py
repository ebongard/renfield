"""
Pydantic schemas for Room Management API

Extracted from rooms.py for better maintainability.
"""

from pydantic import BaseModel

# --- Room Models ---

class RoomCreate(BaseModel):
    name: str
    icon: str | None = None
    ha_area_id: str | None = None


class RoomUpdate(BaseModel):
    name: str | None = None
    icon: str | None = None


class RoomResponse(BaseModel):
    id: int
    name: str
    alias: str
    ha_area_id: str | None
    source: str
    icon: str | None
    created_at: str | None
    updated_at: str | None
    last_synced_at: str | None
    # Device counts
    device_count: int = 0
    satellite_count: int = 0
    online_count: int = 0
    # Device lists
    devices: list[dict] = []
    satellites: list[dict] = []  # Legacy compatibility

    class Config:
        from_attributes = True


# --- Home Assistant Models ---

class HAAreaResponse(BaseModel):
    area_id: str
    name: str
    icon: str | None
    is_linked: bool
    linked_room_id: int | None
    linked_room_name: str | None


class HAImportRequest(BaseModel):
    conflict_resolution: str = "skip"  # skip, link, overwrite


class HAImportResponse(BaseModel):
    imported: int
    linked: int
    skipped: int
    errors: list[str]


class HAExportResponse(BaseModel):
    exported: int
    linked: int
    errors: list[str]


class SyncResponse(BaseModel):
    import_results: HAImportResponse
    export_results: HAExportResponse


class LinkHAAreaRequest(BaseModel):
    ha_area_id: str


# --- Satellite Models ---

class SatelliteAssignRequest(BaseModel):
    satellite_id: str


# --- Device Models ---

class DeviceResponse(BaseModel):
    """Response model for device info"""
    id: int
    device_id: str
    device_type: str
    device_name: str | None
    room_id: int
    room_name: str
    capabilities: dict
    is_online: bool
    is_stationary: bool
    last_connected_at: str | None
    user_agent: str | None
    ip_address: str | None

    class Config:
        from_attributes = True


class DeviceRegisterRequest(BaseModel):
    """Request model for manual device registration"""
    device_id: str
    device_type: str = "web_browser"
    device_name: str | None = None
    capabilities: dict | None = None
    is_stationary: bool = True


class ConnectedDeviceResponse(BaseModel):
    """Response model for currently connected device (from DeviceManager)"""
    device_id: str
    device_type: str
    device_name: str | None
    room: str
    room_id: int | None
    state: str
    connected_at: float
    last_heartbeat: float
    has_active_session: bool
    is_stationary: bool
    capabilities: dict


# --- Output Device Models ---

class OutputDeviceCreate(BaseModel):
    """Request model for creating an output device"""
    output_type: str = "audio"  # "audio" or "visual"
    renfield_device_id: str | None = None
    ha_entity_id: str | None = None
    priority: int = 1
    allow_interruption: bool = False
    tts_volume: float | None = 0.5
    device_name: str | None = None


class OutputDeviceUpdate(BaseModel):
    """Request model for updating an output device"""
    priority: int | None = None
    allow_interruption: bool | None = None
    tts_volume: float | None = None
    is_enabled: bool | None = None
    device_name: str | None = None


class OutputDeviceResponse(BaseModel):
    """Response model for output device"""
    id: int
    room_id: int
    output_type: str
    renfield_device_id: str | None
    ha_entity_id: str | None
    priority: int
    allow_interruption: bool
    tts_volume: float | None
    device_name: str | None
    is_enabled: bool
    created_at: str | None
    updated_at: str | None

    class Config:
        from_attributes = True


class OutputDeviceReorderRequest(BaseModel):
    """Request model for reordering output devices"""
    device_ids: list[int]


class AvailableOutputResponse(BaseModel):
    """Response model for available output devices (HA + Renfield)"""
    renfield_devices: list[dict]
    ha_media_players: list[dict]
