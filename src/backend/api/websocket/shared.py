"""
Shared utilities for WebSocket handlers.

This module contains:
- ConversationSessionState: Session state management for WebSocket conversations
- Helper functions for WebSocket communication
- Whisper service singleton
"""

import re
from dataclasses import dataclass, field
from time import time

from fastapi import WebSocket
from loguru import logger

from models.websocket_messages import WSErrorCode, create_error_response

# =============================================================================
# Session State Management
# =============================================================================

@dataclass
class ConversationSessionState:
    """
    Maintains conversation context across multiple messages in a WebSocket session.

    This state supports:
    - General conversation history (for follow-up questions like "Mach es aus")
    - RAG context persistence (for document Q&A)
    - Last entities/actions (for pronoun resolution)
    """
    # General conversation history (for all message types)
    conversation_history: list[dict] = field(default_factory=list)
    history_loaded: bool = False  # Whether history was loaded from DB
    db_session_id: str | None = None  # Session ID for DB persistence
    #: Die hoechste Nachrichten-Kennung, die diese Sitzung KENNT.
    #:
    #: 🛑 Wozu: die Historie wird einmal je Socket aus der Datenbank geladen und
    #: waechst danach nur noch ueber `add_to_history`. Ein ASYNCHRONER Erzeuger
    #: — der Scanner mit seiner Fertigmeldung, die Gespraechsuebergabe, die
    #: Paperless-Bestaetigung — schreibt in die Datenbank, ohne diese Liste
    #: anzufassen. Der Agent las damit einen veralteten Kontext und antwortete
    #: daraus, ohne das Werkzeug zu rufen.
    #:
    #: Gemessen am 2026-10-26: ein Scan scheiterte am Papierstau, die Meldung
    #: stand in der Datenbank — und der Agent sagte auf zwei weitere Bitten
    #: „der Scan laeuft bereits im Hintergrund, Du wirst benachrichtigt" und
    #: rief `scan_document` NIE. Erfundene Beruhigung ueber einen Scan, der
    #: nicht lief.
    #:
    #: Liegt in der Datenbank eine hoehere Kennung als diese Marke, hat jemand
    #: anders geschrieben und die Historie wird neu geladen.
    history_watermark: int | None = None

    # RAG-specific state
    last_rag_context: str | None = None  # Last retrieved document context
    last_rag_results: list[dict] | None = None  # Raw search results
    last_query: str | None = None  # Last user query
    last_rag_timestamp: float = 0  # When last RAG search was performed
    knowledge_base_id: int | None = None  # Current knowledge base

    # Last action context (for pronoun resolution like "es" referring to last entity)
    last_intent: dict | None = None
    last_action_result: dict | None = None
    last_entities: list[str] = field(default_factory=list)

    # Last room used for media playback (for follow-up transport commands)
    last_media_room: str | None = None

    # Configuration
    CONTEXT_TIMEOUT_SECONDS: int = 300  # 5 minutes
    MAX_HISTORY_MESSAGES: int = 10  # Keep last 10 messages in memory

    def is_rag_context_valid(self) -> bool:
        """Check if the cached RAG context is still valid."""
        if not self.last_rag_context:
            return False
        return (time() - self.last_rag_timestamp) < self.CONTEXT_TIMEOUT_SECONDS

    def add_to_history(self, role: str, content: str, metadata: dict | None = None):
        """Add a message to the conversation history.

        When ``metadata`` is provided it is stored alongside role/content so
        downstream consumers (e.g. the agent prompt builder) can distinguish
        successful from failed tool-backed turns. Kept optional for callers
        that don't track action outcomes.
        """
        entry: dict = {"role": role, "content": content}
        if metadata is not None:
            entry["metadata"] = metadata
        self.conversation_history.append(entry)
        # Keep only last N messages in memory
        if len(self.conversation_history) > self.MAX_HISTORY_MESSAGES:
            self.conversation_history = self.conversation_history[-self.MAX_HISTORY_MESSAGES:]

    def update_rag_context(self, context: str, results: list[dict], query: str, kb_id: int | None = None):
        """Update the RAG context after a successful search."""
        self.last_rag_context = context
        self.last_rag_results = results
        self.last_query = query
        self.last_rag_timestamp = time()
        self.knowledge_base_id = kb_id

    def update_action_context(self, intent: dict, result: dict):
        """Update the last action context for pronoun resolution."""
        self.last_intent = intent
        self.last_action_result = result
        # Extract entity IDs for "es/das" resolution
        if intent and intent.get("parameters"):
            entity_id = intent["parameters"].get("entity_id")
            if entity_id:
                self.last_entities = [entity_id]

    def clear_rag(self):
        """Clear RAG-specific state."""
        self.last_rag_context = None
        self.last_rag_results = None
        self.last_query = None
        self.last_rag_timestamp = 0
        self.knowledge_base_id = None

    def clear_all(self):
        """Clear all session state."""
        self.conversation_history = []
        self.history_loaded = False
        self.history_watermark = None
        self.clear_rag()
        self.last_intent = None
        self.last_action_result = None
        self.last_entities = []
        self.last_media_room = None


# Alias for backwards compatibility
RAGSessionState = ConversationSessionState


# =============================================================================
# Helper Functions
# =============================================================================

def is_followup_question(query: str, previous_query: str | None = None) -> bool:
    """
    Detect if a query is likely a follow-up question about previous context.

    Indicators of follow-up questions:
    - Short queries (typically < 8 words)
    - Contains pronouns referring to previous context
    - Starts with question words without new topic
    - Contains comparative/continuation words
    """
    query_lower = query.lower().strip()
    words = query_lower.split()

    # Very short queries are often follow-ups
    if len(words) <= 4:
        return True

    # German pronouns and references to previous context
    followup_indicators = [
        r'\b(es|das|dies|dieser|diese|dieses|deren|dessen)\b',  # Demonstrative pronouns
        r'\b(ihm|ihr|ihnen|ihn|sie)\b',  # Personal pronouns
        r'\b(welche[rsmn]?|wieviel|wie\s*viel|wann|warum|wieso|weshalb)\b',  # Question words without topic
        r'\b(mehr|weitere|noch|auch|außerdem|zusätzlich)\b',  # Continuation words
        r'\b(davon|dazu|darüber|darin|damit|dafür|dagegen)\b',  # Prepositional pronouns
        r'\b(genauer|details?|einzelheiten)\b',  # Asking for more details
        r'\b(und\s+was|was\s+noch|sonst\s+noch)\b',  # And what else patterns
        r'^(und|aber|oder|also)\b',  # Starts with conjunction
        r'\b(der|die|das)\s+(rechnung|dokument|datei|beleg)\b',  # Referring to "the document"
    ]

    for pattern in followup_indicators:
        if re.search(pattern, query_lower):
            return True

    # If previous query exists, check for topic continuity
    if previous_query:
        prev_words = set(previous_query.lower().split())
        curr_words = set(words)
        # If very few new content words, likely a follow-up
        new_words = curr_words - prev_words - {'ist', 'sind', 'war', 'hat', 'haben', 'der', 'die', 'das', 'ein', 'eine', 'und', 'oder', 'aber', 'was', 'wie', 'wann', 'wo', 'wer'}
        if len(new_words) <= 2:
            return True

    return False


# =============================================================================
# Whisper Service Singleton
# =============================================================================

_whisper_service = None


def get_whisper_service():
    """Get or create the global WhisperService instance."""
    global _whisper_service
    if _whisper_service is None:
        from services.whisper_service import WhisperService
        _whisper_service = WhisperService()
    return _whisper_service


# =============================================================================
# WebSocket Helpers
# =============================================================================

async def send_ws_error(websocket: WebSocket, code: WSErrorCode, message: str, request_id: str = None):
    """Send a structured error response to the WebSocket client."""
    try:
        await websocket.send_json(create_error_response(code, message, request_id))
    except Exception:
        pass  # WebSocket may already be closed


# =============================================================================
# WebSocket Connection Registry
# =============================================================================

_ws_connections: dict[str, WebSocket] = {}


def history_needs_reload(
    *, history_loaded: bool, known_session_id: str | None, wanted_session_id: str,
    watermark: int | None, newest_in_db: int | None,
) -> bool:
    """Muss die Historie dieser Sitzung aus der Datenbank neu geladen werden?

    Eine REINE Funktion, damit die Regel pruefbar ist statt im Handler zu
    stecken. Drei Gruende, und der dritte ist der neue:

    1. Noch nie geladen.
    2. Der Socket wechselt die Sitzung.
    3. In der Datenbank steht eine HOEHERE Kennung als die bekannte — dann hat
       ein asynchroner Erzeuger geschrieben, ohne die lebende Liste
       anzufassen.

    🛑 `newest_in_db is None` heisst „keine Auskunft" und laedt NICHT neu. Eine
    fehlende Messung darf keinen Neuladezyklus erzwingen; sonst tauscht man
    einen veralteten Kontext gegen eine Abfrage je Zug, die bei jeder Stoerung
    der Datenbank zuschlaegt.

    🛑 `watermark is None` bei geladener Historie heisst ebenfalls nicht neu
    laden: die Marke fehlt nur, wenn das Lesen der Zahl scheiterte, und daraus
    einen Dauer-Neuladezyklus zu machen waere schlimmer als der Zustand, den
    sie verhindert.
    """
    if not history_loaded or known_session_id != wanted_session_id:
        return True
    if watermark is None or newest_in_db is None:
        return False
    return newest_in_db > watermark


def register_ws_connection(session_id: str, websocket: WebSocket) -> None:
    """Register a WebSocket connection for a chat session."""
    _ws_connections[session_id] = websocket


def unregister_ws_connection(session_id: str, websocket: "WebSocket | None" = None) -> None:
    """Unregister a session's WebSocket connection. No-op if not registered.

    Identity-aware: when ``websocket`` is given, only pop if the stored socket
    IS that socket. Two tabs can share a session_id (localStorage), and the
    register-on-connect effect means a later tab overwrites the entry — without
    this guard, the first tab's disconnect would evict the live tab and kill its
    pushes. Pass the disconnecting socket so it can only remove its own entry."""
    if websocket is not None and _ws_connections.get(session_id) is not websocket:
        return
    _ws_connections.pop(session_id, None)


async def notify_session(session_id: str, message: dict) -> bool:
    """Send a notification to a connected chat session.

    Returns True if the message was sent, False if the session is not
    connected or the connection is broken (auto-cleans broken entries).
    """
    ws = _ws_connections.get(session_id)
    if ws is None:
        return False
    try:
        await ws.send_json(message)
        return True
    except Exception:
        # Connection is dead — clean up
        _ws_connections.pop(session_id, None)
        logger.debug(f"Removed dead WS connection for session {session_id}")
        return False
