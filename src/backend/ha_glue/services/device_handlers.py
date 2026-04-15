"""Hook handlers for device + notification delivery.

Phase 1 W2 Phase B.2b. Three handlers that replace direct ha-glue
service imports in platform files:

1. **`ha_get_connected_device_summary`** — reports DeviceManager
   counters for main.py's `/health` endpoint. Returns
   `{"connected": int, "active_sessions": int}` or None on error.

2. **`ha_deliver_notification`** — owns the entire notification
   delivery flow (WebSocket broadcast + TTS via audio_output +
   output_routing). Was 150 lines split across
   `notification_service._deliver` + `_deliver_tts`; now a single
   handler in ha_glue. Returns `list[str]` of delivered device IDs.

3. **`ha_device_shutdown_broadcast`** — helper used by
   `ha_glue_on_shutdown` to notify all connected devices that the
   server is going down. Was `api/lifecycle.py::_notify_devices_shutdown`.
"""

from __future__ import annotations

import base64
from typing import Any

from loguru import logger


# ---------------------------------------------------------------------------
# /health device count
# ---------------------------------------------------------------------------


async def ha_get_connected_device_summary() -> dict | None:
    """Return {connected, active_sessions} for the /health endpoint.

    Platform-only deploys don't register this handler, so /health
    reports `{"status": "unknown"}` for the devices section, which
    is correct (there are no satellites to count).
    """
    try:
        from ha_glue.services.device_manager import get_device_manager  # moves in Phase B.3
        dm = get_device_manager()
        return {
            "connected": len(dm.devices),
            "active_sessions": len(dm.sessions),
        }
    except Exception as e:  # noqa: BLE001
        logger.debug(f"ha_glue get_connected_device_summary failed: {e}")
        return None


# ---------------------------------------------------------------------------
# Notification delivery (WS broadcast + TTS)
# ---------------------------------------------------------------------------


async def ha_deliver_notification(
    *,
    notification: Any,
    tts: bool = True,
) -> list[str]:
    """Broadcast a notification to connected devices + optional TTS.

    Combines the logic that used to live in
    `notification_service._deliver` + `_deliver_tts` (~150 lines).
    Returns the list of device_ids that actually received the
    notification via WebSocket.
    """
    from ha_glue.services.device_manager import get_device_manager  # moves in Phase B.3

    device_manager = get_device_manager()
    delivered_ids: list[str] = []

    ws_message = {
        "type": "notification",
        "notification_id": notification.id,
        "title": notification.title,
        "message": notification.message,
        "urgency": notification.urgency,
        "source": notification.source,
        "room": notification.room_name,
        "tts_handled": False,
        "created_at": notification.created_at.isoformat() if notification.created_at else None,
    }

    # Determine target devices
    if notification.room_name:
        devices = device_manager.get_devices_in_room(notification.room_name)
        if notification.room_id:
            devices = devices or device_manager.get_devices_in_room_by_id(notification.room_id)
    else:
        devices = list(device_manager.devices.values())

    # WebSocket broadcast to display-capable devices
    for device in devices:
        if device.capabilities.supports_notifications or device.capabilities.has_display:
            try:
                await device.websocket.send_json(ws_message)
                delivered_ids.append(device.device_id)
            except Exception as e:  # noqa: BLE001
                logger.warning(f"⚠️ Notification delivery failed for {device.device_id}: {e}")

    logger.info(f"📤 Notification #{notification.id} an {len(delivered_ids)} Geräte gesendet")

    # TTS delivery (privacy-gated)
    if tts:
        tts_allowed = True
        if notification.privacy and notification.privacy != "public":
            # Non-public privacy needs domain-specific presence check.
            # ha_glue has its own should_play_tts handler wired too, so
            # this run_hooks call will invoke it within the same process.
            try:
                from utils.hooks import run_hooks
                results = await run_hooks(
                    "should_play_tts_for_notification",
                    privacy=notification.privacy,
                    target_user_id=notification.target_user_id,
                    room_id=notification.room_id,
                )
                tts_allowed = False
                for r in results:
                    if isinstance(r, bool):
                        tts_allowed = r
                        break
            except Exception as e:  # noqa: BLE001
                logger.warning(f"Privacy gate error, suppressing TTS: {e}")
                tts_allowed = False

        if tts_allowed:
            tts_delivered = await _deliver_notification_tts(notification)
            if tts_delivered:
                notification.tts_delivered = True
                ws_message["tts_handled"] = True
        else:
            logger.info(
                f"TTS suppressed for #{notification.id} (privacy={notification.privacy})"
            )

    return delivered_ids


async def _deliver_notification_tts(notification: Any) -> bool:
    """Generate TTS for a notification and route to the best room output."""
    try:
        from services.piper_service import PiperService

        piper = PiperService()
        tts_audio = await piper.synthesize_to_bytes(notification.message)

        if not tts_audio:
            logger.warning(f"⚠️ TTS synthesis failed for notification #{notification.id}")
            return False

        # Route via OutputRoutingService if room is known
        if notification.room_id:
            # NOTE: audio_output_service + output_routing_service still live
            # in services/ until Phase B.3. Imports update in that sweep.
            from ha_glue.services.audio_output_service import get_audio_output_service
            from services.database import AsyncSessionLocal
            from ha_glue.services.output_routing_service import OutputRoutingService

            async with AsyncSessionLocal() as db_session:
                routing_service = OutputRoutingService(db_session)
                audio_output_service = get_audio_output_service()

                decision = await routing_service.get_audio_output_for_room(
                    room_id=notification.room_id,
                )

                if decision.output_device and not decision.fallback_to_input:
                    success = await audio_output_service.play_audio(
                        audio_bytes=tts_audio,
                        output_device=decision.output_device,
                        session_id=f"notification-{notification.id}",
                    )
                    if success:
                        logger.info(f"🔊 TTS für Notification #{notification.id} abgespielt")
                        return True

        # Fallback: send TTS to all speakers in the target room (or all rooms)
        from ha_glue.services.device_manager import get_device_manager

        device_manager = get_device_manager()
        if notification.room_name:
            devices = device_manager.get_devices_in_room(notification.room_name)
        else:
            devices = list(device_manager.devices.values())

        audio_b64 = base64.b64encode(tts_audio).decode("utf-8")

        for device in devices:
            if device.capabilities.has_speaker:
                try:
                    await device.websocket.send_json({
                        "type": "tts_audio",
                        "session_id": f"notification-{notification.id}",
                        "audio": audio_b64,
                        "is_final": True,
                    })
                    logger.info(f"🔊 TTS an {device.device_id} gesendet")
                    return True
                except Exception as e:  # noqa: BLE001
                    logger.warning(f"⚠️ TTS delivery to {device.device_id} failed: {e}")

        return False

    except Exception as e:  # noqa: BLE001
        logger.error(f"❌ TTS delivery failed for notification #{notification.id}: {e}")
        return False
