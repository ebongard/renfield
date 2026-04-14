"""HA-specific SQLAlchemy models extracted from `models/database.py`.

Part of Phase 1 Week 1 of the Renfield open-source extraction. See
`docs/architecture/renfield-platform-boundary.md` in the parent Reva
repo for the full boundary definition.

All classes here use the platform's `Base` and share its metadata so
that:

- `Base.metadata.create_all()` picks up these tables when ha_glue is
  imported, and produces a platform-only schema when it isn't
- Cross-side `relationship("User")` string references (e.g. for the
  Room.owner FK to platform's users table) resolve correctly at
  mapper-configure time
- Foreign keys from ha-glue to platform tables (Room.owner_id → users.id,
  UserBleDevice.user_id → users.id, PresenceEvent.user_id → users.id,
  RadioFavorite.user_id → users.id) work transparently

The reverse direction — platform tables with ForeignKeys pointing INTO
ha-glue tables — was removed from Notification and Reminder in this
same commit. They used to have `ForeignKey("rooms.id")` and
`relationship("Room")`, but those relationships were never actually
read by application code, only declared. Dropping them restores the
layering rule that platform must not hard-depend on ha-glue.

## Scope of this file

Contains nine table classes:

- `CameraEvent` — Frigate camera event log
- `HomeAssistantEntity` — HA entity state cache
- `Room` — room registry (includes HA area_id)
- `RoomDevice` — unified satellite + web device registry (+ DEVICE_TYPE_*
  constants and DEFAULT_CAPABILITIES dict)
- `RoomOutputDevice` — per-room TTS output routing (+ OUTPUT_TYPE_*
  constants)
- `UserBleDevice` — registered BLE devices for presence detection
- `PresenceEvent` — persisted presence enter/leave events
- `PaperlessAuditResult` — LLM audit of paperless-ngx documents
- `RadioFavorite` — user's favorite radio stations

Also re-exports the legacy `RoomSatellite = RoomDevice` alias.
"""

from __future__ import annotations

from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import relationship

from models.database import Base, _utcnow

# Ensure User is registered with Base.metadata before ha_glue classes that
# FK into users.id are defined. Importing the class triggers registration
# as a side effect of the decorator-less declarative mapping.
from models.database import User  # noqa: F401 — side-effect import


# ---------------------------------------------------------------------------
# CameraEvent — Frigate event log
# ---------------------------------------------------------------------------


class CameraEvent(Base):
    """Kamera-Events."""

    __tablename__ = "camera_events"

    id = Column(Integer, primary_key=True, index=True)
    camera_name = Column(String)
    event_type = Column(String)  # 'person', 'car', 'animal'
    confidence = Column(Integer)
    timestamp = Column(DateTime, default=_utcnow)
    snapshot_path = Column(String, nullable=True)
    event_metadata = Column(JSON, nullable=True)
    notified = Column(Boolean, default=False)


# ---------------------------------------------------------------------------
# HomeAssistantEntity — HA state cache
# ---------------------------------------------------------------------------


class HomeAssistantEntity(Base):
    """Home Assistant Entities Cache."""

    __tablename__ = "ha_entities"

    id = Column(Integer, primary_key=True, index=True)
    entity_id = Column(String, unique=True, index=True)
    friendly_name = Column(String)
    domain = Column(String)
    state = Column(String, nullable=True)
    attributes = Column(JSON, nullable=True)
    last_updated = Column(DateTime, default=_utcnow)


# ---------------------------------------------------------------------------
# Room Management
# ---------------------------------------------------------------------------


class Room(Base):
    """Raum für Smart Home und Device-Zuordnung."""

    __tablename__ = "rooms"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(100), nullable=False, unique=True)
    alias = Column(String(50), index=True)

    # Home Assistant Sync
    ha_area_id = Column(String(100), nullable=True, unique=True, index=True)
    source = Column(String(20), default="renfield")

    # Room owner (for Media Follow Me conflict resolution)
    owner_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    owner = relationship("User", foreign_keys="Room.owner_id")

    icon = Column(String(50), nullable=True)

    created_at = Column(DateTime, default=_utcnow)
    updated_at = Column(DateTime, default=_utcnow, onupdate=_utcnow)
    last_synced_at = Column(DateTime, nullable=True)

    devices = relationship(
        "RoomDevice",
        back_populates="room",
        cascade="all, delete-orphan",
    )
    output_devices = relationship(
        "RoomOutputDevice",
        back_populates="room",
        cascade="all, delete-orphan",
        order_by="RoomOutputDevice.priority",
    )

    @property
    def satellites(self):
        """Backward compatibility: get only satellite-type devices."""
        return [d for d in self.devices if d.device_type == "satellite"]

    @property
    def online_devices(self):
        """Get all online devices in this room."""
        return [d for d in self.devices if d.is_online]


# Device Types
DEVICE_TYPE_SATELLITE = "satellite"      # Physical Pi Zero + ReSpeaker
DEVICE_TYPE_WEB_PANEL = "web_panel"      # Stationary web device (wall-mounted iPad)
DEVICE_TYPE_WEB_TABLET = "web_tablet"    # Mobile web device (iPad, tablet)
DEVICE_TYPE_WEB_BROWSER = "web_browser"  # Desktop browser
DEVICE_TYPE_WEB_KIOSK = "web_kiosk"      # Touch kiosk terminal

DEVICE_TYPES = [
    DEVICE_TYPE_SATELLITE,
    DEVICE_TYPE_WEB_PANEL,
    DEVICE_TYPE_WEB_TABLET,
    DEVICE_TYPE_WEB_BROWSER,
    DEVICE_TYPE_WEB_KIOSK,
]


class RoomDevice(Base):
    """Unified device model for room-based input/output devices.

    Supports both physical satellites (Raspberry Pi) and web-based clients
    (iPad, Browser). Capabilities are stored as JSON for flexibility.
    """

    __tablename__ = "room_devices"

    id = Column(Integer, primary_key=True, index=True)
    room_id = Column(Integer, ForeignKey("rooms.id"), nullable=False, index=True)
    device_id = Column(String(100), nullable=False, unique=True, index=True)

    device_type = Column(String(20), nullable=False, default=DEVICE_TYPE_WEB_BROWSER)
    device_name = Column(String(100), nullable=True)

    capabilities = Column(JSON, nullable=False, default=dict)

    is_online = Column(Boolean, default=False)
    is_stationary = Column(Boolean, default=True)
    last_connected_at = Column(DateTime, nullable=True)

    user_agent = Column(String(500), nullable=True)
    ip_address = Column(String(45), nullable=True)

    created_at = Column(DateTime, default=_utcnow)
    updated_at = Column(DateTime, default=_utcnow, onupdate=_utcnow)

    room = relationship("Room", back_populates="devices")

    def has_capability(self, capability: str) -> bool:
        """Check if device has a specific capability."""
        return self.capabilities.get(capability, False)

    @property
    def can_record_audio(self) -> bool:
        return self.has_capability("has_microphone")

    @property
    def can_play_audio(self) -> bool:
        return self.has_capability("has_speaker")

    @property
    def can_show_display(self) -> bool:
        return self.has_capability("has_display")

    @property
    def has_wakeword(self) -> bool:
        return self.has_capability("has_wakeword")


DEFAULT_CAPABILITIES = {
    DEVICE_TYPE_SATELLITE: {
        "has_microphone": True,
        "has_speaker": True,
        "has_wakeword": True,
        "wakeword_method": "openwakeword",
        "has_display": False,
        "has_leds": True,
        "led_count": 3,
        "has_button": True,
    },
    DEVICE_TYPE_WEB_PANEL: {
        "has_microphone": True,
        "has_speaker": True,
        "has_wakeword": True,
        "wakeword_method": "browser_wasm",
        "has_display": True,
        "display_size": "large",
        "supports_notifications": True,
        "has_leds": False,
        "has_button": False,
    },
    DEVICE_TYPE_WEB_TABLET: {
        "has_microphone": True,
        "has_speaker": True,
        "has_wakeword": True,
        "wakeword_method": "browser_wasm",
        "has_display": True,
        "display_size": "medium",
        "supports_notifications": True,
        "has_leds": False,
        "has_button": False,
    },
    DEVICE_TYPE_WEB_BROWSER: {
        "has_microphone": False,
        "has_speaker": False,
        "has_wakeword": False,
        "has_display": True,
        "display_size": "large",
        "supports_notifications": True,
        "has_leds": False,
        "has_button": False,
    },
    DEVICE_TYPE_WEB_KIOSK: {
        "has_microphone": True,
        "has_speaker": True,
        "has_wakeword": False,
        "has_display": True,
        "display_size": "large",
        "supports_notifications": False,
        "has_leds": False,
        "has_button": False,
    },
}


# Output Device Types
OUTPUT_TYPE_AUDIO = "audio"
OUTPUT_TYPE_VISUAL = "visual"

OUTPUT_TYPES = [OUTPUT_TYPE_AUDIO, OUTPUT_TYPE_VISUAL]


class RoomOutputDevice(Base):
    """Output device configuration for a room.

    Defines which devices should be used for TTS audio output in a room,
    with priority ordering and interruption settings.

    Exactly one of renfield_device_id, ha_entity_id, or dlna_renderer_name
    must be set.
    """

    __tablename__ = "room_output_devices"

    id = Column(Integer, primary_key=True, index=True)
    room_id = Column(Integer, ForeignKey("rooms.id"), nullable=False, index=True)

    renfield_device_id = Column(
        String(100), ForeignKey("room_devices.device_id"), nullable=True
    )
    ha_entity_id = Column(String(255), nullable=True)
    dlna_renderer_name = Column(String(255), nullable=True)

    output_type = Column(String(20), nullable=False, default=OUTPUT_TYPE_AUDIO)

    priority = Column(Integer, nullable=False, default=1)

    allow_interruption = Column(Boolean, default=False)

    tts_volume = Column(Float, nullable=True, default=0.5)

    device_name = Column(String(255), nullable=True)

    is_enabled = Column(Boolean, default=True)

    created_at = Column(DateTime, default=_utcnow)
    updated_at = Column(DateTime, default=_utcnow, onupdate=_utcnow)

    room = relationship("Room", back_populates="output_devices")
    renfield_device = relationship("RoomDevice", foreign_keys=[renfield_device_id])

    @property
    def is_renfield_device(self) -> bool:
        return self.renfield_device_id is not None

    @property
    def is_ha_device(self) -> bool:
        return self.ha_entity_id is not None

    @property
    def is_dlna_device(self) -> bool:
        return self.dlna_renderer_name is not None

    @property
    def target_id(self) -> str:
        return self.renfield_device_id or self.ha_entity_id or self.dlna_renderer_name or ""

    @property
    def target_type(self) -> str:
        if self.renfield_device_id:
            return "renfield"
        if self.ha_entity_id:
            return "homeassistant"
        if self.dlna_renderer_name:
            return "dlna"
        return "renfield"


# Legacy alias for backward compatibility (kept next to RoomDevice so the
# import stays local to ha_glue).
RoomSatellite = RoomDevice


# ---------------------------------------------------------------------------
# Presence
# ---------------------------------------------------------------------------


class UserBleDevice(Base):
    """Registered BLE device for room-level presence detection."""

    __tablename__ = "user_ble_devices"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    mac_address = Column(String(17), unique=True, nullable=False, index=True)
    device_name = Column(String(100), nullable=False)
    device_type = Column(String(50), default="phone")
    detection_method = Column(String(20), default="ble")
    is_enabled = Column(Boolean, default=True)
    created_at = Column(DateTime, default=_utcnow)

    user = relationship("User", backref="ble_devices")


class PresenceEvent(Base):
    """Persisted presence event for analytics (heatmap, predictions)."""

    __tablename__ = "presence_events"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    room_id = Column(Integer, ForeignKey("rooms.id"), nullable=False, index=True)
    event_type = Column(String(20), nullable=False)  # "enter" | "leave"
    source = Column(String(20), default="ble")        # "ble" | "voice" | "web"
    confidence = Column(Float, nullable=True)
    created_at = Column(DateTime, default=_utcnow, index=True)

    __table_args__ = (
        Index("ix_presence_events_analytics", "user_id", "room_id", "created_at"),
    )


# ---------------------------------------------------------------------------
# Paperless Document Audit
# ---------------------------------------------------------------------------


class PaperlessAuditResult(Base):
    """Paperless document audit results from LLM analysis."""

    __tablename__ = "paperless_audit_results"

    id = Column(Integer, primary_key=True)
    paperless_doc_id = Column(Integer, index=True, unique=True)

    current_title = Column(String, nullable=True)
    current_correspondent = Column(String, nullable=True)
    current_document_type = Column(String, nullable=True)
    current_tags = Column(JSON, nullable=True)

    suggested_title = Column(String, nullable=True)
    suggested_correspondent = Column(String, nullable=True)
    suggested_document_type = Column(String, nullable=True)
    suggested_tags = Column(JSON, nullable=True)

    current_date = Column(String, nullable=True)
    suggested_date = Column(String, nullable=True)

    missing_fields = Column(JSON, nullable=True)

    duplicate_group_id = Column(String, nullable=True, index=True)
    duplicate_score = Column(Float, nullable=True)

    current_custom_fields = Column(JSON, nullable=True)
    suggested_custom_fields = Column(JSON, nullable=True)

    detected_language = Column(String(10), nullable=True)

    current_storage_path = Column(String, nullable=True)
    suggested_storage_path = Column(String, nullable=True)

    content_completeness = Column(Integer, nullable=True)
    completeness_issues = Column(String, nullable=True)

    content_hash = Column(String(32), nullable=True)

    ocr_quality = Column(Integer, nullable=True)
    ocr_issues = Column(String, nullable=True)
    confidence = Column(Float, nullable=True)
    changes_needed = Column(Boolean, default=False)
    reasoning = Column(Text, nullable=True)

    status = Column(String, default="pending")

    renfield_ocr_text = Column(Text, nullable=True)

    audited_at = Column(DateTime, default=_utcnow)
    applied_at = Column(DateTime, nullable=True)
    audit_run_id = Column(String, nullable=True, index=True)


# ---------------------------------------------------------------------------
# Radio Favorites
# ---------------------------------------------------------------------------


class RadioFavorite(Base):
    """User's favorite radio stations (provider-agnostic, currently TuneIn)."""

    __tablename__ = "radio_favorites"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    station_id = Column(String(50), nullable=False)
    station_name = Column(String(255), nullable=False)
    station_image = Column(String(512), nullable=True)
    genre = Column(String(100), nullable=True)
    created_at = Column(DateTime, default=_utcnow)

    __table_args__ = (
        Index("ix_radio_favorites_user_station", "user_id", "station_id", unique=True),
    )


__all__ = [
    # Tables
    "CameraEvent",
    "HomeAssistantEntity",
    "Room",
    "RoomDevice",
    "RoomOutputDevice",
    "RoomSatellite",
    "UserBleDevice",
    "PresenceEvent",
    "PaperlessAuditResult",
    "RadioFavorite",
    # Device type constants
    "DEVICE_TYPE_SATELLITE",
    "DEVICE_TYPE_WEB_PANEL",
    "DEVICE_TYPE_WEB_TABLET",
    "DEVICE_TYPE_WEB_BROWSER",
    "DEVICE_TYPE_WEB_KIOSK",
    "DEVICE_TYPES",
    "DEFAULT_CAPABILITIES",
    # Output type constants
    "OUTPUT_TYPE_AUDIO",
    "OUTPUT_TYPE_VISUAL",
    "OUTPUT_TYPES",
]
