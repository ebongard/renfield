"""`announce_in_room` hook: speak a short text in one room, addressed by id.

Core code cannot import the room/TTS layer, so it fires this hook instead. First
user: a scan started by voice announces its outcome in the room it was asked from
(`services/scanner_jobs.py`). Callers pass public, content-free text only — this
handler announces with `privacy="public"` and adds no privacy gate of its own.
"""

from __future__ import annotations

from loguru import logger


async def ha_announce_in_room(*, room_id: int, text: str) -> dict:
    from services.database import AsyncSessionLocal

    from ha_glue.services.internal_tools import InternalToolService
    from ha_glue.services.room_service import RoomService

    async with AsyncSessionLocal() as db:
        room = await RoomService(db).get_room(room_id)
        # Read the name while the session is open — never touch a detached row.
        room_name = room.name if room is not None else None

    if room_name is None:
        logger.info(f"announce_in_room: room {room_id} not found — nothing announced")
        return {"success": False, "message": f"Room {room_id} not found", "action_taken": False}

    return await InternalToolService()._announce_core(
        room_name=room_name, text=text, audio_bytes=None,
        privacy="public", for_users=[], force=False,
    )
