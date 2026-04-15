"""
Media Follow Me — Playback follows the user between rooms.

Tracks active media sessions per user and handles presence hooks to
suspend/resume playback as users move between rooms.

Requires: MEDIA_FOLLOW_ENABLED=true AND PRESENCE_ENABLED=true
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from enum import Enum

from loguru import logger

from utils.config import settings
from ha_glue.utils.config import ha_glue_settings


class MediaType(str, Enum):
    SINGLE_URL = "single_url"      # HA media_player.play_media
    DLNA_ALBUM = "dlna_album"      # DLNA play_tracks (album queue)
    RADIO = "radio"                # Radio stream (TuneIn)
    DLNA_VIDEO = "dlna_video"      # DLNA video playback (Jellyfin movie/episode)


class SessionState(str, Enum):
    PLAYING = "playing"
    SUSPENDED = "suspended"


@dataclass
class MediaSession:
    user_id: int
    room_id: int
    room_name: str
    media_type: MediaType
    state: SessionState = SessionState.PLAYING

    # Playback data
    media_url: str | None = None
    station_id: str | None = None
    station_name: str | None = None
    album_id: str | None = None
    album_name: str | None = None
    renderer_name: str | None = None
    current_track: int | None = None       # 1-based
    total_tracks: int | None = None
    title: str | None = None
    thumb: str | None = None

    # Timestamps
    started_at: float = field(default_factory=time.time)
    suspended_at: float | None = None


class MediaFollowService:
    """In-memory singleton that tracks media sessions and handles room transitions."""

    def __init__(self) -> None:
        self._sessions: dict[int, MediaSession] = {}   # user_id → session

    # ------------------------------------------------------------------
    # Playback Registration (called by InternalToolService)
    # ------------------------------------------------------------------

    def register_playback(
        self,
        user_id: int,
        room_id: int,
        room_name: str,
        media_type: MediaType,
        **kwargs,
    ) -> None:
        """Register a new media playback session for a user."""
        session = MediaSession(
            user_id=user_id,
            room_id=room_id,
            room_name=room_name,
            media_type=media_type,
            media_url=kwargs.get("media_url"),
            station_id=kwargs.get("station_id"),
            station_name=kwargs.get("station_name"),
            album_id=kwargs.get("album_id"),
            album_name=kwargs.get("album_name"),
            renderer_name=kwargs.get("renderer_name"),
            current_track=kwargs.get("current_track"),
            total_tracks=kwargs.get("total_tracks"),
            title=kwargs.get("title"),
            thumb=kwargs.get("thumb"),
        )
        self._sessions[user_id] = session
        logger.info(
            f"🎵 Media session registered: user={user_id} "
            f"type={media_type.value} room={room_name}"
        )

    def clear_session(self, user_id: int) -> None:
        """Remove a user's media session."""
        removed = self._sessions.pop(user_id, None)
        if removed:
            logger.debug(f"🎵 Media session cleared: user={user_id}")

    def clear_session_by_room(self, room_id: int) -> None:
        """Clear all sessions in a given room (e.g. on explicit stop)."""
        to_remove = [
            uid for uid, s in self._sessions.items()
            if s.room_id == room_id and s.state == SessionState.PLAYING
        ]
        for uid in to_remove:
            self._sessions.pop(uid, None)
            logger.debug(f"🎵 Media session cleared by room stop: user={uid} room_id={room_id}")

    def get_session(self, user_id: int) -> MediaSession | None:
        return self._sessions.get(user_id)

    # ------------------------------------------------------------------
    # Presence Hook Handlers
    # ------------------------------------------------------------------

    async def on_user_leave_room(
        self, user_id: int, room_id: int, room_name: str, **kw
    ) -> None:
        """Called when a user leaves a room (presence hook)."""
        session = self._sessions.get(user_id)
        if not session or session.room_id != room_id:
            return
        if session.state != SessionState.PLAYING:
            return

        # Check per-user opt-in
        if not await self._is_user_opted_in(user_id):
            logger.debug(f"🎵 User {user_id} has media_follow disabled, clearing session")
            self.clear_session(user_id)
            return

        # Suspend: stop playback, mark session
        logger.info(
            f"🎵 User {user_id} left {room_name} — suspending "
            f"'{session.title or session.station_name or session.album_name}'"
        )
        await self._stop_playback(session)
        session.state = SessionState.SUSPENDED
        session.suspended_at = time.time()

    async def on_user_enter_room(
        self, user_id: int, room_id: int, room_name: str, **kw
    ) -> None:
        """Called when a user enters a room (presence hook)."""
        session = self._sessions.get(user_id)
        if not session or session.state != SessionState.SUSPENDED:
            return

        # Cleanup expired sessions
        self._cleanup_expired_sessions()
        # Re-check after cleanup
        session = self._sessions.get(user_id)
        if not session or session.state != SessionState.SUSPENDED:
            return

        # Check for conflict: another user playing in this room
        existing_user_id = self._find_playing_user_in_room(room_id)

        if existing_user_id is not None:
            winner = await self._resolve_conflict(user_id, existing_user_id, room_id)
            if winner == existing_user_id:
                # Entering user loses — keep session suspended
                logger.info(
                    f"🎵 Conflict in {room_name}: user {existing_user_id} wins, "
                    f"user {user_id} stays suspended"
                )
                return
            else:
                # Entering user wins — suspend existing
                existing_session = self._sessions.get(existing_user_id)
                if existing_session:
                    logger.info(
                        f"🎵 Conflict in {room_name}: user {user_id} wins, "
                        f"suspending user {existing_user_id}"
                    )
                    await self._stop_playback(existing_session)
                    existing_session.state = SessionState.SUSPENDED
                    existing_session.suspended_at = time.time()

        # Resume in new room
        logger.info(
            f"🎵 User {user_id} entered {room_name} — resuming "
            f"'{session.title or session.station_name or session.album_name}'"
        )

        if ha_glue_settings.media_follow_resume_delay > 0:
            await asyncio.sleep(ha_glue_settings.media_follow_resume_delay)

        await self._resume_playback(session, room_id, room_name)

    async def on_last_left(self, room_id: int, room_name: str, **kw) -> None:
        """Safety net: stop all playback when the last user leaves a room."""
        to_stop = [
            s for s in self._sessions.values()
            if s.room_id == room_id and s.state == SessionState.PLAYING
        ]
        for session in to_stop:
            logger.info(
                f"🎵 Last user left {room_name} — stopping playback for user {session.user_id}"
            )
            await self._stop_playback(session)
            session.state = SessionState.SUSPENDED
            session.suspended_at = time.time()

    # ------------------------------------------------------------------
    # Internal: Stop / Resume
    # ------------------------------------------------------------------

    async def _stop_playback(self, session: MediaSession) -> None:
        """Stop current playback via MCP/HA."""
        try:
            from services.internal_tools import InternalToolService

            svc = InternalToolService()
            result = await svc._media_control({
                "action": "stop",
                "room_name": session.room_name,
                "force": "true",
            })
            if not result.get("success"):
                logger.warning(
                    f"🎵 Stop playback failed in {session.room_name}: "
                    f"{result.get('message')}"
                )
        except Exception as e:
            logger.error(f"🎵 Error stopping playback in {session.room_name}: {e}")

    async def _resume_playback(
        self, session: MediaSession, new_room_id: int, new_room_name: str
    ) -> None:
        """Resume playback in a new room."""
        try:
            from services.internal_tools import InternalToolService

            svc = InternalToolService()
            result: dict = {}

            if session.media_type == MediaType.RADIO:
                if session.station_id:
                    result = await svc._play_radio({
                        "station_id": session.station_id,
                        "room_name": new_room_name,
                        "station_name": session.station_name or "",
                        "force": "true",
                    })
                elif session.media_url:
                    result = await svc._play_in_room({
                        "media_url": session.media_url,
                        "room_name": new_room_name,
                        "title": session.station_name or session.title or "",
                        "force": "true",
                    })

            elif session.media_type == MediaType.SINGLE_URL:
                if session.media_url:
                    result = await svc._play_in_room({
                        "media_url": session.media_url,
                        "room_name": new_room_name,
                        "title": session.title or "",
                        "thumb": session.thumb or "",
                        "force": "true",
                    })

            elif session.media_type == MediaType.DLNA_ALBUM:
                if session.album_id:
                    result = await svc._play_album_on_dlna({
                        "album_id": session.album_id,
                        "room_name": new_room_name,
                        "album_name": session.album_name or "",
                    })

            elif session.media_type == MediaType.DLNA_VIDEO:
                if session.album_id:  # album_id reused as item_id for videos
                    result = await svc._play_video_on_dlna({
                        "item_id": session.album_id,
                        "room_name": new_room_name,
                        "title": session.title or "",
                    })

            if result.get("success"):
                session.room_id = new_room_id
                session.room_name = new_room_name
                session.state = SessionState.PLAYING
                session.suspended_at = None
                logger.info(f"🎵 Resumed playback for user {session.user_id} in {new_room_name}")

                # Notify user via WebSocket
                await self._notify_user(
                    session.user_id,
                    new_room_name,
                    session.title or session.station_name or session.album_name or "Media",
                )
            else:
                logger.warning(
                    f"🎵 Resume failed for user {session.user_id} in {new_room_name}: "
                    f"{result.get('message', 'unknown error')}"
                )

        except Exception as e:
            logger.error(f"🎵 Error resuming playback in {new_room_name}: {e}")

    # ------------------------------------------------------------------
    # Internal: Conflict Resolution
    # ------------------------------------------------------------------

    async def _resolve_conflict(
        self, entering_user_id: int, existing_user_id: int, room_id: int
    ) -> int:
        """
        Determine whose media should play. Returns the winner's user_id.

        Priority: Room owner > Role priority (lower=higher) > First-come.
        """
        # 1. Room owner always wins
        owner_id = await self._get_room_owner_id(room_id)
        if owner_id == entering_user_id:
            return entering_user_id
        if owner_id == existing_user_id:
            return existing_user_id

        # 2. Higher role priority wins (lower number = higher priority)
        entering_prio = await self._get_role_priority(entering_user_id)
        existing_prio = await self._get_role_priority(existing_user_id)
        if entering_prio < existing_prio:
            return entering_user_id
        if existing_prio < entering_prio:
            return existing_user_id

        # 3. Equal: first-come keeps media
        return existing_user_id

    # ------------------------------------------------------------------
    # Internal: DB Lookups
    # ------------------------------------------------------------------

    async def _is_user_opted_in(self, user_id: int) -> bool:
        """Check if user has media_follow_enabled."""
        try:
            from models.database import User
            from services.database import AsyncSessionLocal

            async with AsyncSessionLocal() as db:
                user = await db.get(User, user_id)
                if user is None:
                    return False
                return bool(user.media_follow_enabled)
        except Exception:
            logger.exception(f"🎵 Error checking media_follow opt-in for user {user_id}")
            return False

    async def _get_role_priority(self, user_id: int) -> int:
        """Lookup role priority. Returns 100 (lowest) on failure."""
        try:
            from models.database import Role, User
            from services.database import AsyncSessionLocal

            async with AsyncSessionLocal() as db:
                user = await db.get(User, user_id)
                if user and user.role_id:
                    role = await db.get(Role, user.role_id)
                    if role:
                        return role.priority
        except Exception:
            logger.exception(f"🎵 Error looking up role priority for user {user_id}")
        return 100

    async def _get_room_owner_id(self, room_id: int) -> int | None:
        """Get room owner_id. Returns None if no owner set."""
        try:
            from models.database import Room
            from services.database import AsyncSessionLocal

            async with AsyncSessionLocal() as db:
                room = await db.get(Room, room_id)
                return room.owner_id if room else None
        except Exception:
            logger.exception(f"🎵 Error looking up room owner for room {room_id}")
            return None

    # ------------------------------------------------------------------
    # Internal: Helpers
    # ------------------------------------------------------------------

    def _find_playing_user_in_room(self, room_id: int) -> int | None:
        """Find user_id of someone actively playing in a room."""
        for uid, s in self._sessions.items():
            if s.room_id == room_id and s.state == SessionState.PLAYING:
                return uid
        return None

    def _cleanup_expired_sessions(self) -> None:
        """Remove suspended sessions that have exceeded the timeout."""
        now = time.time()
        timeout = ha_glue_settings.media_follow_suspend_timeout
        expired = [
            uid for uid, s in self._sessions.items()
            if s.state == SessionState.SUSPENDED
            and s.suspended_at is not None
            and (now - s.suspended_at) > timeout
        ]
        for uid in expired:
            logger.debug(f"🎵 Session expired for user {uid} (timeout={timeout}s)")
            self._sessions.pop(uid, None)

    async def _notify_user(
        self, user_id: int, room_name: str, media_title: str
    ) -> None:
        """Send an info notification to the user via Device WebSocket."""
        try:
            from ha_glue.services.device_manager import get_device_manager

            dm = get_device_manager()
            message = {
                "type": "info",
                "message": (
                    f"Musik folgt dir: '{media_title}' spielt jetzt im {room_name}"
                ),
            }
            # Broadcast to the room the user just entered
            await dm.broadcast_to_room(room_name, message)
        except Exception:
            # Non-critical — don't let notification failure break the flow
            logger.debug(f"🎵 Could not notify user {user_id} about media follow")


# ------------------------------------------------------------------
# Singleton
# ------------------------------------------------------------------

_media_follow_service: MediaFollowService | None = None


def get_media_follow_service() -> MediaFollowService:
    """Get the singleton MediaFollowService instance."""
    global _media_follow_service
    if _media_follow_service is None:
        _media_follow_service = MediaFollowService()
    return _media_follow_service
