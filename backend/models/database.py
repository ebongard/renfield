"""
Datenbank Models
"""
from sqlalchemy import Column, Integer, String, DateTime, Text, Boolean, JSON, ForeignKey
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import relationship
from datetime import datetime

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
    """Raum für Smart Home und Satellite-Zuordnung"""
    __tablename__ = "rooms"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(100), nullable=False, unique=True)  # "Wohnzimmer"
    alias = Column(String(50), index=True)  # "wohnzimmer" (für Sprachbefehle, normalisiert)

    # Home Assistant Sync
    ha_area_id = Column(String(100), nullable=True, unique=True, index=True)
    source = Column(String(20), default="renfield")  # renfield/homeassistant/satellite

    # Metadata
    icon = Column(String(50), nullable=True)  # "mdi:sofa"

    # Timestamps
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    last_synced_at = Column(DateTime, nullable=True)

    # Beziehungen
    satellites = relationship("RoomSatellite", back_populates="room", cascade="all, delete-orphan")


class RoomSatellite(Base):
    """Satellite-Zuordnung zu einem Raum"""
    __tablename__ = "room_satellites"

    id = Column(Integer, primary_key=True, index=True)
    room_id = Column(Integer, ForeignKey("rooms.id"), nullable=False)
    satellite_id = Column(String(100), nullable=False, unique=True, index=True)

    # Status
    is_online = Column(Boolean, default=False)
    last_connected_at = Column(DateTime, nullable=True)

    # Timestamps
    created_at = Column(DateTime, default=datetime.utcnow)

    # Beziehungen
    room = relationship("Room", back_populates="satellites")
