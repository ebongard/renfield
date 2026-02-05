"""
WebSocket handler for Satellite Voice Assistants (/ws/satellite endpoint).

This module handles:
- Raspberry Pi satellite registration and management
- Wake word detection and audio streaming
- Speech-to-text transcription with speaker recognition
- Intent extraction and action execution
- TTS response generation and routing
- OTA update progress tracking
"""

from datetime import date

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect
from loguru import logger

from models.websocket_messages import WSErrorCode
from services.database import AsyncSessionLocal
from services.wakeword_config_manager import get_wakeword_config_manager
from services.websocket_auth import WSAuthError, authenticate_websocket
from services.websocket_rate_limiter import get_connection_limiter, get_rate_limiter
from utils.config import settings

from .shared import get_whisper_service, send_ws_error

router = APIRouter()


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
        logger.debug("Satellite has no room_id, using satellite for output")
        await satellite_manager.send_tts_audio(session_id, tts_audio, is_final=True)
        return

    try:
        from services.audio_output_service import get_audio_output_service
        from services.output_routing_service import OutputRoutingService

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
                    logger.warning("Output device playback failed, falling back to satellite")
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


@router.websocket("/ws/satellite")
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

    # Access app state through websocket.app
    app = websocket.app

    from services.satellite_manager import SatelliteState, get_satellite_manager
    satellite_manager = get_satellite_manager()
    rate_limiter = get_rate_limiter()

    satellite_id = None

    # Conversation history tracking for satellite (in-memory per connection)
    satellite_conversation_history: list[dict] = []
    satellite_history_loaded = False
    satellite_db_session_id = None  # Will be set after registration

    try:
        while True:
            data = await websocket.receive_json()

            # Rate limiting
            rate_key = satellite_id if satellite_id else ip_address
            allowed, rate_reason = rate_limiter.check(rate_key)
            if not allowed:
                await send_ws_error(websocket, WSErrorCode.RATE_LIMITED, rate_reason)
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
                        await send_ws_error(websocket, WSErrorCode.BUFFER_FULL, error)

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

                    # Extract ranked intents with room context and conversation history
                    ranked_intents = await ollama.extract_ranked_intents(
                        text,
                        plugin_registry,
                        room_context=room_context,
                        conversation_history=satellite_conversation_history if satellite_conversation_history else None
                    )

                    # Fallback chain: try intents until one works
                    from services.action_executor import ActionExecutor
                    mcp_mgr = getattr(websocket.app.state, 'mcp_manager', None)
                    action_result = None
                    intent = None

                    for intent_candidate in ranked_intents:
                        intent_name = intent_candidate.get("intent", "general.conversation")
                        logger.info(f"🎯 Satellite versucht Intent: {intent_name} (confidence: {intent_candidate.get('confidence', 0):.2f})")

                        if intent_name == "general.conversation":
                            intent = intent_candidate
                            break

                        executor = ActionExecutor(plugin_registry, mcp_manager=mcp_mgr)
                        candidate_result = await executor.execute(intent_candidate)

                        if candidate_result.get("success") and not candidate_result.get("empty_result"):
                            intent = intent_candidate
                            action_result = candidate_result
                            logger.info(f"⚡ Action result: {candidate_result.get('success')}")
                            await satellite_manager.send_action_result(
                                session_id, intent, candidate_result.get("success", False)
                            )
                            break

                        logger.info(f"⏭️ Intent {intent_name} leer, versuche nächsten...")

                    # Fallback to conversation if no intent worked
                    if intent is None:
                        intent = {"intent": "general.conversation", "parameters": {}, "confidence": 1.0}

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
