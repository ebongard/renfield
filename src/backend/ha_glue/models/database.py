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

    The output target is the generic ``(output_provider, output_target_id)``
    pair — ``output_provider`` IS the ``target_type`` value space
    (``renfield`` | ``homeassistant`` | ``dlna`` | ``samsung`` | ``sonos`` | …)
    and ``output_target_id`` is the provider-scoped id (device_id / HA entity id
    / DLNA renderer name / TV host / …). See docs/design/output-providers.md.

    The three legacy brand-identity columns (``renfield_device_id`` /
    ``ha_entity_id`` / ``dlna_renderer_name``) were dropped in migration
    ``pc20260617b_drop_outlegacy`` after the additive pair soaked in prod; all
    reads now go through this pair.
    """

    __tablename__ = "room_output_devices"

    id = Column(Integer, primary_key=True, index=True)
    room_id = Column(Integer, ForeignKey("rooms.id"), nullable=False, index=True)

    # Generic output-provider pair (docs/design/output-providers.md). The sole
    # target-identity columns since the legacy brand columns were dropped.
    output_provider = Column(String(50), nullable=True)
    output_target_id = Column(String(255), nullable=True)

    output_type = Column(String(20), nullable=False, default=OUTPUT_TYPE_AUDIO)

    priority = Column(Integer, nullable=False, default=1)

    allow_interruption = Column(Boolean, default=False)

    tts_volume = Column(Float, nullable=True, default=0.5)

    device_name = Column(String(255), nullable=True)

    is_enabled = Column(Boolean, default=True)

    created_at = Column(DateTime, default=_utcnow)
    updated_at = Column(DateTime, default=_utcnow, onupdate=_utcnow)

    room = relationship("Room", back_populates="output_devices")

    @property
    def is_renfield_device(self) -> bool:
        return self.output_provider == "renfield"

    @property
    def is_ha_device(self) -> bool:
        return self.output_provider == "homeassistant"

    @property
    def is_dlna_device(self) -> bool:
        return self.output_provider == "dlna"

    @property
    def target_id(self) -> str:
        return self.output_target_id or ""

    @property
    def target_type(self) -> str:
        # output_provider IS the target_type value space. Default to renfield
        # for a malformed/empty row (mirrors the pre-cleanup behavior).
        return self.output_provider or "renfield"


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


class UserBleIrk(Base):
    """Per-person BLE Identity Resolving Key for presence.

    Modern phones (iPhone/Android) advertise with rotating Resolvable Private
    Addresses, so a static MAC whitelist can't track them. The IRK — obtained
    out-of-band once (iPhone: Mac/iCloud keychain or a one-time bond to a
    satellite; Android: bonded-device info) — lets a satellite resolve the
    rotating address back to this stable `label`. See
    docs/design/ble-presence-improvement.md.

    The IRK is a device-tracking secret: stored ENCRYPTED at rest
    (services/secret_encryption) and pushed to satellites over the WS link only.
    """

    __tablename__ = "user_ble_irks"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    # Globally-unique stable identity the satellite reports on a resolved match;
    # the backend maps it back to this user for presence attribution.
    label = Column(String(100), unique=True, nullable=False, index=True)
    # Fernet token of the 32-hex-char IRK — never stored or logged in plaintext.
    irk_encrypted = Column(String(255), nullable=False)
    is_enabled = Column(Boolean, default=True)
    created_at = Column(DateTime, default=_utcnow)
    updated_at = Column(DateTime, default=_utcnow, onupdate=_utcnow)

    user = relationship("User", backref="ble_irks")


class Satellite(Base):
    """Per-satellite enrollment credential (PSK) for the /ws/satellite path.

    Closes the assertion-based-trust hole (security review H1): a satellite
    today *claims* a ``satellite_id`` in its register frame with no proof, so
    any LAN device can register as ``sat-wohnzimmer``, evict the incumbent, and
    harvest the IRK push. Each enrolled satellite holds a random 256-bit token,
    stored here ONLY as a bcrypt hash; the satellite presents the plaintext in
    the register frame's ``token`` field and the backend verifies it
    constant-time. See docs/private/security/satellite-trust-design.md.

    The plaintext token is shown exactly once (at enrollment) and is never
    stored or returned afterwards — same posture as the folder/email ingest
    push tokens.
    """

    __tablename__ = "satellites"

    id = Column(Integer, primary_key=True, index=True)
    # The asserted identity the satellite registers under; the token is bound
    # to this id, so a register frame whose satellite_id ≠ the token's id fails.
    satellite_id = Column(String(100), unique=True, nullable=False, index=True)
    # bcrypt hash of the enrollment PSK — never the plaintext.
    token_hash = Column(String(255), nullable=False)
    # Cosmetic; the runtime room still comes from the register frame.
    room = Column(String(100), nullable=True)
    # Audit: which admin enrolled it (NULL for Ansible/bin-script seeding).
    enrolled_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    enrolled_at = Column(DateTime, default=_utcnow)
    # Set on every successful PSK verification — drives the auto-flip readiness
    # check (the fleet only enforces once EVERY enrolled row has connected once).
    last_authenticated_at = Column(DateTime, nullable=True)
    # Revocation: a revoked or disabled row never authenticates.
    revoked_at = Column(DateTime, nullable=True)
    is_enabled = Column(Boolean, default=True, nullable=False)

    enrolled_by = relationship("User")


class SatelliteFleetState(Base):
    """Singleton row holding the enrollment-enforcement latch.

    Auto-flip (security review decision ③) transitions the fleet from PERMISSIVE
    to ENFORCING once every enrolled satellite has authenticated at least once.
    The transition is LATCHED here (``enrollment_enforced_at`` set once, never
    cleared automatically) so a later UI-enrolled-but-not-yet-connected
    satellite — whose row has a NULL ``last_authenticated_at`` — cannot silently
    re-open the fleet to unauthenticated registration. Break-glass: clear the
    latch (or flip ``satellite_enrollment_enabled=False``).
    """

    __tablename__ = "satellite_fleet_state"

    # Always id=1 — enforced by the service (single fleet per backend).
    id = Column(Integer, primary_key=True)
    enrollment_enforced_at = Column(DateTime, nullable=True)
    updated_at = Column(DateTime, default=_utcnow, onupdate=_utcnow)


class PresenceEvent(Base):
    """Persisted presence event for analytics (heatmap, predictions)."""

    __tablename__ = "presence_events"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    room_id = Column(Integer, ForeignKey("rooms.id"), nullable=False, index=True)
    event_type = Column(String(20), nullable=False)  # "enter" | "leave"
    source = Column(String(20), default="ble")        # "ble" | "voice" | "web"
    confidence = Column(Float, nullable=True)
    satellite_id = Column(String(100), nullable=True)  # satellite producing the enter detection (NULL on leave/voice/web)
    created_at = Column(DateTime, default=_utcnow, index=True)

    __table_args__ = (
        Index("ix_presence_events_analytics", "user_id", "room_id", "created_at"),
        Index("ix_presence_events_history", "user_id", "created_at"),
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

    # User review overlay: manual edits to the suggested values + a per-field
    # apply selection. Both NULL = no review → apply uses ALL suggested_* changes
    # (legacy behavior, byte-identical). See PaperlessAuditService.EDITABLE_FIELDS
    # for the canonical field names used as keys/entries here.
    user_overrides = Column(JSON, nullable=True)   # {field: edited_value}
    field_selection = Column(JSON, nullable=True)  # [field, ...] to apply; NULL = all changed

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
