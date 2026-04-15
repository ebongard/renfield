"""Hook handlers for chat/voice routing that used to live inline in
platform `api/websocket/chat_handler.py` and `api/routes/voice.py`.

Phase 1 W2 Phase B.2a follow-up. Three small handlers:

1. **`ha_route_chat_tts_to_device_output`** — replaces the 60-line
   `_route_chat_tts_output` function in `chat_handler.py` that did
   HA-media-player TTS routing for chat responses. The function
   imported `audio_output_service` + `output_routing_service`
   (platform → ha_glue leak). Now lives here, reached via the
   `route_chat_tts_to_device_output` hook.

2. **`ha_resolve_room_context_by_ip`** — replaces the
   `RoomService.get_room_context_by_ip()` call in `chat_handler.py`
   that auto-detected which room a WebSocket client's IP belonged
   to. The call imported `services.room_service` (platform →
   ha_glue leak). Now lives here, reached via the
   `resolve_room_context_by_ip` hook.

3. **`ha_fetch_tts_audio_cache`** — replaces the
   `audio_output_service.get_cached_audio()` call in
   `api/routes/voice.py::get_tts_cache` that served HA media
   players fetching pre-generated TTS audio. The endpoint docstring
   explicitly said "This endpoint is used by Home Assistant media
   players." Now reached via the `fetch_tts_audio_cache` hook.

All three are registered in `ha_glue/bootstrap.py::register()` and
early-return None/False when ha_glue's own preconditions aren't met
(presence disabled, HA not configured, etc.).
"""

from __future__ import annotations

from loguru import logger


# ---------------------------------------------------------------------------
# Chat TTS routing — the big one
# ---------------------------------------------------------------------------


async def ha_route_chat_tts_to_device_output(
    *,
    room_context: dict,
    response_text: str,
) -> bool | None:
    """Route a chat response's TTS audio to an HA media player for the room.

    Returns:
        True  — TTS was synthesized and sent to an HA media player
                (frontend should NOT play it)
        False — no HA routing happened (explicit) — frontend should
                play TTS directly
        None  — hook fall-through (no room context or unexpected shape) —
                platform treats this as False anyway, frontend plays
    """
    room_id = room_context.get("room_id")
    device_id = room_context.get("device_id")

    if not room_id:
        return None

    try:
        from services.database import AsyncSessionLocal

        # NOTE: these 3 services live at services/*.py for now and move
        # to ha_glue/services/ in Phase B.3. Imports will be updated in
        # the same perl sweep that performs the move.
        from services.audio_output_service import get_audio_output_service
        from services.output_routing_service import OutputRoutingService

        async with AsyncSessionLocal() as db_session:
            routing_service = OutputRoutingService(db_session)

            # Get the best audio output device for this room
            decision = await routing_service.get_audio_output_for_room(
                room_id=room_id,
                input_device_id=device_id,
            )

            logger.info(
                f"🔊 Chat output routing: {decision.reason} → "
                f"{decision.target_type}:{decision.target_id}"
            )

            # Only handle server-side if we have a configured HA output device
            # (Renfield devices would be the input device itself in this case)
            if (
                decision.output_device
                and not decision.fallback_to_input
                and decision.target_type == "homeassistant"
            ):
                # Generate TTS via the platform Piper service
                from services.piper_service import get_piper_service
                piper = get_piper_service()
                tts_audio = await piper.synthesize_to_bytes(response_text)

                if tts_audio:
                    audio_output_service = get_audio_output_service()
                    success = await audio_output_service.play_audio(
                        audio_bytes=tts_audio,
                        output_device=decision.output_device,
                        session_id=f"chat-{room_id}-{device_id}",
                    )

                    if success:
                        logger.info(f"🔊 TTS sent to HA media player: {decision.target_id}")
                        return True
                    logger.warning(
                        f"Failed to send TTS to {decision.target_id}, "
                        "frontend will handle"
                    )

            return False

    except Exception as e:  # noqa: BLE001
        logger.error(f"❌ ha_glue chat TTS routing failed: {e}")
        import traceback
        logger.error(traceback.format_exc())
        return False


# ---------------------------------------------------------------------------
# Room context auto-detection from IP address
# ---------------------------------------------------------------------------


async def ha_resolve_room_context_by_ip(*, ip_address: str) -> dict | None:
    """Look up a room context by client IP address.

    Used by `chat_handler.py` on WebSocket connect to auto-detect
    which room the client device is in. Returns a dict with
    `room_id`, `room_name`, `device_id`, `device_name` on match,
    or None if no registered device has that IP.
    """
    try:
        from services.database import AsyncSessionLocal
        from services.room_service import RoomService  # moves in Phase B.3

        async with AsyncSessionLocal() as db_session:
            room_service = RoomService(db_session)
            return await room_service.get_room_context_by_ip(ip_address)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"⚠️  ha_glue room context lookup failed: {e}")
        return None


# ---------------------------------------------------------------------------
# TTS audio cache fetch (for HA media players)
# ---------------------------------------------------------------------------


async def ha_fetch_tts_audio_cache(*, audio_id: str) -> bytes | None:
    """Fetch a cached TTS audio blob by its audio_id.

    Platform `api/routes/voice.py::get_tts_cache` fires this hook to
    serve HA media players that fetch pre-generated TTS audio via
    HTTP. Returns the raw audio bytes or None if the cache entry
    doesn't exist (expired / not found / never stored).
    """
    try:
        from services.audio_output_service import get_audio_output_service  # moves in Phase B.3

        service = get_audio_output_service()
        return service.get_cached_audio(audio_id)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"⚠️  ha_glue TTS cache fetch failed: {e}")
        return None
