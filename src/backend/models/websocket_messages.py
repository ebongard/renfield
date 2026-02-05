"""
WebSocket Message Models for Renfield

Pydantic models for validating incoming WebSocket messages.
Provides type safety and automatic validation for all message types.
"""

import re
from datetime import datetime
from enum import Enum
from typing import Any, Literal, Union

from pydantic import BaseModel, Field, field_validator


class WSMessageType(str, Enum):
    """All supported WebSocket message types."""
    # Client -> Server
    REGISTER = "register"
    TEXT = "text"
    AUDIO = "audio"
    AUDIO_END = "audio_end"
    WAKEWORD_DETECTED = "wakeword_detected"
    START_SESSION = "start_session"
    HEARTBEAT = "heartbeat"

    # Server -> Client
    REGISTER_ACK = "register_ack"
    STATE = "state"
    TRANSCRIPTION = "transcription"
    ACTION = "action"
    TTS_AUDIO = "tts_audio"
    RESPONSE_TEXT = "response_text"
    STREAM = "stream"
    SESSION_END = "session_end"
    SESSION_STARTED = "session_started"
    ERROR = "error"
    HEARTBEAT_ACK = "heartbeat_ack"
    SERVER_SHUTDOWN = "server_shutdown"


class WSErrorCode(str, Enum):
    """WebSocket error codes."""
    INVALID_MESSAGE = "INVALID_MESSAGE"
    UNAUTHORIZED = "UNAUTHORIZED"
    RATE_LIMITED = "RATE_LIMITED"
    SESSION_ERROR = "SESSION_ERROR"
    DEVICE_ERROR = "DEVICE_ERROR"
    INTERNAL_ERROR = "INTERNAL_ERROR"
    MESSAGE_TOO_LARGE = "MESSAGE_TOO_LARGE"
    BUFFER_FULL = "BUFFER_FULL"
    PROTOCOL_ERROR = "PROTOCOL_ERROR"


# =============================================================================
# Base Messages
# =============================================================================

class WSBaseMessage(BaseModel):
    """Base class for all WebSocket messages."""
    type: str
    request_id: str | None = Field(None, max_length=64)

    class Config:
        extra = "allow"  # Allow extra fields for forward compatibility


class WSErrorResponse(BaseModel):
    """Error response message."""
    type: Literal["error"] = "error"
    code: WSErrorCode
    message: str
    details: dict[str, Any] | None = None
    request_id: str | None = None
    timestamp: datetime = Field(default_factory=datetime.utcnow)


# =============================================================================
# Client -> Server Messages
# =============================================================================

class WSCapabilities(BaseModel):
    """Device capabilities."""
    has_microphone: bool = False
    has_speaker: bool = False
    has_wakeword: bool = False
    wakeword_method: str | None = None
    has_display: bool = False
    display_size: str | None = None
    supports_notifications: bool = False
    has_leds: bool = False
    led_count: int = Field(default=0, ge=0)
    has_button: bool = False


class WSRegisterMessage(BaseModel):
    """Device registration message."""
    type: Literal["register"] = "register"
    device_id: str = Field(..., min_length=1, max_length=128)
    device_type: str = Field(default="web_browser", max_length=32)
    room: str = Field(default="Unknown Room", max_length=128)
    device_name: str | None = Field(None, max_length=128)
    is_stationary: bool = True
    capabilities: WSCapabilities | None = None
    protocol_version: str | None = Field(None, max_length=16)
    request_id: str | None = Field(None, max_length=64)

    @field_validator("device_id")
    @classmethod
    def validate_device_id(cls, v: str) -> str:
        """Validate device_id format (alphanumeric with dashes/underscores)."""
        if not re.match(r"^[a-zA-Z0-9_-]+$", v):
            raise ValueError("device_id must be alphanumeric with dashes/underscores only")
        return v


class WSTextMessage(BaseModel):
    """Text input message."""
    type: Literal["text"] = "text"
    content: str = Field(..., min_length=1, max_length=10000)
    session_id: str | None = Field(None, max_length=128)
    request_id: str | None = Field(None, max_length=64)


class WSAudioMessage(BaseModel):
    """Audio chunk message."""
    type: Literal["audio"] = "audio"
    session_id: str = Field(..., min_length=1, max_length=128)
    chunk: str = Field(..., max_length=2_000_000)  # ~1.5MB decoded
    sequence: int = Field(..., ge=0)
    request_id: str | None = Field(None, max_length=64)


class WSAudioEndMessage(BaseModel):
    """End of audio stream message."""
    type: Literal["audio_end"] = "audio_end"
    session_id: str = Field(..., min_length=1, max_length=128)
    reason: str = Field(default="unknown", max_length=64)
    request_id: str | None = Field(None, max_length=64)


class WSWakewordDetectedMessage(BaseModel):
    """Wake word detection message."""
    type: Literal["wakeword_detected"] = "wakeword_detected"
    keyword: str = Field(default="unknown", max_length=64)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    session_id: str | None = Field(None, max_length=128)
    satellite_id: str | None = Field(None, max_length=128)
    request_id: str | None = Field(None, max_length=64)


class WSStartSessionMessage(BaseModel):
    """Manual session start message."""
    type: Literal["start_session"] = "start_session"
    request_id: str | None = Field(None, max_length=64)


class WSHeartbeatMessage(BaseModel):
    """Heartbeat message."""
    type: Literal["heartbeat"] = "heartbeat"
    status: str | None = Field(None, max_length=32)
    uptime_seconds: int | None = Field(None, ge=0)


# =============================================================================
# Chat WebSocket Messages (simpler protocol)
# =============================================================================

class WSChatMessage(BaseModel):
    """Chat message for /ws endpoint."""
    type: Literal["text"] = "text"
    content: str = Field(..., min_length=1, max_length=10000)
    session_id: str | None = Field(None, max_length=128, description="Session ID for conversation persistence")
    request_id: str | None = Field(None, max_length=64)
    # RAG options
    use_rag: bool = Field(default=False, description="Enable RAG context for this query")
    knowledge_base_id: int | None = Field(None, description="Specific knowledge base to search")


# =============================================================================
# Message Parsing
# =============================================================================

# Map of message types to their model classes
MESSAGE_MODELS: dict[str, type] = {
    "register": WSRegisterMessage,
    "text": WSTextMessage,
    "audio": WSAudioMessage,
    "audio_end": WSAudioEndMessage,
    "wakeword_detected": WSWakewordDetectedMessage,
    "start_session": WSStartSessionMessage,
    "heartbeat": WSHeartbeatMessage,
}


def parse_ws_message(data: dict[str, Any]) -> Union[WSBaseMessage, WSErrorResponse]:
    """
    Parse and validate a WebSocket message.

    Args:
        data: Raw message dict from websocket.receive_json()

    Returns:
        Validated message model or error response
    """
    msg_type = data.get("type")

    if not msg_type:
        return WSErrorResponse(
            code=WSErrorCode.INVALID_MESSAGE,
            message="Missing 'type' field in message"
        )

    model_class = MESSAGE_MODELS.get(msg_type)

    if not model_class:
        # Unknown message type - return base message for forward compatibility
        return WSBaseMessage(**data)

    try:
        return model_class(**data)
    except Exception as e:
        return WSErrorResponse(
            code=WSErrorCode.INVALID_MESSAGE,
            message=f"Invalid message format: {e!s}",
            details={"type": msg_type, "error": str(e)}
        )


def create_error_response(
    code: WSErrorCode,
    message: str,
    request_id: str | None = None,
    details: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Create an error response dict ready to send."""
    return WSErrorResponse(
        code=code,
        message=message,
        request_id=request_id,
        details=details
    ).model_dump(mode="json")
