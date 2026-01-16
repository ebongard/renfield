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
