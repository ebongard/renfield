"""
WebSocket handlers for Renfield AI Assistant.

This module contains the WebSocket endpoint handlers for:
- /ws - Chat WebSocket
- /ws/device - Device WebSocket
- /ws/satellite - Satellite WebSocket
- /ws/wakeword - Wake word detection WebSocket
"""

from .shared import (
    ConversationSessionState,
    RAGSessionState,
    is_followup_question,
    get_whisper_service,
    send_ws_error,
)

__all__ = [
    "ConversationSessionState",
    "RAGSessionState",
    "is_followup_question",
    "get_whisper_service",
    "send_ws_error",
]
