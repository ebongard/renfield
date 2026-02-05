"""
WebSocket handlers for Renfield AI Assistant.

This module contains the WebSocket endpoint handlers for:
- /ws - Chat WebSocket
- /ws/device - Device WebSocket
- /ws/satellite - Satellite WebSocket
- /ws/wakeword - Wake word detection WebSocket (still in main.py)
"""

from .chat_handler import router as chat_router
from .device_handler import router as device_router
from .satellite_handler import router as satellite_router
from .shared import (
    ConversationSessionState,
    RAGSessionState,
    get_whisper_service,
    is_followup_question,
    send_ws_error,
)

__all__ = [
    "ConversationSessionState",
    "RAGSessionState",
    "chat_router",
    "device_router",
    "get_whisper_service",
    "is_followup_question",
    "satellite_router",
    "send_ws_error",
]
