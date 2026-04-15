"""
WebSocket handlers for Renfield AI Assistant.

This module contains the WebSocket endpoint handlers for:
- /ws - Chat WebSocket
- /ws/satellite - Satellite WebSocket (ha-glue-destined, moves in Phase C)
- /ws/knowledge-graph - Live KG graph updates
- /ws/wakeword - Wake word detection WebSocket (still in main.py)

`/ws/device` moved to `ha_glue.api.websocket.device_handler` and is
mounted via the `register_routes` hook from `ha_glue.bootstrap` —
not exported from this module anymore.
"""

from .chat_handler import router as chat_router
from .kg_live_handler import router as kg_live_router
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
    "get_whisper_service",
    "is_followup_question",
    "kg_live_router",
    "satellite_router",
    "send_ws_error",
]
