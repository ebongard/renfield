"""
Datenbank Models
"""
from sqlalchemy import Column, Integer, String, DateTime, Text, Boolean, JSON, ForeignKey, Float, Index
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import relationship
from datetime import datetime

try:
    from pgvector.sqlalchemy import Vector
    PGVECTOR_AVAILABLE = True
except ImportError:
    # Fallback für Tests ohne pgvector
    PGVECTOR_AVAILABLE = False
    Vector = None

Base = declarative_base()

class Conversation(Base):
    """Konversationen / Chat-Historie"""
    __tablename__ = "conversations"
    
    id = Column(Integer, primary_key=True, index=True)
    session_id = Column(String, unique=True, index=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    # Beziehungen
    messages = relationship("Message", back_populates="conversation", cascade="all, delete-orphan")

class Message(Base):
    """Einzelne Nachrichten"""
    __tablename__ = "messages"
    
    id = Column(Integer, primary_key=True, index=True)
    conversation_id = Column(Integer, ForeignKey("conversations.id"))
    role = Column(String)  # 'user' oder 'assistant'
    content = Column(Text)
    timestamp = Column(DateTime, default=datetime.utcnow)
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
    created_at = Column(DateTime, default=datetime.utcnow)
    completed_at = Column(DateTime, nullable=True)
    created_by = Column(String, nullable=True)

class CameraEvent(Base):
    """Kamera-Events"""
    __tablename__ = "camera_events"
    
    id = Column(Integer, primary_key=True, index=True)
    camera_name = Column(String)
    event_type = Column(String)  # 'person', 'car', 'animal'
    confidence = Column(Integer)
    timestamp = Column(DateTime, default=datetime.utcnow)
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
    last_updated = Column(DateTime, default=datetime.utcnow)


# --- Speaker Recognition Models ---

class Speaker(Base):
    """Registrierter Sprecher für Speaker Recognition"""
    __tablename__ = "speakers"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(100), nullable=False)       # "Max Mustermann"
    alias = Column(String(50), unique=True, index=True)  # "max" (für Ansprache)
    is_admin = Column(Boolean, default=False)        # Admin-Berechtigung
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Beziehungen
    embeddings = relationship("SpeakerEmbedding", back_populates="speaker", cascade="all, delete-orphan")


class SpeakerEmbedding(Base):
    """Voice Embedding für einen Sprecher (mehrere pro Speaker für bessere Erkennung)"""
    __tablename__ = "speaker_embeddings"

    id = Column(Integer, primary_key=True, index=True)
    speaker_id = Column(Integer, ForeignKey("speakers.id"), nullable=False)
    embedding = Column(Text, nullable=False)         # Base64-encoded numpy array
    sample_duration = Column(Integer, nullable=True)  # Dauer des Samples in Millisekunden
    created_at = Column(DateTime, default=datetime.utcnow)

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
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
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
    room_id = Column(Integer, ForeignKey("rooms.id"), nullable=False)
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
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

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

    Either renfield_device_id OR ha_entity_id must be set (not both).
    """
    __tablename__ = "room_output_devices"

    id = Column(Integer, primary_key=True, index=True)
    room_id = Column(Integer, ForeignKey("rooms.id"), nullable=False, index=True)

    # Device source: either Renfield device OR Home Assistant entity
    renfield_device_id = Column(String(100), ForeignKey("room_devices.device_id"), nullable=True)
    ha_entity_id = Column(String(255), nullable=True)  # e.g. "media_player.linn_dsm"

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
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

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
    def target_id(self) -> str:
        """Get the target device/entity ID"""
        return self.renfield_device_id or self.ha_entity_id or ""


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

    # Timestamps
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Beziehungen
    documents = relationship("Document", back_populates="knowledge_base", cascade="all, delete-orphan")


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

    # Processing Status
    status = Column(String(50), default="pending", index=True)  # pending, processing, completed, failed
    error_message = Column(Text, nullable=True)

    # Metadata (extrahiert aus Dokument)
    title = Column(String(512), nullable=True)
    author = Column(String(255), nullable=True)
    page_count = Column(Integer, nullable=True)
    chunk_count = Column(Integer, default=0)

    # Timestamps
    created_at = Column(DateTime, default=datetime.utcnow)
    processed_at = Column(DateTime, nullable=True)

    # Beziehungen
    knowledge_base = relationship("KnowledgeBase", back_populates="documents")
    chunks = relationship("DocumentChunk", back_populates="document", cascade="all, delete-orphan")


# Document Chunk Embedding Dimension (nomic-embed-text = 768)
EMBEDDING_DIMENSION = 768


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

    # Additional Metadata (JSON für Flexibilität)
    chunk_metadata = Column(JSON, nullable=True)  # Umbenannt von 'metadata' (SQLAlchemy reserved)

    # Timestamps
    created_at = Column(DateTime, default=datetime.utcnow)

    # Beziehungen
    document = relationship("Document", back_populates="chunks")

    # Index für Vektor-Suche (wird bei Migration erstellt)
    # CREATE INDEX idx_document_chunks_embedding ON document_chunks
    # USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);


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
