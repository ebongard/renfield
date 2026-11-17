"""
Shared utilities for WebSocket handlers.

This module contains:
- ConversationSessionState: Session state management for WebSocket conversations
- Helper functions for WebSocket communication
- Whisper service singleton
"""

import asyncio
import json
import re
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from time import time
from typing import Any

from fastapi import WebSocket, WebSocketDisconnect
from loguru import logger

from models.websocket_messages import WSErrorCode, create_error_response
from utils.log_safety import error_text_without_content

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
    #: The highest message id this session KNOWS about.
    #:
    #: 🛑 Why: the history is loaded from the database once per socket and after
    #: that only grows through `add_to_history`. An ASYNCHRONOUS producer --
    #: the scanner with its completion notice, the conversation handover, the
    #: Paperless confirmation -- writes to the database without touching this
    #: list. The agent therefore read a stale context and answered from it
    #: without calling the tool.
    #:
    #: Measured on 2026-10-26: a scan failed on a paper jam, the notice was in
    #: the database -- and on two further requests the agent said "the scan is
    #: already running in the background, you will be notified" and NEVER
    #: called `scan_document`. Invented reassurance about a scan that was not
    #: running.
    #:
    #: If the database holds a higher id than this mark, somebody else wrote
    #: and the history gets reloaded.
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
        # Extract entity IDs for resolving "es"/"das"
        if intent and intent.get("parameters"):
            entity_id = intent["parameters"].get("entity_id")
            if entity_id:
                self.last_entities = [entity_id]

    def bind_session(self, session_id: str) -> None:
        """Point this socket at ``session_id``; another id starts a clean history.

        The in-memory history, its load flag and its watermark describe ONE
        conversation. Rebinding to a different one -- a ``register`` frame, a
        message naming another id, a replacement for a refused or deleted id
        (#1485) -- without dropping them would carry the previous
        conversation's turns into the next prompt, and the freshness rule
        (``history_needs_reload``) would compare the new conversation against
        the old one's watermark. Dropped here, the next message loads the
        history of the conversation it actually continues.
        """
        if session_id != self.db_session_id:
            self.conversation_history = []
            self.history_loaded = False
            self.history_watermark = None
        self.db_session_id = session_id

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
# Continuous Reading (#1570)
# =============================================================================

#: Memory a connection may hold in frames its handler has not taken yet,
#: measured as the Python size of the raw frame payloads -- so it bounds memory
#: whatever ``--ws-max-size`` is (prod 1 MB; uvicorn's default 16 MiB under
#: Dockerfile.gpu). 8 MiB is 8 maximal prod frames, and a frame that arrives
#: while nothing is queued is always taken, so no single legitimate frame can
#: trip it. A satellite's state frames during a long turn (a heartbeat every
#: 30 s, a BLE report every ~3 s, each a few hundred bytes to ~2 KB) take hours
#: to fill it; what fills it is a genuine flood, e.g. a second recording
#: streamed at 12.5 audio frames/s while the first turn still runs.
CLIENT_FRAME_BUDGET_BYTES = 8 * 1024 * 1024

#: Close code when the budget is exceeded: 1013 "try again later". Such a
#: connection used to die anyway (uvicorn's 1011 after its keepalive budget),
#: and every client reconnects on a close.
FLOOD_CLOSE_CODE = 1013
FLOOD_CLOSE_REASON = "client frame budget exceeded"


def _utc_now() -> datetime:
    # The clock WSRateLimiter uses; it compares these arrival times.
    return datetime.now(UTC).replace(tzinfo=None)


def _frame_size(message: dict) -> int:
    payload = message.get("text")
    if payload is None:
        payload = message.get("bytes")
    return sys.getsizeof(message) + (sys.getsizeof(payload) if payload is not None else 0)


class _ReaderEnd:
    """The end of the stream: what ``receive`` raised, or the disconnect message."""

    __slots__ = ("exc", "message")

    def __init__(self, *, exc: Exception | None = None, message: dict | None = None):
        self.exc = exc
        self.message = message


class ClientFrameReader:
    """Reads a WebSocket continuously while its handler works on one frame.

    🛑 Why: uvicorn's ``websockets-sansio`` protocol pauses reading the
    transport as soon as a client message is queued while the app is not inside
    ``receive()``. While paused it does not read the pong to its own keepalive
    ping either, and after ``ws_ping_interval + ws_ping_timeout`` (20 s + 20 s)
    it closes the socket itself with 1011 "keepalive ping timeout". A handler
    that processes a long turn inline therefore lost the socket whenever the
    client sent anything during that turn.

    This task stays inside ``receive()`` for the whole connection -- it never
    waits for the handler -- and hands each frame over through a queue, so the
    handler still processes frames ONE AT A TIME and IN ARRIVAL ORDER. Nothing
    is acted on earlier, merged or dropped; frames are only read earlier.

    Limit: the waiting frames may hold ``CLIENT_FRAME_BUDGET_BYTES`` (8 MiB).
    A frame that would exceed it while others wait ends the connection with
    ``FLOOD_CLOSE_CODE`` (1013) and one WARNING with the counts; the waiting
    frames are discarded, the next ``next()`` reports the disconnect, and
    ``closed_for_flood`` tells the handler's error path why a send failed.

    ``decode="json"`` gives what ``websocket.receive_json()`` gave and raises
    ``WebSocketDisconnect`` at the end; ``decode="raw"`` gives the ASGI message
    and ends with the ``websocket.disconnect`` message. The end is STICKY:
    every ``next()`` after it repeats it. ``arrived_at`` is the arrival time of
    the frame ``next()`` returned last -- a rate limit judges the client's
    sending, not the handler catching up after a long turn.

    In ``finally``: ``stop()`` first (synchronous, so it runs even when a later
    await of the cleanup is cancelled), ``await aclose()`` last.
    """

    def __init__(
        self,
        websocket: WebSocket,
        *,
        decode: str,
        label: str,
        budget_bytes: int | None = None,
    ):
        if decode not in ("json", "raw"):
            raise ValueError(f"decode must be 'json' or 'raw', not {decode!r}")
        self.budget_bytes = budget_bytes or CLIENT_FRAME_BUDGET_BYTES
        self.closed_for_flood = False
        self.arrived_at: datetime | None = None
        self._ws = websocket
        self._decode = decode
        self._label = label
        self._queue: asyncio.Queue = asyncio.Queue()
        self._pending_bytes = 0
        self._end: _ReaderEnd | None = None
        self.task = asyncio.create_task(self._run(), name=f"ws-reader:{label}")

    async def _run(self) -> None:
        while True:
            try:
                message = await self._ws.receive()
            except Exception as exc:
                self._queue.put_nowait((_utc_now(), 0, _ReaderEnd(exc=exc)))
                return
            if message.get("type") == "websocket.disconnect":
                self._queue.put_nowait((_utc_now(), 0, _ReaderEnd(message=message)))
                return
            size = _frame_size(message)
            if not self._queue.empty() and self._pending_bytes + size > self.budget_bytes:
                await self._close_for_flood(size)
                return
            self._pending_bytes += size
            self._queue.put_nowait((_utc_now(), size, message))

    async def _close_for_flood(self, size: int) -> None:
        self.closed_for_flood = True
        waiting, held = self._queue.qsize(), self._pending_bytes
        while not self._queue.empty():
            self._queue.get_nowait()
        self._pending_bytes = 0
        logger.warning(
            f"🚫 {self._label}: {waiting + 1} client frames ({held + size} bytes) "
            f"arrived while the handler was busy, over the {self.budget_bytes}-byte "
            f"budget -- closing with {FLOOD_CLOSE_CODE}"
        )
        self._queue.put_nowait((_utc_now(), 0, _ReaderEnd(message={
            "type": "websocket.disconnect",
            "code": FLOOD_CLOSE_CODE,
            "reason": FLOOD_CLOSE_REASON,
        })))
        try:
            await self._ws.close(code=FLOOD_CLOSE_CODE, reason=FLOOD_CLOSE_REASON)
        except Exception as exc:
            logger.warning(
                f"⚠️ {self._label}: closing a flooded socket failed: "
                f"{type(exc).__name__}: {error_text_without_content(exc)}"
            )

    def _resolve_end(self, end: _ReaderEnd) -> Any:
        if end.exc is not None:
            raise end.exc
        message = end.message or {}
        if self._decode == "json":
            raise WebSocketDisconnect(message.get("code") or 1000, message.get("reason"))
        return dict(message)

    async def next(self) -> Any:
        """The next client frame, in arrival order."""
        if self._end is not None:
            return self._resolve_end(self._end)
        arrived, size, item = await self._queue.get()
        self._pending_bytes -= size
        self.arrived_at = arrived
        if isinstance(item, _ReaderEnd):
            self._end = item
            return self._resolve_end(item)
        if self._decode == "raw":
            return item
        try:
            # Exactly websocket.receive_json(): a binary frame is a KeyError.
            return json.loads(item["text"])
        except Exception as exc:
            # As before, a frame that does not parse ends the stream.
            self._end = _ReaderEnd(exc=exc)
            self.stop()
            raise

    def stop(self) -> None:
        """Stop reading; synchronous, so no cancelled await can skip it."""
        self.task.cancel()

    async def aclose(self) -> None:
        """Stop reading and wait until the reader task has ended."""
        self.stop()
        await asyncio.wait({self.task})
        # The reader never raises (it queues what `receive` raised), so a
        # failure here is a bug in the reader itself and must be seen.
        if not self.task.cancelled() and self.task.exception() is not None:
            exc = self.task.exception()
            logger.error(
                f"❌ {self._label}: frame reader failed: "
                f"{type(exc).__name__}: {error_text_without_content(exc)}"
            )


# =============================================================================
# WebSocket Connection Registry
# =============================================================================

_ws_connections: dict[str, WebSocket] = {}


def history_needs_reload(
    *, history_loaded: bool, known_session_id: str | None, wanted_session_id: str,
    watermark: int | None, newest_in_db: int | None,
    session_deleted: bool = False,
) -> bool:
    """Does this session's history have to be reloaded from the database?

    A PURE function, so the rule is testable instead of buried in the
    handler. Four reasons:

    1. Never loaded before.
    2. The socket switches session.
    3. The database holds a HIGHER id than the known one -- then an
       asynchronous producer wrote without touching the live list.

    🛑 `newest_in_db is None` means "no answer" and does NOT reload. A missing
    measurement must not force a reload cycle; that would trade stale context
    for a query per turn that fires on every database hiccup.

    🛑 `watermark is None` with a loaded history also means do not reload: the
    mark is only absent when reading the number failed, and turning that into a
    permanent reload cycle would be worse than the state it prevents.

    4. The conversation was deleted elsewhere (#1485). The reload is where the
       boundary check runs, and that check hands out a fresh id instead of
       recreating the deleted conversation under the old one.
    """
    if not history_loaded or known_session_id != wanted_session_id or session_deleted:
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
