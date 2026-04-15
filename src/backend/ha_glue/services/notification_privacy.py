"""Privacy-aware TTS gating for notifications.

Moved from platform `services/notification_privacy.py` to ha_glue in
Phase 1 W2 because this file is 100% HomeAssistant-flavored — the
entire privacy decision depends on BLE presence data, household-role
classification, and room-occupancy tracking that only exist in
ha_glue's presence subsystem.

Determines whether a notification should be played via TTS based on
its privacy level and room occupancy from the BLE presence system.

Privacy levels:
  - public: always play TTS
  - personal: play only when all room occupants are household members
  - confidential: play only when the target user is completely alone

## Wired via hook

Registered as a `should_play_tts_for_notification` handler by
`ha_glue/bootstrap.py::register()`. Platform `notification_service.py`
fires the hook when delivering a non-public notification; this
handler returns the presence-aware decision. On platform-only deploys
where ha_glue isn't loaded, the hook has no handler and the call site
fails safe to "suppress non-public TTS."
"""

from __future__ import annotations

from loguru import logger
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from ha_glue.utils.config import ha_glue_settings


async def ha_should_play_tts_for_notification(
    *,
    privacy: str,
    target_user_id: int | None,
    room_id: int | None,
) -> bool | None:
    """Decide whether TTS should play for a notification.

    Opens its own database session so the call site doesn't need to
    pass one across the hook boundary.

    Args:
        privacy: Privacy level ("public", "personal", "confidential").
        target_user_id: The user this notification is intended for.
        room_id: The room where TTS would play (if known).

    Returns:
        True if TTS is allowed, False if it should be suppressed.
        Never returns None (platform default when no handler).
    """
    # public always passes — but the platform shouldn't fire this hook
    # for public notifications at all. Handle defensively anyway.
    if privacy == "public":
        return True

    if not ha_glue_settings.presence_enabled:
        logger.debug(
            "ha_glue notification_privacy: presence disabled — "
            f"suppressing non-public TTS (privacy={privacy})"
        )
        return False

    from services.database import AsyncSessionLocal
    from ha_glue.services.presence_service import get_presence_service

    presence = get_presence_service()

    if privacy == "confidential":
        if target_user_id is None:
            return False
        alone = presence.is_user_alone_in_room(target_user_id)
        if alone is None:
            # User not tracked by BLE — fail-safe: don't play
            return False
        return alone

    if privacy == "personal":
        if room_id is None:
            return False
        occupants = presence.get_room_occupants(room_id)
        if not occupants:
            return False

        occupant_ids = [o.user_id for o in occupants]
        async with AsyncSessionLocal() as db:
            return await _all_household_members(occupant_ids, db)

    # Unknown privacy level — fail-safe
    logger.warning(
        f"ha_glue notification_privacy: unknown privacy level {privacy!r} — suppressing TTS"
    )
    return False


async def _all_household_members(user_ids: list[int], db) -> bool:
    """Check whether all given user IDs belong to household roles."""
    from models.database import User

    household_roles = {
        r.strip()
        for r in ha_glue_settings.presence_household_roles.split(",")
        if r.strip()
    }

    result = await db.execute(
        select(User).options(selectinload(User.role)).where(User.id.in_(user_ids))
    )
    users = result.scalars().all()

    if len(users) != len(user_ids):
        # Some user IDs not found in DB — fail-safe
        return False

    return all(u.role and u.role.name in household_roles for u in users)
