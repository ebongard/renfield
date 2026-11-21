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

from ha_glue.utils.config import ha_glue_settings
from utils.config import settings
from utils.log_safety import error_text_without_content, frames_without_content, logged_content


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
        # Last now-playing set we broadcast to the kiosk hub. The delta is
        # diff-gated against this so a no-op mutation (e.g. a suspend that leaves
        # the PLAYING set unchanged) never fires a spurious push. None = never
        # broadcast yet.
        self._last_now_playing: list[dict] | None = None
        # Strong refs to fire-and-forget broadcast tasks scheduled from SYNC
        # mutation sites, so they aren't GC'd mid-flight (the boot-reconnect
        # strand bug). Discarded on completion.
        self._np_bg_tasks: set[asyncio.Task] = set()

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
        self._schedule_now_playing_broadcast()

    def clear_session(self, user_id: int) -> None:
        """Remove a user's media session."""
        removed = self._sessions.pop(user_id, None)
        if removed:
            logger.debug(f"🎵 Media session cleared: user={user_id}")
            self._schedule_now_playing_broadcast()

    def clear_session_by_room(self, room_id: int) -> None:
        """Clear all sessions in a given room (e.g. on explicit stop)."""
        to_remove = [
            uid for uid, s in self._sessions.items()
            if s.room_id == room_id and s.state == SessionState.PLAYING
        ]
        for uid in to_remove:
            self._sessions.pop(uid, None)
            logger.debug(f"🎵 Media session cleared by room stop: user={uid} room_id={room_id}")
        if to_remove:
            self._schedule_now_playing_broadcast()

    def get_session(self, user_id: int) -> MediaSession | None:
        return self._sessions.get(user_id)

    def active_sessions(self) -> list[dict]:
        """Content-minimal snapshot of the live PLAYING sessions, for the kiosk
        now-playing tile. One entry per room (a room plays one thing), newest
        first. No user ids leave here — only the room, what is playing, and the
        media kind. Suspended sessions (user left the room) are omitted."""
        self._cleanup_expired_sessions()
        by_room: dict[int, MediaSession] = {}
        for s in self._sessions.values():
            if s.state is not SessionState.PLAYING:
                continue
            # Keep the most recently started session per room.
            prev = by_room.get(s.room_id)
            if prev is None or s.started_at > prev.started_at:
                by_room[s.room_id] = s
        out: list[dict] = []
        for s in sorted(by_room.values(), key=lambda x: x.started_at, reverse=True):
            title = s.title or s.station_name or s.album_name or ""
            out.append(
                {
                    "room": s.room_name,
                    "kind": s.media_type.value,
                    "title": title,
                    "subtitle": s.album_name if title != s.album_name else None,
                    "track": s.current_track,
                    "total": s.total_tracks,
                }
            )
        return out

    # ------------------------------------------------------------------
    # Kiosk push (now_playing_changed delta)
    # ------------------------------------------------------------------

    async def _broadcast_now_playing_if_changed(self) -> None:
        """Push a content-free ``now_playing_changed`` delta to the kiosk hub,
        but ONLY when the deduped per-room PLAYING set actually changed
        (start / stop / track / room move). Diff-gated against the last broadcast
        so a mutation that leaves the visible set identical stays silent — the
        transition guard that keeps this from firing on every internal touch.
        Fire-and-forget: a hub failure must never disrupt playback."""
        try:
            sessions = self.active_sessions()
            if sessions == self._last_now_playing:
                return
            self._last_now_playing = sessions
            from api.websocket.kiosk_handler import broadcast_kiosk_event

            await broadcast_kiosk_event(
                {"type": "now_playing_changed", "sessions": sessions}
            )
        except Exception as e:
            logger.debug(f"kiosk now_playing broadcast failed: {error_text_without_content(e)}")

    def _schedule_now_playing_broadcast(self) -> None:
        """Fire the diff-gated now_playing broadcast from a SYNC mutation site
        (register/clear). Strong-refs the task so it isn't GC'd before it runs."""
        try:
            task = asyncio.ensure_future(self._broadcast_now_playing_if_changed())
        except RuntimeError:
            return  # no running loop (e.g. a unit test poking the sync setter)
        self._np_bg_tasks.add(task)
        task.add_done_callback(self._np_bg_tasks.discard)

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
            f"{logged_content(session.title or session.station_name or session.album_name)}"
        )
        await self._stop_playback(session)
        session.state = SessionState.SUSPENDED
        session.suspended_at = time.time()
        await self._broadcast_now_playing_if_changed()

    async def on_user_enter_room(
        self, user_id: int, room_id: int, room_name: str, **kw
    ) -> None:
        """Called when a user enters a room (presence hook)."""
        # Drop expired suspended sessions first, then look up the live one.
        self._cleanup_expired_sessions()
        session = self._sessions.get(user_id)
        if not session:
            return

        # Decide whether this enter should (re)start playback here:
        #  - SUSPENDED: the user left elsewhere first (the leave fired) and is now
        #    arriving — resume.
        #  - PLAYING in a DIFFERENT room: the user moved but the matching 'leave'
        #    has NOT fired yet. presence_stale_timeout (120s) means the old room's
        #    satellite keeps seeing the device long after the move, so the enter
        #    routinely arrives while the session is still PLAYING. Treat it as a
        #    move: stop the old room, then resume here. Without this branch the
        #    late leave suspends the session AFTER this enter already bailed, and
        #    the music never follows (the live-test bug, 2026-06-14).
        if session.state == SessionState.PLAYING:
            if session.room_id == room_id:
                return  # already playing in this room — nothing to do
            await self._stop_playback(session)
            session.state = SessionState.SUSPENDED
            session.suspended_at = time.time()
        elif session.state != SessionState.SUSPENDED:
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
                # The mover's old-room playback was already stopped above, so the
                # kiosk now-playing tile must reflect that (diff-gated → no-op if
                # nothing actually changed).
                await self._broadcast_now_playing_if_changed()
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
            f"{logged_content(session.title or session.station_name or session.album_name)}"
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
        if to_stop:
            await self._broadcast_now_playing_if_changed()

    # ------------------------------------------------------------------
    # Internal: Stop / Resume
    # ------------------------------------------------------------------

    async def _stop_playback(self, session: MediaSession) -> None:
        """Stop current playback via MCP/HA."""
        try:
            from ha_glue.services.internal_tools import InternalToolService

            svc = InternalToolService()
            result = await svc._media_control({
                "action": "stop",
                "room_name": session.room_name,
                "force": "true",
                # Mark this as Media Follow's OWN suspend-stop, not a user "stop".
                # _media_control clears the follow session on a stop; without this
                # marker the suspend would delete the very session we're about to
                # resume in the next room, so the music never follows (live-test
                # 2026-06-14).
                "_media_follow_internal": True,
            })
            if not result.get("success"):
                logger.warning(
                    f"🎵 Stop playback failed in {session.room_name}: "
                    f"{logged_content(result.get('message'))}"
                )
        except Exception as e:
            logger.error(f"🎵 Error stopping playback in {session.room_name}: {error_text_without_content(e)}")

    async def _resume_playback(
        self, session: MediaSession, new_room_id: int, new_room_name: str
    ) -> None:
        """Resume playback in a new room."""
        try:
            from ha_glue.services.internal_tools import InternalToolService

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
                await self._broadcast_now_playing_if_changed()
            else:
                logger.warning(
                    f"🎵 Resume failed for user {session.user_id} in {new_room_name}: "
                    f"{logged_content(result.get('message', 'unknown error'))}"
                )
                # The old-room session was already stopped before this resume
                # attempt, so reflect the now-stopped state on the kiosk too.
                await self._broadcast_now_playing_if_changed()

        except Exception as e:
            logger.error(f"🎵 Error resuming playback in {new_room_name}: {error_text_without_content(e)}")

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
        except Exception as e:
            logger.error(
                f"🎵 Error checking media_follow opt-in for user {user_id}"
                f": {type(e).__name__}: {error_text_without_content(e)} | at {frames_without_content(e)}"
            )
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
        except Exception as e:
            logger.error(
                f"🎵 Error looking up role priority for user {user_id}"
                f": {type(e).__name__}: {error_text_without_content(e)} | at {frames_without_content(e)}"
            )
        return 100

    async def _get_room_owner_id(self, room_id: int) -> int | None:
        """Get room owner_id. Returns None if no owner set."""
        try:
            from models.database import Room
            from services.database import AsyncSessionLocal

            async with AsyncSessionLocal() as db:
                room = await db.get(Room, room_id)
                return room.owner_id if room else None
        except Exception as e:
            logger.error(
                f"🎵 Error looking up room owner for room {room_id}"
                f": {type(e).__name__}: {error_text_without_content(e)} | at {frames_without_content(e)}"
            )
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

    def playing_renderer_in_room(self, room_id: int) -> str | None:
        """The DLNA renderer name of media actively PLAYING in a room, else None.

        Used by duck-on-listen (Phase 4) to find the echo source to duck while a
        satellite in that room is listening.
        """
        uid = self._find_playing_user_in_room(room_id)
        if uid is None:
            return None
        session = self.get_session(uid)
        return session.renderer_name if session else None

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
        """Notify the user that their playback has followed them via Device WS.

        Two frames, both broadcast to the room the user just ENTERED (i.e. their
        own current location — no other user's location is exposed, and the
        audience is exactly the destination room, the same audience the legacy
        ``info`` toast already reached):

        * ``info`` — the existing transient toast (unconditional, back-compat).
        * ``media_handoff`` — the typed frame backing the chat room-handoff
          affordance (item 8). Emitted ONLY when ``ROOM_HANDOFF_ENABLED`` so the
          feature ships dark. The chat thread renders it as a quiet inline meta
          line ("🔊 Wiedergabe folgt nach {room}"). Transient — never persisted.
        """
        try:
            from ha_glue.services.device_manager import get_device_manager

            dm = get_device_manager()
            # Existing transient toast (unchanged).
            await dm.broadcast_to_room(room_name, {
                "type": "info",
                "message": (
                    f"Musik folgt dir: '{media_title}' spielt jetzt im {room_name}"
                ),
            })

            # Typed room-handoff frame for the chat affordance (flag-gated, dark).
            if settings.room_handoff_enabled:
                await dm.broadcast_to_room(room_name, {
                    "type": "media_handoff",
                    "kind": "media_followed",   # vs a future "continued"
                    "room": room_name,          # the room the user just entered
                    "title": media_title,
                })
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
