"""
Privacy-aware TTS gating for notifications.

Determines whether a notification should be played via TTS based on
its privacy level and room occupancy from the BLE presence system.

Privacy levels:
  - public: always play TTS
  - personal: play only when all room occupants are household members
  - confidential: play only when the target user is completely alone
"""

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from utils.config import settings


async def should_play_tts(
    privacy: str,
    target_user_id: int | None,
    room_id: int | None,
    db: AsyncSession,
) -> bool:
    """
    Decide whether TTS should play for a notification.

    Args:
        privacy: Privacy level ("public", "personal", "confidential").
        target_user_id: The user this notification is intended for (if any).
        room_id: The room where TTS would play (if known).
        db: Database session for querying user roles.

    Returns:
        True if TTS is allowed, False if it should be suppressed.
    """
    if privacy == "public":
        return True

    if not settings.presence_enabled:
        logger.debug("Presence disabled — suppressing non-public TTS (privacy=%s)", privacy)
        return False

    from services.presence_service import get_presence_service
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
        return await _all_household_members(occupant_ids, db)

    # Unknown privacy level — fail-safe
    logger.warning("Unknown privacy level '%s' — suppressing TTS", privacy)
    return False


async def _all_household_members(user_ids: list[int], db: AsyncSession) -> bool:
    """Check whether all given user IDs belong to household roles."""
    from models.database import User

    household_roles = {r.strip() for r in settings.presence_household_roles.split(",") if r.strip()}

    result = await db.execute(
        select(User).options(selectinload(User.role)).where(User.id.in_(user_ids))
    )
    users = result.scalars().all()

    if len(users) != len(user_ids):
        # Some user IDs not found in DB — fail-safe
        return False

    return all(u.role and u.role.name in household_roles for u in users)
