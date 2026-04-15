"""Small hook handlers for the Phase 1 W2 platform-leak sweep.

These handlers replace direct `ha_glue_settings` imports that used to
live in platform files:

- `ha_chat_context_established` — replaces the BLE voice-presence
  registration block in `api/websocket/chat_handler.py`. Fires on the
  `chat_context_established` hook and calls
  `presence_service.register_voice_presence()` when presence is enabled.

- `ha_resolve_user_current_room` — replaces the room-lookup block in
  `services/notification_service.py::_persist`. Fires on the
  `resolve_user_current_room` hook and returns the user's current
  room from the BLE presence service, or None if unknown.

Both handlers early-return None (hook fall-through) when
`ha_glue_settings.presence_enabled` is False, so pro deploys don't
activate any presence-dependent behavior even though the handlers
are registered in the same `register()` call.
"""

from __future__ import annotations

from loguru import logger


async def ha_chat_context_established(
    *,
    user_id: int,
    room_id: int,
    room_name: str | None = None,
    lang: str = "de",
) -> None:
    """Register BLE voice-auth presence when a user speaks from a known room.

    The platform chat handler fires this hook whenever an authenticated
    user's message starts processing with a known room context. If
    presence is enabled, record that the user is (now) in that room
    so presence-derived features (media follow, privacy gating,
    notifications) see the updated location.
    """
    from ha_glue.utils.config import ha_glue_settings

    if not ha_glue_settings.presence_enabled:
        return

    try:
        from services.presence_service import get_presence_service
        presence_svc = get_presence_service()
        await presence_svc.register_voice_presence(
            user_id=user_id,
            room_id=room_id,
            room_name=room_name,
        )
    except Exception as e:  # noqa: BLE001
        logger.warning(f"⚠️  ha_glue chat_context_established: voice presence update failed: {e}")


async def ha_resolve_user_current_room(
    *,
    user_id: int,
) -> dict | None:
    """Return the user's current room from BLE presence, or None.

    Used by `notification_service` to target a notification at the
    user's actual room when no explicit room was specified by the
    caller. Returns `None` when:

    - Presence is disabled at the ha_glue level
    - The user isn't tracked by BLE
    - The presence service raises for any reason
    """
    from ha_glue.utils.config import ha_glue_settings

    if not ha_glue_settings.presence_enabled:
        return None

    try:
        from services.presence_service import get_presence_service
        presence = get_presence_service()
        user_p = presence.get_user_presence(user_id)
    except Exception as e:  # noqa: BLE001
        logger.debug(f"ha_glue resolve_user_current_room: presence lookup failed: {e}")
        return None

    if not user_p or not user_p.room_id:
        return None

    return {
        "room_id": user_p.room_id,
        "room_name": user_p.room_name or "",
    }
