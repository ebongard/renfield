"""
Renfield - Persönlicher KI-Assistent
Hauptanwendung mit FastAPI
"""
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, UploadFile, File, HTTPException, Query, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, JSONResponse
from contextlib import asynccontextmanager
from loguru import logger
from datetime import datetime
from pydantic import ValidationError
import asyncio
import os
import sys
import uuid
from typing import Optional, List

# Logging konfigurieren
logger.remove()
logger.add(sys.stderr, level=os.getenv("LOG_LEVEL", "INFO"))

# Lokale Imports
from api.routes import chat, tasks, voice, camera, homeassistant as ha_routes, settings as settings_routes, speakers, rooms, knowledge, satellites
from api.routes import auth, roles, users, plugins, preferences
from services.auth_service import require_permission, get_optional_user
from models.permissions import Permission
from services.database import init_db, AsyncSessionLocal
from services.ollama_service import OllamaService
from services.task_queue import TaskQueue
from services.whisper_service import WhisperService
from services.device_manager import get_device_manager, DeviceState, DeviceManager
from services.wakeword_config_manager import get_wakeword_config_manager
from services.websocket_auth import (
    get_token_store, authenticate_websocket, close_unauthorized, WSAuthError
)
from services.websocket_rate_limiter import (
    get_rate_limiter, get_connection_limiter, WSRateLimiter, WSConnectionLimiter
)
from models.database import (
    DEVICE_TYPE_SATELLITE, DEVICE_TYPE_WEB_BROWSER, DEVICE_TYPE_WEB_PANEL,
    DEVICE_TYPE_WEB_TABLET, DEVICE_TYPE_WEB_KIOSK, DEVICE_TYPES, DEFAULT_CAPABILITIES
)
from models.websocket_messages import (
    parse_ws_message, create_error_response, WSErrorCode, WSErrorResponse,
    WSRegisterMessage, WSTextMessage, WSAudioMessage, WSAudioEndMessage,
    WSWakewordDetectedMessage, WSStartSessionMessage, WSHeartbeatMessage, WSChatMessage
)
from utils.config import settings
from dataclasses import dataclass, field
from time import time
import re

# =============================================================================
# RAG Session State - Maintains context across multiple messages
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
    conversation_history: List[dict] = field(default_factory=list)
    history_loaded: bool = False  # Whether history was loaded from DB
    db_session_id: Optional[str] = None  # Session ID for DB persistence

    # RAG-specific state
    last_rag_context: Optional[str] = None  # Last retrieved document context
    last_rag_results: Optional[List[dict]] = None  # Raw search results
    last_query: Optional[str] = None  # Last user query
    last_rag_timestamp: float = 0  # When last RAG search was performed
    knowledge_base_id: Optional[int] = None  # Current knowledge base

    # Last action context (for pronoun resolution like "es" referring to last entity)
    last_intent: Optional[dict] = None
    last_action_result: Optional[dict] = None
    last_entities: List[str] = field(default_factory=list)

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

    def update_rag_context(self, context: str, results: List[dict], query: str, kb_id: Optional[int] = None):
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


def is_followup_question(query: str, previous_query: Optional[str] = None) -> bool:
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


# Track background tasks for graceful shutdown
_startup_tasks: List[asyncio.Task] = []

# Global Whisper Service (singleton to avoid reloading model)
_whisper_service: Optional[WhisperService] = None

def get_whisper_service() -> WhisperService:
    """Get or create the global WhisperService instance"""
    global _whisper_service
    if _whisper_service is None:
        _whisper_service = WhisperService()
    return _whisper_service

# Lifecycle Management
@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup und Shutdown Events"""
    logger.info("🚀 Renfield startet...")
    
    # Datenbank initialisieren
    await init_db()
    logger.info("✅ Datenbank initialisiert")

    # Initialize authentication system (roles and default admin)
    try:
        from services.auth_service import ensure_default_roles, ensure_admin_user

        async with AsyncSessionLocal() as db_session:
            # Ensure default roles exist
            roles = await ensure_default_roles(db_session)
            logger.info(f"✅ Auth-Rollen initialisiert: {[r.name for r in roles]}")

            # Ensure default admin user exists (only if no users exist)
            admin = await ensure_admin_user(db_session)
            if admin:
                logger.warning(
                    f"⚠️  Standard-Admin erstellt: '{admin.username}' - "
                    f"BITTE PASSWORT SOFORT ÄNDERN!"
                )
    except Exception as e:
        logger.error(f"❌ Auth-Initialisierung fehlgeschlagen: {e}")
        import traceback
        logger.error(traceback.format_exc())
    
    # Ollama Service starten
    ollama = OllamaService()
    await ollama.ensure_model_loaded()
    app.state.ollama = ollama
    logger.info("✅ Ollama Service bereit")
    
    # Task Queue initialisieren
    task_queue = TaskQueue()
    app.state.task_queue = task_queue
    logger.info("✅ Task Queue bereit")

    # Plugin System (NEW)
    if settings.plugins_enabled:
        try:
            from integrations.core.plugin_loader import PluginLoader
            from integrations.core.plugin_registry import PluginRegistry

            loader = PluginLoader(settings.plugins_dir)
            plugins = loader.load_all_plugins()

            plugin_registry = PluginRegistry()
            plugin_registry.register_plugins(plugins)

            app.state.plugin_registry = plugin_registry
            logger.info(f"✅ Plugin System bereit: {len(plugins)} plugins geladen")
        except Exception as e:
            logger.error(f"❌ Plugin System konnte nicht geladen werden: {e}")
            app.state.plugin_registry = None
    else:
        app.state.plugin_registry = None
        logger.info("⏭️  Plugin System deaktiviert")

    # Whisper Service vorladen (für STT)
    try:
        async def preload_whisper():
            """Lade Whisper-Modell im Hintergrund"""
            try:
                whisper_service = get_whisper_service()
                whisper_service.load_model()
                logger.info("✅ Whisper Service bereit (STT aktiviert)")
            except Exception as e:
                logger.warning(f"⚠️  Whisper konnte nicht vorgeladen werden: {e}")
                logger.warning("💡 Spracheingabe wird beim ersten Gebrauch geladen")

        # Starte im Hintergrund und tracke Task
        task = asyncio.create_task(preload_whisper())
        _startup_tasks.append(task)
    except Exception as e:
        logger.warning(f"⚠️  Whisper-Preloading fehlgeschlagen: {e}")

    # Home Assistant Keywords vorladen (optional, im Hintergrund)
    try:
        from integrations.homeassistant import HomeAssistantClient

        async def preload_keywords():
            """Lade HA Keywords im Hintergrund"""
            try:
                ha_client = HomeAssistantClient()
                keywords = await ha_client.get_keywords()
                logger.info(f"✅ Home Assistant Keywords vorgeladen: {len(keywords)} Keywords")
            except Exception as e:
                logger.warning(f"⚠️  Keywords konnten nicht vorgeladen werden: {e}")

        # Starte im Hintergrund und tracke Task
        task = asyncio.create_task(preload_keywords())
        _startup_tasks.append(task)
    except Exception as e:
        logger.warning(f"⚠️  Keyword-Preloading fehlgeschlagen: {e}")

    # Zeroconf Service für Satellite Auto-Discovery
    zeroconf_service = None
    try:
        from services.zeroconf_service import get_zeroconf_service
        zeroconf_service = get_zeroconf_service(port=8000)
        await zeroconf_service.start()
        app.state.zeroconf_service = zeroconf_service
    except Exception as e:
        logger.warning(f"⚠️  Zeroconf Service konnte nicht gestartet werden: {e}")

    yield

    # Graceful Shutdown
    logger.info("👋 Renfield wird heruntergefahren...")

    # Cancel pending startup tasks
    for task in _startup_tasks:
        if not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    # Notify all connected devices about shutdown
    try:
        device_manager = get_device_manager()
        shutdown_msg = {"type": "server_shutdown", "message": "Server is shutting down"}
        for device in list(device_manager.devices.values()):
            try:
                await device.websocket.send_json(shutdown_msg)
                await device.websocket.close(code=1001, reason="Server shutdown")
            except Exception:
                pass
        logger.info(f"👋 Notified {len(device_manager.devices)} devices about shutdown")
    except Exception as e:
        logger.warning(f"⚠️ Error notifying devices: {e}")

    # Zeroconf Service stoppen
    if zeroconf_service:
        await zeroconf_service.stop()

    logger.info("✅ Shutdown complete")

# FastAPI App erstellen
app = FastAPI(
    title="Renfield AI Assistant",
    description="Vollständig offline-fähiger persönlicher KI-Assistent",
    version="1.0.0",
    lifespan=lifespan
)

# CORS Middleware - configured via settings
_cors_origins = (
    ["*"] if settings.cors_origins == "*"
    else [origin.strip() for origin in settings.cors_origins.split(",")]
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "X-Request-ID"],
)

# Router einbinden
app.include_router(auth.router, prefix="/api/auth", tags=["Authentication"])
app.include_router(roles.router, prefix="/api/roles", tags=["Roles"])
app.include_router(users.router, prefix="/api/users", tags=["Users"])
app.include_router(chat.router, prefix="/api/chat", tags=["Chat"])
app.include_router(tasks.router, prefix="/api/tasks", tags=["Tasks"])
app.include_router(voice.router, prefix="/api/voice", tags=["Voice"])
app.include_router(camera.router, prefix="/api/camera", tags=["Camera"])
app.include_router(ha_routes.router, prefix="/api/homeassistant", tags=["Home Assistant"])
app.include_router(settings_routes.router, prefix="/api/settings", tags=["Settings"])
app.include_router(satellites.router, prefix="/api/satellites", tags=["Satellites"])
app.include_router(speakers.router, prefix="/api/speakers", tags=["Speakers"])
app.include_router(rooms.router, prefix="/api/rooms", tags=["Rooms"])
app.include_router(knowledge.router, prefix="/api/knowledge", tags=["Knowledge"])
app.include_router(plugins.router, prefix="/api/plugins", tags=["Plugins"])
app.include_router(preferences.router, prefix="/api/preferences", tags=["Preferences"])

# Helper function for sending WebSocket errors
async def _send_ws_error(websocket: WebSocket, code: WSErrorCode, message: str, request_id: str = None):
    """Send a structured error response to the WebSocket client."""
    try:
        await websocket.send_json(create_error_response(code, message, request_id))
    except Exception:
        pass


# WebSocket für Echtzeit-Chat
@app.websocket("/ws")
async def websocket_endpoint(
    websocket: WebSocket,
    token: str = Query(None, description="Authentication token")
):
    """WebSocket Verbindung für Echtzeit-Chat"""
    # Get client IP for rate limiting
    ip_address = websocket.client.host if websocket.client else "unknown"

    # Check authentication if enabled
    auth_result = await authenticate_websocket(websocket, token)
    if not auth_result:
        await websocket.close(code=WSAuthError.UNAUTHORIZED, reason="Authentication required")
        return

    await websocket.accept()
    logger.info(f"✅ WebSocket Verbindung hergestellt (IP: {ip_address})")

    # Get rate limiter
    rate_limiter = get_rate_limiter()

    # Initialize session state for conversation persistence
    session_state = ConversationSessionState()

    # Try to auto-detect room context from IP address
    room_context = None
    try:
        if ip_address and ip_address != "unknown":
            from services.room_service import RoomService

            async with AsyncSessionLocal() as db_session:
                room_service = RoomService(db_session)
                room_context = await room_service.get_room_context_by_ip(ip_address)

            if room_context:
                logger.info(f"🏠 Auto-detected room from IP {ip_address}: {room_context.get('room_name')} (device: {room_context.get('device_name', room_context.get('device_id'))})")
            else:
                logger.debug(f"📍 No room context for IP {ip_address}")
    except Exception as e:
        logger.warning(f"⚠️ Failed to detect room context: {e}")

    try:
        while True:
            # Nachricht empfangen
            data = await websocket.receive_json()

            # Rate limiting check
            allowed, reason = rate_limiter.check(ip_address)
            if not allowed:
                await _send_ws_error(websocket, WSErrorCode.RATE_LIMITED, reason)
                continue

            # Validate message
            try:
                msg = WSChatMessage(**data)
                message_type = msg.type
                content = msg.content
                msg_session_id = msg.session_id
                request_id = msg.request_id
                use_rag = msg.use_rag
                knowledge_base_id = msg.knowledge_base_id
            except ValidationError as e:
                await _send_ws_error(websocket, WSErrorCode.INVALID_MESSAGE, str(e))
                continue

            logger.info(f"📨 WebSocket Nachricht: {message_type} - '{content[:100]}' (RAG: {use_rag}, session: {msg_session_id})")

            # Ollama Service (needed for history loading and intent extraction)
            ollama = app.state.ollama
            plugin_registry = app.state.plugin_registry

            # Handle session_id for conversation persistence
            if msg_session_id:
                # Load history from DB if this is the first message with this session_id
                if not session_state.history_loaded or session_state.db_session_id != msg_session_id:
                    session_state.db_session_id = msg_session_id
                    try:
                        async with AsyncSessionLocal() as db_session:
                            db_history = await ollama.load_conversation_context(
                                msg_session_id, db_session, max_messages=10
                            )
                            if db_history:
                                session_state.conversation_history = db_history
                                logger.info(f"📚 Conversation history loaded: {len(db_history)} messages for session {msg_session_id}")
                            session_state.history_loaded = True
                    except Exception as e:
                        logger.warning(f"⚠️ Failed to load conversation history: {e}")

            # Intent extrahieren (mit automatischem Raum-Kontext und Konversationshistorie)
            logger.info("🔍 Extrahiere Intent...")
            intent = await ollama.extract_intent(
                content,
                plugin_registry,
                room_context=room_context,
                conversation_history=session_state.conversation_history if session_state.conversation_history else None
            )
            logger.info(f"🎯 Intent erkannt: {intent.get('intent')} | Entity: {intent.get('parameters', {}).get('entity_id', 'none')}")
            
            # Action ausführen falls nötig
            action_result = None
            if intent.get("intent") != "general.conversation":
                logger.info(f"⚡ Führe Aktion aus: {intent.get('intent')}")
                from services.action_executor import ActionExecutor
                executor = ActionExecutor(plugin_registry)
                action_result = await executor.execute(intent)
                logger.info(f"✅ Aktion: {action_result.get('success')} - {action_result.get('message')}")

                # Update action context for pronoun resolution (e.g., "es aus")
                session_state.update_action_context(intent, action_result)

                # Sende Action-Ergebnis an Frontend
                await websocket.send_json({
                    "type": "action",
                    "intent": intent,
                    "result": action_result
                })
            
            # Response generieren und Text sammeln für TTS
            full_response = ""

            if action_result and action_result.get("success"):
                # Erfolgreiche Aktion - nutze Ergebnis
                result_info = action_result.get('message', '')

                # Füge Daten hinzu, falls vorhanden
                if action_result.get('data'):
                    import json
                    data_str = json.dumps(action_result['data'], ensure_ascii=False, indent=2)
                    result_info = f"{result_info}\n\nDaten:\n{data_str}"

                enhanced_prompt = f"""Der Nutzer hat gefragt: "{content}"

Die Aktion wurde ausgeführt:
{result_info}

Gib eine kurze, natürliche Antwort basierend auf den Daten.
WICHTIG: Nutze die ECHTEN Daten aus dem Ergebnis! Gib NUR die Antwort, KEIN JSON!"""

                # Stream die Antwort (with conversation history for context)
                async for chunk in ollama.chat_stream(enhanced_prompt, history=session_state.conversation_history):
                    full_response += chunk
                    await websocket.send_json({
                        "type": "stream",
                        "content": chunk
                    })

            elif action_result and not action_result.get("success"):
                # Aktion fehlgeschlagen
                full_response = f"Entschuldigung, das konnte ich nicht ausführen: {action_result.get('message')}"
                await websocket.send_json({
                    "type": "stream",
                    "content": full_response
                })

            else:
                # Normale Konversation (optional mit RAG)
                if use_rag and settings.rag_enabled:
                    # RAG-erweiterte Konversation mit Kontext-Persistenz
                    try:
                        from services.rag_service import RAGService

                        rag_context = None
                        is_followup = is_followup_question(content, session_state.last_query)

                        # Check if this is a follow-up question and we have valid cached context
                        if is_followup and session_state.is_rag_context_valid():
                            # Reuse cached context for follow-up questions
                            rag_context = session_state.last_rag_context
                            logger.info(f"📚 RAG Follow-up erkannt, nutze gecachten Kontext ({len(rag_context)} Zeichen)")
                        else:
                            # New search needed
                            async with AsyncSessionLocal() as db_session:
                                rag_service = RAGService(db_session)

                                logger.info(f"📚 RAG Suche: query='{content[:50]}...', kb_id={knowledge_base_id}, is_followup={is_followup}")

                                # Kontext aus Wissensdatenbank abrufen
                                search_results = await rag_service.search(
                                    query=content,
                                    knowledge_base_id=knowledge_base_id
                                )

                                if search_results:
                                    rag_context = await rag_service.get_context(
                                        query=content,
                                        knowledge_base_id=knowledge_base_id
                                    )
                                    # Update session state with new context
                                    session_state.update_rag_context(
                                        context=rag_context,
                                        results=search_results,
                                        query=content,
                                        kb_id=knowledge_base_id
                                    )
                                    logger.info(f"📚 RAG Kontext gefunden und gecacht ({len(rag_context)} Zeichen)")

                        if rag_context:
                            # Sende Info über verwendeten Kontext
                            await websocket.send_json({
                                "type": "rag_context",
                                "has_context": True,
                                "knowledge_base_id": knowledge_base_id,
                                "is_followup": is_followup
                            })

                            # RAG-erweiterte Antwort streamen mit Konversationshistorie
                            async for chunk in ollama.chat_stream_with_rag(
                                content,
                                rag_context,
                                history=session_state.conversation_history if is_followup else None
                            ):
                                full_response += chunk
                                await websocket.send_json({
                                    "type": "stream",
                                    "content": chunk
                                })

                            # Add this exchange to conversation history
                            session_state.add_to_history("user", content)
                            session_state.add_to_history("assistant", full_response)
                        else:
                            logger.info("📚 Kein RAG Kontext gefunden, nutze normale Konversation")
                            await websocket.send_json({
                                "type": "rag_context",
                                "has_context": False,
                                "knowledge_base_id": knowledge_base_id
                            })
                            async for chunk in ollama.chat_stream(content, history=session_state.conversation_history):
                                full_response += chunk
                                await websocket.send_json({
                                    "type": "stream",
                                    "content": chunk
                                })
                    except Exception as e:
                        logger.error(f"❌ RAG-Fehler, Fallback zu normaler Konversation: {e}")
                        import traceback
                        logger.error(traceback.format_exc())
                        async for chunk in ollama.chat_stream(content, history=session_state.conversation_history):
                            full_response += chunk
                            await websocket.send_json({
                                "type": "stream",
                                "content": chunk
                            })
                else:
                    # Standard-Konversation ohne RAG (with conversation history for context)
                    async for chunk in ollama.chat_stream(content, history=session_state.conversation_history):
                        full_response += chunk
                        await websocket.send_json({
                            "type": "stream",
                            "content": chunk
                        })

            # Update conversation history with this exchange (in-memory)
            session_state.add_to_history("user", content)
            if full_response:
                session_state.add_to_history("assistant", full_response)

            # Persist messages to DB if session_id is provided
            if msg_session_id and full_response:
                try:
                    async with AsyncSessionLocal() as db_session:
                        # Save user message
                        await ollama.save_message(
                            msg_session_id, "user", content, db_session,
                            metadata={"room_context": room_context} if room_context else None
                        )
                        # Save assistant response
                        await ollama.save_message(
                            msg_session_id, "assistant", full_response, db_session,
                            metadata={
                                "intent": intent.get("intent") if intent else None,
                                "action_success": action_result.get("success") if action_result else None
                            }
                        )
                        logger.debug(f"💾 Messages saved to DB: session_id={msg_session_id}")
                except Exception as e:
                    logger.warning(f"⚠️ Failed to save messages to DB: {e}")

            # Check for output routing if we have room context
            tts_handled_by_server = False
            if room_context and room_context.get("room_id") and full_response:
                tts_handled_by_server = await _route_chat_tts_output(
                    room_context, full_response, websocket
                )

            # Stream beendet - tell frontend if TTS was handled server-side
            await websocket.send_json({
                "type": "done",
                "tts_handled": tts_handled_by_server
            })

            logger.info(f"✅ WebSocket Response gesendet (tts_handled={tts_handled_by_server})")
            
    except WebSocketDisconnect:
        logger.info("👋 WebSocket Verbindung getrennt")
    except Exception as e:
        logger.error(f"❌ WebSocket Fehler: {e}")
        import traceback
        logger.error(traceback.format_exc())
        await websocket.close()


# WebSocket for Satellite Voice Assistants
@app.websocket("/ws/satellite")
async def satellite_websocket(
    websocket: WebSocket,
    token: str = Query(None, description="Authentication token")
):
    """
    WebSocket endpoint for Raspberry Pi satellite voice assistants.

    Protocol v1.0:
    Satellite → Server:
        - {"type": "register", "satellite_id": str, "room": str, "capabilities": {...}}
        - {"type": "wakeword_detected", "keyword": str, "confidence": float, "session_id": str}
        - {"type": "audio", "chunk": str (base64), "sequence": int, "session_id": str}
        - {"type": "audio_end", "session_id": str, "reason": str}
        - {"type": "heartbeat", "status": str, "uptime_seconds": int}

    Server → Satellite:
        - {"type": "register_ack", "success": bool, "config": {...}, "protocol_version": str}
        - {"type": "state", "state": "idle|listening|processing|speaking"}
        - {"type": "transcription", "session_id": str, "text": str}
        - {"type": "action", "session_id": str, "intent": {...}, "success": bool}
        - {"type": "tts_audio", "session_id": str, "audio": str (base64), "is_final": bool}
        - {"type": "error", "code": str, "message": str}
    """
    # Extract client info
    ip_address = websocket.client.host if websocket.client else "unknown"

    # Check authentication if enabled
    auth_result = await authenticate_websocket(websocket, token)
    if not auth_result:
        await websocket.close(code=WSAuthError.UNAUTHORIZED, reason="Authentication required")
        return

    # Check connection limits
    connection_limiter = get_connection_limiter()
    can_connect, reason = connection_limiter.can_connect(ip_address, f"sat-pending-{ip_address}")
    if not can_connect:
        await websocket.close(code=4003, reason=reason)
        return

    await websocket.accept()
    logger.info(f"📡 Satellite WebSocket connection established (IP: {ip_address})")

    from services.satellite_manager import get_satellite_manager, SatelliteState
    satellite_manager = get_satellite_manager()
    rate_limiter = get_rate_limiter()

    satellite_id = None

    # Conversation history tracking for satellite (in-memory per connection)
    satellite_conversation_history: List[dict] = []
    satellite_history_loaded = False
    satellite_db_session_id = None  # Will be set after registration

    try:
        while True:
            data = await websocket.receive_json()

            # Rate limiting
            rate_key = satellite_id if satellite_id else ip_address
            allowed, rate_reason = rate_limiter.check(rate_key)
            if not allowed:
                await _send_ws_error(websocket, WSErrorCode.RATE_LIMITED, rate_reason)
                continue

            msg_type = data.get("type", "")

            # Handle registration
            if msg_type == "register":
                satellite_id = data.get("satellite_id", "unknown")
                room = data.get("room", "Unknown Room")
                capabilities = data.get("capabilities", {})
                language = data.get("language", settings.default_language)
                version = data.get("version", "unknown")

                # Update connection limiter with actual satellite_id
                connection_limiter.add_connection(ip_address, satellite_id)

                success = await satellite_manager.register(
                    satellite_id=satellite_id,
                    room=room,
                    websocket=websocket,
                    capabilities=capabilities,
                    language=language,
                    version=version
                )

                # Persist room assignment to database
                room_id = None
                if success and settings.rooms_auto_create_from_satellite:
                    try:
                        from services.room_service import RoomService

                        async with AsyncSessionLocal() as db_session:
                            room_service = RoomService(db_session)
                            db_room = await room_service.get_or_create_room_for_satellite(
                                satellite_id=satellite_id,
                                room_name=room,
                                auto_create=True
                            )
                            if db_room:
                                room_id = db_room.id
                                satellite_manager.set_room_id(satellite_id, room_id)
                                logger.info(f"📍 Satellite {satellite_id} linked to room '{db_room.name}' (id: {room_id})")
                    except Exception as e:
                        logger.warning(f"⚠️ Failed to persist room for satellite: {e}")

                # Generate daily DB session ID for conversation persistence
                from datetime import date
                satellite_db_session_id = f"satellite-{satellite_id}-{date.today().isoformat()}"
                logger.info(f"📚 Satellite DB session: {satellite_db_session_id}")

                # Load wake word config from config manager
                wakeword_config_manager = get_wakeword_config_manager()
                async with AsyncSessionLocal() as db_session:
                    wakeword_config = await wakeword_config_manager.get_config(db_session)

                # Subscribe to config updates with device info for tracking
                wakeword_config_manager.subscribe(
                    websocket=websocket,
                    device_id=satellite_id,
                    device_type="satellite"
                )

                await websocket.send_json({
                    "type": "register_ack",
                    "success": success,
                    "config": wakeword_config.to_satellite_config(),
                    "room_id": room_id,
                    "protocol_version": settings.ws_protocol_version,
                    "model_download_url": "/api/settings/wakeword/models",
                })
                logger.info(f"📡 Satellite {satellite_id} registered from {room}")

            # Handle config acknowledgment from satellite
            elif msg_type == "config_ack":
                ack_success = data.get("success", False)
                active_keywords = data.get("active_keywords", [])
                failed_keywords = data.get("failed_keywords", [])
                ack_error = data.get("error")

                wakeword_config_manager = get_wakeword_config_manager()
                wakeword_config_manager.handle_config_ack(
                    device_id=satellite_id,
                    success=ack_success,
                    active_keywords=active_keywords,
                    failed_keywords=failed_keywords,
                    error=ack_error,
                )

            # Handle wake word detection
            elif msg_type == "wakeword_detected":
                keyword = data.get("keyword", "unknown")
                confidence = data.get("confidence", 0.0)
                sat_id = data.get("satellite_id", satellite_id)
                # Use the session_id provided by the satellite (important for matching audio chunks)
                client_session_id = data.get("session_id")

                session_id = await satellite_manager.start_session(
                    satellite_id=sat_id,
                    keyword=keyword,
                    confidence=confidence,
                    session_id=client_session_id  # Use satellite's session ID
                )

                if session_id:
                    logger.info(f"🎙️ Wake word '{keyword}' detected by {sat_id}, session: {session_id}")
                else:
                    logger.warning(f"⚠️ Could not start session for {sat_id}")

            # Handle audio chunks
            elif msg_type == "audio":
                session_id = data.get("session_id")
                chunk_b64 = data.get("chunk", "")
                sequence = data.get("sequence", 0)

                if session_id and chunk_b64:
                    success, error = satellite_manager.buffer_audio(session_id, chunk_b64, sequence)
                    if not success:
                        # End session on buffer full to prevent further errors
                        if "buffer full" in error.lower():
                            await satellite_manager.end_session(session_id, reason="buffer_full")
                        await _send_ws_error(websocket, WSErrorCode.BUFFER_FULL, error)

            # Handle end of audio
            elif msg_type == "audio_end":
                session_id = data.get("session_id")
                reason = data.get("reason", "unknown")

                if not session_id:
                    continue

                logger.info(f"🔚 Audio ended for session {session_id} (reason: {reason})")

                # Update state to processing
                await satellite_manager.set_session_state(session_id, SatelliteState.PROCESSING)

                # Get audio buffer
                audio_bytes = satellite_manager.get_audio_buffer(session_id)

                if not audio_bytes:
                    logger.warning(f"⚠️ No audio buffered for session {session_id}")
                    await satellite_manager.end_session(session_id, reason="no_audio")
                    continue

                logger.info(f"🎵 Processing {len(audio_bytes)} bytes of audio")

                # Get satellite's configured language
                satellite_info = satellite_manager.get_satellite_by_session(session_id)
                satellite_language = satellite_info.language if satellite_info else settings.default_language
                logger.info(f"🌐 Using language: {satellite_language}")

                # Transcribe with Whisper (with speaker recognition)
                try:
                    whisper = get_whisper_service()
                    whisper.load_model()  # No-op if already loaded

                    # Create WAV file with proper header
                    import io
                    import wave
                    wav_buffer = io.BytesIO()
                    with wave.open(wav_buffer, 'wb') as wav_file:
                        wav_file.setnchannels(1)
                        wav_file.setsampwidth(2)  # 16-bit
                        wav_file.setframerate(16000)
                        wav_file.writeframes(audio_bytes)

                    wav_bytes = wav_buffer.getvalue()
                    logger.info(f"📦 Created WAV: {len(wav_bytes)} bytes")

                    # Transcribe with speaker recognition (if enabled)
                    speaker_name = None
                    speaker_alias = None
                    speaker_confidence = 0.0

                    if settings.speaker_recognition_enabled:
                        async with AsyncSessionLocal() as db_session:
                            result = await whisper.transcribe_bytes_with_speaker(
                                wav_bytes,
                                filename="satellite_audio.wav",
                                db_session=db_session,
                                language=satellite_language
                            )
                            text = result.get("text", "")
                            speaker_name = result.get("speaker_name")
                            speaker_alias = result.get("speaker_alias")
                            speaker_confidence = result.get("speaker_confidence", 0.0)

                            if speaker_name:
                                logger.info(f"🎤 Satellite Sprecher erkannt: {speaker_name} (@{speaker_alias}) - Konfidenz: {speaker_confidence:.2f}")
                            else:
                                logger.info("🎤 Satellite Sprecher nicht erkannt")
                    else:
                        text = await whisper.transcribe_bytes(wav_bytes, "satellite_audio.wav", language=satellite_language)

                    if not text or not text.strip():
                        logger.warning(f"⚠️ Empty transcription for session {session_id}")
                        await satellite_manager.end_session(session_id, reason="empty_transcription")
                        continue

                    logger.info(f"📝 Transcription: '{text}'")
                    await satellite_manager.send_transcription(session_id, text)

                except Exception as e:
                    logger.error(f"❌ Whisper transcription failed: {e}")
                    import traceback
                    logger.error(traceback.format_exc())
                    await satellite_manager.end_session(session_id, reason="transcription_error")
                    continue

                # Process with Ollama (intent extraction + action)
                try:
                    ollama = app.state.ollama
                    plugin_registry = app.state.plugin_registry

                    # Load conversation history from DB if not already loaded (once per day)
                    if satellite_db_session_id and not satellite_history_loaded:
                        try:
                            async with AsyncSessionLocal() as db_session:
                                db_history = await ollama.load_conversation_context(
                                    satellite_db_session_id, db_session, max_messages=5
                                )
                                if db_history:
                                    satellite_conversation_history.extend(db_history)
                                    logger.info(f"📚 Satellite conversation history loaded: {len(db_history)} messages")
                                satellite_history_loaded = True
                        except Exception as e:
                            logger.warning(f"⚠️ Failed to load satellite conversation history: {e}")

                    # Build room context for satellite
                    satellite = satellite_manager.get_satellite(satellite_id)
                    room_context = None
                    if satellite:
                        room_context = {
                            "room_name": satellite.room,
                            "room_id": satellite.room_id,
                            "device_type": "satellite",
                        }
                        if speaker_name:
                            room_context["speaker_name"] = speaker_name
                        if speaker_alias:
                            room_context["speaker_alias"] = speaker_alias

                        logger.info(f"🏠 Satellite room context: {satellite.room} (ID: {satellite.room_id})")

                    # Extract intent with room context and conversation history
                    intent = await ollama.extract_intent(
                        text,
                        plugin_registry,
                        room_context=room_context,
                        conversation_history=satellite_conversation_history if satellite_conversation_history else None
                    )
                    logger.info(f"🎯 Intent: {intent.get('intent')}")

                    # Execute action if needed
                    action_result = None
                    if intent.get("intent") != "general.conversation":
                        from services.action_executor import ActionExecutor
                        executor = ActionExecutor(plugin_registry)
                        action_result = await executor.execute(intent)
                        logger.info(f"⚡ Action result: {action_result.get('success')}")
                        await satellite_manager.send_action_result(
                            session_id, intent, action_result.get("success", False)
                        )

                    # Generate response (with conversation history for context)
                    response_text = ""
                    if action_result and action_result.get("success"):
                        result_info = action_result.get("message", "")
                        enhanced_prompt = f"""Der Nutzer hat gefragt: "{text}"
Die Aktion wurde ausgeführt: {result_info}
Gib eine kurze, natürliche Antwort. KEIN JSON, nur Text."""

                        async for chunk in ollama.chat_stream(enhanced_prompt, history=satellite_conversation_history):
                            response_text += chunk
                    elif action_result and not action_result.get("success"):
                        response_text = f"Entschuldigung, das konnte ich nicht ausführen: {action_result.get('message')}"
                    else:
                        # Normal conversation (with history for follow-up questions)
                        async for chunk in ollama.chat_stream(text, history=satellite_conversation_history):
                            response_text += chunk

                    logger.info(f"💬 Response: '{response_text[:100]}...'")

                    # Update in-memory conversation history (keep max 5 exchanges = 10 messages)
                    satellite_conversation_history.append({"role": "user", "content": text})
                    satellite_conversation_history.append({"role": "assistant", "content": response_text})
                    if len(satellite_conversation_history) > 10:
                        satellite_conversation_history[:] = satellite_conversation_history[-10:]

                    # Persist messages to DB if we have a session ID
                    if satellite_db_session_id and response_text:
                        try:
                            async with AsyncSessionLocal() as db_session:
                                await ollama.save_message(
                                    satellite_db_session_id, "user", text, db_session,
                                    metadata={
                                        "satellite_id": satellite_id,
                                        "room": satellite.room if satellite else None,
                                        "speaker": speaker_name
                                    }
                                )
                                await ollama.save_message(
                                    satellite_db_session_id, "assistant", response_text, db_session,
                                    metadata={
                                        "intent": intent.get("intent") if intent else None,
                                        "action_success": action_result.get("success") if action_result else None
                                    }
                                )
                                logger.debug(f"💾 Satellite messages saved to DB: {satellite_db_session_id}")
                        except Exception as e:
                            logger.warning(f"⚠️ Failed to save satellite messages to DB: {e}")

                    # Generate TTS with satellite's language
                    from services.piper_service import PiperService
                    piper = PiperService()
                    tts_audio = await piper.synthesize_to_bytes(response_text, language=satellite_language)

                    if tts_audio:
                        # Route TTS to the best available output device
                        await _route_satellite_tts_output(
                            satellite_manager, satellite, session_id, tts_audio
                        )
                    else:
                        logger.warning(f"⚠️ TTS synthesis failed for session {session_id}")

                except Exception as e:
                    logger.error(f"❌ Processing failed: {e}")
                    import traceback
                    logger.error(traceback.format_exc())

                # End session
                await satellite_manager.end_session(session_id, reason="completed")

            # Handle heartbeat with optional metrics
            elif msg_type == "heartbeat":
                if satellite_id:
                    # Extract metrics and version from heartbeat if present
                    metrics = data.get("metrics")
                    version = data.get("version")
                    satellite_manager.update_heartbeat(satellite_id, metrics, version)
                    # Send heartbeat ack
                    await websocket.send_json({"type": "heartbeat_ack"})

            # Handle OTA update progress
            elif msg_type == "update_progress":
                if satellite_id:
                    from services.satellite_manager import UpdateStatus
                    stage = data.get("stage", "unknown")
                    progress = data.get("progress", 0)
                    message = data.get("message", "")
                    logger.info(f"📥 Update progress from {satellite_id}: {stage} ({progress}%) - {message}")
                    satellite_manager.set_update_status(
                        satellite_id,
                        UpdateStatus.IN_PROGRESS,
                        stage=stage,
                        progress=progress
                    )

            # Handle OTA update complete
            elif msg_type == "update_complete":
                if satellite_id:
                    from services.satellite_manager import UpdateStatus
                    success = data.get("success", False)
                    old_version = data.get("old_version", "unknown")
                    new_version = data.get("new_version", "unknown")

                    if success:
                        logger.info(f"✅ Satellite {satellite_id} updated: {old_version} → {new_version}")
                        satellite_manager.set_update_status(
                            satellite_id,
                            UpdateStatus.COMPLETED,
                            stage="completed",
                            progress=100
                        )
                        # Update the stored version
                        sat = satellite_manager.get_satellite(satellite_id)
                        if sat:
                            sat.version = new_version
                    else:
                        error = data.get("error", "Unknown error")
                        logger.error(f"❌ Satellite {satellite_id} update failed: {error}")
                        satellite_manager.set_update_status(
                            satellite_id,
                            UpdateStatus.FAILED,
                            stage="failed",
                            progress=0,
                            error=error
                        )

            # Handle OTA update failed
            elif msg_type == "update_failed":
                if satellite_id:
                    from services.satellite_manager import UpdateStatus
                    stage = data.get("stage", "unknown")
                    error = data.get("error", "Unknown error")
                    rolled_back = data.get("rolled_back", False)
                    logger.error(f"❌ Satellite {satellite_id} update failed at {stage}: {error} (rolled_back: {rolled_back})")
                    satellite_manager.set_update_status(
                        satellite_id,
                        UpdateStatus.FAILED,
                        stage=stage,
                        progress=0,
                        error=error
                    )

    except WebSocketDisconnect:
        logger.info(f"👋 Satellite WebSocket disconnected: {satellite_id}")
    except Exception as e:
        logger.error(f"❌ Satellite WebSocket error: {e}")
        import traceback
        logger.error(traceback.format_exc())
    finally:
        # Clean up connection limiter
        if satellite_id and ip_address:
            connection_limiter.remove_connection(ip_address, satellite_id)

        if satellite_id:
            # Mark satellite offline in database
            try:
                from services.room_service import RoomService

                async with AsyncSessionLocal() as db_session:
                    room_service = RoomService(db_session)
                    await room_service.set_satellite_online(satellite_id, False)
            except Exception as e:
                logger.warning(f"⚠️ Failed to mark satellite offline: {e}")

            # Unsubscribe from wake word config updates
            wakeword_config_manager = get_wakeword_config_manager()
            wakeword_config_manager.unsubscribe(websocket)

            await satellite_manager.unregister(satellite_id)


# Unified WebSocket for All Device Types (Satellites + Web Clients)
@app.websocket("/ws/device")
async def device_websocket(
    websocket: WebSocket,
    token: str = Query(None, description="Authentication token")
):
    """
    Unified WebSocket endpoint for all device types (satellites and web clients).

    Supports:
    - Physical satellites (Raspberry Pi with ReSpeaker)
    - Web panels (stationary iPad/tablet)
    - Web tablets (mobile tablet)
    - Web browsers (desktop browser)
    - Web kiosks (touch terminals)

    Protocol v1.0:
    Device → Server:
        - {"type": "register", "device_id": str, "device_type": str, "room": str,
           "capabilities": {...}, "device_name": str?, "is_stationary": bool?, "protocol_version": str?}
        - {"type": "wakeword_detected", "keyword": str, "confidence": float, "session_id": str?}
        - {"type": "audio", "chunk": str (base64), "sequence": int, "session_id": str}
        - {"type": "audio_end", "session_id": str, "reason": str}
        - {"type": "start_session"} - Manual session start (web clients without wakeword)
        - {"type": "text", "content": str, "session_id": str?} - Text input (web clients)
        - {"type": "heartbeat", "status": str}

    Server → Device:
        - {"type": "register_ack", "success": bool, "config": {...}, "room_id": int?, "protocol_version": str}
        - {"type": "state", "state": "idle|listening|processing|speaking"}
        - {"type": "transcription", "session_id": str, "text": str, "speaker_name": str?, ...}
        - {"type": "action", "session_id": str, "intent": {...}, "success": bool}
        - {"type": "tts_audio", "session_id": str, "audio": str (base64), "is_final": bool}
        - {"type": "response_text", "session_id": str, "text": str, "is_final": bool}
        - {"type": "stream", "session_id": str, "content": str}
        - {"type": "session_end", "session_id": str, "reason": str}
        - {"type": "error", "code": str, "message": str}
    """
    # Extract client info
    user_agent = websocket.headers.get("user-agent", "") if websocket.headers else ""
    ip_address = websocket.client.host if websocket.client else "unknown"

    # Check authentication if enabled
    auth_result = await authenticate_websocket(websocket, token)
    if not auth_result:
        await websocket.close(code=WSAuthError.UNAUTHORIZED, reason="Authentication required")
        return

    # Check connection limits
    connection_limiter = get_connection_limiter()
    can_connect, reason = connection_limiter.can_connect(ip_address, f"pending-{ip_address}")
    if not can_connect:
        await websocket.close(code=4003, reason=reason)
        return

    await websocket.accept()
    logger.info(f"📱 Device WebSocket connection established (IP: {ip_address})")

    device_manager = get_device_manager()
    rate_limiter = get_rate_limiter()
    device_id = None

    try:
        while True:
            data = await websocket.receive_json()

            # Rate limiting (use device_id if registered, otherwise IP)
            rate_key = device_id if device_id else ip_address
            allowed, reason = rate_limiter.check(rate_key)
            if not allowed:
                await _send_ws_error(websocket, WSErrorCode.RATE_LIMITED, reason)
                continue

            msg_type = data.get("type", "")

            # === REGISTRATION ===
            if msg_type == "register":
                # Validate registration message
                try:
                    reg_msg = WSRegisterMessage(**data)
                    device_id = reg_msg.device_id
                    device_type = reg_msg.device_type
                    room = reg_msg.room
                    device_name = reg_msg.device_name
                    is_stationary = reg_msg.is_stationary
                    client_protocol = reg_msg.protocol_version
                except ValidationError as e:
                    await _send_ws_error(websocket, WSErrorCode.INVALID_MESSAGE, f"Invalid registration: {e}")
                    continue

                # Validate device type
                if device_type not in DEVICE_TYPES:
                    logger.warning(f"⚠️ Unknown device type: {device_type}, defaulting to web_browser")
                    device_type = DEVICE_TYPE_WEB_BROWSER

                # Check connection limits with actual device_id
                can_connect, conn_reason = connection_limiter.can_connect(ip_address, device_id)
                if not can_connect:
                    await _send_ws_error(websocket, WSErrorCode.DEVICE_ERROR, conn_reason)
                    continue

                # Track connection
                connection_limiter.add_connection(ip_address, device_id)

                # Merge default capabilities with provided ones
                default_caps = DEFAULT_CAPABILITIES.get(device_type, {}).copy()
                provided_caps = reg_msg.capabilities.model_dump() if reg_msg.capabilities else {}
                capabilities = {**default_caps, **provided_caps}

                # Register device
                success = await device_manager.register(
                    device_id=device_id,
                    device_type=device_type,
                    room=room,
                    websocket=websocket,
                    capabilities=capabilities,
                    device_name=device_name,
                    is_stationary=is_stationary,
                    user_agent=user_agent,
                    ip_address=ip_address
                )

                # Persist to database
                room_id = None
                if success:
                    try:
                        from services.room_service import RoomService

                        async with AsyncSessionLocal() as db_session:
                            room_service = RoomService(db_session)
                            db_device = await room_service.register_device(
                                device_id=device_id,
                                room_name=room,
                                device_type=device_type,
                                device_name=device_name,
                                capabilities=capabilities,
                                is_stationary=is_stationary,
                                user_agent=user_agent,
                                ip_address=ip_address
                            )
                            if db_device:
                                room_id = db_device.room_id
                                device_manager.set_room_id(device_id, room_id)
                                logger.info(f"📍 Device {device_id} linked to room '{room}' (id: {room_id})")
                    except Exception as e:
                        logger.warning(f"⚠️ Failed to persist device to database: {e}")

                # Load wake word config from config manager
                wakeword_config_manager = get_wakeword_config_manager()
                async with AsyncSessionLocal() as db_session:
                    wakeword_config = await wakeword_config_manager.get_config(db_session)

                # Subscribe to config updates with device info for tracking
                wakeword_config_manager.subscribe(
                    websocket=websocket,
                    device_id=device_id,
                    device_type="web_device"
                )

                # Send registration acknowledgement with protocol version
                await websocket.send_json({
                    "type": "register_ack",
                    "success": success,
                    "device_id": device_id,
                    "config": wakeword_config.to_satellite_config(),
                    "room_id": room_id,
                    "capabilities": capabilities,
                    "protocol_version": settings.ws_protocol_version
                })

                type_emoji = "📡" if device_type == DEVICE_TYPE_SATELLITE else "📱"
                logger.info(f"{type_emoji} Device {device_id} ({device_type}) registered in '{room}'")

            # === CONFIG ACK (after config_update is applied) ===
            elif msg_type == "config_ack":
                ack_success = data.get("success", False)
                active_keywords = data.get("active_keywords", [])
                failed_keywords = data.get("failed_keywords", [])
                ack_error = data.get("error")

                wakeword_config_manager = get_wakeword_config_manager()
                wakeword_config_manager.handle_config_ack(
                    device_id=device_id,
                    success=ack_success,
                    active_keywords=active_keywords,
                    failed_keywords=failed_keywords,
                    error=ack_error,
                )

            # === WAKE WORD DETECTION (satellites and web clients with wakeword) ===
            elif msg_type == "wakeword_detected":
                keyword = data.get("keyword", "unknown")
                confidence = data.get("confidence", 0.0)
                client_session_id = data.get("session_id")

                session_id = await device_manager.start_session(
                    device_id=device_id,
                    keyword=keyword,
                    confidence=confidence,
                    session_id=client_session_id
                )

                if session_id:
                    logger.info(f"🎙️ Wake word '{keyword}' detected by {device_id}, session: {session_id}")
                else:
                    logger.warning(f"⚠️ Could not start session for {device_id}")

            # === MANUAL SESSION START (web clients without wakeword) ===
            elif msg_type == "start_session":
                session_id = await device_manager.start_session(
                    device_id=device_id,
                    keyword=None,
                    confidence=0.0
                )

                if session_id:
                    await websocket.send_json({
                        "type": "session_started",
                        "session_id": session_id
                    })
                    logger.info(f"🎙️ Manual session started by {device_id}: {session_id}")
                else:
                    await websocket.send_json({
                        "type": "error",
                        "message": "Could not start session"
                    })

            # === AUDIO STREAMING ===
            elif msg_type == "audio":
                # Validate audio message
                try:
                    audio_msg = WSAudioMessage(**data)
                    session_id = audio_msg.session_id
                    chunk_b64 = audio_msg.chunk
                    sequence = audio_msg.sequence
                except ValidationError as e:
                    await _send_ws_error(websocket, WSErrorCode.INVALID_MESSAGE, f"Invalid audio message: {e}")
                    continue

                if session_id and chunk_b64:
                    success, error = device_manager.buffer_audio(session_id, chunk_b64, sequence)
                    if not success:
                        # End session on buffer full to prevent further errors
                        if "buffer full" in error.lower():
                            await device_manager.end_session(session_id, reason="buffer_full")
                        await _send_ws_error(websocket, WSErrorCode.BUFFER_FULL, error)

            # === END OF AUDIO / PROCESS REQUEST ===
            elif msg_type == "audio_end":
                session_id = data.get("session_id")
                reason = data.get("reason", "unknown")

                if not session_id:
                    continue

                logger.info(f"🔚 Audio ended for session {session_id} (reason: {reason})")
                await _process_device_session(app, device_manager, session_id)

            # === TEXT INPUT (web clients) ===
            elif msg_type == "text":
                content = data.get("content", "").strip()
                session_id = data.get("session_id")

                if not content:
                    continue

                # Start session if not provided
                if not session_id:
                    session_id = await device_manager.start_session(device_id=device_id)

                if not session_id:
                    continue

                logger.info(f"📝 Text input from {device_id}: '{content[:50]}...'")

                # Process text directly (no STT needed)
                await _process_text_input(app, device_manager, session_id, content)

            # === HEARTBEAT ===
            elif msg_type == "heartbeat":
                if device_id:
                    device_manager.update_heartbeat(device_id)
                    await websocket.send_json({"type": "heartbeat_ack"})

    except WebSocketDisconnect:
        logger.info(f"👋 Device WebSocket disconnected: {device_id}")
    except Exception as e:
        logger.error(f"❌ Device WebSocket error: {e}")
        import traceback
        logger.error(traceback.format_exc())
    finally:
        # Clean up connection limiter
        if device_id and ip_address:
            connection_limiter.remove_connection(ip_address, device_id)

        if device_id:
            # Mark device offline in database
            try:
                from services.room_service import RoomService

                async with AsyncSessionLocal() as db_session:
                    room_service = RoomService(db_session)
                    await room_service.set_device_online(device_id, False)
            except Exception as e:
                logger.warning(f"⚠️ Failed to mark device offline: {e}")

            # Unsubscribe from wake word config updates
            wakeword_config_manager = get_wakeword_config_manager()
            wakeword_config_manager.unsubscribe(websocket)

            await device_manager.unregister(device_id)


async def _process_device_session(app: FastAPI, device_manager: DeviceManager, session_id: str):
    """Process audio from a device session (STT → Intent → Action → Response → TTS)"""

    # Update state to processing
    await device_manager.set_session_state(session_id, DeviceState.PROCESSING)

    # Get audio buffer
    audio_bytes = device_manager.get_audio_buffer(session_id)

    if not audio_bytes:
        logger.warning(f"⚠️ No audio buffered for session {session_id}")
        await device_manager.end_session(session_id, reason="no_audio")
        return

    logger.info(f"🎵 Processing {len(audio_bytes)} bytes of audio")

    # Get device info for capability checks
    device = device_manager.get_device_by_session(session_id)

    # Transcribe with Whisper
    try:
        from services.database import AsyncSessionLocal
        whisper = get_whisper_service()
        whisper.load_model()

        # Create WAV file with proper header
        import io
        import wave
        wav_buffer = io.BytesIO()
        with wave.open(wav_buffer, 'wb') as wav_file:
            wav_file.setnchannels(1)
            wav_file.setsampwidth(2)  # 16-bit
            wav_file.setframerate(16000)
            wav_file.writeframes(audio_bytes)

        wav_bytes = wav_buffer.getvalue()
        logger.info(f"📦 Created WAV: {len(wav_bytes)} bytes")

        # Transcribe with speaker recognition (if enabled)
        speaker_name = None
        speaker_alias = None
        speaker_confidence = 0.0

        if settings.speaker_recognition_enabled:
            async with AsyncSessionLocal() as db_session:
                result = await whisper.transcribe_bytes_with_speaker(
                    wav_bytes,
                    filename="device_audio.wav",
                    db_session=db_session
                )
                text = result.get("text", "")
                speaker_name = result.get("speaker_name")
                speaker_alias = result.get("speaker_alias")
                speaker_confidence = result.get("speaker_confidence", 0.0)

                if speaker_name:
                    logger.info(f"🎤 Speaker identified: {speaker_name} (@{speaker_alias}) - {speaker_confidence:.2f}")
        else:
            text = await whisper.transcribe_bytes(wav_bytes, "device_audio.wav")

        if not text or not text.strip():
            logger.warning(f"⚠️ Empty transcription for session {session_id}")
            await device_manager.end_session(session_id, reason="empty_transcription")
            return

        logger.info(f"📝 Transcription: '{text}'")
        await device_manager.send_transcription(session_id, text, speaker_name, speaker_alias)

        # Process the transcribed text
        await _process_text_input(app, device_manager, session_id, text, speaker_name, speaker_alias)

    except Exception as e:
        logger.error(f"❌ Audio processing failed: {e}")
        import traceback
        logger.error(traceback.format_exc())
        await device_manager.end_session(session_id, reason="processing_error")


async def _process_text_input(
    app: FastAPI,
    device_manager: DeviceManager,
    session_id: str,
    text: str,
    speaker_name: Optional[str] = None,
    speaker_alias: Optional[str] = None
):
    """Process text input (from transcription or direct text) through intent → action → response"""

    device = device_manager.get_device_by_session(session_id)
    session = device_manager.get_session(session_id)

    if not device or not session:
        return

    try:
        ollama = app.state.ollama
        plugin_registry = app.state.plugin_registry

        # Build room context for intent processing
        room_context = None
        if device:
            room_context = {
                "room_name": device.room,
                "room_id": device.room_id,
                "device_type": device.device_type,
            }
            # Add speaker info if available
            if speaker_name:
                room_context["speaker_name"] = speaker_name
            if speaker_alias:
                room_context["speaker_alias"] = speaker_alias

            logger.info(f"🏠 Room context: {device.room} (ID: {device.room_id})")

        # Extract intent with room context
        intent = await ollama.extract_intent(text, plugin_registry, room_context=room_context)
        logger.info(f"🎯 Intent: {intent.get('intent')}")

        # Execute action if needed
        action_result = None
        if intent.get("intent") != "general.conversation":
            from services.action_executor import ActionExecutor
            executor = ActionExecutor(plugin_registry)
            action_result = await executor.execute(intent)
            logger.info(f"⚡ Action result: {action_result.get('success')}")
            await device_manager.send_action_result(session_id, intent, action_result.get("success", False))

        # Generate response
        response_text = ""
        if action_result and action_result.get("success"):
            result_info = action_result.get("message", "")
            enhanced_prompt = f"""Der Nutzer hat gefragt: "{text}"
Die Aktion wurde ausgeführt: {result_info}
Gib eine kurze, natürliche Antwort. KEIN JSON, nur Text."""

            async for chunk in ollama.chat_stream(enhanced_prompt):
                response_text += chunk
                # Send streaming chunks to display-capable devices
                if device.capabilities.has_display:
                    await device_manager.send_stream_chunk(session_id, chunk)

        elif action_result and not action_result.get("success"):
            response_text = f"Entschuldigung, das konnte ich nicht ausführen: {action_result.get('message')}"
            if device.capabilities.has_display:
                await device_manager.send_stream_chunk(session_id, response_text)
        else:
            # Normal conversation
            async for chunk in ollama.chat_stream(text):
                response_text += chunk
                if device.capabilities.has_display:
                    await device_manager.send_stream_chunk(session_id, chunk)

        logger.info(f"💬 Response: '{response_text[:100]}...'")

        # Send final response text (for display devices)
        if device.capabilities.has_display:
            await device_manager.send_response_text(session_id, response_text, is_final=True)

        # Generate TTS audio if response text exists
        if response_text:
            from services.piper_service import PiperService
            piper = PiperService()
            tts_audio = await piper.synthesize_to_bytes(response_text)

            if tts_audio:
                # Route TTS to the best available output device
                await _route_tts_output(device_manager, device, session_id, tts_audio)
            else:
                logger.warning(f"⚠️ TTS synthesis failed for session {session_id}")

    except Exception as e:
        logger.error(f"❌ Text processing failed: {e}")
        import traceback
        logger.error(traceback.format_exc())

    # End session
    await device_manager.end_session(session_id, reason="completed")


async def _route_tts_output(
    device_manager: DeviceManager,
    device,
    session_id: str,
    tts_audio: bytes
):
    """
    Route TTS audio to the best available output device.

    Routing logic:
    1. If device has no room_id → output on input device
    2. Check configured output devices for room
    3. Use best available output device (by priority)
    4. Fallback to input device if no output devices available
    """
    # If device is not registered or has no room, fallback to input device
    if not device.room_id:
        logger.debug(f"Device {device.device_id} has no room_id, using input device for output")
        if device.capabilities.has_speaker:
            await device_manager.send_tts_audio(session_id, tts_audio, is_final=True)
        return

    try:
        from services.database import AsyncSessionLocal
        from services.output_routing_service import OutputRoutingService
        from services.audio_output_service import get_audio_output_service

        async with AsyncSessionLocal() as db_session:
            routing_service = OutputRoutingService(db_session)
            audio_output_service = get_audio_output_service()

            # Get the best audio output device for this room
            decision = await routing_service.get_audio_output_for_room(
                room_id=device.room_id,
                input_device_id=device.device_id
            )

            logger.info(f"🔊 Output routing decision: {decision.reason} → {decision.target_type}:{decision.target_id}")

            if decision.output_device and not decision.fallback_to_input:
                # Use configured output device
                success = await audio_output_service.play_audio(
                    audio_bytes=tts_audio,
                    output_device=decision.output_device,
                    session_id=session_id
                )

                if not success:
                    # Fallback to input device if output failed
                    logger.warning(f"Output device playback failed, falling back to input device")
                    if device.capabilities.has_speaker:
                        await device_manager.send_tts_audio(session_id, tts_audio, is_final=True)
            else:
                # Fallback to input device
                if device.capabilities.has_speaker:
                    await device_manager.send_tts_audio(session_id, tts_audio, is_final=True)

    except Exception as e:
        logger.error(f"❌ Output routing failed: {e}, falling back to input device")
        import traceback
        logger.error(traceback.format_exc())
        # Fallback to input device on error
        if device.capabilities.has_speaker:
            await device_manager.send_tts_audio(session_id, tts_audio, is_final=True)


async def _route_chat_tts_output(
    room_context: dict,
    response_text: str,
    websocket: WebSocket
) -> bool:
    """
    Route TTS audio for chat WebSocket to the best available output device.

    Returns True if TTS was handled server-side (sent to HA media player),
    False if frontend should handle TTS.
    """
    room_id = room_context.get("room_id")
    device_id = room_context.get("device_id")

    if not room_id:
        return False

    try:
        from services.database import AsyncSessionLocal
        from services.output_routing_service import OutputRoutingService
        from services.audio_output_service import get_audio_output_service

        async with AsyncSessionLocal() as db_session:
            routing_service = OutputRoutingService(db_session)

            # Get the best audio output device for this room
            decision = await routing_service.get_audio_output_for_room(
                room_id=room_id,
                input_device_id=device_id
            )

            logger.info(f"🔊 Chat output routing: {decision.reason} → {decision.target_type}:{decision.target_id}")

            # Only handle server-side if we have a configured HA output device
            # (Renfield devices would be the input device itself in this case)
            if decision.output_device and not decision.fallback_to_input and decision.target_type == "homeassistant":
                # Generate TTS
                from services.piper_service import PiperService
                piper = PiperService()
                tts_audio = await piper.synthesize_to_bytes(response_text)

                if tts_audio:
                    audio_output_service = get_audio_output_service()
                    success = await audio_output_service.play_audio(
                        audio_bytes=tts_audio,
                        output_device=decision.output_device,
                        session_id=f"chat-{room_id}-{device_id}"
                    )

                    if success:
                        logger.info(f"🔊 TTS sent to HA media player: {decision.target_id}")
                        return True
                    else:
                        logger.warning(f"Failed to send TTS to {decision.target_id}, frontend will handle")

            return False

    except Exception as e:
        logger.error(f"❌ Chat TTS routing failed: {e}")
        import traceback
        logger.error(traceback.format_exc())
        return False


async def _route_satellite_tts_output(
    satellite_manager,
    satellite,
    session_id: str,
    tts_audio: bytes
):
    """
    Route TTS audio for satellite devices to the best available output device.

    Similar to _route_tts_output but for the satellite WebSocket handler.
    """
    # If satellite has no room_id, fallback to satellite itself
    if not satellite or not satellite.room_id:
        logger.debug(f"Satellite has no room_id, using satellite for output")
        await satellite_manager.send_tts_audio(session_id, tts_audio, is_final=True)
        return

    try:
        from services.database import AsyncSessionLocal
        from services.output_routing_service import OutputRoutingService
        from services.audio_output_service import get_audio_output_service

        async with AsyncSessionLocal() as db_session:
            routing_service = OutputRoutingService(db_session)
            audio_output_service = get_audio_output_service()

            # Get the best audio output device for this room
            decision = await routing_service.get_audio_output_for_room(
                room_id=satellite.room_id,
                input_device_id=satellite.satellite_id
            )

            logger.info(f"🔊 Satellite output routing: {decision.reason} → {decision.target_type}:{decision.target_id}")

            if decision.output_device and not decision.fallback_to_input:
                # Use configured output device
                success = await audio_output_service.play_audio(
                    audio_bytes=tts_audio,
                    output_device=decision.output_device,
                    session_id=session_id
                )

                if not success:
                    # Fallback to satellite if output failed
                    logger.warning(f"Output device playback failed, falling back to satellite")
                    await satellite_manager.send_tts_audio(session_id, tts_audio, is_final=True)
            else:
                # Fallback to satellite
                await satellite_manager.send_tts_audio(session_id, tts_audio, is_final=True)

    except Exception as e:
        logger.error(f"❌ Satellite output routing failed: {e}, falling back to satellite")
        import traceback
        logger.error(traceback.format_exc())
        # Fallback to satellite on error
        await satellite_manager.send_tts_audio(session_id, tts_audio, is_final=True)


# WebSocket for Wake Word Detection (Server-Side Fallback)
@app.websocket("/ws/wakeword")
async def wakeword_websocket(websocket: WebSocket):
    """
    WebSocket endpoint for server-side wake word detection.

    This is a fallback for clients where browser-based WASM detection
    is not available or performant.

    Protocol:
    - Client sends: Raw audio bytes (16-bit PCM, 16kHz, mono)
                   Expected: 2560 bytes per chunk (1280 samples * 2 bytes)
    - Server sends: JSON messages
                   {"type": "ready"} - Service is ready
                   {"type": "wakeword_detected", "keyword": str, "score": float}
                   {"type": "error", "message": str}
    """
    await websocket.accept()
    logger.info("🎤 Wake word WebSocket connection established")

    try:
        from services.wakeword_service import get_wakeword_service

        service = get_wakeword_service()

        # Check if service is available
        if not service.available:
            await websocket.send_json({
                "type": "error",
                "message": "OpenWakeWord not installed on server"
            })
            await websocket.close()
            return

        # Load model if not already loaded
        if not service.load_model():
            await websocket.send_json({
                "type": "error",
                "message": "Failed to load wake word model"
            })
            await websocket.close()
            return

        # Signal ready
        await websocket.send_json({
            "type": "ready",
            "keywords": service.keywords,
            "threshold": service.threshold
        })

        # Process audio chunks
        while True:
            # Receive audio chunk
            audio_bytes = await websocket.receive_bytes()

            # Process chunk
            result = service.process_audio_chunk(audio_bytes)

            # Send detection if wake word found
            if result.get("detected"):
                await websocket.send_json({
                    "type": "wakeword_detected",
                    "keyword": result["keyword"],
                    "score": result["score"]
                })

    except WebSocketDisconnect:
        logger.info("👋 Wake word WebSocket connection closed")
    except Exception as e:
        logger.error(f"❌ Wake word WebSocket error: {e}")
        try:
            await websocket.send_json({
                "type": "error",
                "message": str(e)
            })
        except:
            pass
        await websocket.close()


# Health Check Endpoints
@app.get("/health")
async def health_check():
    """Quick health check for load balancers."""
    return {"status": "ok"}


@app.get("/health/ready")
async def readiness_check():
    """Kubernetes readiness probe - checks all dependencies."""
    from sqlalchemy import text

    checks = {}
    overall_healthy = True

    # Database check
    try:
        async with AsyncSessionLocal() as db:
            await db.execute(text("SELECT 1"))
        checks["database"] = {"status": "healthy"}
    except Exception as e:
        checks["database"] = {"status": "unhealthy", "error": str(e)}
        overall_healthy = False

    # Ollama check
    try:
        ollama = app.state.ollama if hasattr(app.state, "ollama") else None
        if ollama:
            checks["ollama"] = {"status": "healthy", "model": settings.ollama_model}
        else:
            checks["ollama"] = {"status": "degraded", "error": "Not initialized"}
    except Exception as e:
        checks["ollama"] = {"status": "unhealthy", "error": str(e)}
        overall_healthy = False

    # Redis check (optional)
    try:
        import redis.asyncio as redis
        r = redis.from_url(settings.redis_url)
        await r.ping()
        await r.close()
        checks["redis"] = {"status": "healthy"}
    except Exception as e:
        checks["redis"] = {"status": "degraded", "error": str(e)}
        # Redis is optional, don't fail health check

    # Connected devices count
    try:
        device_manager = get_device_manager()
        checks["devices"] = {
            "status": "healthy",
            "connected": len(device_manager.devices),
            "active_sessions": len(device_manager.sessions)
        }
    except Exception:
        checks["devices"] = {"status": "unknown"}

    status = "healthy" if overall_healthy else "unhealthy"
    status_code = 200 if overall_healthy else 503

    return JSONResponse(
        content={
            "status": status,
            "version": "1.0.0",
            "checks": checks,
            "timestamp": datetime.utcnow().isoformat()
        },
        status_code=status_code
    )


@app.get("/health/live")
async def liveness_check():
    """Kubernetes liveness probe - just checks if app is running."""
    return {"status": "alive", "timestamp": datetime.utcnow().isoformat()}

# WebSocket Token Generation Endpoint
@app.post("/api/ws/token")
async def create_ws_token(
    device_id: str = None,
    device_type: str = None
):
    """
    Generate a WebSocket authentication token.

    Only relevant when WS_AUTH_ENABLED=true.
    In production, this endpoint should be protected by authentication.
    """
    if not settings.ws_auth_enabled:
        return {
            "token": None,
            "message": "WebSocket authentication is disabled",
            "expires_in": None
        }

    token_store = get_token_store()
    token = token_store.create_token(
        device_id=device_id,
        device_type=device_type
    )

    return {
        "token": token,
        "expires_in": settings.ws_token_expire_minutes * 60,
        "protocol_version": settings.ws_protocol_version
    }


# Admin Endpoint: Refresh HA Keywords
@app.post("/admin/refresh-keywords")
async def refresh_keywords(
    user = Depends(require_permission(Permission.ADMIN))
):
    """
    Lade Home Assistant Keywords neu

    Nützlich nach dem Hinzufügen neuer Geräte in HA

    Requires: admin permission (when auth is enabled)
    """
    try:
        from integrations.homeassistant import HomeAssistantClient
        ha_client = HomeAssistantClient()
        keywords = await ha_client.get_keywords(refresh=True)

        return {
            "status": "success",
            "keywords_count": len(keywords),
            "sample_keywords": list(keywords)[:20]
        }
    except Exception as e:
        logger.error(f"❌ Keyword Refresh Fehler: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# Debug Endpoint: Test Intent Extraction
@app.post("/debug/intent")
async def debug_intent(
    message: str,
    user = Depends(require_permission(Permission.ADMIN))
):
    """
    Teste Intent-Extraction für eine Nachricht

    Nützlich zum Debuggen von Intent-Erkennungsproblemen

    Requires: admin permission (when auth is enabled)
    """
    try:
        ollama: OllamaService = app.state.ollama
        intent = await ollama.extract_intent(message)

        return {
            "message": message,
            "intent": intent,
            "timestamp": datetime.utcnow().isoformat()
        }
    except Exception as e:
        logger.error(f"❌ Intent Debug Fehler: {e}")
        raise HTTPException(status_code=500, detail=str(e))

# Root Endpoint
@app.get("/")
async def root():
    """API Root"""
    return {
        "name": "Renfield AI Assistant",
        "version": "1.0.0",
        "status": "online",
        "docs": "/docs"
    }

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=8000,
        reload=True,
        log_level="info"
    )
