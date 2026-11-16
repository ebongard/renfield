"""Duck-on-listen (docs/design/speaker-enrollment-redesign.md Phase 4, item 4).

When a room's voice satellite starts LISTENING, lower the volume of any media
playing in that room over DLNA so the far-field mic doesn't capture the room's
own audio — then restore it when the turn ends. The XVF3800's hardware AEC has
NO reference for audio rendered on a separate networked speaker (the reference
would need a synchronized clock + known delay we don't have across heterogeneous
DLNA renderers), so it can't cancel the room's media. Ducking the SOURCE is the
industry-standard answer (Amazon/Google "duck/erase nearby-device audio"): cheap,
robust, and it removes the echo rather than trying to subtract it.

Dark by default (`duck_on_listen_enabled`). Every path is best-effort — a duck or
restore failure must NEVER break voice. Restore is driven by the satellite
returning to IDLE (`satellite_manager._end_session_internal`, which runs on a
normal turn end AND on unregister / heartbeat-timeout / session-timeout), with a
`duck_on_listen_max_seconds` safety timer as the backstop.

Known limitations (accepted; the turn is only a few seconds):
  * two satellites listening in ONE room: the first to reach IDLE restores while
    the other is still listening (no per-room refcount),
  * if the user manually changes the renderer's volume DURING a ducked turn,
    restore writes back the pre-duck level (stash-and-restore can't see the
    manual change).
"""
from __future__ import annotations

import asyncio
import json

from loguru import logger

from ha_glue.utils.config import ha_glue_settings
from utils.log_safety import error_text_without_content


def _get_mcp_manager():
    """The live MCPManager off the FastAPI app (None if unavailable). Factored to
    a module function so tests can patch it."""
    try:
        from main import app

        return getattr(app.state, "mcp_manager", None)
    except Exception:
        return None


def _parse_volume(res: dict) -> int | None:
    """0-100 int out of an mcp.dlna.get_volume result (flat `volume`, else nested
    in the `data` content blocks / `message` JSON — the execute_tool wrapper nests
    the real payload; mirrors internal_tools._extract_dlna_volume)."""
    if not isinstance(res, dict):
        return None
    v = res.get("volume")
    if v is None:
        raw = ""
        data = res.get("data", [])
        if isinstance(data, list):
            for item in data:
                if isinstance(item, dict) and item.get("type") == "text":
                    raw = item.get("text", "")
                    break
        if not raw:
            raw = res.get("message", "") or ""
        try:
            parsed = json.loads(raw)
            v = parsed.get("volume") if isinstance(parsed, dict) else None
        except (ValueError, TypeError):
            v = None
    if v is None:
        return None
    try:
        return int(v)
    except (ValueError, TypeError):
        return None


class _Ducked:
    __slots__ = ("original", "renderer", "task")

    def __init__(self, renderer: str, original: int, task: asyncio.Task | None):
        self.renderer = renderer
        self.original = original
        self.task = task


class DuckService:
    """Per-room duck/restore of DLNA playback volume around a voice turn."""

    def __init__(self):
        self._ducked: dict[int, _Ducked] = {}  # room_id -> pre-duck stash
        self._lock = asyncio.Lock()

    async def duck_room(self, room_id: int | None, room_name: str | None = None) -> None:
        """Lower the volume of media playing in ``room_id`` (no-op if nothing is
        playing there — no echo source — or already ducked). The lock is held only
        for the dict check/insert, NEVER across the DLNA round-trips, so a hung
        renderer in one room can't wedge duck/restore for every other room."""
        if not ha_glue_settings.duck_on_listen_enabled or room_id is None:
            return
        try:
            async with self._lock:
                if room_id in self._ducked:
                    return  # already ducked for this turn — idempotent
            renderer = self._playing_renderer(room_id)
            if not renderer:
                return  # nothing playing → no echo to remove
            mgr = _get_mcp_manager()
            if mgr is None:
                return
            original = await self._get_volume(mgr, renderer)
            if original is None:
                return
            target = max(0, min(100, ha_glue_settings.duck_on_listen_volume))
            if original <= target:
                return  # already at/below the duck level — nothing to do
            if not await self._set_volume(mgr, renderer, target):
                return
            task = asyncio.create_task(self._safety_restore(room_id))
            async with self._lock:
                if room_id in self._ducked:
                    # A concurrent duck_room won the race; ours is redundant (it
                    # read the same original and set the same target). Drop it.
                    task.cancel()
                    return
                self._ducked[room_id] = _Ducked(renderer, original, task)
            logger.info(
                f"🔉 Ducked '{renderer}' in room {room_id} "
                f"({original}→{target}) for a voice turn"
            )
        except Exception as e:
            logger.debug(f"duck_room failed for room {room_id}: {error_text_without_content(e)}")

    async def restore_room(self, room_id: int | None) -> None:
        """Restore the pre-duck volume for ``room_id`` (no-op if it wasn't ducked)."""
        if room_id is None:
            return
        try:
            async with self._lock:
                stash = self._ducked.pop(room_id, None)
            if stash is None:
                return
            # Cancel the safety timer — UNLESS we ARE the safety task: cancelling
            # our own task delivers CancelledError at the next await (the set_volume
            # below), aborting the restore and leaving media stuck ducked. That is
            # exactly the satellite-dropped-mid-turn case this timer exists for.
            if stash.task is not None and stash.task is not asyncio.current_task():
                stash.task.cancel()
            mgr = _get_mcp_manager()
            if mgr is not None and stash.renderer:
                await self._set_volume(mgr, stash.renderer, stash.original)
            logger.info(f"🔊 Restored '{stash.renderer}' in room {room_id} → {stash.original}")
        except Exception as e:
            logger.debug(f"restore_room failed for room {room_id}: {error_text_without_content(e)}")

    async def _safety_restore(self, room_id: int) -> None:
        """Restore even if the turn never returns to IDLE (satellite dropped mid-turn)."""
        try:
            await asyncio.sleep(ha_glue_settings.duck_on_listen_max_seconds)
        except asyncio.CancelledError:
            return
        logger.warning(f"🔊 Duck safety-timeout in room {room_id} — restoring")
        await self.restore_room(room_id)

    @staticmethod
    def _playing_renderer(room_id: int) -> str | None:
        from ha_glue.services.media_follow_service import get_media_follow_service

        return get_media_follow_service().playing_renderer_in_room(room_id)

    @staticmethod
    async def _get_volume(mgr, renderer: str) -> int | None:
        try:
            res = await mgr.execute_tool("mcp.dlna.get_volume", {"renderer_name": renderer})
            return _parse_volume(res if isinstance(res, dict) else {})
        except Exception as e:
            logger.debug(f"duck get_volume failed for '{renderer}': {error_text_without_content(e)}")
            return None

    @staticmethod
    async def _set_volume(mgr, renderer: str, volume: int) -> bool:
        try:
            await mgr.execute_tool(
                "mcp.dlna.set_volume", {"renderer_name": renderer, "volume": int(volume)}
            )
            return True
        except Exception as e:
            logger.debug(f"duck set_volume failed for '{renderer}': {error_text_without_content(e)}")
            return False


_duck_service: DuckService | None = None


def get_duck_service() -> DuckService:
    global _duck_service
    if _duck_service is None:
        _duck_service = DuckService()
    return _duck_service
