"""
Pydantic schemas for Room Management API

Extracted from rooms.py for better maintainability.
"""
from pydantic import BaseModel
from typing import Optional, List


# --- Room Models ---

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
    # Device counts
    device_count: int = 0
    satellite_count: int = 0
    online_count: int = 0
    # Device lists
    devices: List[dict] = []
    satellites: List[dict] = []  # Legacy compatibility

    class Config:
        from_attributes = True


# --- Home Assistant Models ---

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


# --- Satellite Models ---

class SatelliteAssignRequest(BaseModel):
    satellite_id: str


# --- Device Models ---

class DeviceResponse(BaseModel):
    """Response model for device info"""
    id: int
    device_id: str
    device_type: str
    device_name: Optional[str]
    room_id: int
    room_name: str
    capabilities: dict
    is_online: bool
    is_stationary: bool
    last_connected_at: Optional[str]
    user_agent: Optional[str]
    ip_address: Optional[str]

    class Config:
        from_attributes = True


class DeviceRegisterRequest(BaseModel):
    """Request model for manual device registration"""
    device_id: str
    device_type: str = "web_browser"
    device_name: Optional[str] = None
    capabilities: Optional[dict] = None
    is_stationary: bool = True


class ConnectedDeviceResponse(BaseModel):
    """Response model for currently connected device (from DeviceManager)"""
    device_id: str
    device_type: str
    device_name: Optional[str]
    room: str
    room_id: Optional[int]
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
    renfield_device_id: Optional[str] = None
    ha_entity_id: Optional[str] = None
    priority: int = 1
    allow_interruption: bool = False
    tts_volume: Optional[float] = 0.5
    device_name: Optional[str] = None


class OutputDeviceUpdate(BaseModel):
    """Request model for updating an output device"""
    priority: Optional[int] = None
    allow_interruption: Optional[bool] = None
    tts_volume: Optional[float] = None
    is_enabled: Optional[bool] = None
    device_name: Optional[str] = None


class OutputDeviceResponse(BaseModel):
    """Response model for output device"""
    id: int
    room_id: int
    output_type: str
    renfield_device_id: Optional[str]
    ha_entity_id: Optional[str]
    priority: int
    allow_interruption: bool
    tts_volume: Optional[float]
    device_name: Optional[str]
    is_enabled: bool
    created_at: Optional[str]
    updated_at: Optional[str]

    class Config:
        from_attributes = True


class OutputDeviceReorderRequest(BaseModel):
    """Request model for reordering output devices"""
    device_ids: List[int]


class AvailableOutputResponse(BaseModel):
    """Response model for available output devices (HA + Renfield)"""
    renfield_devices: List[dict]
    ha_media_players: List[dict]
