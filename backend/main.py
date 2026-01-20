"""
Renfield - Persönlicher KI-Assistent
Hauptanwendung mit FastAPI
"""
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, UploadFile, File, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from contextlib import asynccontextmanager
from loguru import logger
from datetime import datetime
import os
import sys
import uuid
from typing import Optional

# Logging konfigurieren
logger.remove()
logger.add(sys.stderr, level=os.getenv("LOG_LEVEL", "INFO"))

# Lokale Imports
from api.routes import chat, tasks, voice, camera, homeassistant as ha_routes, settings as settings_routes, speakers, rooms
from services.database import init_db
from services.ollama_service import OllamaService
from services.task_queue import TaskQueue
from services.whisper_service import WhisperService
from services.device_manager import get_device_manager, DeviceState, DeviceManager
from models.database import (
    DEVICE_TYPE_SATELLITE, DEVICE_TYPE_WEB_BROWSER, DEVICE_TYPE_WEB_PANEL,
    DEVICE_TYPE_WEB_TABLET, DEVICE_TYPE_WEB_KIOSK, DEVICE_TYPES, DEFAULT_CAPABILITIES
)
from utils.config import settings

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
        import asyncio

        async def preload_whisper():
            """Lade Whisper-Modell im Hintergrund"""
            try:
                whisper_service = get_whisper_service()
                whisper_service.load_model()
                logger.info("✅ Whisper Service bereit (STT aktiviert)")
            except Exception as e:
                logger.warning(f"⚠️  Whisper konnte nicht vorgeladen werden: {e}")
                logger.warning("💡 Spracheingabe wird beim ersten Gebrauch geladen")

        # Starte im Hintergrund
        asyncio.create_task(preload_whisper())
    except Exception as e:
        logger.warning(f"⚠️  Whisper-Preloading fehlgeschlagen: {e}")
    
    # Home Assistant Keywords vorladen (optional, im Hintergrund)
    try:
        from integrations.homeassistant import HomeAssistantClient
        import asyncio

        async def preload_keywords():
            """Lade HA Keywords im Hintergrund"""
            try:
                ha_client = HomeAssistantClient()
                keywords = await ha_client.get_keywords()
                logger.info(f"✅ Home Assistant Keywords vorgeladen: {len(keywords)} Keywords")
            except Exception as e:
                logger.warning(f"⚠️  Keywords konnten nicht vorgeladen werden: {e}")

        # Starte im Hintergrund (blockiert Start nicht)
        asyncio.create_task(preload_keywords())
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

    # Zeroconf Service stoppen
    if zeroconf_service:
        await zeroconf_service.stop()
    
    # Cleanup
    logger.info("👋 Renfield wird heruntergefahren...")

# FastAPI App erstellen
app = FastAPI(
    title="Renfield AI Assistant",
    description="Vollständig offline-fähiger persönlicher KI-Assistent",
    version="1.0.0",
    lifespan=lifespan
)

# CORS Middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # In Produktion einschränken
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Router einbinden
app.include_router(chat.router, prefix="/api/chat", tags=["Chat"])
app.include_router(tasks.router, prefix="/api/tasks", tags=["Tasks"])
app.include_router(voice.router, prefix="/api/voice", tags=["Voice"])
app.include_router(camera.router, prefix="/api/camera", tags=["Camera"])
app.include_router(ha_routes.router, prefix="/api/homeassistant", tags=["Home Assistant"])
app.include_router(settings_routes.router, prefix="/api/settings", tags=["Settings"])
app.include_router(speakers.router, prefix="/api/speakers", tags=["Speakers"])
app.include_router(rooms.router, prefix="/api/rooms", tags=["Rooms"])

# WebSocket für Echtzeit-Chat
@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    """WebSocket Verbindung für Echtzeit-Chat"""
    await websocket.accept()
    logger.info("✅ WebSocket Verbindung hergestellt")

    # Try to auto-detect room context from IP address
    room_context = None
    try:
        ip_address = websocket.client.host if websocket.client else None
        if ip_address:
            from services.database import AsyncSessionLocal
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
            message_type = data.get("type", "text")
            content = data.get("content", "")

            logger.info(f"📨 WebSocket Nachricht: {message_type} - '{content[:100]}'")

            # Ollama Service
            ollama = app.state.ollama
            plugin_registry = app.state.plugin_registry

            # Intent extrahieren (mit automatischem Raum-Kontext falls verfügbar)
            logger.info("🔍 Extrahiere Intent...")
            intent = await ollama.extract_intent(content, plugin_registry, room_context=room_context)
            logger.info(f"🎯 Intent erkannt: {intent.get('intent')} | Entity: {intent.get('parameters', {}).get('entity_id', 'none')}")
            
            # Action ausführen falls nötig
            action_result = None
            if intent.get("intent") != "general.conversation":
                logger.info(f"⚡ Führe Aktion aus: {intent.get('intent')}")
                from services.action_executor import ActionExecutor
                executor = ActionExecutor(plugin_registry)
                action_result = await executor.execute(intent)
                logger.info(f"✅ Aktion: {action_result.get('success')} - {action_result.get('message')}")
                
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

                # Stream die Antwort
                async for chunk in ollama.chat_stream(enhanced_prompt):
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
                # Normale Konversation
                async for chunk in ollama.chat_stream(content):
                    full_response += chunk
                    await websocket.send_json({
                        "type": "stream",
                        "content": chunk
                    })

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
async def satellite_websocket(websocket: WebSocket):
    """
    WebSocket endpoint for Raspberry Pi satellite voice assistants.

    Protocol:
    Satellite → Server:
        - {"type": "register", "satellite_id": str, "room": str, "capabilities": {...}}
        - {"type": "wakeword_detected", "keyword": str, "confidence": float, "session_id": str}
        - {"type": "audio", "chunk": str (base64), "sequence": int, "session_id": str}
        - {"type": "audio_end", "session_id": str, "reason": str}
        - {"type": "heartbeat", "status": str, "uptime_seconds": int}

    Server → Satellite:
        - {"type": "register_ack", "success": bool, "config": {...}}
        - {"type": "state", "state": "idle|listening|processing|speaking"}
        - {"type": "transcription", "session_id": str, "text": str}
        - {"type": "action", "session_id": str, "intent": {...}, "success": bool}
        - {"type": "tts_audio", "session_id": str, "audio": str (base64), "is_final": bool}
    """
    await websocket.accept()
    logger.info("📡 Satellite WebSocket connection established")

    from services.satellite_manager import get_satellite_manager, SatelliteState
    satellite_manager = get_satellite_manager()

    satellite_id = None

    try:
        while True:
            data = await websocket.receive_json()
            msg_type = data.get("type", "")

            # Handle registration
            if msg_type == "register":
                satellite_id = data.get("satellite_id", "unknown")
                room = data.get("room", "Unknown Room")
                capabilities = data.get("capabilities", {})

                success = await satellite_manager.register(
                    satellite_id=satellite_id,
                    room=room,
                    websocket=websocket,
                    capabilities=capabilities
                )

                # Persist room assignment to database
                room_id = None
                if success and settings.rooms_auto_create_from_satellite:
                    try:
                        from services.database import AsyncSessionLocal
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

                await websocket.send_json({
                    "type": "register_ack",
                    "success": success,
                    "config": {
                        "wake_words": satellite_manager.default_wake_words,
                        "threshold": satellite_manager.default_threshold
                    },
                    "room_id": room_id
                })
                logger.info(f"📡 Satellite {satellite_id} registered from {room}")

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
                    satellite_manager.buffer_audio(session_id, chunk_b64, sequence)

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

                # Transcribe with Whisper (with speaker recognition)
                try:
                    from services.database import AsyncSessionLocal
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
                                db_session=db_session
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
                        text = await whisper.transcribe_bytes(wav_bytes, "satellite_audio.wav")

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
                        await satellite_manager.send_action_result(
                            session_id, intent, action_result.get("success", False)
                        )

                    # Generate response
                    response_text = ""
                    if action_result and action_result.get("success"):
                        result_info = action_result.get("message", "")
                        enhanced_prompt = f"""Der Nutzer hat gefragt: "{text}"
Die Aktion wurde ausgeführt: {result_info}
Gib eine kurze, natürliche Antwort. KEIN JSON, nur Text."""

                        async for chunk in ollama.chat_stream(enhanced_prompt):
                            response_text += chunk
                    elif action_result and not action_result.get("success"):
                        response_text = f"Entschuldigung, das konnte ich nicht ausführen: {action_result.get('message')}"
                    else:
                        # Normal conversation
                        async for chunk in ollama.chat_stream(text):
                            response_text += chunk

                    logger.info(f"💬 Response: '{response_text[:100]}...'")

                    # Generate TTS
                    from services.piper_service import PiperService
                    piper = PiperService()
                    tts_audio = await piper.synthesize_to_bytes(response_text)

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

            # Handle heartbeat
            elif msg_type == "heartbeat":
                if satellite_id:
                    satellite_manager.update_heartbeat(satellite_id)
                    # Optionally send heartbeat ack
                    await websocket.send_json({"type": "heartbeat_ack"})

    except WebSocketDisconnect:
        logger.info(f"👋 Satellite WebSocket disconnected: {satellite_id}")
    except Exception as e:
        logger.error(f"❌ Satellite WebSocket error: {e}")
        import traceback
        logger.error(traceback.format_exc())
    finally:
        if satellite_id:
            # Mark satellite offline in database
            try:
                from services.database import AsyncSessionLocal
                from services.room_service import RoomService

                async with AsyncSessionLocal() as db_session:
                    room_service = RoomService(db_session)
                    await room_service.set_satellite_online(satellite_id, False)
            except Exception as e:
                logger.warning(f"⚠️ Failed to mark satellite offline: {e}")

            await satellite_manager.unregister(satellite_id)


# Unified WebSocket for All Device Types (Satellites + Web Clients)
@app.websocket("/ws/device")
async def device_websocket(websocket: WebSocket):
    """
    Unified WebSocket endpoint for all device types (satellites and web clients).

    Supports:
    - Physical satellites (Raspberry Pi with ReSpeaker)
    - Web panels (stationary iPad/tablet)
    - Web tablets (mobile tablet)
    - Web browsers (desktop browser)
    - Web kiosks (touch terminals)

    Protocol:
    Device → Server:
        - {"type": "register", "device_id": str, "device_type": str, "room": str,
           "capabilities": {...}, "device_name": str?, "is_stationary": bool?}
        - {"type": "wakeword_detected", "keyword": str, "confidence": float, "session_id": str?}
        - {"type": "audio", "chunk": str (base64), "sequence": int, "session_id": str}
        - {"type": "audio_end", "session_id": str, "reason": str}
        - {"type": "start_session"} - Manual session start (web clients without wakeword)
        - {"type": "text", "content": str, "session_id": str?} - Text input (web clients)
        - {"type": "heartbeat", "status": str}

    Server → Device:
        - {"type": "register_ack", "success": bool, "config": {...}, "room_id": int?}
        - {"type": "state", "state": "idle|listening|processing|speaking"}
        - {"type": "transcription", "session_id": str, "text": str, "speaker_name": str?, ...}
        - {"type": "action", "session_id": str, "intent": {...}, "success": bool}
        - {"type": "tts_audio", "session_id": str, "audio": str (base64), "is_final": bool}
        - {"type": "response_text", "session_id": str, "text": str, "is_final": bool}
        - {"type": "stream", "session_id": str, "content": str}
        - {"type": "session_end", "session_id": str, "reason": str}
    """
    await websocket.accept()
    logger.info("📱 Device WebSocket connection established")

    device_manager = get_device_manager()
    device_id = None

    # Extract client info from headers
    user_agent = None
    ip_address = None
    try:
        user_agent = websocket.headers.get("user-agent", "")
        ip_address = websocket.client.host if websocket.client else None
    except:
        pass

    try:
        while True:
            data = await websocket.receive_json()
            msg_type = data.get("type", "")

            # === REGISTRATION ===
            if msg_type == "register":
                device_id = data.get("device_id", f"device-{uuid.uuid4().hex[:8]}")
                device_type = data.get("device_type", DEVICE_TYPE_WEB_BROWSER)
                room = data.get("room", "Unknown Room")
                device_name = data.get("device_name")
                is_stationary = data.get("is_stationary", True)

                # Validate device type
                if device_type not in DEVICE_TYPES:
                    logger.warning(f"⚠️ Unknown device type: {device_type}, defaulting to web_browser")
                    device_type = DEVICE_TYPE_WEB_BROWSER

                # Merge default capabilities with provided ones
                default_caps = DEFAULT_CAPABILITIES.get(device_type, {}).copy()
                provided_caps = data.get("capabilities", {})
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
                        from services.database import AsyncSessionLocal
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
                                # Use the provided room name (avoid lazy loading in async context)
                                logger.info(f"📍 Device {device_id} linked to room '{room}' (id: {room_id})")
                    except Exception as e:
                        logger.warning(f"⚠️ Failed to persist device to database: {e}")

                # Send registration acknowledgement
                await websocket.send_json({
                    "type": "register_ack",
                    "success": success,
                    "device_id": device_id,
                    "config": {
                        "wake_words": device_manager.default_wake_words,
                        "threshold": device_manager.default_threshold
                    },
                    "room_id": room_id,
                    "capabilities": capabilities
                })

                type_emoji = "📡" if device_type == DEVICE_TYPE_SATELLITE else "📱"
                logger.info(f"{type_emoji} Device {device_id} ({device_type}) registered in '{room}'")

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
                session_id = data.get("session_id")
                chunk_b64 = data.get("chunk", "")
                sequence = data.get("sequence", 0)

                if session_id and chunk_b64:
                    device_manager.buffer_audio(session_id, chunk_b64, sequence)

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
        if device_id:
            # Mark device offline in database
            try:
                from services.database import AsyncSessionLocal
                from services.room_service import RoomService

                async with AsyncSessionLocal() as db_session:
                    room_service = RoomService(db_session)
                    await room_service.set_device_online(device_id, False)
            except Exception as e:
                logger.warning(f"⚠️ Failed to mark device offline: {e}")

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


# Health Check
@app.get("/health")
async def health_check():
    """System Health Check"""
    return {
        "status": "healthy",
        "services": {
            "ollama": "ok",
            "database": "ok",
            "redis": "ok"
        }
    }

# Admin Endpoint: Refresh HA Keywords
@app.post("/admin/refresh-keywords")
async def refresh_keywords():
    """
    Lade Home Assistant Keywords neu
    
    Nützlich nach dem Hinzufügen neuer Geräte in HA
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
async def debug_intent(message: str):
    """
    Teste Intent-Extraction für eine Nachricht
    
    Nützlich zum Debuggen von Intent-Erkennungsproblemen
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
