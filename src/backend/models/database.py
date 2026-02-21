"""
Datenbank Models
"""
from datetime import UTC, datetime

from sqlalchemy import JSON, Boolean, Column, DateTime, Float, ForeignKey, Index, Integer, String, Text
from sqlalchemy.dialects.postgresql import TSVECTOR
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import relationship

from utils.config import settings

try:
    from pgvector.sqlalchemy import Vector
    PGVECTOR_AVAILABLE = True
except ImportError:
    # Fallback für Tests ohne pgvector
    PGVECTOR_AVAILABLE = False
    Vector = None

Base = declarative_base()


def _utcnow():
    """Return current UTC time as naive datetime (DB compat, replaces deprecated utcnow)."""
    return datetime.now(UTC).replace(tzinfo=None)


class Conversation(Base):
    """Konversationen / Chat-Historie"""
    __tablename__ = "conversations"

    id = Column(Integer, primary_key=True, index=True)
    session_id = Column(String, unique=True, index=True)
    created_at = Column(DateTime, default=_utcnow)
    updated_at = Column(DateTime, default=_utcnow, onupdate=_utcnow)

    # Ownership (nullable for anonymous/legacy conversations)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)

    # Beziehungen
    messages = relationship("Message", back_populates="conversation", cascade="all, delete-orphan")
    user = relationship("User", back_populates="conversations", foreign_keys=[user_id])

class Message(Base):
    """Einzelne Nachrichten"""
    __tablename__ = "messages"

    id = Column(Integer, primary_key=True, index=True)
    conversation_id = Column(Integer, ForeignKey("conversations.id"), index=True)
    role = Column(String)  # 'user' oder 'assistant'
    content = Column(Text)
    timestamp = Column(DateTime, default=_utcnow)
    message_metadata = Column(JSON, nullable=True)  # Umbenannt von 'metadata'

    # Beziehungen
    conversation = relationship("Conversation", back_populates="messages")

class Task(Base):
    """Aufgaben"""
    __tablename__ = "tasks"

    id = Column(Integer, primary_key=True, index=True)
    title = Column(String)
    description = Column(Text, nullable=True)
    task_type = Column(String)  # 'homeassistant', 'n8n', 'research', 'camera'
    status = Column(String, default="pending")  # pending, running, completed, failed
    priority = Column(Integer, default=0)
    parameters = Column(JSON)
    result = Column(JSON, nullable=True)
    error = Column(Text, nullable=True)
    created_at = Column(DateTime, default=_utcnow)
    completed_at = Column(DateTime, nullable=True)
    created_by = Column(String, nullable=True)

class CameraEvent(Base):
    """Kamera-Events"""
    __tablename__ = "camera_events"

    id = Column(Integer, primary_key=True, index=True)
    camera_name = Column(String)
    event_type = Column(String)  # 'person', 'car', 'animal'
    confidence = Column(Integer)
    timestamp = Column(DateTime, default=_utcnow)
    snapshot_path = Column(String, nullable=True)
    event_metadata = Column(JSON, nullable=True)  # Umbenannt von 'metadata'
    notified = Column(Boolean, default=False)

class HomeAssistantEntity(Base):
    """Home Assistant Entities Cache"""
    __tablename__ = "ha_entities"

    id = Column(Integer, primary_key=True, index=True)
    entity_id = Column(String, unique=True, index=True)
    friendly_name = Column(String)
    domain = Column(String)
    state = Column(String, nullable=True)
    attributes = Column(JSON, nullable=True)
    last_updated = Column(DateTime, default=_utcnow)


# --- Speaker Recognition Models ---

class Speaker(Base):
    """Registrierter Sprecher für Speaker Recognition"""
    __tablename__ = "speakers"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(100), nullable=False)       # "Max Mustermann"
    alias = Column(String(50), unique=True, index=True)  # "max" (für Ansprache)
    is_admin = Column(Boolean, default=False)        # Admin-Berechtigung (legacy, use User.role)
    created_at = Column(DateTime, default=_utcnow)
    updated_at = Column(DateTime, default=_utcnow, onupdate=_utcnow)

    # Beziehungen
    embeddings = relationship("SpeakerEmbedding", back_populates="speaker", cascade="all, delete-orphan")

    # Link to User account (for voice authentication)
    user = relationship("User", back_populates="speaker", uselist=False, foreign_keys="User.speaker_id")


class SpeakerEmbedding(Base):
    """Voice Embedding für einen Sprecher (mehrere pro Speaker für bessere Erkennung)"""
    __tablename__ = "speaker_embeddings"

    id = Column(Integer, primary_key=True, index=True)
    speaker_id = Column(Integer, ForeignKey("speakers.id"), nullable=False, index=True)
    embedding = Column(Text, nullable=False)         # Base64-encoded numpy array
    sample_duration = Column(Integer, nullable=True)  # Dauer des Samples in Millisekunden
    created_at = Column(DateTime, default=_utcnow)

    # Beziehungen
    speaker = relationship("Speaker", back_populates="embeddings")


# --- Room Management Models ---

class Room(Base):
    """Raum für Smart Home und Device-Zuordnung"""
    __tablename__ = "rooms"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(100), nullable=False, unique=True)  # "Wohnzimmer"
    alias = Column(String(50), index=True)  # "wohnzimmer" (für Sprachbefehle, normalisiert)

    # Home Assistant Sync
    ha_area_id = Column(String(100), nullable=True, unique=True, index=True)
    source = Column(String(20), default="renfield")  # renfield/homeassistant/satellite/device

    # Metadata
    icon = Column(String(50), nullable=True)  # "mdi:sofa"

    # Timestamps
    created_at = Column(DateTime, default=_utcnow)
    updated_at = Column(DateTime, default=_utcnow, onupdate=_utcnow)
    last_synced_at = Column(DateTime, nullable=True)

    # Beziehungen
    devices = relationship("RoomDevice", back_populates="room", cascade="all, delete-orphan")
    output_devices = relationship(
        "RoomOutputDevice",
        back_populates="room",
        cascade="all, delete-orphan",
        order_by="RoomOutputDevice.priority"
    )

    @property
    def satellites(self):
        """Backward compatibility: Get only satellite-type devices"""
        return [d for d in self.devices if d.device_type == "satellite"]

    @property
    def online_devices(self):
        """Get all online devices in this room"""
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
    """
    Unified Device Model for Room-based Input/Output Devices

    Supports both physical satellites (Raspberry Pi) and web-based clients (iPad, Browser).
    Capabilities are stored as JSON for flexibility.
    """
    __tablename__ = "room_devices"

    id = Column(Integer, primary_key=True, index=True)
    room_id = Column(Integer, ForeignKey("rooms.id"), nullable=False, index=True)
    device_id = Column(String(100), nullable=False, unique=True, index=True)

    # Device Classification
    device_type = Column(String(20), nullable=False, default=DEVICE_TYPE_WEB_BROWSER)
    device_name = Column(String(100), nullable=True)  # User-friendly name: "iPad Wohnzimmer"

    # Capabilities (JSON for flexibility)
    # Example: {"has_microphone": true, "has_speaker": true, "has_display": true, ...}
    capabilities = Column(JSON, nullable=False, default=dict)

    # Status
    is_online = Column(Boolean, default=False)
    is_stationary = Column(Boolean, default=True)  # Stationary vs. mobile device
    last_connected_at = Column(DateTime, nullable=True)

    # Connection Info
    user_agent = Column(String(500), nullable=True)  # Browser/client info
    ip_address = Column(String(45), nullable=True)   # IPv4 or IPv6

    # Timestamps
    created_at = Column(DateTime, default=_utcnow)
    updated_at = Column(DateTime, default=_utcnow, onupdate=_utcnow)

    # Beziehungen
    room = relationship("Room", back_populates="devices")

    def has_capability(self, capability: str) -> bool:
        """Check if device has a specific capability"""
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


# Default capabilities for different device types
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
        "has_microphone": False,  # May need permission
        "has_speaker": False,     # May need permission
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
    """
    Output device configuration for a room.

    Defines which devices should be used for TTS audio output
    in a room, with priority ordering and interruption settings.

    Exactly one of renfield_device_id, ha_entity_id, or dlna_renderer_name must be set.
    """
    __tablename__ = "room_output_devices"

    id = Column(Integer, primary_key=True, index=True)
    room_id = Column(Integer, ForeignKey("rooms.id"), nullable=False, index=True)

    # Device source: exactly one of Renfield device, HA entity, or DLNA renderer
    renfield_device_id = Column(String(100), ForeignKey("room_devices.device_id"), nullable=True)
    ha_entity_id = Column(String(255), nullable=True)  # e.g. "media_player.linn_dsm"
    dlna_renderer_name = Column(String(255), nullable=True)  # e.g. "Arbeitszimmer"

    # Output type
    output_type = Column(String(20), nullable=False, default=OUTPUT_TYPE_AUDIO)

    # Priority (1 = highest)
    priority = Column(Integer, nullable=False, default=1)

    # Interruption setting
    allow_interruption = Column(Boolean, default=False)

    # Volume setting (0.0 - 1.0, None = no change)
    tts_volume = Column(Float, nullable=True, default=0.5)

    # Device name (cached for display)
    device_name = Column(String(255), nullable=True)

    # Status
    is_enabled = Column(Boolean, default=True)

    # Timestamps
    created_at = Column(DateTime, default=_utcnow)
    updated_at = Column(DateTime, default=_utcnow, onupdate=_utcnow)

    # Relationships
    room = relationship("Room", back_populates="output_devices")
    renfield_device = relationship("RoomDevice", foreign_keys=[renfield_device_id])

    @property
    def is_renfield_device(self) -> bool:
        """Check if this output uses a Renfield device"""
        return self.renfield_device_id is not None

    @property
    def is_ha_device(self) -> bool:
        """Check if this output uses a Home Assistant entity"""
        return self.ha_entity_id is not None

    @property
    def is_dlna_device(self) -> bool:
        """Check if this output uses a DLNA renderer"""
        return self.dlna_renderer_name is not None

    @property
    def target_id(self) -> str:
        """Get the target device/entity ID"""
        return self.renfield_device_id or self.ha_entity_id or self.dlna_renderer_name or ""

    @property
    def target_type(self) -> str:
        """Get the device type string"""
        if self.renfield_device_id:
            return "renfield"
        if self.ha_entity_id:
            return "homeassistant"
        if self.dlna_renderer_name:
            return "dlna"
        return "renfield"


# Legacy alias for backward compatibility
RoomSatellite = RoomDevice


# =============================================================================
# RAG (Retrieval-Augmented Generation) Models
# =============================================================================

class KnowledgeBase(Base):
    """Gruppierung von Dokumenten für RAG"""
    __tablename__ = "knowledge_bases"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(255), nullable=False, unique=True)
    description = Column(Text, nullable=True)
    is_active = Column(Boolean, default=True)

    # Ownership (nullable for legacy KBs)
    owner_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)

    # Public KBs are visible to all users with at least kb.shared permission
    is_public = Column(Boolean, default=False, nullable=False)

    # Timestamps
    created_at = Column(DateTime, default=_utcnow)
    updated_at = Column(DateTime, default=_utcnow, onupdate=_utcnow)

    # Beziehungen
    documents = relationship("Document", back_populates="knowledge_base", cascade="all, delete-orphan")
    owner = relationship("User", back_populates="knowledge_bases", foreign_keys=[owner_id])
    permissions = relationship("KBPermission", back_populates="knowledge_base", cascade="all, delete-orphan")


class Document(Base):
    """Hochgeladene Dokumente (Metadaten)"""
    __tablename__ = "documents"

    id = Column(Integer, primary_key=True, index=True)
    knowledge_base_id = Column(Integer, ForeignKey("knowledge_bases.id"), nullable=True, index=True)

    # File Info
    filename = Column(String(255), nullable=False)
    file_path = Column(String(512), nullable=False)
    file_type = Column(String(50))  # pdf, docx, txt, etc.
    file_size = Column(Integer)     # in bytes
    file_hash = Column(String(64), nullable=True, index=True)  # SHA256 hash for duplicate detection

    # Processing Status
    status = Column(String(50), default="pending", index=True)  # pending, processing, completed, failed
    error_message = Column(Text, nullable=True)

    # Metadata (extrahiert aus Dokument)
    title = Column(String(512), nullable=True)
    author = Column(String(255), nullable=True)
    page_count = Column(Integer, nullable=True)
    chunk_count = Column(Integer, default=0)

    # Timestamps
    created_at = Column(DateTime, default=_utcnow)
    processed_at = Column(DateTime, nullable=True)

    # Beziehungen
    knowledge_base = relationship("KnowledgeBase", back_populates="documents")
    chunks = relationship("DocumentChunk", back_populates="document", cascade="all, delete-orphan")


# Document Chunk Embedding Dimension (configurable, default: nomic-embed-text = 768)
EMBEDDING_DIMENSION = settings.embedding_dimension


class DocumentChunk(Base):
    """
    Text-Chunks mit Embedding-Vektor für RAG

    Jedes Dokument wird in kleinere Chunks aufgeteilt,
    die einzeln in der Vektordatenbank indexiert werden.
    """
    __tablename__ = "document_chunks"

    id = Column(Integer, primary_key=True, index=True)
    document_id = Column(Integer, ForeignKey("documents.id"), nullable=False, index=True)

    # Content
    content = Column(Text, nullable=False)

    # Embedding Vector (768 dimensions for nomic-embed-text)
    # Uses pgvector extension for vector similarity search
    embedding = Column(
        Vector(EMBEDDING_DIMENSION) if PGVECTOR_AVAILABLE else Text,
        nullable=True
    )

    # Chunk Metadata
    chunk_index = Column(Integer)           # Position im Dokument (0-basiert)
    page_number = Column(Integer, nullable=True)
    section_title = Column(String(512), nullable=True)
    chunk_type = Column(String(50), default="paragraph")  # paragraph, table, code, formula, etc.

    # Full-text search vector (populated during ingestion via to_tsvector())
    search_vector = Column(TSVECTOR, nullable=True)

    # Additional Metadata (JSON für Flexibilität)
    chunk_metadata = Column(JSON, nullable=True)  # Umbenannt von 'metadata' (SQLAlchemy reserved)

    # Timestamps
    created_at = Column(DateTime, default=_utcnow)

    # Beziehungen
    document = relationship("Document", back_populates="chunks")

    # Index für Vektor-Suche (wird bei Migration erstellt)
    # CREATE INDEX idx_document_chunks_embedding ON document_chunks
    # USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);


# =============================================================================
# Chat Upload Model
# =============================================================================

# Upload Status Constants
UPLOAD_STATUS_PROCESSING = "processing"
UPLOAD_STATUS_COMPLETED = "completed"
UPLOAD_STATUS_FAILED = "failed"

UPLOAD_STATUSES = [UPLOAD_STATUS_PROCESSING, UPLOAD_STATUS_COMPLETED, UPLOAD_STATUS_FAILED]


class ChatUpload(Base):
    """Dokument-Upload direkt im Chat (ohne RAG-Indexierung)"""
    __tablename__ = "chat_uploads"

    id = Column(Integer, primary_key=True, index=True)
    session_id = Column(String(128), nullable=False, index=True)
    document_id = Column(Integer, ForeignKey("documents.id"), nullable=True)
    filename = Column(String(255), nullable=False)
    file_type = Column(String(50))
    file_size = Column(Integer)
    file_hash = Column(String(64), nullable=True, index=True)
    extracted_text = Column(Text, nullable=True)
    status = Column(String(50), default=UPLOAD_STATUS_PROCESSING, index=True)
    error_message = Column(Text, nullable=True)
    knowledge_base_id = Column(Integer, ForeignKey("knowledge_bases.id"), nullable=True)
    file_path = Column(String(512), nullable=True)
    created_at = Column(DateTime, default=_utcnow)


# Document Processing Status Constants
DOC_STATUS_PENDING = "pending"
DOC_STATUS_PROCESSING = "processing"
DOC_STATUS_COMPLETED = "completed"
DOC_STATUS_FAILED = "failed"

DOC_STATUSES = [DOC_STATUS_PENDING, DOC_STATUS_PROCESSING, DOC_STATUS_COMPLETED, DOC_STATUS_FAILED]


# Chunk Type Constants
CHUNK_TYPE_PARAGRAPH = "paragraph"
CHUNK_TYPE_TABLE = "table"
CHUNK_TYPE_CODE = "code"
CHUNK_TYPE_FORMULA = "formula"
CHUNK_TYPE_HEADING = "heading"
CHUNK_TYPE_LIST = "list"
CHUNK_TYPE_IMAGE_CAPTION = "image_caption"

CHUNK_TYPES = [
    CHUNK_TYPE_PARAGRAPH,
    CHUNK_TYPE_TABLE,
    CHUNK_TYPE_CODE,
    CHUNK_TYPE_FORMULA,
    CHUNK_TYPE_HEADING,
    CHUNK_TYPE_LIST,
    CHUNK_TYPE_IMAGE_CAPTION,
]


# =============================================================================
# Authentication & Authorization Models (RPBAC)
# =============================================================================

class Role(Base):
    """
    User role with associated permissions.

    Roles define a set of permissions that can be assigned to users.
    System roles (is_system=True) cannot be deleted.
    """
    __tablename__ = "roles"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(50), unique=True, nullable=False, index=True)
    description = Column(String(255), nullable=True)

    # Permissions as JSON array of permission strings
    # Example: ["ha.full", "kb.shared", "cam.view", "chat.own"]
    permissions = Column(JSON, default=list, nullable=False)

    # System roles cannot be deleted
    is_system = Column(Boolean, default=False, nullable=False)

    # Timestamps
    created_at = Column(DateTime, default=_utcnow)
    updated_at = Column(DateTime, default=_utcnow, onupdate=_utcnow)

    # Relationships
    users = relationship("User", back_populates="role")

    def has_permission(self, permission: str) -> bool:
        """Check if this role has a specific permission."""
        from models.permissions import Permission, has_permission
        try:
            perm = Permission(permission)
            return has_permission(self.permissions or [], perm)
        except ValueError:
            return permission in (self.permissions or [])



class User(Base):
    """
    User account for authentication and authorization.

    Users are assigned a role which determines their permissions.
    Users can optionally be linked to a Speaker for voice authentication.
    """
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    username = Column(String(100), unique=True, nullable=False, index=True)
    first_name = Column(String(100), nullable=True)
    last_name = Column(String(100), nullable=True)
    email = Column(String(255), unique=True, nullable=True, index=True)
    password_hash = Column(String(255), nullable=False)

    # Role assignment
    role_id = Column(Integer, ForeignKey("roles.id"), nullable=False, index=True)

    # Account status
    is_active = Column(Boolean, default=True, nullable=False)
    must_change_password = Column(Boolean, default=False, nullable=False, server_default="false")

    # User preferences
    preferred_language = Column(String(10), default="de", nullable=False)

    # Optional link to Speaker for voice authentication
    speaker_id = Column(Integer, ForeignKey("speakers.id"), nullable=True, unique=True)

    # Timestamps
    created_at = Column(DateTime, default=_utcnow)
    updated_at = Column(DateTime, default=_utcnow, onupdate=_utcnow)
    last_login = Column(DateTime, nullable=True)

    # Relationships
    role = relationship("Role", back_populates="users")
    speaker = relationship("Speaker", back_populates="user", foreign_keys=[speaker_id])

    # Owned resources (will be added as relationships are defined)
    knowledge_bases = relationship("KnowledgeBase", back_populates="owner", foreign_keys="KnowledgeBase.owner_id")
    conversations = relationship("Conversation", back_populates="user", foreign_keys="Conversation.user_id")

    def has_permission(self, permission: str) -> bool:
        """Check if this user has a specific permission via their role."""
        if not self.role:
            return False
        return self.role.has_permission(permission)

    def get_permissions(self) -> list:
        """Get all permissions for this user."""
        if not self.role:
            return []
        return self.role.permissions or []



# =============================================================================
# Knowledge Base Permissions (for sharing)
# =============================================================================

class KBPermission(Base):
    """
    Per-user permission for a specific Knowledge Base.

    Allows sharing knowledge bases with specific users at different
    permission levels (read, write, admin).
    """
    __tablename__ = "kb_permissions"

    id = Column(Integer, primary_key=True, index=True)
    knowledge_base_id = Column(Integer, ForeignKey("knowledge_bases.id"), nullable=False, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)

    # Permission level: read, write, admin
    permission = Column(String(20), nullable=False, default="read")

    # Who granted this permission
    granted_by = Column(Integer, ForeignKey("users.id"), nullable=True)

    # Timestamps
    created_at = Column(DateTime, default=_utcnow)

    # Unique constraint: one permission entry per user per KB
    __table_args__ = (
        Index('idx_kb_permissions_kb_user', 'knowledge_base_id', 'user_id', unique=True),
    )

    # Relationships
    knowledge_base = relationship("KnowledgeBase", back_populates="permissions")
    user = relationship("User", foreign_keys=[user_id])
    granter = relationship("User", foreign_keys=[granted_by])


# KB Permission Levels
KB_PERM_READ = "read"      # Can view and use in RAG
KB_PERM_WRITE = "write"    # Can add/edit documents
KB_PERM_ADMIN = "admin"    # Can delete, share with others

KB_PERMISSION_LEVELS = [KB_PERM_READ, KB_PERM_WRITE, KB_PERM_ADMIN]


# =============================================================================
# System Settings (Key-Value Store)
# =============================================================================

class SystemSetting(Base):
    """
    Key-Value Store for runtime system settings.

    Used for settings that can be changed at runtime without restarting
    the server, like wake word configuration.

    Keys follow a namespace pattern: "category.setting_name"
    Values are stored as JSON strings for type flexibility.
    """
    __tablename__ = "system_settings"

    key = Column(String(100), primary_key=True)
    value = Column(Text, nullable=False)  # JSON-encoded value
    updated_at = Column(DateTime, default=_utcnow, onupdate=_utcnow)
    updated_by = Column(Integer, ForeignKey("users.id"), nullable=True)

    # Relationship to user who last updated
    updater = relationship("User", foreign_keys=[updated_by])


# ==========================================================================
# Intent Correction Feedback
# ==========================================================================

class IntentCorrection(Base):
    """
    Stores user corrections for wrong intent classifications, agent tool choices,
    and complexity detection. Embeddings enable semantic similarity search for
    few-shot prompt injection — the system learns from its mistakes.

    feedback_type:
      - "intent": Wrong intent classification (Single-Intent path)
      - "agent_tool": Wrong tool choice in Agent Loop
      - "complexity": Wrong simple/complex classification
    """
    __tablename__ = "intent_corrections"

    id = Column(Integer, primary_key=True, index=True)
    message_text = Column(Text, nullable=False)
    feedback_type = Column(String(20), nullable=False, index=True)
    original_value = Column(String(100), nullable=False)
    corrected_value = Column(String(100), nullable=False)
    embedding = Column(
        Vector(EMBEDDING_DIMENSION) if PGVECTOR_AVAILABLE else Text,
        nullable=True
    )
    context = Column(JSON, nullable=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    created_at = Column(DateTime, default=_utcnow)

    user = relationship("User", foreign_keys=[user_id])


# ==========================================================================
# Proactive Notifications
# ==========================================================================

# Notification Status Constants
NOTIFICATION_PENDING = "pending"
NOTIFICATION_DELIVERED = "delivered"
NOTIFICATION_ACKNOWLEDGED = "acknowledged"
NOTIFICATION_DISMISSED = "dismissed"

NOTIFICATION_STATUSES = [
    NOTIFICATION_PENDING,
    NOTIFICATION_DELIVERED,
    NOTIFICATION_ACKNOWLEDGED,
    NOTIFICATION_DISMISSED,
]

# Notification Urgency Constants
URGENCY_CRITICAL = "critical"
URGENCY_INFO = "info"
URGENCY_LOW = "low"

URGENCY_LEVELS = [URGENCY_CRITICAL, URGENCY_INFO, URGENCY_LOW]


class Notification(Base):
    """
    Proaktive Benachrichtigungen — empfangen via Webhook (z.B. von HA-Automationen),
    gespeichert in der DB und an verbundene Geräte ausgeliefert (WS + TTS).
    """
    __tablename__ = "notifications"

    id = Column(Integer, primary_key=True, index=True)
    event_type = Column(String(100), nullable=False, index=True)
    title = Column(String(255), nullable=False)
    message = Column(Text, nullable=False)
    urgency = Column(String(20), default=URGENCY_INFO)
    room_id = Column(Integer, ForeignKey("rooms.id"), nullable=True, index=True)
    room_name = Column(String(100), nullable=True)
    source = Column(String(50), default="ha_automation")
    source_data = Column(JSON, nullable=True)
    status = Column(String(20), default=NOTIFICATION_PENDING, index=True)
    delivered_to = Column(JSON, nullable=True)
    acknowledged_by = Column(String(100), nullable=True)
    tts_delivered = Column(Boolean, default=False)
    dedup_key = Column(String(255), nullable=True, index=True)
    created_at = Column(DateTime, default=_utcnow, index=True)
    delivered_at = Column(DateTime, nullable=True)
    acknowledged_at = Column(DateTime, nullable=True)
    expires_at = Column(DateTime, nullable=True)

    # Phase 2: Intelligence columns
    embedding = Column(
        Vector(EMBEDDING_DIMENSION) if PGVECTOR_AVAILABLE else Text,
        nullable=True,
    )
    enriched = Column(Boolean, default=False)
    original_message = Column(Text, nullable=True)
    urgency_auto = Column(Boolean, default=False)

    # Privacy-aware TTS delivery
    privacy = Column(String(20), default="public")
    target_user_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)

    # Relationships
    room = relationship("Room", foreign_keys=[room_id])
    target_user = relationship("User", foreign_keys=[target_user_id])


class NotificationSuppression(Base):
    """
    Feedback-Learning: Benutzer unterdrückt ähnliche Benachrichtigungen.
    Speichert Event-Pattern + Embedding für semantischen Abgleich.
    """
    __tablename__ = "notification_suppressions"

    id = Column(Integer, primary_key=True, index=True)
    event_pattern = Column(String(255), nullable=False, index=True)
    embedding = Column(
        Vector(EMBEDDING_DIMENSION) if PGVECTOR_AVAILABLE else Text,
        nullable=True,
    )
    source_notification_id = Column(Integer, ForeignKey("notifications.id"), nullable=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    reason = Column(String(255), nullable=True)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=_utcnow)

    source_notification = relationship("Notification", foreign_keys=[source_notification_id])
    user = relationship("User", foreign_keys=[user_id])


# Reminder Status Constants
REMINDER_PENDING = "pending"
REMINDER_FIRED = "fired"
REMINDER_CANCELLED = "cancelled"


class Reminder(Base):
    """
    Timer-basierte Erinnerungen ("in 30 Minuten", "um 18:00").
    """
    __tablename__ = "reminders"

    id = Column(Integer, primary_key=True, index=True)
    message = Column(Text, nullable=False)
    trigger_at = Column(DateTime, nullable=False, index=True)
    room_id = Column(Integer, ForeignKey("rooms.id"), nullable=True)
    room_name = Column(String(100), nullable=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    session_id = Column(String(255), nullable=True)
    status = Column(String(20), default=REMINDER_PENDING)
    notification_id = Column(Integer, ForeignKey("notifications.id"), nullable=True)
    created_at = Column(DateTime, default=_utcnow)
    fired_at = Column(DateTime, nullable=True)

    room = relationship("Room", foreign_keys=[room_id])
    user = relationship("User", foreign_keys=[user_id])
    notification = relationship("Notification", foreign_keys=[notification_id])


# ==========================================================================
# Conversation Memory (Long-term)
# ==========================================================================

# Memory Category Constants
MEMORY_CATEGORY_PREFERENCE = "preference"   # User preferences ("Ich mag Jazz")
MEMORY_CATEGORY_FACT = "fact"               # Personal facts ("Mein Hund heißt Bello")
MEMORY_CATEGORY_CONTEXT = "context"         # Ephemeral context (decays over time)
MEMORY_CATEGORY_INSTRUCTION = "instruction" # Standing instructions ("Sprich mich mit Du an")

MEMORY_CATEGORIES = [
    MEMORY_CATEGORY_PREFERENCE,
    MEMORY_CATEGORY_FACT,
    MEMORY_CATEGORY_CONTEXT,
    MEMORY_CATEGORY_INSTRUCTION,
]


class ConversationMemory(Base):
    """
    Long-term memory extracted from conversations.

    Stores facts, preferences, instructions, and context that the assistant
    should remember across sessions. Uses pgvector embeddings for semantic
    retrieval of relevant memories.
    """
    __tablename__ = "conversation_memories"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    content = Column(Text, nullable=False)
    category = Column(String(20), nullable=False, default=MEMORY_CATEGORY_FACT, index=True)

    # Source tracking
    source_session_id = Column(String(255), nullable=True, index=True)
    source_message_id = Column(Integer, ForeignKey("messages.id"), nullable=True)

    # Embedding for semantic search
    embedding = Column(
        Vector(EMBEDDING_DIMENSION) if PGVECTOR_AVAILABLE else Text,
        nullable=True
    )

    # Importance and lifecycle
    importance = Column(Float, default=0.5)
    expires_at = Column(DateTime, nullable=True)
    access_count = Column(Integer, default=0)
    last_accessed_at = Column(DateTime, nullable=True)
    is_active = Column(Boolean, default=True, index=True)

    # Timestamps
    created_at = Column(DateTime, default=_utcnow)

    # Relationships
    user = relationship("User", foreign_keys=[user_id])
    source_message = relationship("Message", foreign_keys=[source_message_id])


# Memory History — Audit trail for memory modifications
MEMORY_ACTION_CREATED = "created"
MEMORY_ACTION_UPDATED = "updated"
MEMORY_ACTION_DELETED = "deleted"
MEMORY_ACTIONS = [MEMORY_ACTION_CREATED, MEMORY_ACTION_UPDATED, MEMORY_ACTION_DELETED]

MEMORY_CHANGED_BY_SYSTEM = "system"
MEMORY_CHANGED_BY_USER = "user"
MEMORY_CHANGED_BY_RESOLUTION = "contradiction_resolution"


# ==========================================================================
# Knowledge Graph (Entity-Relation Triples)
# ==========================================================================

# Entity Type Constants
KG_ENTITY_TYPES = ["person", "place", "organization", "thing", "event", "concept"]

# Knowledge Graph Scope Constants
KG_SCOPE_PERSONAL = "personal"  # Built-in scope, always exists (owner-only)


class KGEntity(Base):
    """Named entity extracted from conversations for the Knowledge Graph."""
    __tablename__ = "kg_entities"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    name = Column(String(255), nullable=False)
    entity_type = Column(String(50), nullable=False)  # person, place, organization, thing, event, concept
    description = Column(Text, nullable=True)
    embedding = Column(
        Vector(EMBEDDING_DIMENSION) if PGVECTOR_AVAILABLE else Text,
        nullable=True
    )
    mention_count = Column(Integer, default=1)
    first_seen_at = Column(DateTime, default=_utcnow)
    last_seen_at = Column(DateTime, default=_utcnow)
    is_active = Column(Boolean, default=True, index=True)
    # Scope references either "personal" or a scope name from kg_scopes.yaml
    scope = Column(String(50), default=KG_SCOPE_PERSONAL, nullable=False, index=True)

    __table_args__ = (
        Index('ix_kg_entities_user_active', 'user_id', 'is_active'),
        Index('ix_kg_entities_scope_active', 'scope', 'is_active'),
    )

    user = relationship("User", foreign_keys=[user_id])
    subject_relations = relationship(
        "KGRelation", foreign_keys="KGRelation.subject_id",
        back_populates="subject", cascade="all, delete-orphan"
    )
    object_relations = relationship(
        "KGRelation", foreign_keys="KGRelation.object_id",
        back_populates="object", cascade="all, delete-orphan"
    )


class KGRelation(Base):
    """Directed relation between two entities in the Knowledge Graph."""
    __tablename__ = "kg_relations"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    subject_id = Column(Integer, ForeignKey("kg_entities.id"), nullable=False, index=True)
    predicate = Column(String(100), nullable=False)
    object_id = Column(Integer, ForeignKey("kg_entities.id"), nullable=False, index=True)
    confidence = Column(Float, default=0.8)
    source_session_id = Column(String(255), nullable=True)
    created_at = Column(DateTime, default=_utcnow)
    is_active = Column(Boolean, default=True, index=True)

    subject = relationship("KGEntity", foreign_keys=[subject_id], back_populates="subject_relations")
    object = relationship("KGEntity", foreign_keys=[object_id], back_populates="object_relations")
    user = relationship("User", foreign_keys=[user_id])


class MemoryHistory(Base):
    """Audit trail for memory modifications (create/update/delete)."""
    __tablename__ = "memory_history"

    id = Column(Integer, primary_key=True, index=True)
    memory_id = Column(Integer, ForeignKey("conversation_memories.id"), nullable=False, index=True)
    action = Column(String(20), nullable=False, index=True)
    old_content = Column(Text, nullable=True)
    old_category = Column(String(20), nullable=True)
    old_importance = Column(Float, nullable=True)
    new_content = Column(Text, nullable=True)
    new_category = Column(String(20), nullable=True)
    new_importance = Column(Float, nullable=True)
    changed_by = Column(String(30), nullable=False, default=MEMORY_CHANGED_BY_SYSTEM)
    created_at = Column(DateTime, default=_utcnow)

    memory = relationship("ConversationMemory", foreign_keys=[memory_id])


# ==========================================================================
# BLE Presence Detection
# ==========================================================================

class UserBleDevice(Base):
    """Registered BLE device for room-level presence detection."""
    __tablename__ = "user_ble_devices"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    mac_address = Column(String(17), unique=True, nullable=False, index=True)  # "AA:BB:CC:DD:EE:FF"
    device_name = Column(String(100), nullable=False)   # "Emma's iPhone"
    device_type = Column(String(50), default="phone")   # phone, watch, tracker
    detection_method = Column(String(20), default="ble")  # "ble" | "classic_bt"
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
        Index('ix_presence_events_analytics', 'user_id', 'room_id', 'created_at'),
    )


# System Setting Keys
SETTING_WAKEWORD_KEYWORD = "wakeword.keyword"
SETTING_WAKEWORD_THRESHOLD = "wakeword.threshold"
SETTING_WAKEWORD_COOLDOWN_MS = "wakeword.cooldown_ms"
SETTING_NOTIFICATION_WEBHOOK_TOKEN = "notification.webhook_token"

SYSTEM_SETTING_KEYS = [
    SETTING_WAKEWORD_KEYWORD,
    SETTING_WAKEWORD_THRESHOLD,
    SETTING_WAKEWORD_COOLDOWN_MS,
    SETTING_NOTIFICATION_WEBHOOK_TOKEN,
]


# ==========================================================================
# Paperless Document Audit
# ==========================================================================

class PaperlessAuditResult(Base):
    """Paperless document audit results from LLM analysis."""
    __tablename__ = "paperless_audit_results"

    id = Column(Integer, primary_key=True)
    paperless_doc_id = Column(Integer, index=True, unique=True)

    # Snapshot of current state
    current_title = Column(String, nullable=True)
    current_correspondent = Column(String, nullable=True)
    current_document_type = Column(String, nullable=True)
    current_tags = Column(JSON, nullable=True)

    # LLM suggestions
    suggested_title = Column(String, nullable=True)
    suggested_correspondent = Column(String, nullable=True)
    suggested_document_type = Column(String, nullable=True)
    suggested_tags = Column(JSON, nullable=True)

    # Document date
    current_date = Column(String, nullable=True)          # Paperless 'created' (YYYY-MM-DD)
    suggested_date = Column(String, nullable=True)        # LLM-extracted date

    # Missing metadata detection
    missing_fields = Column(JSON, nullable=True)          # ["correspondent", "document_type", ...]

    # Duplicate detection
    duplicate_group_id = Column(String, nullable=True, index=True)
    duplicate_score = Column(Float, nullable=True)        # 0.0-1.0

    # Custom fields
    current_custom_fields = Column(JSON, nullable=True)
    suggested_custom_fields = Column(JSON, nullable=True)

    # Language detection
    detected_language = Column(String(10), nullable=True) # "de", "en", etc.

    # Storage path
    current_storage_path = Column(String, nullable=True)
    suggested_storage_path = Column(String, nullable=True)

    # Content completeness
    content_completeness = Column(Integer, nullable=True) # 1-5
    completeness_issues = Column(String, nullable=True)

    # Content hash for duplicate detection
    content_hash = Column(String(32), nullable=True)      # MD5 of first 1000 chars

    # Assessment
    ocr_quality = Column(Integer, nullable=True)          # 1-5
    ocr_issues = Column(String, nullable=True)
    confidence = Column(Float, nullable=True)             # 0.0-1.0
    changes_needed = Column(Boolean, default=False)
    reasoning = Column(Text, nullable=True)               # LLM reasoning

    # Status: pending → applied/skipped/failed
    status = Column(String, default="pending")

    # Re-OCR
    renfield_ocr_text = Column(Text, nullable=True)

    # Timestamps
    audited_at = Column(DateTime, default=_utcnow)
    applied_at = Column(DateTime, nullable=True)
    audit_run_id = Column(String, nullable=True, index=True)
