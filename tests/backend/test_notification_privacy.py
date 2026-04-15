"""
Tests for privacy-aware TTS gating (ha_should_play_tts_for_notification).

Tests cover all privacy levels, presence states, and edge cases.

After Phase 1 W2, this module moved from `services.notification_privacy`
to `ha_glue.services.notification_privacy`. The function was renamed from
`should_play_tts` to `ha_should_play_tts_for_notification` and its
signature changed: keyword-only args and the database session is opened
internally (not passed in) so the hook call site doesn't need to carry
a session across the hook boundary.
"""

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ha_glue.services.notification_privacy import ha_should_play_tts_for_notification


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_user(user_id: int, role_name: str):
    """Create a mock User with a Role."""
    role = MagicMock()
    role.name = role_name
    user = MagicMock()
    user.id = user_id
    user.role = role
    return user


def _make_presence(user_id: int, room_id: int | None = 1, room_name: str | None = "Wohnzimmer"):
    """Create a mock UserPresence."""
    p = MagicMock()
    p.user_id = user_id
    p.room_id = room_id
    p.room_name = room_name
    return p


def _mock_db_returning_users(users: list):
    """Create an AsyncSession mock that returns the given users from a select query."""
    db = AsyncMock()
    result_mock = MagicMock()
    scalars_mock = MagicMock()
    scalars_mock.all.return_value = users
    result_mock.scalars.return_value = scalars_mock
    db.execute = AsyncMock(return_value=result_mock)
    return db


def _patch_session_local(db):
    """Return a patch() for services.database.AsyncSessionLocal yielding `db`."""

    @asynccontextmanager
    async def _cm():
        yield db

    fake_sessionmaker = MagicMock(side_effect=lambda: _cm())
    return patch("services.database.AsyncSessionLocal", fake_sessionmaker, create=True)


PATCH_SETTINGS = "ha_glue.services.notification_privacy.ha_glue_settings"
PATCH_PRESENCE = "ha_glue.services.presence_service.get_presence_service"


# ---------------------------------------------------------------------------
# Public
# ---------------------------------------------------------------------------

class TestPublicPrivacy:
    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_public_always_allowed(self):
        """Public notifications always get TTS."""
        result = await ha_should_play_tts_for_notification(
            privacy="public", target_user_id=None, room_id=None
        )
        assert result is True

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_public_allowed_even_without_presence(self):
        """Public TTS works regardless of presence system state."""
        with patch(PATCH_SETTINGS) as mock_settings:
            mock_settings.presence_enabled = False
            result = await ha_should_play_tts_for_notification(
                privacy="public", target_user_id=None, room_id=None
            )
            assert result is True


# ---------------------------------------------------------------------------
# Confidential
# ---------------------------------------------------------------------------

class TestConfidentialPrivacy:
    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_confidential_user_alone(self):
        """Confidential TTS plays when target user is alone in their room."""
        presence = MagicMock()
        presence.is_user_alone_in_room.return_value = True

        with patch(PATCH_SETTINGS) as mock_settings, \
             patch(PATCH_PRESENCE, return_value=presence):
            mock_settings.presence_enabled = True
            result = await ha_should_play_tts_for_notification(
                privacy="confidential", target_user_id=1, room_id=10
            )
            assert result is True
            presence.is_user_alone_in_room.assert_called_once_with(1)

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_confidential_user_not_alone(self):
        """Confidential TTS suppressed when others are in the room."""
        presence = MagicMock()
        presence.is_user_alone_in_room.return_value = False

        with patch(PATCH_SETTINGS) as mock_settings, \
             patch(PATCH_PRESENCE, return_value=presence):
            mock_settings.presence_enabled = True
            result = await ha_should_play_tts_for_notification(
                privacy="confidential", target_user_id=1, room_id=10
            )
            assert result is False

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_confidential_user_not_tracked(self):
        """Confidential TTS suppressed when user is not tracked (conservative)."""
        presence = MagicMock()
        presence.is_user_alone_in_room.return_value = None  # not tracked

        with patch(PATCH_SETTINGS) as mock_settings, \
             patch(PATCH_PRESENCE, return_value=presence):
            mock_settings.presence_enabled = True
            result = await ha_should_play_tts_for_notification(
                privacy="confidential", target_user_id=1, room_id=10
            )
            assert result is False

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_confidential_no_target_user(self):
        """Confidential TTS suppressed when no target_user_id specified."""
        with patch(PATCH_SETTINGS) as mock_settings, \
             patch(PATCH_PRESENCE):
            mock_settings.presence_enabled = True
            result = await ha_should_play_tts_for_notification(
                privacy="confidential", target_user_id=None, room_id=10
            )
            assert result is False

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_confidential_user_wrong_room(self):
        """Confidential: user not alone in their room — TTS suppressed."""
        presence = MagicMock()
        presence.is_user_alone_in_room.return_value = False

        with patch(PATCH_SETTINGS) as mock_settings, \
             patch(PATCH_PRESENCE, return_value=presence):
            mock_settings.presence_enabled = True
            result = await ha_should_play_tts_for_notification(
                privacy="confidential", target_user_id=1, room_id=10
            )
            assert result is False


# ---------------------------------------------------------------------------
# Personal
# ---------------------------------------------------------------------------

class TestPersonalPrivacy:
    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_personal_all_household(self):
        """Personal TTS plays when all room occupants are household members."""
        presence = MagicMock()
        presence.get_room_occupants.return_value = [
            _make_presence(1), _make_presence(2),
        ]

        users = [_make_user(1, "Admin"), _make_user(2, "Familie")]
        db = _mock_db_returning_users(users)

        with patch(PATCH_SETTINGS) as mock_settings, \
             patch(PATCH_PRESENCE, return_value=presence), \
             _patch_session_local(db):
            mock_settings.presence_enabled = True
            mock_settings.presence_household_roles = "Admin,Familie"
            result = await ha_should_play_tts_for_notification(
                privacy="personal", target_user_id=None, room_id=10
            )
            assert result is True

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_personal_non_household_occupant(self):
        """Personal TTS suppressed when a non-household member is in the room."""
        presence = MagicMock()
        presence.get_room_occupants.return_value = [
            _make_presence(1), _make_presence(3),
        ]

        users = [_make_user(1, "Familie"), _make_user(3, "Gast")]
        db = _mock_db_returning_users(users)

        with patch(PATCH_SETTINGS) as mock_settings, \
             patch(PATCH_PRESENCE, return_value=presence), \
             _patch_session_local(db):
            mock_settings.presence_enabled = True
            mock_settings.presence_household_roles = "Admin,Familie"
            result = await ha_should_play_tts_for_notification(
                privacy="personal", target_user_id=None, room_id=10
            )
            assert result is False

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_personal_no_occupants(self):
        """Personal TTS suppressed when room has no occupants (conservative)."""
        presence = MagicMock()
        presence.get_room_occupants.return_value = []

        with patch(PATCH_SETTINGS) as mock_settings, \
             patch(PATCH_PRESENCE, return_value=presence):
            mock_settings.presence_enabled = True
            result = await ha_should_play_tts_for_notification(
                privacy="personal", target_user_id=None, room_id=10
            )
            assert result is False

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_personal_no_room(self):
        """Personal TTS suppressed when no room_id is provided."""
        with patch(PATCH_SETTINGS) as mock_settings, \
             patch(PATCH_PRESENCE):
            mock_settings.presence_enabled = True
            result = await ha_should_play_tts_for_notification(
                privacy="personal", target_user_id=None, room_id=None
            )
            assert result is False


# ---------------------------------------------------------------------------
# Presence Disabled
# ---------------------------------------------------------------------------

class TestPresenceDisabled:
    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_presence_disabled_blocks_nonpublic(self):
        """When presence is disabled, non-public notifications don't get TTS."""
        with patch(PATCH_SETTINGS) as mock_settings:
            mock_settings.presence_enabled = False
            assert await ha_should_play_tts_for_notification(
                privacy="personal", target_user_id=None, room_id=10
            ) is False
            assert await ha_should_play_tts_for_notification(
                privacy="confidential", target_user_id=1, room_id=10
            ) is False


# ---------------------------------------------------------------------------
# Unknown Privacy Level
# ---------------------------------------------------------------------------

class TestUnknownPrivacy:
    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_unknown_privacy_level_denied(self):
        """Unknown privacy levels are denied (fail-safe)."""
        with patch(PATCH_SETTINGS) as mock_settings, \
             patch(PATCH_PRESENCE):
            mock_settings.presence_enabled = True
            result = await ha_should_play_tts_for_notification(
                privacy="secret", target_user_id=None, room_id=10
            )
            assert result is False
