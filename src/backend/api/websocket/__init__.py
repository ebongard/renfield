"""
WebSocket handlers for Renfield AI Assistant.

This module contains the WebSocket endpoint handlers for:
- /ws - Chat WebSocket
- /ws/device - Device WebSocket
- /ws/satellite - Satellite WebSocket
- /ws/wakeword - Wake word detection WebSocket (still in main.py)
"""

from .shared import (
    ConversationSessionState,
    RAGSessionState,
    is_followup_question,
    get_whisper_service,
    send_ws_error,
)

from .chat_handler import router as chat_router
from .satellite_handler import router as satellite_router
from .device_handler import router as device_router

__all__ = [
    # Shared utilities
    "ConversationSessionState",
    "RAGSessionState",
    "is_followup_question",
    "get_whisper_service",
    "send_ws_error",
    # Routers
    "chat_router",
    "satellite_router",
    "device_router",
]
