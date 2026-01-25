"""
WebSocket handler for all device types (/ws/device endpoint).

This module handles:
- Unified endpoint for satellites and web clients
- Device registration and capability management
- Wake word detection and audio streaming
- Text input processing
- Session management
"""

from typing import Optional

from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Query, FastAPI
from pydantic import ValidationError
from loguru import logger

from services.database import AsyncSessionLocal
from services.device_manager import get_device_manager, DeviceState, DeviceManager
from services.websocket_auth import authenticate_websocket, WSAuthError
from services.websocket_rate_limiter import get_rate_limiter, get_connection_limiter
from services.wakeword_config_manager import get_wakeword_config_manager
from models.database import (
    DEVICE_TYPE_SATELLITE, DEVICE_TYPE_WEB_BROWSER,
    DEVICE_TYPES, DEFAULT_CAPABILITIES
)
from models.websocket_messages import (
    WSErrorCode, WSRegisterMessage, WSAudioMessage
)
from utils.config import settings

from .shared import get_whisper_service, send_ws_error

router = APIRouter()


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


@router.websocket("/ws/device")
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

    # Access app state through websocket.app
    app = websocket.app

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
                await send_ws_error(websocket, WSErrorCode.RATE_LIMITED, reason)
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
                    await send_ws_error(websocket, WSErrorCode.INVALID_MESSAGE, f"Invalid registration: {e}")
                    continue

                # Validate device type
                if device_type not in DEVICE_TYPES:
                    logger.warning(f"⚠️ Unknown device type: {device_type}, defaulting to web_browser")
                    device_type = DEVICE_TYPE_WEB_BROWSER

                # Check connection limits with actual device_id
                can_connect, conn_reason = connection_limiter.can_connect(ip_address, device_id)
                if not can_connect:
                    await send_ws_error(websocket, WSErrorCode.DEVICE_ERROR, conn_reason)
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
                    await send_ws_error(websocket, WSErrorCode.INVALID_MESSAGE, f"Invalid audio message: {e}")
                    continue

                if session_id and chunk_b64:
                    success, error = device_manager.buffer_audio(session_id, chunk_b64, sequence)
                    if not success:
                        # End session on buffer full to prevent further errors
                        if "buffer full" in error.lower():
                            await device_manager.end_session(session_id, reason="buffer_full")
                        await send_ws_error(websocket, WSErrorCode.BUFFER_FULL, error)

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
