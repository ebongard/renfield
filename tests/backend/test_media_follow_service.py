"""
Tests for MediaFollowService — Media Follow Me.

Tests session lifecycle, conflict resolution, opt-in/opt-out, timeout expiry.
"""

import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ha_glue.services.media_follow_service import (
    MediaFollowService,
    MediaSession,
    MediaType,
    SessionState,
)


@pytest.fixture
def service():
    """Create a fresh MediaFollowService for each test."""
    svc = MediaFollowService()
    return svc


@pytest.fixture
def playing_session(service):
    """Register a playing radio session for user 1 in room 1."""
    service.register_playback(
        user_id=1,
        room_id=10,
        room_name="Arbeitszimmer",
        media_type=MediaType.RADIO,
        station_id="s12345",
        station_name="BBC Radio 1",
        media_url="http://stream.example.com/bbc1",
    )
    return service


# =============================================================================
# Session Registration
# =============================================================================


@pytest.mark.unit
class TestSessionRegistration:
    def test_register_playback_creates_session(self, service):
        service.register_playback(
            user_id=1,
            room_id=10,
            room_name="Wohnzimmer",
            media_type=MediaType.RADIO,
            station_id="s123",
            station_name="BBC Radio 1",
        )
        session = service.get_session(1)
        assert session is not None
        assert session.user_id == 1
        assert session.room_id == 10
        assert session.room_name == "Wohnzimmer"
        assert session.media_type == MediaType.RADIO
        assert session.station_id == "s123"
        assert session.state == SessionState.PLAYING

    def test_register_replaces_existing_session(self, playing_session):
        playing_session.register_playback(
            user_id=1,
            room_id=20,
            room_name="Wohnzimmer",
            media_type=MediaType.SINGLE_URL,
            media_url="http://example.com/song.mp3",
        )
        session = playing_session.get_session(1)
        assert session.room_id == 20
        assert session.media_type == MediaType.SINGLE_URL

    def test_clear_session(self, playing_session):
        playing_session.clear_session(1)
        assert playing_session.get_session(1) is None

    def test_clear_session_nonexistent_user(self, service):
        # Should not raise
        service.clear_session(999)

    def test_clear_session_by_room(self, playing_session):
        playing_session.clear_session_by_room(10)
        assert playing_session.get_session(1) is None

    def test_clear_session_by_room_only_playing(self, service):
        """Suspended sessions should not be cleared by room stop."""
        service.register_playback(
            user_id=1, room_id=10, room_name="Room",
            media_type=MediaType.RADIO,
        )
        session = service.get_session(1)
        session.state = SessionState.SUSPENDED
        service.clear_session_by_room(10)
        # Suspended session should remain
        assert service.get_session(1) is not None

    def test_register_album_session(self, service):
        service.register_playback(
            user_id=2,
            room_id=20,
            room_name="Wohnzimmer",
            media_type=MediaType.DLNA_ALBUM,
            album_id="abc123",
            album_name="Dark Side of the Moon",
            renderer_name="Wohnzimmer DLNA",
            total_tracks=10,
        )
        session = service.get_session(2)
        assert session.media_type == MediaType.DLNA_ALBUM
        assert session.album_id == "abc123"
        assert session.total_tracks == 10


# =============================================================================
# User Leave Room
# =============================================================================


@pytest.mark.unit
class TestOnUserLeaveRoom:
    @pytest.mark.asyncio
    async def test_leave_suspends_session(self, playing_session):
        with patch.object(playing_session, "_is_user_opted_in", return_value=True), \
             patch.object(playing_session, "_stop_playback", new_callable=AsyncMock):
            await playing_session.on_user_leave_room(
                user_id=1, room_id=10, room_name="Arbeitszimmer"
            )
        session = playing_session.get_session(1)
        assert session.state == SessionState.SUSPENDED
        assert session.suspended_at is not None

    @pytest.mark.asyncio
    async def test_leave_stops_playback(self, playing_session):
        mock_stop = AsyncMock()
        with patch.object(playing_session, "_is_user_opted_in", return_value=True), \
             patch.object(playing_session, "_stop_playback", mock_stop):
            await playing_session.on_user_leave_room(
                user_id=1, room_id=10, room_name="Arbeitszimmer"
            )
        mock_stop.assert_called_once()

    @pytest.mark.asyncio
    async def test_leave_wrong_room_no_action(self, playing_session):
        """Leaving a different room should not affect the session."""
        with patch.object(playing_session, "_stop_playback", new_callable=AsyncMock) as mock_stop:
            await playing_session.on_user_leave_room(
                user_id=1, room_id=99, room_name="Other"
            )
        mock_stop.assert_not_called()
        assert playing_session.get_session(1).state == SessionState.PLAYING

    @pytest.mark.asyncio
    async def test_leave_no_session_no_action(self, service):
        """User without a session should not cause errors."""
        with patch.object(service, "_stop_playback", new_callable=AsyncMock) as mock_stop:
            await service.on_user_leave_room(
                user_id=99, room_id=10, room_name="Room"
            )
        mock_stop.assert_not_called()

    @pytest.mark.asyncio
    async def test_leave_opted_out_clears_session(self, playing_session):
        """User with media_follow disabled should have session cleared, not suspended."""
        with patch.object(playing_session, "_is_user_opted_in", return_value=False):
            await playing_session.on_user_leave_room(
                user_id=1, room_id=10, room_name="Arbeitszimmer"
            )
        assert playing_session.get_session(1) is None


# =============================================================================
# User Enter Room
# =============================================================================


@pytest.mark.unit
class TestOnUserEnterRoom:
    @pytest.mark.asyncio
    async def test_enter_resumes_suspended_session(self, playing_session):
        # Manually suspend
        session = playing_session.get_session(1)
        session.state = SessionState.SUSPENDED
        session.suspended_at = time.time()

        mock_resume = AsyncMock()
        with patch.object(playing_session, "_resume_playback", mock_resume), \
             patch("ha_glue.services.media_follow_service.settings") as mock_settings:
            mock_settings.media_follow_suspend_timeout = 600.0
            mock_settings.media_follow_resume_delay = 0  # no delay in tests
            await playing_session.on_user_enter_room(
                user_id=1, room_id=20, room_name="Wohnzimmer"
            )
        mock_resume.assert_called_once_with(session, 20, "Wohnzimmer")

    @pytest.mark.asyncio
    async def test_enter_no_suspended_session_no_action(self, playing_session):
        """Playing session (not suspended) should not trigger resume."""
        mock_resume = AsyncMock()
        with patch.object(playing_session, "_resume_playback", mock_resume):
            await playing_session.on_user_enter_room(
                user_id=1, room_id=20, room_name="Wohnzimmer"
            )
        mock_resume.assert_not_called()

    @pytest.mark.asyncio
    async def test_enter_expired_session_no_resume(self, playing_session):
        """Expired suspended session should be cleaned up."""
        session = playing_session.get_session(1)
        session.state = SessionState.SUSPENDED
        session.suspended_at = time.time() - 700  # Expired (>600s)

        mock_resume = AsyncMock()
        with patch.object(playing_session, "_resume_playback", mock_resume), \
             patch("ha_glue.services.media_follow_service.settings") as mock_settings:
            mock_settings.media_follow_suspend_timeout = 600.0
            mock_settings.media_follow_resume_delay = 0
            await playing_session.on_user_enter_room(
                user_id=1, room_id=20, room_name="Wohnzimmer"
            )
        mock_resume.assert_not_called()
        assert playing_session.get_session(1) is None


# =============================================================================
# Conflict Resolution
# =============================================================================


@pytest.mark.unit
class TestConflictResolution:
    @pytest.mark.asyncio
    async def test_room_owner_wins(self, service):
        with patch.object(service, "_get_room_owner_id", return_value=1), \
             patch.object(service, "_get_role_priority", return_value=50):
            winner = await service._resolve_conflict(
                entering_user_id=1, existing_user_id=2, room_id=10
            )
        assert winner == 1

    @pytest.mark.asyncio
    async def test_room_owner_existing_wins(self, service):
        with patch.object(service, "_get_room_owner_id", return_value=2), \
             patch.object(service, "_get_role_priority", return_value=50):
            winner = await service._resolve_conflict(
                entering_user_id=1, existing_user_id=2, room_id=10
            )
        assert winner == 2

    @pytest.mark.asyncio
    async def test_higher_role_priority_wins(self, service):
        """Lower priority number = higher privilege."""
        with patch.object(service, "_get_room_owner_id", return_value=None):
            # User 1: Admin (10), User 2: Familie (50)
            async def mock_priority(uid):
                return 10 if uid == 1 else 50
            with patch.object(service, "_get_role_priority", side_effect=mock_priority):
                winner = await service._resolve_conflict(
                    entering_user_id=1, existing_user_id=2, room_id=10
                )
        assert winner == 1

    @pytest.mark.asyncio
    async def test_lower_role_priority_loses(self, service):
        with patch.object(service, "_get_room_owner_id", return_value=None):
            async def mock_priority(uid):
                return 90 if uid == 1 else 10
            with patch.object(service, "_get_role_priority", side_effect=mock_priority):
                winner = await service._resolve_conflict(
                    entering_user_id=1, existing_user_id=2, room_id=10
                )
        assert winner == 2

    @pytest.mark.asyncio
    async def test_equal_priority_first_come_wins(self, service):
        """Same priority → existing user keeps their media."""
        with patch.object(service, "_get_room_owner_id", return_value=None), \
             patch.object(service, "_get_role_priority", return_value=50):
            winner = await service._resolve_conflict(
                entering_user_id=1, existing_user_id=2, room_id=10
            )
        assert winner == 2

    @pytest.mark.asyncio
    async def test_no_owner_falls_through_to_role(self, service):
        with patch.object(service, "_get_room_owner_id", return_value=None):
            async def mock_priority(uid):
                return 50 if uid == 1 else 90
            with patch.object(service, "_get_role_priority", side_effect=mock_priority):
                winner = await service._resolve_conflict(
                    entering_user_id=1, existing_user_id=2, room_id=10
                )
        assert winner == 1


# =============================================================================
# Conflict During Enter
# =============================================================================


@pytest.mark.unit
class TestConflictDuringEnter:
    @pytest.mark.asyncio
    async def test_entering_user_wins_conflict(self, service):
        """When entering user has higher priority, existing user gets suspended."""
        # User 2 is playing in room 20
        service.register_playback(
            user_id=2, room_id=20, room_name="Wohnzimmer",
            media_type=MediaType.RADIO, station_id="s1",
        )
        # User 1 has a suspended session
        service.register_playback(
            user_id=1, room_id=10, room_name="Arbeitszimmer",
            media_type=MediaType.RADIO, station_id="s2",
        )
        session1 = service.get_session(1)
        session1.state = SessionState.SUSPENDED
        session1.suspended_at = time.time()

        mock_resume = AsyncMock()
        mock_stop = AsyncMock()
        with patch.object(service, "_resume_playback", mock_resume), \
             patch.object(service, "_stop_playback", mock_stop), \
             patch.object(service, "_resolve_conflict", return_value=1), \
             patch("ha_glue.services.media_follow_service.settings") as mock_settings:
            mock_settings.media_follow_suspend_timeout = 600.0
            mock_settings.media_follow_resume_delay = 0
            await service.on_user_enter_room(
                user_id=1, room_id=20, room_name="Wohnzimmer"
            )

        # Existing user's playback should be stopped
        mock_stop.assert_called_once()
        # Entering user's session should be resumed
        mock_resume.assert_called_once()
        # Existing user should be suspended
        assert service.get_session(2).state == SessionState.SUSPENDED

    @pytest.mark.asyncio
    async def test_entering_user_loses_conflict(self, service):
        """When existing user has higher priority, entering user stays suspended."""
        # User 2 is playing in room 20
        service.register_playback(
            user_id=2, room_id=20, room_name="Wohnzimmer",
            media_type=MediaType.RADIO, station_id="s1",
        )
        # User 1 has a suspended session
        service.register_playback(
            user_id=1, room_id=10, room_name="Arbeitszimmer",
            media_type=MediaType.RADIO, station_id="s2",
        )
        session1 = service.get_session(1)
        session1.state = SessionState.SUSPENDED
        session1.suspended_at = time.time()

        mock_resume = AsyncMock()
        with patch.object(service, "_resume_playback", mock_resume), \
             patch.object(service, "_resolve_conflict", return_value=2), \
             patch("ha_glue.services.media_follow_service.settings") as mock_settings:
            mock_settings.media_follow_suspend_timeout = 600.0
            mock_settings.media_follow_resume_delay = 0
            await service.on_user_enter_room(
                user_id=1, room_id=20, room_name="Wohnzimmer"
            )

        # No resume should happen
        mock_resume.assert_not_called()
        # User 1 stays suspended
        assert service.get_session(1).state == SessionState.SUSPENDED


# =============================================================================
# Last Left
# =============================================================================


@pytest.mark.unit
class TestOnLastLeft:
    @pytest.mark.asyncio
    async def test_last_left_stops_all_playback(self, playing_session):
        mock_stop = AsyncMock()
        with patch.object(playing_session, "_stop_playback", mock_stop):
            await playing_session.on_last_left(room_id=10, room_name="Arbeitszimmer")
        mock_stop.assert_called_once()
        session = playing_session.get_session(1)
        assert session.state == SessionState.SUSPENDED

    @pytest.mark.asyncio
    async def test_last_left_no_sessions_in_room(self, service):
        """Should not raise for empty rooms."""
        mock_stop = AsyncMock()
        with patch.object(service, "_stop_playback", mock_stop):
            await service.on_last_left(room_id=99, room_name="Empty")
        mock_stop.assert_not_called()


# =============================================================================
# Session Expiry
# =============================================================================


@pytest.mark.unit
class TestSessionExpiry:
    def test_cleanup_removes_expired(self, service):
        service.register_playback(
            user_id=1, room_id=10, room_name="Room",
            media_type=MediaType.RADIO,
        )
        session = service.get_session(1)
        session.state = SessionState.SUSPENDED
        session.suspended_at = time.time() - 700

        with patch("ha_glue.services.media_follow_service.settings") as mock_settings:
            mock_settings.media_follow_suspend_timeout = 600.0
            service._cleanup_expired_sessions()

        assert service.get_session(1) is None

    def test_cleanup_keeps_non_expired(self, service):
        service.register_playback(
            user_id=1, room_id=10, room_name="Room",
            media_type=MediaType.RADIO,
        )
        session = service.get_session(1)
        session.state = SessionState.SUSPENDED
        session.suspended_at = time.time() - 100  # Not expired

        with patch("ha_glue.services.media_follow_service.settings") as mock_settings:
            mock_settings.media_follow_suspend_timeout = 600.0
            service._cleanup_expired_sessions()

        assert service.get_session(1) is not None

    def test_cleanup_keeps_playing_sessions(self, playing_session):
        """Playing sessions should never be cleaned up."""
        with patch("ha_glue.services.media_follow_service.settings") as mock_settings:
            mock_settings.media_follow_suspend_timeout = 600.0
            playing_session._cleanup_expired_sessions()

        assert playing_session.get_session(1) is not None


# =============================================================================
# Find Playing User in Room
# =============================================================================


@pytest.mark.unit
class TestFindPlayingUserInRoom:
    def test_finds_playing_user(self, playing_session):
        assert playing_session._find_playing_user_in_room(10) == 1

    def test_returns_none_for_empty_room(self, service):
        assert service._find_playing_user_in_room(99) is None

    def test_ignores_suspended_sessions(self, playing_session):
        session = playing_session.get_session(1)
        session.state = SessionState.SUSPENDED
        assert playing_session._find_playing_user_in_room(10) is None


# =============================================================================
# MediaType Enum
# =============================================================================


@pytest.mark.unit
class TestMediaType:
    def test_string_values(self):
        assert MediaType.SINGLE_URL == "single_url"
        assert MediaType.DLNA_ALBUM == "dlna_album"
        assert MediaType.RADIO == "radio"
        assert MediaType.DLNA_VIDEO == "dlna_video"

    def test_from_string(self):
        assert MediaType("single_url") == MediaType.SINGLE_URL
        assert MediaType("radio") == MediaType.RADIO
        assert MediaType("dlna_video") == MediaType.DLNA_VIDEO


# =============================================================================
# DLNA Video Resume
# =============================================================================


@pytest.mark.unit
class TestResumeVideoPlayback:
    async def test_dlna_video_resume(self, service):
        """DLNA_VIDEO session resumed correctly via _play_video_on_dlna."""
        service.register_playback(
            user_id=1,
            room_id=10,
            room_name="Wohnzimmer",
            media_type=MediaType.DLNA_VIDEO,
            album_id="movie1",  # reused as item_id
            title="Interstellar",
            media_url="http://jellyfin/Videos/movie1/stream",
            renderer_name="Samsung TV",
        )
        session = service.get_session(1)
        session.state = SessionState.SUSPENDED
        session.suspended_at = time.time()

        mock_result = {"success": True, "message": "Playing"}
        with patch("ha_glue.services.media_follow_service.settings") as mock_settings, \
             patch("ha_glue.services.internal_tools.InternalToolService._play_video_on_dlna",
                   new_callable=AsyncMock, return_value=mock_result) as mock_play:
            mock_settings.media_follow_resume_delay = 0
            await service._resume_playback(session, 20, "Schlafzimmer")
            mock_play.assert_called_once()
            call_params = mock_play.call_args.args[0]
            assert call_params["item_id"] == "movie1"
            assert call_params["room_name"] == "Schlafzimmer"

        assert session.state == SessionState.PLAYING
        assert session.room_id == 20
        assert session.room_name == "Schlafzimmer"
