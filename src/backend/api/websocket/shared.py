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

from models.websocket_messages import WSErrorCode, create_error_response
from services.whisper_service import WhisperService

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

    # Configuration
    CONTEXT_TIMEOUT_SECONDS: int = 300  # 5 minutes
    MAX_HISTORY_MESSAGES: int = 10  # Keep last 10 messages in memory

    def is_rag_context_valid(self) -> bool:
        """Check if the cached RAG context is still valid."""
        if not self.last_rag_context:
            return False
        return (time() - self.last_rag_timestamp) < self.CONTEXT_TIMEOUT_SECONDS

    def add_to_history(self, role: str, content: str):
        """Add a message to the conversation history."""
        self.conversation_history.append({"role": role, "content": content})
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
        self.clear_rag()
        self.last_intent = None
        self.last_action_result = None
        self.last_entities = []


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

_whisper_service: WhisperService | None = None


def get_whisper_service() -> WhisperService:
    """Get or create the global WhisperService instance."""
    global _whisper_service
    if _whisper_service is None:
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
