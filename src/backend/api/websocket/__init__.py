"""
WebSocket handlers for Renfield AI Assistant.

This module contains the WebSocket endpoint handlers for:
- /ws - Chat WebSocket
- /ws/knowledge-graph - Live KG graph updates
- /ws/wakeword - Wake word detection WebSocket (still in main.py)

`/ws/device` and `/ws/satellite` both moved to `ha_glue.api.websocket.*`
and are mounted via the `register_routes` hook from `ha_glue.bootstrap`.
Platform-only deploys don't see these endpoints.
"""

from .chat_handler import router as chat_router
from .kg_live_handler import router as kg_live_router
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
    "get_whisper_service",
    "is_followup_question",
    "kg_live_router",
    "send_ws_error",
]
