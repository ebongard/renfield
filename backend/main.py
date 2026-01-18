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
from typing import Optional

# Logging konfigurieren
logger.remove()
logger.add(sys.stderr, level=os.getenv("LOG_LEVEL", "INFO"))

# Lokale Imports
from api.routes import chat, tasks, voice, camera, homeassistant as ha_routes, settings as settings_routes
from services.database import init_db
from services.ollama_service import OllamaService
from services.task_queue import TaskQueue
from utils.config import settings

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
        from services.whisper_service import WhisperService
        import asyncio
        
        async def preload_whisper():
            """Lade Whisper-Modell im Hintergrund"""
            try:
                whisper_service = WhisperService()
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

# WebSocket für Echtzeit-Chat
@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    """WebSocket Verbindung für Echtzeit-Chat"""
    await websocket.accept()
    logger.info("✅ WebSocket Verbindung hergestellt")
    
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

            # Intent extrahieren
            logger.info("🔍 Extrahiere Intent...")
            intent = await ollama.extract_intent(content, plugin_registry)
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
            
            # Response generieren
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
                    await websocket.send_json({
                        "type": "stream",
                        "content": chunk
                    })
            
            elif action_result and not action_result.get("success"):
                # Aktion fehlgeschlagen
                error_message = f"Entschuldigung, das konnte ich nicht ausführen: {action_result.get('message')}"
                await websocket.send_json({
                    "type": "stream",
                    "content": error_message
                })
            
            else:
                # Normale Konversation
                async for chunk in ollama.chat_stream(content):
                    await websocket.send_json({
                        "type": "stream",
                        "content": chunk
                    })
            
            # Stream beendet
            await websocket.send_json({
                "type": "done"
            })
            
            logger.info("✅ WebSocket Response gesendet")
            
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

                await websocket.send_json({
                    "type": "register_ack",
                    "success": success,
                    "config": {
                        "wake_words": satellite_manager.default_wake_words,
                        "threshold": satellite_manager.default_threshold
                    }
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

                # Transcribe with Whisper
                try:
                    from services.whisper_service import WhisperService
                    whisper = WhisperService()
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

                    # Transcribe the audio
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

                    # Extract intent
                    intent = await ollama.extract_intent(text, plugin_registry)
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
                        await satellite_manager.send_tts_audio(session_id, tts_audio, is_final=True)
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
            await satellite_manager.unregister(satellite_id)


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
