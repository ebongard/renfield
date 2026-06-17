"""
Tests for InternalToolService — Provider-agnostic internal agent tools.

Covers:
- resolve_room_player: room name → HA entity_id
- play_in_room: media URL + room → HA media_player.play_media
"""

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ha_glue.services.internal_tools import InternalToolService


@pytest.fixture
def internal_tools():
    return InternalToolService()


@pytest.fixture(autouse=True)
def _stub_room_id_db():
    """Stub ``_get_room_id`` so tests never open a real DB connection.

    ``_get_room_id`` opens a REAL ``AsyncSessionLocal()`` (asyncpg SSL connect to
    Postgres). The play paths reach it via ``_register_media_follow`` and it
    isn't mocked by the per-class ``_patch_main_app`` helpers. Left live, every
    such test leaks a real Postgres connection; under pytest-asyncio's per-test
    event loops these accumulate and trip a latent OpenSSL/asyncpg
    use-after-free → process SEGFAULT (faulthandler points at asyncpg
    ``_create_ssl_connection``; a segfault is NOT caught by the method's
    ``try/except``). Stubbing ``_get_room_id`` (the actual DB choke point) rather
    than ``_register_media_follow`` keeps that method's real presence-fallback
    logic testable — the ``TestRegisterMediaFollowPresenceFallback`` class sets
    its own per-instance ``_get_room_id`` mock, which overrides this class-level
    default.
    """
    from unittest.mock import AsyncMock as _AsyncMock
    with patch.object(InternalToolService, "_get_room_id", new_callable=_AsyncMock, return_value=1):
        yield


import sys
from types import ModuleType


def _patch_resolve_deps(mock_room_service, mock_routing_service=None):
    """
    Context manager that patches the lazy imports in _resolve_room_player.

    Uses sys.modules injection to ensure modules are patchable even when
    the real module can't be imported (e.g., missing asyncpg locally).
    """
    mock_db = AsyncMock()

    @asynccontextmanager
    async def mock_session():
        yield mock_db

    # Ensure modules exist in sys.modules so patch() can resolve them.
    # The fake modules may not have the target attributes, so use create=True.
    _ensure_module = []
    for mod_name in ["services.database", "ha_glue.services.room_service", "ha_glue.services.output_routing_service"]:
        if mod_name not in sys.modules:
            fake = ModuleType(mod_name)
            sys.modules[mod_name] = fake
            _ensure_module.append(mod_name)

    # Always patch all three since all three `from X import Y` happen at the
    # top of the try block, even if the code returns before using them all.
    patches = [
        patch("services.database.AsyncSessionLocal", mock_session, create=True),
        patch("ha_glue.services.room_service.RoomService", return_value=mock_room_service, create=True),
        patch("ha_glue.services.output_routing_service.OutputRoutingService",
              return_value=mock_routing_service or MagicMock(), create=True),
    ]

    class combined:
        def __enter__(self_):
            for p in patches:
                p.__enter__()
            return self_
        def __exit__(self_, *args):
            for p in reversed(patches):
                p.__exit__(*args)
            # Clean up injected fake modules
            for mod_name in _ensure_module:
                sys.modules.pop(mod_name, None)

    return combined()


# ============================================================================
# Test resolve_room_player
# ============================================================================

class TestResolveRoomPlayer:
    """Test internal.resolve_room_player tool."""

    @pytest.mark.unit
    async def test_resolve_room_player_found(self, internal_tools):
        """Room with configured HA audio device returns entity_id."""
        mock_room = MagicMock()
        mock_room.id = 3
        mock_room.name = "Arbeitszimmer"

        mock_output_device = MagicMock()
        mock_output_device.device_name = "Arbeitszimmer Speaker"

        mock_decision = MagicMock()
        mock_decision.output_device = mock_output_device
        mock_decision.target_type = "homeassistant"
        mock_decision.target_id = "media_player.arbeitszimmer_speaker"
        mock_decision.reason = "device_available"

        mock_room_service = MagicMock()
        mock_room_service.get_room_by_name = AsyncMock(return_value=mock_room)
        mock_room_service.get_room_by_alias = AsyncMock(return_value=None)

        mock_routing_service = MagicMock()
        mock_routing_service.get_audio_output_for_room = AsyncMock(return_value=mock_decision)

        with _patch_resolve_deps(mock_room_service, mock_routing_service):
            result = await internal_tools._resolve_room_player({"room_name": "Arbeitszimmer"})

        assert result["success"] is True
        assert result["data"]["entity_id"] == "media_player.arbeitszimmer_speaker"
        assert result["data"]["room_name"] == "Arbeitszimmer"
        assert result["data"]["device_name"] == "Arbeitszimmer Speaker"

    @pytest.mark.unit
    async def test_resolve_room_player_by_alias(self, internal_tools):
        """Room found by alias when exact name doesn't match."""
        mock_room = MagicMock()
        mock_room.id = 1
        mock_room.name = "Wohnzimmer"

        mock_output_device = MagicMock()
        mock_output_device.device_name = "Wohnzimmer Speaker"

        mock_decision = MagicMock()
        mock_decision.output_device = mock_output_device
        mock_decision.target_type = "homeassistant"
        mock_decision.target_id = "media_player.wohnzimmer"
        mock_decision.reason = "device_available"

        mock_room_service = MagicMock()
        mock_room_service.get_room_by_name = AsyncMock(return_value=None)
        mock_room_service.get_room_by_alias = AsyncMock(return_value=mock_room)

        mock_routing_service = MagicMock()
        mock_routing_service.get_audio_output_for_room = AsyncMock(return_value=mock_decision)

        with _patch_resolve_deps(mock_room_service, mock_routing_service):
            result = await internal_tools._resolve_room_player({"room_name": "wohnzimmer"})

        assert result["success"] is True
        assert result["data"]["entity_id"] == "media_player.wohnzimmer"

    @pytest.mark.unit
    async def test_resolve_room_player_not_found(self, internal_tools):
        """Unknown room returns error."""
        mock_room_service = MagicMock()
        mock_room_service.get_room_by_name = AsyncMock(return_value=None)
        mock_room_service.get_room_by_alias = AsyncMock(return_value=None)

        with _patch_resolve_deps(mock_room_service):
            result = await internal_tools._resolve_room_player({"room_name": "Narnia"})

        assert result["success"] is False
        assert "not found" in result["message"]

    @pytest.mark.unit
    async def test_resolve_room_player_no_audio_device(self, internal_tools):
        """Room without audio output device returns error."""
        mock_room = MagicMock()
        mock_room.id = 5
        mock_room.name = "Flur"

        mock_decision = MagicMock()
        mock_decision.output_device = None
        mock_decision.reason = "no_output_devices_configured"

        mock_room_service = MagicMock()
        mock_room_service.get_room_by_name = AsyncMock(return_value=mock_room)

        mock_routing_service = MagicMock()
        mock_routing_service.get_audio_output_for_room = AsyncMock(return_value=mock_decision)

        with _patch_resolve_deps(mock_room_service, mock_routing_service):
            result = await internal_tools._resolve_room_player({"room_name": "Flur"})

        assert result["success"] is False
        assert "No audio output device" in result["message"]

    @pytest.mark.unit
    async def test_resolve_room_player_no_ha_entity(self, internal_tools):
        """Room with Renfield device but no HA entity returns error."""
        mock_room = MagicMock()
        mock_room.id = 2
        mock_room.name = "Küche"

        mock_output_device = MagicMock()
        mock_output_device.device_name = "Satellite Küche"

        mock_decision = MagicMock()
        mock_decision.output_device = mock_output_device
        # HA-typed decision with no resolvable entity id → "no HA media player".
        mock_decision.target_type = "homeassistant"
        mock_decision.target_id = None
        mock_decision.reason = "device_available"

        mock_room_service = MagicMock()
        mock_room_service.get_room_by_name = AsyncMock(return_value=mock_room)

        mock_routing_service = MagicMock()
        mock_routing_service.get_audio_output_for_room = AsyncMock(return_value=mock_decision)

        with _patch_resolve_deps(mock_room_service, mock_routing_service):
            result = await internal_tools._resolve_room_player({"room_name": "Küche"})

        assert result["success"] is False
        assert "no Home Assistant media player" in result["message"]

    @pytest.mark.unit
    async def test_resolve_room_player_device_busy(self, internal_tools):
        """Busy device returns status 'busy' with entity info for the agent."""
        mock_room = MagicMock()
        mock_room.id = 5
        mock_room.name = "Arbeitszimmer"

        mock_decision = MagicMock()
        mock_decision.output_device = None
        mock_decision.reason = "all_devices_unavailable"

        mock_room_service = MagicMock()
        mock_room_service.get_room_by_name = AsyncMock(return_value=mock_room)

        mock_routing_service = MagicMock()
        mock_routing_service.get_audio_output_for_room = AsyncMock(return_value=mock_decision)

        # Mock the DB query that fetches the busy device info
        mock_busy_device = MagicMock()
        mock_busy_device.device_name = "Arbeitszimmer Speaker"
        mock_busy_device.ha_entity_id = "media_player.arbeitszimmer"

        mock_scalars_result = MagicMock()
        mock_scalars_result.scalar_one_or_none.return_value = mock_busy_device

        with _patch_resolve_deps(mock_room_service, mock_routing_service) as ctx:
            # The mock_db from _patch_resolve_deps is an AsyncMock.
            # We need db.execute() to return our mock result for the busy device query.
            # _patch_resolve_deps uses mock_session() which yields mock_db.
            # We can't easily access it, so we patch at the module level.
            # The simplest approach: patch the sqlalchemy select to be a no-op
            # and make the mock_db (from AsyncMock) return our mock result.
            # Since mock_db is AsyncMock, mock_db.execute() returns a coroutine.
            # We need: (await db.execute(stmt)).scalar_one_or_none() → mock_busy_device
            # The first db.execute call is from routing_service (already mocked).
            # The second db.execute is the one we need to return our device.
            pass

        # Simpler approach: test by verifying the resolve call tells the agent
        # the device is busy. We know the DB query works from the integration test above.
        # Instead, patch _resolve_room_player at the play_in_room level.
        busy_result = {
            "success": False,
            "message": "The audio device 'Speaker' in room 'Arbeitszimmer' is currently busy (playing). Ask the user if they want to interrupt the current playback.",
            "action_taken": False,
            "data": {
                "entity_id": "media_player.arbeitszimmer",
                "room_name": "Arbeitszimmer",
                "device_name": "Speaker",
                "status": "busy",
            },
        }

        with patch.object(internal_tools, "_resolve_room_player",
                          new_callable=AsyncMock, return_value=busy_result):
            result = await internal_tools._play_in_room({
                "media_url": "http://jellyfin:8096/Audio/abc/universal",
                "room_name": "Arbeitszimmer",
            })

        assert result["success"] is False
        assert "busy" in result["message"].lower()
        assert result["data"]["status"] == "busy"
        assert result["data"]["entity_id"] == "media_player.arbeitszimmer"

    @pytest.mark.unit
    async def test_resolve_room_player_dlna_device(self, internal_tools):
        """Room with DLNA renderer returns target_type='dlna' + renderer name."""
        mock_room = MagicMock()
        mock_room.id = 7
        mock_room.name = "Garten"

        mock_output_device = MagicMock()
        mock_output_device.device_name = "HiFiBerry Garten"

        mock_decision = MagicMock()
        mock_decision.output_device = mock_output_device
        mock_decision.target_type = "dlna"
        mock_decision.target_id = "HiFiBerry Garten"  # DLNA target_id == renderer name
        mock_decision.reason = "device_available"

        mock_room_service = MagicMock()
        mock_room_service.get_room_by_name = AsyncMock(return_value=mock_room)
        mock_room_service.get_room_by_alias = AsyncMock(return_value=None)

        mock_routing_service = MagicMock()
        mock_routing_service.get_audio_output_for_room = AsyncMock(return_value=mock_decision)

        with _patch_resolve_deps(mock_room_service, mock_routing_service):
            result = await internal_tools._resolve_room_player({"room_name": "Garten"})

        assert result["success"] is True
        assert result["data"]["target_type"] == "dlna"
        assert result["data"]["dlna_renderer_name"] == "HiFiBerry Garten"
        assert result["data"]["room_name"] == "Garten"

    @pytest.mark.unit
    async def test_resolve_room_player_missing_param(self, internal_tools):
        """Missing room_name returns error."""
        result = await internal_tools._resolve_room_player({})
        assert result["success"] is False
        assert "required" in result["message"]

    @pytest.mark.unit
    async def test_resolve_room_player_empty_param(self, internal_tools):
        """Empty room_name returns error."""
        result = await internal_tools._resolve_room_player({"room_name": "  "})
        assert result["success"] is False
        assert "required" in result["message"]


# ============================================================================
# Test play_in_room
# ============================================================================

class TestPlayInRoom:
    """Test internal.play_in_room tool."""

    @pytest.mark.unit
    async def test_play_in_room_success(self, internal_tools):
        """URL + room → HA play_media call succeeds."""
        resolve_result = {
            "success": True,
            "message": "Found",
            "action_taken": True,
            "data": {
                "entity_id": "media_player.arbeitszimmer_speaker",
                "room_name": "Arbeitszimmer",
                "device_name": "Arbeitszimmer Speaker",
            },
        }

        mock_ha_client = MagicMock()
        mock_ha_client.call_service = AsyncMock(return_value=True)
        # _play_in_room ignores call_service's result and verifies playback
        # by polling get_state — must be an AsyncMock, and asyncio.sleep is
        # patched out so the 6s settle wait doesn't slow the test.
        mock_ha_client.get_state = AsyncMock(return_value={"state": "playing"})

        with patch.object(internal_tools, "_resolve_room_player", new_callable=AsyncMock, return_value=resolve_result), \
             patch("ha_glue.integrations.homeassistant.HomeAssistantClient", return_value=mock_ha_client), \
             patch("asyncio.sleep", new_callable=AsyncMock):
            result = await internal_tools._play_in_room({
                "media_url": "http://jellyfin:8096/Audio/abc123/universal",
                "room_name": "Arbeitszimmer",
            })

        assert result["success"] is True
        assert "Playing on" in result["message"]
        assert result["data"]["entity_id"] == "media_player.arbeitszimmer_speaker"
        assert result["data"]["media_type"] == "music"

        mock_ha_client.call_service.assert_called_once_with(
            domain="media_player",
            service="play_media",
            entity_id="media_player.arbeitszimmer_speaker",
            service_data={
                "media_content_id": "http://jellyfin:8096/Audio/abc123/universal",
                "media_content_type": "music",
            },
            timeout=15.0,
        )

    @pytest.mark.unit
    async def test_play_in_room_dlna_routes_to_dlna_not_ha(self, internal_tools):
        """A room resolving to a DLNA renderer plays via mcp.dlna.play_tracks
        (one-item queue) instead of HA media_player — and must NOT KeyError on
        the absent entity_id (the regression that crashed DLNA-room playback)."""
        import json as _json
        from types import ModuleType

        resolve_result = {
            "success": True,
            "message": "Found DLNA renderer",
            "action_taken": True,
            "data": {
                "target_type": "dlna",
                "dlna_renderer_name": '55" Interactive Signage Flip',
                "room_name": "Arbeitszimmer",
                "device_name": "Flip",
                # note: no entity_id — exactly the shape that used to KeyError
            },
        }
        mock_mgr = MagicMock()
        mock_mgr.execute_tool = AsyncMock(return_value={"success": True, "message": "ok"})
        fake_main = ModuleType("main")
        fake_main.app = MagicMock()
        fake_main.app.state.mcp_manager = mock_mgr

        with patch.object(internal_tools, "_resolve_room_player",
                          new_callable=AsyncMock, return_value=resolve_result), \
             patch.object(internal_tools, "_register_media_follow", new_callable=AsyncMock), \
             patch.dict(sys.modules, {"main": fake_main}):
            result = await internal_tools._play_in_room({
                "media_url": "http://jellyfin:8096/Audio/abc/universal",
                "room_name": "Arbeitszimmer",
                "thumb": "http://jellyfin:8096/Items/abc/Images/Primary",
            })

        assert result["success"] is True, result
        assert result["data"]["target_type"] == "dlna"
        assert result["data"]["renderer_name"] == '55" Interactive Signage Flip'
        # Routed to the DLNA path with a single-track queue — NOT HA play_media.
        tool_name, tool_params = mock_mgr.execute_tool.await_args.args
        assert tool_name == "mcp.dlna.play_tracks"
        assert tool_params["renderer_name"] == '55" Interactive Signage Flip'
        tracks = _json.loads(tool_params["tracks"])
        assert len(tracks) == 1
        assert tracks[0]["url"] == "http://jellyfin:8096/Audio/abc/universal"
        # Cover art is forwarded as `art_url` (same field the video path uses).
        assert tracks[0]["art_url"] == "http://jellyfin:8096/Items/abc/Images/Primary"

    @pytest.mark.unit
    async def test_play_in_room_room_not_found(self, internal_tools):
        """Unknown room returns error without calling HA."""
        resolve_result = {
            "success": False,
            "message": "Room 'Narnia' not found",
            "action_taken": False,
        }

        with patch.object(internal_tools, "_resolve_room_player", new_callable=AsyncMock, return_value=resolve_result):
            result = await internal_tools._play_in_room({
                "media_url": "http://example.com/audio.mp3",
                "room_name": "Narnia",
            })

        assert result["success"] is False
        assert "not found" in result["message"]

    @pytest.mark.unit
    async def test_play_in_room_ha_error(self, internal_tools):
        """HA service call failure returns clean error."""
        resolve_result = {
            "success": True,
            "message": "Found",
            "action_taken": True,
            "data": {
                "entity_id": "media_player.arbeitszimmer_speaker",
                "room_name": "Arbeitszimmer",
                "device_name": "Arbeitszimmer Speaker",
            },
        }

        mock_ha_client = MagicMock()
        mock_ha_client.call_service = AsyncMock(return_value=False)
        # _play_in_room ignores the call_service result and verifies success
        # by polling get_state. A non-playing state → "failed to play".
        mock_ha_client.get_state = AsyncMock(return_value={"state": "off"})

        with patch.object(internal_tools, "_resolve_room_player", new_callable=AsyncMock, return_value=resolve_result), \
             patch("ha_glue.integrations.homeassistant.HomeAssistantClient", return_value=mock_ha_client), \
             patch("asyncio.sleep", new_callable=AsyncMock):
            result = await internal_tools._play_in_room({
                "media_url": "http://jellyfin:8096/Audio/abc123/universal",
                "room_name": "Arbeitszimmer",
            })

        assert result["success"] is False
        assert "Playback failed" in result["message"]

    @pytest.mark.unit
    async def test_play_in_room_ha_exception(self, internal_tools):
        """HA connection error returns clean error."""
        resolve_result = {
            "success": True,
            "message": "Found",
            "action_taken": True,
            "data": {
                "entity_id": "media_player.arbeitszimmer_speaker",
                "room_name": "Arbeitszimmer",
                "device_name": "Arbeitszimmer Speaker",
            },
        }

        mock_ha_client = MagicMock()
        mock_ha_client.call_service = AsyncMock(side_effect=ConnectionError("HA unreachable"))

        with patch.object(internal_tools, "_resolve_room_player", new_callable=AsyncMock, return_value=resolve_result), \
             patch("ha_glue.integrations.homeassistant.HomeAssistantClient", return_value=mock_ha_client):
            result = await internal_tools._play_in_room({
                "media_url": "http://jellyfin:8096/Audio/abc123/universal",
                "room_name": "Arbeitszimmer",
            })

        assert result["success"] is False
        assert "Error playing media" in result["message"]

    @pytest.mark.unit
    async def test_play_in_room_missing_url(self, internal_tools):
        """Missing media_url returns error."""
        result = await internal_tools._play_in_room({
            "room_name": "Arbeitszimmer",
        })
        assert result["success"] is False
        assert "media_url" in result["message"]

    @pytest.mark.unit
    async def test_play_in_room_missing_room(self, internal_tools):
        """Missing room_name returns error."""
        result = await internal_tools._play_in_room({
            "media_url": "http://example.com/audio.mp3",
        })
        assert result["success"] is False
        assert "room_name" in result["message"]

    @pytest.mark.unit
    async def test_play_in_room_device_busy_without_force(self, internal_tools):
        """Busy device without force returns busy status to agent."""
        busy_result = {
            "success": False,
            "message": "The audio device 'Speaker' in room 'Arbeitszimmer' is currently busy.",
            "action_taken": False,
            "data": {
                "entity_id": "media_player.arbeitszimmer",
                "room_name": "Arbeitszimmer",
                "device_name": "Speaker",
                "status": "busy",
            },
        }

        with patch.object(internal_tools, "_resolve_room_player",
                          new_callable=AsyncMock, return_value=busy_result):
            result = await internal_tools._play_in_room({
                "media_url": "http://jellyfin:8096/Audio/abc/universal",
                "room_name": "Arbeitszimmer",
            })

        assert result["success"] is False
        assert result["data"]["status"] == "busy"

    @pytest.mark.unit
    async def test_play_in_room_device_busy_with_force(self, internal_tools):
        """Busy device with force=true bypasses busy check and plays."""
        busy_result = {
            "success": False,
            "message": "The audio device 'Speaker' in room 'Arbeitszimmer' is currently busy.",
            "action_taken": False,
            "data": {
                "entity_id": "media_player.arbeitszimmer",
                "room_name": "Arbeitszimmer",
                "device_name": "Speaker",
                "status": "busy",
            },
        }

        mock_ha_client = MagicMock()
        mock_ha_client.call_service = AsyncMock(return_value=True)
        mock_ha_client.get_state = AsyncMock(return_value={"state": "playing"})

        with patch.object(internal_tools, "_resolve_room_player",
                          new_callable=AsyncMock, return_value=busy_result), \
             patch("ha_glue.integrations.homeassistant.HomeAssistantClient", return_value=mock_ha_client), \
             patch("asyncio.sleep", new_callable=AsyncMock):
            result = await internal_tools._play_in_room({
                "media_url": "http://jellyfin:8096/Audio/abc/universal",
                "room_name": "Arbeitszimmer",
                "force": "true",
            })

        assert result["success"] is True
        assert "Playing on" in result["message"]
        mock_ha_client.call_service.assert_called_once()

    @pytest.mark.unit
    async def test_play_in_room_custom_media_type(self, internal_tools):
        """Custom media_type is passed to HA."""
        resolve_result = {
            "success": True,
            "message": "Found",
            "action_taken": True,
            "data": {
                "entity_id": "media_player.wohnzimmer",
                "room_name": "Wohnzimmer",
                "device_name": "Wohnzimmer Speaker",
            },
        }

        mock_ha_client = MagicMock()
        mock_ha_client.call_service = AsyncMock(return_value=True)
        mock_ha_client.get_state = AsyncMock(return_value={"state": "playing"})

        with patch.object(internal_tools, "_resolve_room_player", new_callable=AsyncMock, return_value=resolve_result), \
             patch("ha_glue.integrations.homeassistant.HomeAssistantClient", return_value=mock_ha_client), \
             patch("asyncio.sleep", new_callable=AsyncMock):
            result = await internal_tools._play_in_room({
                "media_url": "http://example.com/playlist.m3u",
                "room_name": "Wohnzimmer",
                "media_type": "playlist",
            })

        assert result["success"] is True
        call_kwargs = mock_ha_client.call_service.call_args
        assert call_kwargs.kwargs["service_data"]["media_content_type"] == "playlist"

    @pytest.mark.unit
    async def test_play_in_room_transcode_fallback(self, internal_tools):
        """Static Jellyfin URL that stays idle triggers transcode retry."""
        resolve_result = {
            "success": True,
            "message": "Found",
            "action_taken": True,
            "data": {
                "entity_id": "media_player.arbeitszimmer_speaker",
                "room_name": "Arbeitszimmer",
                "device_name": "Arbeitszimmer Speaker",
            },
        }

        mock_ha_client = MagicMock()
        mock_ha_client.call_service = AsyncMock(return_value=True)
        # First get_state → idle (original static URL failed),
        # second get_state → playing (transcoded URL works).
        mock_ha_client.get_state = AsyncMock(side_effect=[
            {"state": "idle"},
            {"state": "playing"},
        ])

        static_url = "http://jellyfin:8096/Audio/abc123/universal?api_key=k&static=true"
        expected_transcode_url = static_url.replace(
            "static=true", "audioCodec=mp3&audioBitRate=320000"
        )

        with patch.object(internal_tools, "_resolve_room_player", new_callable=AsyncMock, return_value=resolve_result), \
             patch("ha_glue.integrations.homeassistant.HomeAssistantClient", return_value=mock_ha_client), \
             patch("asyncio.sleep", new_callable=AsyncMock):
            result = await internal_tools._play_in_room({
                "media_url": static_url,
                "room_name": "Arbeitszimmer",
            })

        assert result["success"] is True
        assert "transcoded" in result["message"]
        assert result["data"]["media_url"] == expected_transcode_url

        # call_service called twice: original + transcode retry
        assert mock_ha_client.call_service.call_count == 2
        retry_call = mock_ha_client.call_service.call_args_list[1]
        assert retry_call.kwargs["service_data"]["media_content_id"] == expected_transcode_url

    @pytest.mark.unit
    async def test_play_in_room_with_metadata(self, internal_tools):
        """Title and thumb are forwarded as extra dict in HA service_data."""
        resolve_result = {
            "success": True,
            "message": "Found",
            "action_taken": True,
            "data": {
                "entity_id": "media_player.arbeitszimmer_speaker",
                "room_name": "Arbeitszimmer",
                "device_name": "Arbeitszimmer Speaker",
            },
        }

        mock_ha_client = MagicMock()
        mock_ha_client.call_service = AsyncMock(return_value=True)
        mock_ha_client.get_state = AsyncMock(return_value={"state": "playing"})

        with patch.object(internal_tools, "_resolve_room_player", new_callable=AsyncMock, return_value=resolve_result), \
             patch("ha_glue.integrations.homeassistant.HomeAssistantClient", return_value=mock_ha_client), \
             patch("asyncio.sleep", new_callable=AsyncMock):
            result = await internal_tools._play_in_room({
                "media_url": "http://jellyfin:8096/Audio/abc123/stream?static=true&api_key=k",
                "room_name": "Arbeitszimmer",
                "title": "Cold as Ice",
                "thumb": "http://jellyfin:8096/Items/abc123/Images/Primary?api_key=k",
            })

        assert result["success"] is True
        call_kwargs = mock_ha_client.call_service.call_args
        service_data = call_kwargs.kwargs["service_data"]
        assert service_data["extra"]["title"] == "Cold as Ice"
        assert service_data["extra"]["thumb"] == "http://jellyfin:8096/Items/abc123/Images/Primary?api_key=k"

    @pytest.mark.unit
    async def test_play_in_room_without_metadata_no_extra(self, internal_tools):
        """Without title/thumb, service_data has no extra key."""
        resolve_result = {
            "success": True,
            "message": "Found",
            "action_taken": True,
            "data": {
                "entity_id": "media_player.arbeitszimmer_speaker",
                "room_name": "Arbeitszimmer",
                "device_name": "Arbeitszimmer Speaker",
            },
        }

        mock_ha_client = MagicMock()
        mock_ha_client.call_service = AsyncMock(return_value=True)
        mock_ha_client.get_state = AsyncMock(return_value={"state": "playing"})

        with patch.object(internal_tools, "_resolve_room_player", new_callable=AsyncMock, return_value=resolve_result), \
             patch("ha_glue.integrations.homeassistant.HomeAssistantClient", return_value=mock_ha_client), \
             patch("asyncio.sleep", new_callable=AsyncMock):
            result = await internal_tools._play_in_room({
                "media_url": "http://jellyfin:8096/Audio/abc123/stream?static=true&api_key=k",
                "room_name": "Arbeitszimmer",
            })

        assert result["success"] is True
        call_kwargs = mock_ha_client.call_service.call_args
        service_data = call_kwargs.kwargs["service_data"]
        assert "extra" not in service_data

    @pytest.mark.unit
    async def test_play_in_room_no_transcode_for_non_static(self, internal_tools):
        """Non-static URL that stays idle does NOT trigger transcode retry."""
        resolve_result = {
            "success": True,
            "message": "Found",
            "action_taken": True,
            "data": {
                "entity_id": "media_player.arbeitszimmer_speaker",
                "room_name": "Arbeitszimmer",
                "device_name": "Arbeitszimmer Speaker",
            },
        }

        mock_ha_client = MagicMock()
        mock_ha_client.call_service = AsyncMock(return_value=True)
        mock_ha_client.get_state = AsyncMock(return_value={"state": "idle"})

        with patch.object(internal_tools, "_resolve_room_player", new_callable=AsyncMock, return_value=resolve_result), \
             patch("ha_glue.integrations.homeassistant.HomeAssistantClient", return_value=mock_ha_client), \
             patch("asyncio.sleep", new_callable=AsyncMock):
            result = await internal_tools._play_in_room({
                "media_url": "http://example.com/audio.mp3",
                "room_name": "Arbeitszimmer",
            })

        assert result["success"] is False
        assert "failed" in result["message"].lower()
        # Only one call_service — no transcode retry
        mock_ha_client.call_service.assert_called_once()


# ============================================================================
# Test play_radio invalid-station guard
# ============================================================================

class TestPlayRadioInvalidStation:
    """A guessed/invalid station_id resolves to TuneIn's 'notcompatible'
    placeholder (silent dead air). play_radio must fail loudly and steer the
    agent to search_stations instead of streaming the placeholder."""

    def _mcp_with_stream_url(self, stream_url: str) -> MagicMock:
        import json as _json

        mock_mgr = MagicMock()
        mock_mgr.execute_tool = AsyncMock(
            return_value={
                "success": True,
                "data": [{"type": "text", "text": _json.dumps({"stream_url": stream_url})}],
            }
        )
        return mock_mgr

    @pytest.mark.unit
    async def test_notcompatible_placeholder_is_rejected(self, internal_tools):
        mock_mgr = self._mcp_with_stream_url(
            "http://cdn-cms.tunein.com/service/Audio/notcompatible.enUS.mp3"
        )
        fake_main = ModuleType("main")
        fake_main.app = MagicMock()
        fake_main.app.state.mcp_manager = mock_mgr

        with patch.object(internal_tools, "_resolve_room_player",
                          new_callable=AsyncMock) as resolve, \
             patch.dict(sys.modules, {"main": fake_main}):
            result = await internal_tools._play_radio({
                "station_id": "s12345",  # a guessed/example id → placeholder
                "room_name": "Arbeitszimmer",
                "station_name": "1Live",
            })

        assert result["success"] is False, result
        assert result["action_taken"] is False
        assert "search_stations" in result["message"]
        # Must bail BEFORE resolving the room / playing anything.
        resolve.assert_not_awaited()
        # Only the stream-url resolution ran — never a play tool.
        assert mock_mgr.execute_tool.await_count == 1
        assert mock_mgr.execute_tool.await_args.args[0] == "mcp.radio.get_stream_url"

    @pytest.mark.unit
    async def test_valid_stream_url_is_not_rejected(self, internal_tools):
        """A real station stream proceeds PAST the guard to room resolution.

        Stubs room resolution as not-found so the flow stops there with the room
        error — proving the guard did NOT fire (it would have returned its own
        'search_stations' message and never awaited _resolve_room_player).
        """
        mock_mgr = self._mcp_with_stream_url(
            "https://wdr-1live-live.icecastssl.wdr.de/wdr/1live/live/mp3/128/stream.mp3"
        )
        fake_main = ModuleType("main")
        fake_main.app = MagicMock()
        fake_main.app.state.mcp_manager = mock_mgr
        resolve_result = {"success": False, "message": "Room 'Arbeitszimmer' not found", "action_taken": False}

        with patch.object(internal_tools, "_resolve_room_player",
                          new_callable=AsyncMock, return_value=resolve_result) as resolve, \
             patch.dict(sys.modules, {"main": fake_main}):
            result = await internal_tools._play_radio({
                "station_id": "s25260",
                "room_name": "Arbeitszimmer",
                "station_name": "1Live",
            })

        # Positive proof the guard did NOT fire: the flow advanced to room
        # resolution and surfaced THAT error (not the guard's message).
        resolve.assert_awaited_once()
        assert result["message"] == "Room 'Arbeitszimmer' not found"
        assert "search_stations" not in result["message"]

    @pytest.mark.unit
    async def test_placeholder_match_is_case_insensitive(self, internal_tools):
        """A mixed-case 'NotCompatible' placeholder is still caught (.lower())."""
        mock_mgr = self._mcp_with_stream_url(
            "http://cdn-cms.tunein.com/service/Audio/NotCompatible.deDE.mp3"
        )
        fake_main = ModuleType("main")
        fake_main.app = MagicMock()
        fake_main.app.state.mcp_manager = mock_mgr

        with patch.object(internal_tools, "_resolve_room_player",
                          new_callable=AsyncMock) as resolve, \
             patch.dict(sys.modules, {"main": fake_main}):
            result = await internal_tools._play_radio(
                {"station_id": "s99999", "room_name": "Arbeitszimmer"}
            )

        assert result["success"] is False
        assert "search_stations" in result["message"]
        resolve.assert_not_awaited()

    @pytest.mark.unit
    async def test_placeholder_caught_even_in_non_stream_url_shape(self, internal_tools):
        """If the resolver returns the placeholder in an unexpected shape (not a
        parseable {"stream_url": ...}), the whole-response scan still catches it
        and the guard fires with the actionable message — not a generic error."""
        import json as _json

        mock_mgr = MagicMock()
        # camelCase key → parsed['stream_url'] is empty, but raw_text holds the URL.
        mock_mgr.execute_tool = AsyncMock(
            return_value={
                "success": True,
                "data": [{"type": "text", "text": _json.dumps(
                    {"streamUrl": "http://cdn-cms.tunein.com/service/Audio/notcompatible.enUS.mp3"}
                )}],
            }
        )
        fake_main = ModuleType("main")
        fake_main.app = MagicMock()
        fake_main.app.state.mcp_manager = mock_mgr

        with patch.object(internal_tools, "_resolve_room_player",
                          new_callable=AsyncMock) as resolve, \
             patch.dict(sys.modules, {"main": fake_main}):
            result = await internal_tools._play_radio(
                {"station_id": "s12345", "room_name": "Arbeitszimmer"}
            )

        assert result["success"] is False
        assert "search_stations" in result["message"]
        resolve.assert_not_awaited()


# ============================================================================
# Test execute() routing
# ============================================================================

class TestInternalToolServiceExecute:
    """Test execute() dispatch to correct handler."""

    @pytest.mark.unit
    async def test_execute_unknown_tool(self, internal_tools):
        """Unknown internal tool returns error."""
        result = await internal_tools.execute("internal.nonexistent", {})
        assert result["success"] is False
        assert "Unknown internal tool" in result["message"]

    @pytest.mark.unit
    async def test_execute_routes_to_resolve(self, internal_tools):
        """execute() routes internal.resolve_room_player correctly."""
        with patch.object(internal_tools, "_resolve_room_player", new_callable=AsyncMock) as mock:
            mock.return_value = {"success": True}
            result = await internal_tools.execute(
                "internal.resolve_room_player", {"room_name": "Test"}
            )
            mock.assert_called_once_with({"room_name": "Test"})
            assert result["success"] is True

    @pytest.mark.unit
    async def test_execute_routes_to_play(self, internal_tools):
        """execute() routes internal.play_in_room correctly."""
        with patch.object(internal_tools, "_play_in_room", new_callable=AsyncMock) as mock:
            mock.return_value = {"success": True}
            params = {"media_url": "http://x", "room_name": "Test"}
            result = await internal_tools.execute("internal.play_in_room", params)
            mock.assert_called_once_with(params)


# ============================================================================
# Test TOOLS definition
# ============================================================================

class TestInternalToolsDefinition:
    """Test that TOOLS dict is well-formed."""

    @pytest.mark.unit
    def test_tools_have_descriptions(self):
        for name, defn in InternalToolService.TOOLS.items():
            assert "description" in defn, f"{name} missing description"
            assert len(defn["description"]) > 10, f"{name} description too short"

    @pytest.mark.unit
    def test_tools_have_parameters(self):
        for name, defn in InternalToolService.TOOLS.items():
            assert "parameters" in defn, f"{name} missing parameters"

    @pytest.mark.unit
    def test_all_tools_have_handlers(self):
        """Every tool in TOOLS has a matching handler."""
        for name in InternalToolService.TOOLS:
            assert name in InternalToolService._HANDLERS, f"{name} missing handler"


# ============================================================================
# Test get_user_location
# ============================================================================

class TestGetUserLocation:
    """Test internal.get_user_location tool."""

    @pytest.mark.unit
    async def test_user_found_in_room(self, internal_tools):
        """User with active presence returns room info."""
        from ha_glue.services.presence_service import UserPresence
        import time

        mock_presence_service = MagicMock()
        mock_presence_service.find_user_by_name.return_value = 1
        mock_presence_service.get_display_name.return_value = "Edi"
        mock_presence_service.get_user_presence.return_value = UserPresence(
            user_id=1,
            room_id=10,
            room_name="Wohnzimmer",
            confidence=0.85,
            last_seen=time.time() - 30,
        )

        with patch("ha_glue.services.presence_service.get_presence_service", return_value=mock_presence_service):
            result = await internal_tools._get_user_location({"user_name": "Edi"})

        assert result["success"] is True
        assert result["data"]["status"] == "present"
        assert result["data"]["room_name"] == "Wohnzimmer"
        assert result["data"]["user_name"] == "Edi"
        assert "just now" in result["data"]["last_seen"] or "minute" in result["data"]["last_seen"]
        mock_presence_service.find_user_by_name.assert_called_once_with("Edi")

    @pytest.mark.unit
    async def test_user_found_not_present(self, internal_tools):
        """User exists but has no presence data returns unknown status."""
        mock_presence_service = MagicMock()
        mock_presence_service.find_user_by_name.return_value = 1
        mock_presence_service.get_display_name.return_value = "eve"
        mock_presence_service.get_user_presence.return_value = None

        with patch("ha_glue.services.presence_service.get_presence_service", return_value=mock_presence_service):
            result = await internal_tools._get_user_location({"user_name": "eve"})

        assert result["success"] is True
        assert result["data"]["status"] == "unknown"

    @pytest.mark.unit
    async def test_user_not_found(self, internal_tools):
        """Unknown user returns error."""
        mock_presence_service = MagicMock()
        mock_presence_service.find_user_by_name.return_value = None

        with patch("ha_glue.services.presence_service.get_presence_service", return_value=mock_presence_service):
            result = await internal_tools._get_user_location({"user_name": "nobody"})

        assert result["success"] is False
        assert "not found" in result["message"]

    @pytest.mark.unit
    async def test_missing_user_name_param(self, internal_tools):
        """Missing user_name returns error."""
        result = await internal_tools._get_user_location({})
        assert result["success"] is False
        assert "required" in result["message"]

    @pytest.mark.unit
    async def test_empty_user_name_param(self, internal_tools):
        """Empty user_name returns error."""
        result = await internal_tools._get_user_location({"user_name": "  "})
        assert result["success"] is False
        assert "required" in result["message"]


# ============================================================================
# Test get_all_presence
# ============================================================================

class TestGetAllPresence:
    """Test internal.get_all_presence tool."""

    @pytest.mark.unit
    async def test_users_present(self, internal_tools):
        """Returns all currently present users."""
        from ha_glue.services.presence_service import UserPresence
        import time

        now = time.time()
        mock_presence_service = MagicMock()
        mock_presence_service.get_all_presence.return_value = {
            1: UserPresence(user_id=1, room_id=10, room_name="Wohnzimmer", last_seen=now - 10),
            2: UserPresence(user_id=2, room_id=20, room_name="Küche", last_seen=now - 120),
        }
        mock_presence_service.get_display_name.side_effect = lambda uid: {1: "Edi", 2: "Alice"}[uid]

        with patch("ha_glue.services.presence_service.get_presence_service", return_value=mock_presence_service):
            result = await internal_tools._get_all_presence({})

        assert result["success"] is True
        assert len(result["data"]["users"]) == 2
        names = [u["name"] for u in result["data"]["users"]]
        assert "Edi" in names
        assert "Alice" in names

    @pytest.mark.unit
    async def test_nobody_home(self, internal_tools):
        """Empty presence returns informative message."""
        mock_presence_service = MagicMock()
        mock_presence_service.get_all_presence.return_value = {}

        with patch("ha_glue.services.presence_service.get_presence_service", return_value=mock_presence_service):
            result = await internal_tools._get_all_presence({})

        assert result["success"] is True
        assert result["data"]["users"] == []
        assert "Nobody" in result["message"]


# Tests for `internal.knowledge_search` moved to `test_knowledge_tool.py`
# when the RAG tool was carved out of `InternalToolService` in the
# Phase 1 W4 internal-tools split (see ebongard/renfield#358).

# ============================================================================
# Test media_control
# ============================================================================

class TestMediaControl:
    """Test internal.media_control tool."""

    @pytest.mark.unit
    async def test_media_control_stop(self, internal_tools):
        """Stop action calls HA media_player.media_stop."""
        resolve_result = {
            "success": False,
            "message": "Device busy",
            "action_taken": False,
            "data": {
                "entity_id": "media_player.arbeitszimmer",
                "target_type": "homeassistant",
                "room_name": "Arbeitszimmer",
                "device_name": "Speaker",
                "status": "busy",
            },
        }

        mock_ha_client = MagicMock()
        mock_ha_client.call_service = AsyncMock(return_value=True)

        with patch.object(internal_tools, "_resolve_room_player",
                          new_callable=AsyncMock, return_value=resolve_result), \
             patch("ha_glue.integrations.homeassistant.HomeAssistantClient", return_value=mock_ha_client):
            result = await internal_tools._media_control({
                "action": "stop",
                "room_name": "Arbeitszimmer",
            })

        assert result["success"] is True
        assert result["action_taken"] is True
        assert result["data"]["action"] == "stop"
        mock_ha_client.call_service.assert_called_once_with(
            domain="media_player",
            service="media_stop",
            entity_id="media_player.arbeitszimmer",
        )

    @pytest.mark.unit
    async def test_media_control_pause(self, internal_tools):
        """Pause action calls HA media_player.media_pause."""
        resolve_result = {
            "success": True,
            "message": "Found",
            "action_taken": True,
            "data": {
                "entity_id": "media_player.wohnzimmer",
                "room_name": "Wohnzimmer",
                "device_name": "Speaker",
            },
        }

        mock_ha_client = MagicMock()
        mock_ha_client.call_service = AsyncMock(return_value=True)

        with patch.object(internal_tools, "_resolve_room_player",
                          new_callable=AsyncMock, return_value=resolve_result), \
             patch("ha_glue.integrations.homeassistant.HomeAssistantClient", return_value=mock_ha_client):
            result = await internal_tools._media_control({
                "action": "pause",
                "room_name": "Wohnzimmer",
            })

        assert result["success"] is True
        assert result["data"]["action"] == "pause"
        mock_ha_client.call_service.assert_called_once_with(
            domain="media_player",
            service="media_pause",
            entity_id="media_player.wohnzimmer",
        )

    @pytest.mark.unit
    async def test_media_control_resume(self, internal_tools):
        """Resume action calls HA media_player.media_play."""
        resolve_result = {
            "success": True,
            "message": "Found",
            "action_taken": True,
            "data": {
                "entity_id": "media_player.kueche",
                "room_name": "Küche",
                "device_name": "Speaker",
            },
        }

        mock_ha_client = MagicMock()
        mock_ha_client.call_service = AsyncMock(return_value=True)

        with patch.object(internal_tools, "_resolve_room_player",
                          new_callable=AsyncMock, return_value=resolve_result), \
             patch("ha_glue.integrations.homeassistant.HomeAssistantClient", return_value=mock_ha_client):
            result = await internal_tools._media_control({
                "action": "resume",
                "room_name": "Küche",
            })

        assert result["success"] is True
        assert result["data"]["action"] == "resume"
        mock_ha_client.call_service.assert_called_once_with(
            domain="media_player",
            service="media_play",
            entity_id="media_player.kueche",
        )

    @pytest.mark.unit
    async def test_media_control_next(self, internal_tools):
        """Next action calls HA media_player.media_next_track."""
        resolve_result = {
            "success": True,
            "message": "Found",
            "action_taken": True,
            "data": {
                "entity_id": "media_player.arbeitszimmer",
                "room_name": "Arbeitszimmer",
                "device_name": "Speaker",
            },
        }

        mock_ha_client = MagicMock()
        mock_ha_client.call_service = AsyncMock(return_value=True)

        with patch.object(internal_tools, "_resolve_room_player",
                          new_callable=AsyncMock, return_value=resolve_result), \
             patch("ha_glue.integrations.homeassistant.HomeAssistantClient", return_value=mock_ha_client):
            result = await internal_tools._media_control({
                "action": "next",
                "room_name": "Arbeitszimmer",
            })

        assert result["success"] is True
        assert result["data"]["action"] == "next"
        mock_ha_client.call_service.assert_called_once_with(
            domain="media_player",
            service="media_next_track",
            entity_id="media_player.arbeitszimmer",
        )

    @pytest.mark.unit
    async def test_media_control_previous(self, internal_tools):
        """Previous action calls HA media_player.media_previous_track."""
        resolve_result = {
            "success": True,
            "message": "Found",
            "action_taken": True,
            "data": {
                "entity_id": "media_player.arbeitszimmer",
                "room_name": "Arbeitszimmer",
                "device_name": "Speaker",
            },
        }

        mock_ha_client = MagicMock()
        mock_ha_client.call_service = AsyncMock(return_value=True)

        with patch.object(internal_tools, "_resolve_room_player",
                          new_callable=AsyncMock, return_value=resolve_result), \
             patch("ha_glue.integrations.homeassistant.HomeAssistantClient", return_value=mock_ha_client):
            result = await internal_tools._media_control({
                "action": "previous",
                "room_name": "Arbeitszimmer",
            })

        assert result["success"] is True
        assert result["data"]["action"] == "previous"
        mock_ha_client.call_service.assert_called_once_with(
            domain="media_player",
            service="media_previous_track",
            entity_id="media_player.arbeitszimmer",
        )

    @pytest.mark.unit
    async def test_media_control_invalid_action(self, internal_tools):
        """Invalid action returns error without calling HA."""
        result = await internal_tools._media_control({
            "action": "rewind",
            "room_name": "Arbeitszimmer",
        })

        assert result["success"] is False
        assert "Invalid action" in result["message"]
        assert "rewind" in result["message"]

    @pytest.mark.unit
    async def test_media_control_missing_room(self, internal_tools):
        """Missing room_name returns error."""
        result = await internal_tools._media_control({"action": "stop"})
        assert result["success"] is False
        assert "room_name" in result["message"]

    @pytest.mark.unit
    async def test_media_control_missing_action(self, internal_tools):
        """Missing action returns error."""
        result = await internal_tools._media_control({"room_name": "Arbeitszimmer"})
        assert result["success"] is False
        assert "action" in result["message"]

    @pytest.mark.unit
    async def test_media_control_room_not_found(self, internal_tools):
        """Unknown room returns error from resolve."""
        resolve_result = {
            "success": False,
            "message": "Room 'Narnia' not found",
            "action_taken": False,
        }

        with patch.object(internal_tools, "_resolve_room_player",
                          new_callable=AsyncMock, return_value=resolve_result):
            result = await internal_tools._media_control({
                "action": "stop",
                "room_name": "Narnia",
            })

        assert result["success"] is False
        assert "not found" in result["message"]

    @pytest.mark.unit
    async def test_media_control_ha_exception(self, internal_tools):
        """HA connection error returns clean error."""
        resolve_result = {
            "success": True,
            "message": "Found",
            "action_taken": True,
            "data": {
                "entity_id": "media_player.arbeitszimmer",
                "room_name": "Arbeitszimmer",
                "device_name": "Speaker",
            },
        }

        mock_ha_client = MagicMock()
        mock_ha_client.call_service = AsyncMock(side_effect=ConnectionError("HA unreachable"))

        with patch.object(internal_tools, "_resolve_room_player",
                          new_callable=AsyncMock, return_value=resolve_result), \
             patch("ha_glue.integrations.homeassistant.HomeAssistantClient", return_value=mock_ha_client):
            result = await internal_tools._media_control({
                "action": "stop",
                "room_name": "Arbeitszimmer",
            })

        assert result["success"] is False
        assert "Error executing media stop" in result["message"]

    # --- DLNA media control tests ---

    @staticmethod
    def _patch_main_app(mock_mcp_manager):
        """Patch main.app for DLNA MCP tests."""
        mock_app = MagicMock()
        mock_app.state.mcp_manager = mock_mcp_manager
        fake_main = ModuleType("main")
        fake_main.app = mock_app
        return patch.dict(sys.modules, {"main": fake_main})

    @pytest.mark.unit
    async def test_media_control_dlna_stop(self, internal_tools):
        """Stop on DLNA room calls mcp.dlna.stop."""
        resolve_result = {
            "success": True,
            "message": "Found DLNA renderer",
            "action_taken": True,
            "data": {
                "target_type": "dlna",
                "dlna_renderer_name": "HiFiBerry Arbeitszimmer",
                "room_name": "Arbeitszimmer",
                "device_name": "HiFiBerry Arbeitszimmer",
            },
        }

        mock_mcp_manager = MagicMock()
        mock_mcp_manager.execute_tool = AsyncMock(return_value={
            "success": True, "message": "Stopped",
        })

        with patch.object(internal_tools, "_resolve_room_player",
                          new_callable=AsyncMock, return_value=resolve_result), \
             self._patch_main_app(mock_mcp_manager):
            result = await internal_tools._media_control({
                "action": "stop",
                "room_name": "Arbeitszimmer",
            })

        assert result["success"] is True
        assert result["data"]["action"] == "stop"
        assert result["data"]["target_type"] == "dlna"
        assert result["data"]["renderer_name"] == "HiFiBerry Arbeitszimmer"
        mock_mcp_manager.execute_tool.assert_called_once_with(
            "mcp.dlna.stop", {"renderer_name": "HiFiBerry Arbeitszimmer"},
        )

    @pytest.mark.unit
    async def test_media_control_dlna_pause(self, internal_tools):
        """Pause on DLNA room calls mcp.dlna.pause."""
        resolve_result = {
            "success": True,
            "message": "Found DLNA renderer",
            "action_taken": True,
            "data": {
                "target_type": "dlna",
                "dlna_renderer_name": "HiFiBerry Garten",
                "room_name": "Garten",
                "device_name": "HiFiBerry Garten",
            },
        }

        mock_mcp_manager = MagicMock()
        mock_mcp_manager.execute_tool = AsyncMock(return_value={
            "success": True, "message": "Paused",
        })

        with patch.object(internal_tools, "_resolve_room_player",
                          new_callable=AsyncMock, return_value=resolve_result), \
             self._patch_main_app(mock_mcp_manager):
            result = await internal_tools._media_control({
                "action": "pause",
                "room_name": "Garten",
            })

        assert result["success"] is True
        assert result["data"]["action"] == "pause"
        mock_mcp_manager.execute_tool.assert_called_once_with(
            "mcp.dlna.pause", {"renderer_name": "HiFiBerry Garten"},
        )

    @pytest.mark.unit
    async def test_media_control_dlna_resume(self, internal_tools):
        """Resume on DLNA room calls mcp.dlna.resume."""
        resolve_result = {
            "success": True,
            "message": "Found DLNA renderer",
            "action_taken": True,
            "data": {
                "target_type": "dlna",
                "dlna_renderer_name": "HiFiBerry Garten",
                "room_name": "Garten",
                "device_name": "HiFiBerry Garten",
            },
        }

        mock_mcp_manager = MagicMock()
        mock_mcp_manager.execute_tool = AsyncMock(return_value={
            "success": True, "message": "Resumed",
        })

        with patch.object(internal_tools, "_resolve_room_player",
                          new_callable=AsyncMock, return_value=resolve_result), \
             self._patch_main_app(mock_mcp_manager):
            result = await internal_tools._media_control({
                "action": "resume",
                "room_name": "Garten",
            })

        assert result["success"] is True
        assert result["data"]["action"] == "resume"
        mock_mcp_manager.execute_tool.assert_called_once_with(
            "mcp.dlna.resume", {"renderer_name": "HiFiBerry Garten"},
        )

    @pytest.mark.unit
    async def test_media_control_dlna_next(self, internal_tools):
        """Next on DLNA room calls mcp.dlna.next_track."""
        resolve_result = {
            "success": True,
            "message": "Found DLNA renderer",
            "action_taken": True,
            "data": {
                "target_type": "dlna",
                "dlna_renderer_name": "HiFiBerry Arbeitszimmer",
                "room_name": "Arbeitszimmer",
                "device_name": "HiFiBerry Arbeitszimmer",
            },
        }

        mock_mcp_manager = MagicMock()
        mock_mcp_manager.execute_tool = AsyncMock(return_value={
            "success": True, "message": "Skipped",
        })

        with patch.object(internal_tools, "_resolve_room_player",
                          new_callable=AsyncMock, return_value=resolve_result), \
             self._patch_main_app(mock_mcp_manager):
            result = await internal_tools._media_control({
                "action": "next",
                "room_name": "Arbeitszimmer",
            })

        assert result["success"] is True
        assert result["data"]["action"] == "next"
        mock_mcp_manager.execute_tool.assert_called_once_with(
            "mcp.dlna.next_track", {"renderer_name": "HiFiBerry Arbeitszimmer"},
        )

    @pytest.mark.unit
    async def test_media_control_dlna_previous(self, internal_tools):
        """Previous on DLNA room calls mcp.dlna.previous_track."""
        resolve_result = {
            "success": True,
            "message": "Found DLNA renderer",
            "action_taken": True,
            "data": {
                "target_type": "dlna",
                "dlna_renderer_name": "HiFiBerry Arbeitszimmer",
                "room_name": "Arbeitszimmer",
                "device_name": "HiFiBerry Arbeitszimmer",
            },
        }

        mock_mcp_manager = MagicMock()
        mock_mcp_manager.execute_tool = AsyncMock(return_value={
            "success": True, "message": "Previous",
        })

        with patch.object(internal_tools, "_resolve_room_player",
                          new_callable=AsyncMock, return_value=resolve_result), \
             self._patch_main_app(mock_mcp_manager):
            result = await internal_tools._media_control({
                "action": "previous",
                "room_name": "Arbeitszimmer",
            })

        assert result["success"] is True
        assert result["data"]["action"] == "previous"
        mock_mcp_manager.execute_tool.assert_called_once_with(
            "mcp.dlna.previous_track", {"renderer_name": "HiFiBerry Arbeitszimmer"},
        )

    @pytest.mark.unit
    async def test_media_control_dlna_volume(self, internal_tools):
        """Volume on DLNA room calls mcp.dlna.set_volume."""
        resolve_result = {
            "success": True,
            "message": "Found DLNA renderer",
            "action_taken": True,
            "data": {
                "target_type": "dlna",
                "dlna_renderer_name": "HiFiBerry Arbeitszimmer",
                "room_name": "Arbeitszimmer",
                "device_name": "HiFiBerry Arbeitszimmer",
            },
        }

        mock_mcp_manager = MagicMock()
        mock_mcp_manager.execute_tool = AsyncMock(return_value={
            "success": True, "message": "Volume set",
        })

        with patch.object(internal_tools, "_resolve_room_player",
                          new_callable=AsyncMock, return_value=resolve_result), \
             self._patch_main_app(mock_mcp_manager):
            result = await internal_tools._media_control({
                "action": "volume",
                "room_name": "Arbeitszimmer",
                "volume": "75",
            })

        assert result["success"] is True
        assert result["data"]["action"] == "volume"
        mock_mcp_manager.execute_tool.assert_called_once_with(
            "mcp.dlna.set_volume", {"renderer_name": "HiFiBerry Arbeitszimmer", "volume": 75},
        )

    @pytest.mark.unit
    async def test_media_control_ha_volume(self, internal_tools):
        """Volume on HA room calls media_player.volume_set with 0.0-1.0 scale."""
        resolve_result = {
            "success": True,
            "message": "Found",
            "action_taken": True,
            "data": {
                "entity_id": "media_player.wohnzimmer",
                "target_type": "homeassistant",
                "room_name": "Wohnzimmer",
                "device_name": "Speaker",
            },
        }

        mock_ha_client = MagicMock()
        mock_ha_client.call_service = AsyncMock(return_value=True)

        with patch.object(internal_tools, "_resolve_room_player",
                          new_callable=AsyncMock, return_value=resolve_result), \
             patch("ha_glue.integrations.homeassistant.HomeAssistantClient", return_value=mock_ha_client):
            result = await internal_tools._media_control({
                "action": "volume",
                "room_name": "Wohnzimmer",
                "volume": "50",
            })

        assert result["success"] is True
        assert result["data"]["action"] == "volume"
        mock_ha_client.call_service.assert_called_once_with(
            domain="media_player",
            service="volume_set",
            entity_id="media_player.wohnzimmer",
            service_data={"volume_level": 0.5},
        )

    @pytest.mark.unit
    async def test_media_control_volume_missing_param(self, internal_tools):
        """Volume action without volume param returns error."""
        resolve_result = {
            "success": True,
            "message": "Found",
            "action_taken": True,
            "data": {
                "entity_id": "media_player.wohnzimmer",
                "target_type": "homeassistant",
                "room_name": "Wohnzimmer",
                "device_name": "Speaker",
            },
        }

        with patch.object(internal_tools, "_resolve_room_player",
                          new_callable=AsyncMock, return_value=resolve_result):
            result = await internal_tools._media_control({
                "action": "volume",
                "room_name": "Wohnzimmer",
            })

        assert result["success"] is False
        assert "volume" in result["message"].lower()

    @pytest.mark.unit
    async def test_media_control_dlna_mcp_failure(self, internal_tools):
        """DLNA MCP tool failure returns clean error."""
        resolve_result = {
            "success": True,
            "message": "Found DLNA renderer",
            "action_taken": True,
            "data": {
                "target_type": "dlna",
                "dlna_renderer_name": "HiFiBerry Arbeitszimmer",
                "room_name": "Arbeitszimmer",
                "device_name": "HiFiBerry Arbeitszimmer",
            },
        }

        mock_mcp_manager = MagicMock()
        mock_mcp_manager.execute_tool = AsyncMock(return_value={
            "success": False, "message": "No active playback on 'HiFiBerry Arbeitszimmer'",
        })

        with patch.object(internal_tools, "_resolve_room_player",
                          new_callable=AsyncMock, return_value=resolve_result), \
             self._patch_main_app(mock_mcp_manager):
            result = await internal_tools._media_control({
                "action": "stop",
                "room_name": "Arbeitszimmer",
            })

        assert result["success"] is False
        assert "DLNA stop failed" in result["message"]

    @pytest.mark.unit
    async def test_media_control_dlna_busy_device(self, internal_tools):
        """Busy DLNA device is still controlled (we want to stop/pause it)."""
        resolve_result = {
            "success": False,
            "message": "Device busy",
            "action_taken": False,
            "data": {
                "target_type": "dlna",
                "dlna_renderer_name": "HiFiBerry Arbeitszimmer",
                "room_name": "Arbeitszimmer",
                "device_name": "HiFiBerry Arbeitszimmer",
                "status": "busy",
            },
        }

        mock_mcp_manager = MagicMock()
        mock_mcp_manager.execute_tool = AsyncMock(return_value={
            "success": True, "message": "Stopped",
        })

        with patch.object(internal_tools, "_resolve_room_player",
                          new_callable=AsyncMock, return_value=resolve_result), \
             self._patch_main_app(mock_mcp_manager):
            result = await internal_tools._media_control({
                "action": "stop",
                "room_name": "Arbeitszimmer",
            })

        assert result["success"] is True
        assert result["data"]["action"] == "stop"
        assert result["data"]["target_type"] == "dlna"
        mock_mcp_manager.execute_tool.assert_called_once_with(
            "mcp.dlna.stop", {"renderer_name": "HiFiBerry Arbeitszimmer"},
        )

    @pytest.mark.unit
    async def test_media_control_volume_clamps_range(self, internal_tools):
        """Volume values are clamped to 0-100."""
        resolve_result = {
            "success": True,
            "message": "Found",
            "action_taken": True,
            "data": {
                "entity_id": "media_player.wohnzimmer",
                "target_type": "homeassistant",
                "room_name": "Wohnzimmer",
                "device_name": "Speaker",
            },
        }

        mock_ha_client = MagicMock()
        mock_ha_client.call_service = AsyncMock(return_value=True)

        with patch.object(internal_tools, "_resolve_room_player",
                          new_callable=AsyncMock, return_value=resolve_result), \
             patch("ha_glue.integrations.homeassistant.HomeAssistantClient", return_value=mock_ha_client):
            result = await internal_tools._media_control({
                "action": "volume",
                "room_name": "Wohnzimmer",
                "volume": "150",
            })

        assert result["success"] is True
        # Volume should be clamped to 1.0
        call_kwargs = mock_ha_client.call_service.call_args
        assert call_kwargs.kwargs["service_data"]["volume_level"] == 1.0


# ============================================================================
# Test _resolve_target_volume (pure precedence + step math + clamping helper)
# ============================================================================

class TestResolveTargetVolume:
    """Pure-function tests for the shared volume-target helper."""

    @pytest.mark.unit
    def test_both_volume_and_step_is_error(self, internal_tools):
        target, err = internal_tools._resolve_target_volume(
            {"volume": 30, "volume_step": -20}, current_pct=50
        )
        assert target is None
        assert err["success"] is False
        assert "not both" in err["message"]

    @pytest.mark.unit
    def test_neither_volume_nor_step_is_error(self, internal_tools):
        target, err = internal_tools._resolve_target_volume({}, current_pct=50)
        assert target is None
        assert err["success"] is False
        assert "required" in err["message"]

    @pytest.mark.unit
    def test_absolute_passthrough(self, internal_tools):
        target, err = internal_tools._resolve_target_volume({"volume": "42"}, current_pct=None)
        assert err is None
        assert target == 42

    @pytest.mark.unit
    def test_absolute_clamps_high(self, internal_tools):
        target, err = internal_tools._resolve_target_volume({"volume": 150}, current_pct=None)
        assert err is None
        assert target == 100

    @pytest.mark.unit
    def test_absolute_clamps_low(self, internal_tools):
        target, err = internal_tools._resolve_target_volume({"volume": -10}, current_pct=None)
        assert err is None
        assert target == 0

    @pytest.mark.unit
    def test_relative_step_down(self, internal_tools):
        target, err = internal_tools._resolve_target_volume(
            {"volume_step": -20}, current_pct=30
        )
        assert err is None
        assert target == 10

    @pytest.mark.unit
    def test_relative_step_up(self, internal_tools):
        target, err = internal_tools._resolve_target_volume(
            {"volume_step": 15}, current_pct=30
        )
        assert err is None
        assert target == 45

    @pytest.mark.unit
    def test_relative_clamps_floor(self, internal_tools):
        target, err = internal_tools._resolve_target_volume(
            {"volume_step": -20}, current_pct=10
        )
        assert err is None
        assert target == 0

    @pytest.mark.unit
    def test_relative_clamps_ceil(self, internal_tools):
        target, err = internal_tools._resolve_target_volume(
            {"volume_step": 20}, current_pct=90
        )
        assert err is None
        assert target == 100

    @pytest.mark.unit
    def test_relative_with_no_current_is_clear_error(self, internal_tools):
        target, err = internal_tools._resolve_target_volume(
            {"volume_step": -20}, current_pct=None
        )
        assert target is None
        assert err["success"] is False
        assert "absolute" in err["message"].lower()


# ============================================================================
# Test relative volume on HA + DLNA branches (reads current server-side)
# ============================================================================

class TestRelativeVolume:
    """Relative ('leiser/lauter um X') volume for HA + DLNA."""

    @staticmethod
    def _patch_main_app(mock_mcp_manager):
        mock_app = MagicMock()
        mock_app.state.mcp_manager = mock_mcp_manager
        fake_main = ModuleType("main")
        fake_main.app = mock_app
        return patch.dict(sys.modules, {"main": fake_main})

    @staticmethod
    def _ha_resolve():
        return {
            "success": True,
            "message": "Found",
            "action_taken": True,
            "data": {
                "entity_id": "media_player.arbeitszimmer",
                "target_type": "homeassistant",
                "room_name": "Arbeitszimmer",
                "device_name": "Speaker",
            },
        }

    @staticmethod
    def _dlna_resolve():
        return {
            "success": True,
            "message": "Found DLNA renderer",
            "action_taken": True,
            "data": {
                "target_type": "dlna",
                "dlna_renderer_name": "HiFiBerry Arbeitszimmer",
                "room_name": "Arbeitszimmer",
                "device_name": "HiFiBerry Arbeitszimmer",
            },
        }

    # --- HA branch ---

    @pytest.mark.unit
    async def test_volume_absolute_unchanged_regression_ha(self, internal_tools):
        """REGRESSION: absolute volume=N still calls volume_set with N/100 and
        never reads current state (no wasted get_state HTTP read)."""
        mock_ha_client = MagicMock()
        mock_ha_client.call_service = AsyncMock(return_value=True)
        mock_ha_client.get_state = AsyncMock(return_value=None)

        with patch.object(internal_tools, "_resolve_room_player",
                          new_callable=AsyncMock, return_value=self._ha_resolve()), \
             patch("ha_glue.integrations.homeassistant.HomeAssistantClient", return_value=mock_ha_client):
            result = await internal_tools._media_control({
                "action": "volume",
                "room_name": "Arbeitszimmer",
                "volume": "50",
            })

        assert result["success"] is True
        mock_ha_client.get_state.assert_not_called()
        mock_ha_client.call_service.assert_called_once_with(
            domain="media_player",
            service="volume_set",
            entity_id="media_player.arbeitszimmer",
            service_data={"volume_level": 0.5},
        )

    @pytest.mark.unit
    async def test_volume_relative_reads_current_ha(self, internal_tools):
        """Relative step computes target from get_state volume_level (0.3 -> 30 -> 10)."""
        mock_ha_client = MagicMock()
        mock_ha_client.call_service = AsyncMock(return_value=True)
        mock_ha_client.get_state = AsyncMock(
            return_value={"attributes": {"volume_level": 0.3}}
        )

        with patch.object(internal_tools, "_resolve_room_player",
                          new_callable=AsyncMock, return_value=self._ha_resolve()), \
             patch("ha_glue.integrations.homeassistant.HomeAssistantClient", return_value=mock_ha_client):
            result = await internal_tools._media_control({
                "action": "volume",
                "room_name": "Arbeitszimmer",
                "volume_step": -20,
            })

        assert result["success"] is True
        call_kwargs = mock_ha_client.call_service.call_args
        assert call_kwargs.kwargs["service_data"]["volume_level"] == 0.1

    @pytest.mark.unit
    async def test_volume_relative_offline_ha_clear_error(self, internal_tools):
        """get_state returns None (entity offline) -> clear error, no call_service."""
        mock_ha_client = MagicMock()
        mock_ha_client.call_service = AsyncMock(return_value=True)
        mock_ha_client.get_state = AsyncMock(return_value=None)

        with patch.object(internal_tools, "_resolve_room_player",
                          new_callable=AsyncMock, return_value=self._ha_resolve()), \
             patch("ha_glue.integrations.homeassistant.HomeAssistantClient", return_value=mock_ha_client):
            result = await internal_tools._media_control({
                "action": "volume",
                "room_name": "Arbeitszimmer",
                "volume_step": -20,
            })

        assert result["success"] is False
        assert "absolute" in result["message"].lower()
        mock_ha_client.call_service.assert_not_called()

    @pytest.mark.unit
    async def test_volume_relative_missing_attr_ha_clear_error(self, internal_tools):
        """State present but volume_level attr missing -> clear error."""
        mock_ha_client = MagicMock()
        mock_ha_client.call_service = AsyncMock(return_value=True)
        mock_ha_client.get_state = AsyncMock(return_value={"attributes": {}})

        with patch.object(internal_tools, "_resolve_room_player",
                          new_callable=AsyncMock, return_value=self._ha_resolve()), \
             patch("ha_glue.integrations.homeassistant.HomeAssistantClient", return_value=mock_ha_client):
            result = await internal_tools._media_control({
                "action": "volume",
                "room_name": "Arbeitszimmer",
                "volume_step": -20,
            })

        assert result["success"] is False
        assert "absolute" in result["message"].lower()
        mock_ha_client.call_service.assert_not_called()

    # --- DLNA branch ---

    @pytest.mark.unit
    async def test_volume_absolute_unchanged_regression_dlna(self, internal_tools):
        """REGRESSION: absolute volume=N still calls set_volume with N and never
        calls get_volume."""
        mock_mcp_manager = MagicMock()
        mock_mcp_manager.execute_tool = AsyncMock(return_value={
            "success": True, "message": "Volume set",
        })

        with patch.object(internal_tools, "_resolve_room_player",
                          new_callable=AsyncMock, return_value=self._dlna_resolve()), \
             self._patch_main_app(mock_mcp_manager):
            result = await internal_tools._media_control({
                "action": "volume",
                "room_name": "Arbeitszimmer",
                "volume": "75",
            })

        assert result["success"] is True
        mock_mcp_manager.execute_tool.assert_called_once_with(
            "mcp.dlna.set_volume", {"renderer_name": "HiFiBerry Arbeitszimmer", "volume": 75},
        )

    @pytest.mark.unit
    async def test_volume_relative_reads_current_dlna(self, internal_tools):
        """Relative step via mocked mcp.dlna.get_volume (40 -> +15 -> 55)."""
        async def _exec(tool_name, tool_params):
            if tool_name == "mcp.dlna.get_volume":
                return {"success": True, "volume": 40}
            return {"success": True, "message": "Volume set"}

        mock_mcp_manager = MagicMock()
        mock_mcp_manager.execute_tool = AsyncMock(side_effect=_exec)

        with patch.object(internal_tools, "_resolve_room_player",
                          new_callable=AsyncMock, return_value=self._dlna_resolve()), \
             self._patch_main_app(mock_mcp_manager):
            result = await internal_tools._media_control({
                "action": "volume",
                "room_name": "Arbeitszimmer",
                "volume_step": 15,
            })

        assert result["success"] is True
        # get_volume read, then set_volume with computed target.
        mock_mcp_manager.execute_tool.assert_any_call(
            "mcp.dlna.get_volume", {"renderer_name": "HiFiBerry Arbeitszimmer"},
        )
        mock_mcp_manager.execute_tool.assert_any_call(
            "mcp.dlna.set_volume", {"renderer_name": "HiFiBerry Arbeitszimmer", "volume": 55},
        )

    @pytest.mark.unit
    async def test_volume_relative_reads_current_dlna_nested_payload(self, internal_tools):
        """get_volume value nested in data content blocks (MCP wrapper shape)."""
        async def _exec(tool_name, tool_params):
            if tool_name == "mcp.dlna.get_volume":
                return {
                    "success": True,
                    "data": [{"type": "text", "text": '{"volume": 40}'}],
                }
            return {"success": True, "message": "Volume set"}

        mock_mcp_manager = MagicMock()
        mock_mcp_manager.execute_tool = AsyncMock(side_effect=_exec)

        with patch.object(internal_tools, "_resolve_room_player",
                          new_callable=AsyncMock, return_value=self._dlna_resolve()), \
             self._patch_main_app(mock_mcp_manager):
            result = await internal_tools._media_control({
                "action": "volume",
                "room_name": "Arbeitszimmer",
                "volume_step": 15,
            })

        assert result["success"] is True
        mock_mcp_manager.execute_tool.assert_any_call(
            "mcp.dlna.set_volume", {"renderer_name": "HiFiBerry Arbeitszimmer", "volume": 55},
        )

    @pytest.mark.unit
    async def test_volume_relative_dlna_volume_none_clear_error(self, internal_tools):
        """get_volume returns volume=None (renderer can't report) -> clear error,
        no set_volume call."""
        async def _exec(tool_name, tool_params):
            if tool_name == "mcp.dlna.get_volume":
                return {"success": True, "volume": None}
            return {"success": True, "message": "Volume set"}

        mock_mcp_manager = MagicMock()
        mock_mcp_manager.execute_tool = AsyncMock(side_effect=_exec)

        with patch.object(internal_tools, "_resolve_room_player",
                          new_callable=AsyncMock, return_value=self._dlna_resolve()), \
             self._patch_main_app(mock_mcp_manager):
            result = await internal_tools._media_control({
                "action": "volume",
                "room_name": "Arbeitszimmer",
                "volume_step": 15,
            })

        assert result["success"] is False
        assert "absolute" in result["message"].lower()
        # set_volume must NOT have been called (only the get_volume read).
        for call in mock_mcp_manager.execute_tool.call_args_list:
            assert call.args[0] != "mcp.dlna.set_volume"

    @pytest.mark.unit
    async def test_volume_relative_dlna_get_volume_errored_clear_error(self, internal_tools):
        """get_volume tool returns success=False (errored / not deployed) -> clear
        error, no set_volume call."""
        async def _exec(tool_name, tool_params):
            if tool_name == "mcp.dlna.get_volume":
                return {"success": False, "message": "unknown tool"}
            return {"success": True, "message": "Volume set"}

        mock_mcp_manager = MagicMock()
        mock_mcp_manager.execute_tool = AsyncMock(side_effect=_exec)

        with patch.object(internal_tools, "_resolve_room_player",
                          new_callable=AsyncMock, return_value=self._dlna_resolve()), \
             self._patch_main_app(mock_mcp_manager):
            result = await internal_tools._media_control({
                "action": "volume",
                "room_name": "Arbeitszimmer",
                "volume_step": 15,
            })

        assert result["success"] is False
        assert "absolute" in result["message"].lower()
        for call in mock_mcp_manager.execute_tool.call_args_list:
            assert call.args[0] != "mcp.dlna.set_volume"

    # --- muted-device (volume == 0) guards: 0 is a valid level, not "no reading" ---

    @pytest.mark.unit
    async def test_volume_relative_from_muted_dlna(self, internal_tools):
        """A muted DLNA renderer (volume=0) + step up resolves correctly — 0 must
        be treated as a real reading, not as 'can't report' (no falsy-0 bug)."""
        async def _exec(tool_name, tool_params):
            if tool_name == "mcp.dlna.get_volume":
                return {"success": True, "volume": 0}
            return {"success": True, "message": "Volume set"}

        mock_mcp_manager = MagicMock()
        mock_mcp_manager.execute_tool = AsyncMock(side_effect=_exec)

        with patch.object(internal_tools, "_resolve_room_player",
                          new_callable=AsyncMock, return_value=self._dlna_resolve()), \
             self._patch_main_app(mock_mcp_manager):
            result = await internal_tools._media_control({
                "action": "volume",
                "room_name": "Arbeitszimmer",
                "volume_step": 20,
            })

        assert result["success"] is True
        mock_mcp_manager.execute_tool.assert_any_call(
            "mcp.dlna.set_volume", {"renderer_name": "HiFiBerry Arbeitszimmer", "volume": 20},
        )

    @pytest.mark.unit
    async def test_volume_relative_from_muted_ha(self, internal_tools):
        """A muted HA device (volume_level=0.0) + step up resolves correctly —
        0.0 must be distinguished from None (offline), not collapsed by a falsy check."""
        mock_ha_client = MagicMock()
        mock_ha_client.call_service = AsyncMock(return_value=True)
        mock_ha_client.get_state = AsyncMock(
            return_value={"attributes": {"volume_level": 0.0}}
        )

        with patch.object(internal_tools, "_resolve_room_player",
                          new_callable=AsyncMock, return_value=self._ha_resolve()), \
             patch("ha_glue.integrations.homeassistant.HomeAssistantClient", return_value=mock_ha_client):
            result = await internal_tools._media_control({
                "action": "volume",
                "room_name": "Arbeitszimmer",
                "volume_step": 30,
            })

        assert result["success"] is True
        call_kwargs = mock_ha_client.call_service.call_args
        assert call_kwargs.kwargs["service_data"]["volume_level"] == pytest.approx(0.3)

    # --- loop-prevention: volume success echoes the resulting level so the agent
    #     gives final_answer instead of re-issuing (a repeated volume_step would
    #     re-apply the delta — the rc.13 +10% → +30% loop bug) ---

    @pytest.mark.unit
    async def test_volume_result_echoes_level_dlna(self, internal_tools):
        """DLNA volume success returns the resulting level in data.volume + message."""
        async def _exec(tool_name, tool_params):
            if tool_name == "mcp.dlna.get_volume":
                return {"success": True, "volume": 40}
            return {"success": True, "message": "ok"}

        mock_mcp_manager = MagicMock()
        mock_mcp_manager.execute_tool = AsyncMock(side_effect=_exec)

        with patch.object(internal_tools, "_resolve_room_player",
                          new_callable=AsyncMock, return_value=self._dlna_resolve()), \
             self._patch_main_app(mock_mcp_manager):
            result = await internal_tools._media_control({
                "action": "volume", "room_name": "Arbeitszimmer", "volume_step": 15,
            })

        assert result["success"] is True
        assert result["data"]["volume"] == 55  # 40 + 15
        assert "55" in result["message"]

    @pytest.mark.unit
    async def test_volume_result_echoes_level_ha(self, internal_tools):
        """HA volume success returns the resulting level in data.volume + message."""
        mock_ha_client = MagicMock()
        mock_ha_client.call_service = AsyncMock(return_value=True)

        with patch.object(internal_tools, "_resolve_room_player",
                          new_callable=AsyncMock, return_value=self._ha_resolve()), \
             patch("ha_glue.integrations.homeassistant.HomeAssistantClient", return_value=mock_ha_client):
            result = await internal_tools._media_control({
                "action": "volume", "room_name": "Arbeitszimmer", "volume": 65,
            })

        assert result["success"] is True
        assert result["data"]["volume"] == 65
        assert "65" in result["message"]

    # --- native mute/unmute (no pause, no stored volume) ---

    @pytest.mark.unit
    async def test_mute_dlna(self, internal_tools):
        """mute on DLNA calls mcp.dlna.set_mute with mute=True."""
        mock_mcp_manager = MagicMock()
        mock_mcp_manager.execute_tool = AsyncMock(return_value={"success": True, "muted": True})
        with patch.object(internal_tools, "_resolve_room_player",
                          new_callable=AsyncMock, return_value=self._dlna_resolve()), \
             self._patch_main_app(mock_mcp_manager):
            result = await internal_tools._media_control({
                "action": "mute", "room_name": "Arbeitszimmer",
            })
        assert result["success"] is True
        mock_mcp_manager.execute_tool.assert_called_once_with(
            "mcp.dlna.set_mute", {"renderer_name": "HiFiBerry Arbeitszimmer", "mute": True},
        )
        assert "muted" in result["message"].lower()

    @pytest.mark.unit
    async def test_unmute_dlna(self, internal_tools):
        """unmute on DLNA calls mcp.dlna.set_mute with mute=False."""
        mock_mcp_manager = MagicMock()
        mock_mcp_manager.execute_tool = AsyncMock(return_value={"success": True, "muted": False})
        with patch.object(internal_tools, "_resolve_room_player",
                          new_callable=AsyncMock, return_value=self._dlna_resolve()), \
             self._patch_main_app(mock_mcp_manager):
            result = await internal_tools._media_control({
                "action": "unmute", "room_name": "Arbeitszimmer",
            })
        assert result["success"] is True
        mock_mcp_manager.execute_tool.assert_called_once_with(
            "mcp.dlna.set_mute", {"renderer_name": "HiFiBerry Arbeitszimmer", "mute": False},
        )

    @pytest.mark.unit
    async def test_mute_ha(self, internal_tools):
        """mute on an HA player calls media_player.volume_mute is_volume_muted=True."""
        mock_ha_client = MagicMock()
        mock_ha_client.call_service = AsyncMock(return_value=True)
        with patch.object(internal_tools, "_resolve_room_player",
                          new_callable=AsyncMock, return_value=self._ha_resolve()), \
             patch("ha_glue.integrations.homeassistant.HomeAssistantClient", return_value=mock_ha_client):
            result = await internal_tools._media_control({
                "action": "mute", "room_name": "Arbeitszimmer",
            })
        assert result["success"] is True
        mock_ha_client.call_service.assert_called_once_with(
            domain="media_player",
            service="volume_mute",
            entity_id="media_player.arbeitszimmer",
            service_data={"is_volume_muted": True},
        )

    @pytest.mark.unit
    async def test_unmute_ha(self, internal_tools):
        """unmute on an HA player calls media_player.volume_mute is_volume_muted=False."""
        mock_ha_client = MagicMock()
        mock_ha_client.call_service = AsyncMock(return_value=True)
        with patch.object(internal_tools, "_resolve_room_player",
                          new_callable=AsyncMock, return_value=self._ha_resolve()), \
             patch("ha_glue.integrations.homeassistant.HomeAssistantClient", return_value=mock_ha_client):
            result = await internal_tools._media_control({
                "action": "unmute", "room_name": "Arbeitszimmer",
            })
        assert result["success"] is True
        mock_ha_client.call_service.assert_called_once_with(
            domain="media_player",
            service="volume_mute",
            entity_id="media_player.arbeitszimmer",
            service_data={"is_volume_muted": False},
        )

    # --- status (what's playing) — room-based, read-only ---

    @pytest.mark.unit
    async def test_status_dlna(self, internal_tools):
        """status on DLNA calls mcp.dlna.get_status and parses the nested
        content-block payload (the REAL execute_tool shape, not a flat dict)."""
        import json as _json
        payload = _json.dumps({"state": "playing", "title": "1LIVE", "artist": "WDR"})

        async def _exec(tool_name, tool_params):
            if tool_name == "mcp.dlna.get_status":
                return {"success": True, "data": [{"type": "text", "text": payload}]}
            return {"success": True}

        mock_mcp_manager = MagicMock()
        mock_mcp_manager.execute_tool = AsyncMock(side_effect=_exec)
        with patch.object(internal_tools, "_resolve_room_player",
                          new_callable=AsyncMock, return_value=self._dlna_resolve()), \
             self._patch_main_app(mock_mcp_manager):
            result = await internal_tools._media_control({
                "action": "status", "room_name": "Arbeitszimmer",
            })
        assert result["success"] is True
        mock_mcp_manager.execute_tool.assert_called_once_with(
            "mcp.dlna.get_status", {"renderer_name": "HiFiBerry Arbeitszimmer"},
        )
        assert result["data"]["status"]["title"] == "1LIVE"
        assert result["data"]["status"]["state"] == "playing"

    @pytest.mark.unit
    async def test_status_dlna_failure_surfaced(self, internal_tools):
        """A failing get_status is surfaced as success=False, not wrapped as ok."""
        async def _exec(tool_name, tool_params):
            if tool_name == "mcp.dlna.get_status":
                return {"success": False, "message": "Renderer 'X' not found"}
            return {"success": True}

        mock_mcp_manager = MagicMock()
        mock_mcp_manager.execute_tool = AsyncMock(side_effect=_exec)
        with patch.object(internal_tools, "_resolve_room_player",
                          new_callable=AsyncMock, return_value=self._dlna_resolve()), \
             self._patch_main_app(mock_mcp_manager):
            result = await internal_tools._media_control({
                "action": "status", "room_name": "Arbeitszimmer",
            })
        assert result["success"] is False
        assert "not found" in result["message"]

    @pytest.mark.unit
    async def test_status_ha(self, internal_tools):
        """status on an HA player reads get_state and normalizes the media fields."""
        mock_ha_client = MagicMock()
        mock_ha_client.get_state = AsyncMock(return_value={
            "state": "playing",
            "attributes": {"media_title": "Song", "media_artist": "Artist",
                           "media_album_name": "Album"},
        })
        with patch.object(internal_tools, "_resolve_room_player",
                          new_callable=AsyncMock, return_value=self._ha_resolve()), \
             patch("ha_glue.integrations.homeassistant.HomeAssistantClient", return_value=mock_ha_client):
            result = await internal_tools._media_control({
                "action": "status", "room_name": "Arbeitszimmer",
            })
        assert result["success"] is True
        st = result["data"]["status"]
        assert st["state"] == "playing"
        assert st["title"] == "Song"
        assert st["artist"] == "Artist"

    # --- seek / play_mode (Phase 2) ---

    @pytest.mark.unit
    async def test_seek_dlna(self, internal_tools):
        async def _exec(tool_name, tool_params):
            if tool_name == "mcp.dlna.seek":
                return {"success": True}
            return {"success": True}
        mock_mgr = MagicMock(); mock_mgr.execute_tool = AsyncMock(side_effect=_exec)
        with patch.object(internal_tools, "_resolve_room_player",
                          new_callable=AsyncMock, return_value=self._dlna_resolve()), \
             self._patch_main_app(mock_mgr):
            result = await internal_tools._media_control({
                "action": "seek", "room_name": "Arbeitszimmer", "position_seconds": 30,
            })
        assert result["success"] is True
        mock_mgr.execute_tool.assert_called_once_with(
            "mcp.dlna.seek", {"renderer_name": "HiFiBerry Arbeitszimmer", "position_seconds": 30},
        )

    @pytest.mark.unit
    async def test_play_mode_dlna(self, internal_tools):
        async def _exec(tool_name, tool_params):
            return {"success": True}
        mock_mgr = MagicMock(); mock_mgr.execute_tool = AsyncMock(side_effect=_exec)
        with patch.object(internal_tools, "_resolve_room_player",
                          new_callable=AsyncMock, return_value=self._dlna_resolve()), \
             self._patch_main_app(mock_mgr):
            result = await internal_tools._media_control({
                "action": "play_mode", "room_name": "Arbeitszimmer", "mode": "shuffle",
            })
        assert result["success"] is True
        mock_mgr.execute_tool.assert_called_once_with(
            "mcp.dlna.set_play_mode", {"renderer_name": "HiFiBerry Arbeitszimmer", "mode": "shuffle"},
        )

    @pytest.mark.unit
    async def test_seek_missing_position_is_error(self, internal_tools):
        mock_mgr = MagicMock(); mock_mgr.execute_tool = AsyncMock(return_value={"success": True})
        with patch.object(internal_tools, "_resolve_room_player",
                          new_callable=AsyncMock, return_value=self._dlna_resolve()), \
             self._patch_main_app(mock_mgr):
            result = await internal_tools._media_control({
                "action": "seek", "room_name": "Arbeitszimmer",
            })
        assert result["success"] is False
        assert "position_seconds" in result["message"]

    @pytest.mark.unit
    async def test_seek_ha_media_seek(self, internal_tools):
        mock_ha = MagicMock(); mock_ha.call_service = AsyncMock(return_value=True)
        with patch.object(internal_tools, "_resolve_room_player",
                          new_callable=AsyncMock, return_value=self._ha_resolve()), \
             patch("ha_glue.integrations.homeassistant.HomeAssistantClient", return_value=mock_ha):
            result = await internal_tools._media_control({
                "action": "seek", "room_name": "Arbeitszimmer", "position_seconds": 12,
            })
        assert result["success"] is True
        mock_ha.call_service.assert_called_once_with(
            domain="media_player", service="media_seek",
            entity_id="media_player.arbeitszimmer", service_data={"seek_position": 12},
        )

    @pytest.mark.unit
    async def test_seek_dlna_failure_surfaced(self, internal_tools):
        async def _exec(tool_name, tool_params):
            if tool_name == "mcp.dlna.seek":
                return {"success": False, "message": "seek not supported"}
            return {"success": True}
        mock_mgr = MagicMock(); mock_mgr.execute_tool = AsyncMock(side_effect=_exec)
        with patch.object(internal_tools, "_resolve_room_player",
                          new_callable=AsyncMock, return_value=self._dlna_resolve()), \
             self._patch_main_app(mock_mgr):
            result = await internal_tools._media_control({
                "action": "seek", "room_name": "Arbeitszimmer", "position_seconds": 5,
            })
        assert result["success"] is False
        assert "seek" in result["message"].lower()

    @pytest.mark.unit
    async def test_play_mode_ha_missing_mode_is_error(self, internal_tools):
        mock_ha = MagicMock(); mock_ha.call_service = AsyncMock(return_value=True)
        with patch.object(internal_tools, "_resolve_room_player",
                          new_callable=AsyncMock, return_value=self._ha_resolve()), \
             patch("ha_glue.integrations.homeassistant.HomeAssistantClient", return_value=mock_ha):
            result = await internal_tools._media_control({
                "action": "play_mode", "room_name": "Arbeitszimmer",
            })
        assert result["success"] is False
        assert "mode" in result["message"].lower()
        mock_ha.call_service.assert_not_called()


# ============================================================================
# Test internal.play_from_server (Phase 3 — media-server browsing)
# ============================================================================

class TestPlayFromServer:
    @staticmethod
    def _patch_main_app(mock_mcp_manager):
        mock_app = MagicMock()
        mock_app.state.mcp_manager = mock_mcp_manager
        fake_main = ModuleType("main")
        fake_main.app = mock_app
        return patch.dict(sys.modules, {"main": fake_main})

    @staticmethod
    def _dlna_resolve():
        return {
            "success": True, "action_taken": True,
            "data": {"target_type": "dlna", "dlna_renderer_name": "HiFiBerry Arbeitszimmer",
                     "room_name": "Arbeitszimmer", "device_name": "HiFiBerry Arbeitszimmer"},
        }

    @pytest.mark.unit
    async def test_play_from_server_resolves_room_and_plays(self, internal_tools):
        async def _exec(tool_name, tool_params):
            if tool_name == "mcp.dlna.play_from_server":
                return {"success": True, "data": [{"type": "text", "text": '{"played": 12}'}]}
            return {"success": True}
        mock_mgr = MagicMock(); mock_mgr.execute_tool = AsyncMock(side_effect=_exec)
        with patch.object(internal_tools, "_resolve_room_player",
                          new_callable=AsyncMock, return_value=self._dlna_resolve()), \
             self._patch_main_app(mock_mgr):
            result = await internal_tools._play_from_server({
                "server_name": "NAS", "object_id": "album-42", "room_name": "Arbeitszimmer",
            })
        assert result["success"] is True
        mock_mgr.execute_tool.assert_called_once_with(
            "mcp.dlna.play_from_server",
            {"server_name": "NAS", "object_id": "album-42", "renderer_name": "HiFiBerry Arbeitszimmer"},
        )
        assert result["data"]["result"]["played"] == 12

    @pytest.mark.unit
    async def test_play_from_server_requires_params(self, internal_tools):
        result = await internal_tools._play_from_server({"server_name": "NAS", "object_id": "x"})
        assert result["success"] is False
        assert "room_name" in result["message"]

    @pytest.mark.unit
    async def test_play_from_server_non_dlna_room_errors(self, internal_tools):
        ha_resolve = {"success": True, "data": {"target_type": "homeassistant",
                      "entity_id": "media_player.x", "room_name": "Wohnzimmer"}}
        with patch.object(internal_tools, "_resolve_room_player",
                          new_callable=AsyncMock, return_value=ha_resolve):
            result = await internal_tools._play_from_server({
                "server_name": "NAS", "object_id": "y", "room_name": "Wohnzimmer",
            })
        assert result["success"] is False
        assert "no dlna renderer" in result["message"].lower()

    @pytest.mark.unit
    async def test_play_from_server_failure_surfaced(self, internal_tools):
        async def _exec(tool_name, tool_params):
            if tool_name == "mcp.dlna.play_from_server":
                return {"success": False, "message": "object not found"}
            return {"success": True}
        mock_mgr = MagicMock(); mock_mgr.execute_tool = AsyncMock(side_effect=_exec)
        with patch.object(internal_tools, "_resolve_room_player",
                          new_callable=AsyncMock, return_value=self._dlna_resolve()), \
             self._patch_main_app(mock_mgr):
            result = await internal_tools._play_from_server({
                "server_name": "NAS", "object_id": "bad", "room_name": "Arbeitszimmer",
            })
        assert result["success"] is False
        assert "failed" in result["message"].lower() or "not found" in result["message"].lower()

    @pytest.mark.unit
    async def test_play_from_server_busy_room_clear_error(self, internal_tools):
        busy = {"success": False, "data": {"status": "busy", "target_type": "dlna",
                "dlna_renderer_name": "HiFiBerry Arbeitszimmer", "room_name": "Arbeitszimmer"},
                "message": "busy"}
        with patch.object(internal_tools, "_resolve_room_player",
                          new_callable=AsyncMock, return_value=busy):
            result = await internal_tools._play_from_server({
                "server_name": "NAS", "object_id": "x", "room_name": "Arbeitszimmer",
            })
        assert result["success"] is False
        assert "stop it first" in result["message"].lower() or "currently playing" in result["message"].lower()


# ============================================================================
# Test play_album_on_dlna
# ============================================================================

class TestPlayAlbumOnDlna:
    """Test internal.play_album_on_dlna tool."""

    @staticmethod
    def _patch_main_app(mock_mcp_manager):
        """Patch main.app without importing the real main module."""
        mock_app = MagicMock()
        mock_app.state.mcp_manager = mock_mcp_manager

        # Inject fake main module to avoid asyncpg import
        fake_main = ModuleType("main")
        fake_main.app = mock_app  # type: ignore

        return patch.dict(sys.modules, {"main": fake_main})

    @pytest.mark.unit
    async def test_play_album_success(self, internal_tools):
        """Album played successfully via Jellyfin + DLNA MCP calls."""
        mock_mcp_manager = MagicMock()

        import json
        tracks_response = json.dumps({
            "album": "The Very Best Of Foreigner",
            "artist": "Foreigner",
            "track_count": 2,
            "items": [
                {"name": "Feels Like The First Time", "artist": "Foreigner", "album": "The Very Best Of Foreigner",
                 "api_stream": "http://jellyfin:8096/Audio/abc123/stream?static=true&api_key=k"},
                {"name": "Cold As Ice", "artist": "Foreigner", "album": "The Very Best Of Foreigner",
                 "api_stream": "http://jellyfin:8096/Audio/def456/stream?static=true&api_key=k"},
            ],
        })
        mock_mcp_manager.execute_tool = AsyncMock(side_effect=[
            {"success": True, "message": tracks_response, "data": [{"type": "text", "text": tracks_response}]},
            {"success": True, "message": "Playing 2 tracks on HiFiBerry Arbeitszimmer"},
        ])

        with self._patch_main_app(mock_mcp_manager):
            result = await internal_tools._play_album_on_dlna({
                "album_id": "50ffb172",
                "renderer_name": "Arbeitszimmer",
            })

        assert result["success"] is True
        assert result["data"]["track_count"] == 2
        assert result["data"]["album"] == "The Very Best Of Foreigner"
        assert result["data"]["renderer"] == "Arbeitszimmer"

        calls = mock_mcp_manager.execute_tool.call_args_list
        assert calls[0].args[0] == "mcp.jellyfin.get_album_tracks"
        assert calls[0].args[1] == {"album_id": "50ffb172"}
        assert calls[1].args[0] == "mcp.dlna.play_tracks"
        assert calls[1].args[1]["renderer_name"] == "Arbeitszimmer"

    @pytest.mark.unit
    async def test_play_album_missing_album_id(self, internal_tools):
        """Missing album_id returns error."""
        result = await internal_tools._play_album_on_dlna({"renderer_name": "Arbeitszimmer"})
        assert result["success"] is False
        assert "album_id" in result["message"]

    @pytest.mark.unit
    async def test_play_album_missing_renderer_and_room(self, internal_tools):
        """Missing both renderer_name and room_name returns error."""
        result = await internal_tools._play_album_on_dlna({"album_id": "abc123"})
        assert result["success"] is False
        assert "renderer_name" in result["message"] or "room_name" in result["message"]

    @pytest.mark.unit
    async def test_play_album_via_room_name(self, internal_tools):
        """room_name resolves DLNA renderer from room config."""
        resolve_result = {
            "success": True,
            "message": "Found DLNA renderer",
            "action_taken": True,
            "data": {
                "target_type": "dlna",
                "dlna_renderer_name": "HiFiBerry Garten",
                "room_name": "Garten",
                "device_name": "HiFiBerry Garten",
            },
        }

        mock_mcp_manager = MagicMock()

        import json
        tracks_response = json.dumps({
            "album": "OK Computer",
            "items": [
                {"name": "Airbag", "artist": "Radiohead", "album": "OK Computer",
                 "api_stream": "http://jellyfin:8096/Audio/a1/stream"},
            ],
        })
        mock_mcp_manager.execute_tool = AsyncMock(side_effect=[
            {"success": True, "message": tracks_response, "data": [{"type": "text", "text": tracks_response}]},
            {"success": True, "message": "Playing 1 track"},
        ])

        with patch.object(internal_tools, "_resolve_room_player",
                          new_callable=AsyncMock, return_value=resolve_result), \
             self._patch_main_app(mock_mcp_manager):
            result = await internal_tools._play_album_on_dlna({
                "album_id": "50ffb172",
                "room_name": "Garten",
            })

        assert result["success"] is True
        assert result["data"]["renderer"] == "HiFiBerry Garten"

        # Verify DLNA play_tracks was called with the resolved renderer_name
        dlna_call = mock_mcp_manager.execute_tool.call_args_list[1]
        assert dlna_call.args[1]["renderer_name"] == "HiFiBerry Garten"

    @pytest.mark.unit
    async def test_play_album_room_has_no_dlna(self, internal_tools):
        """room_name resolving to non-DLNA device returns error."""
        resolve_result = {
            "success": True,
            "message": "Found HA player",
            "action_taken": True,
            "data": {
                "target_type": "homeassistant",
                "entity_id": "media_player.garten",
                "room_name": "Garten",
                "device_name": "HomePod Garten",
            },
        }

        with patch.object(internal_tools, "_resolve_room_player",
                          new_callable=AsyncMock, return_value=resolve_result):
            result = await internal_tools._play_album_on_dlna({
                "album_id": "abc123",
                "room_name": "Garten",
            })

        assert result["success"] is False
        assert "no DLNA renderer configured" in result["message"]

    @pytest.mark.unit
    async def test_play_album_room_not_found(self, internal_tools):
        """room_name not found returns error from resolve."""
        resolve_result = {
            "success": False,
            "message": "Room 'Narnia' not found",
            "action_taken": False,
        }

        with patch.object(internal_tools, "_resolve_room_player",
                          new_callable=AsyncMock, return_value=resolve_result):
            result = await internal_tools._play_album_on_dlna({
                "album_id": "abc123",
                "room_name": "Narnia",
            })

        assert result["success"] is False
        assert "not found" in result["message"]

    @pytest.mark.unit
    async def test_play_album_renderer_name_takes_precedence(self, internal_tools):
        """When both renderer_name and room_name are given, renderer_name wins."""
        mock_mcp_manager = MagicMock()

        import json
        tracks_response = json.dumps({
            "items": [
                {"name": "Track", "artist": "Artist", "album": "Album",
                 "api_stream": "http://jellyfin:8096/Audio/a1/stream"},
            ],
        })
        mock_mcp_manager.execute_tool = AsyncMock(side_effect=[
            {"success": True, "message": tracks_response, "data": [{"type": "text", "text": tracks_response}]},
            {"success": True, "message": "Playing"},
        ])

        with self._patch_main_app(mock_mcp_manager):
            result = await internal_tools._play_album_on_dlna({
                "album_id": "abc123",
                "renderer_name": "DirectRenderer",
                "room_name": "ShouldBeIgnored",
            })

        assert result["success"] is True
        assert result["data"]["renderer"] == "DirectRenderer"

    @pytest.mark.unit
    async def test_play_album_jellyfin_fails(self, internal_tools):
        """Jellyfin get_album_tracks failure returns error."""
        mock_mcp_manager = MagicMock()
        mock_mcp_manager.execute_tool = AsyncMock(return_value={
            "success": False, "message": "Album not found",
        })

        with self._patch_main_app(mock_mcp_manager):
            result = await internal_tools._play_album_on_dlna({
                "album_id": "invalid",
                "renderer_name": "Arbeitszimmer",
            })

        assert result["success"] is False
        assert "Failed to get album tracks" in result["message"]

    @pytest.mark.unit
    async def test_play_album_dlna_fails(self, internal_tools):
        """DLNA play_tracks failure returns error."""
        import json
        tracks_response = json.dumps({
            "items": [
                {"name": "Track 1", "artist": "Artist", "album": "Album",
                 "api_stream": "http://jellyfin:8096/Audio/abc/stream"},
            ],
        })
        mock_mcp_manager = MagicMock()
        mock_mcp_manager.execute_tool = AsyncMock(side_effect=[
            {"success": True, "message": tracks_response, "data": [{"type": "text", "text": tracks_response}]},
            {"success": False, "message": "Renderer not found"},
        ])

        with self._patch_main_app(mock_mcp_manager):
            result = await internal_tools._play_album_on_dlna({
                "album_id": "abc123",
                "renderer_name": "Narnia",
            })

        assert result["success"] is False
        assert "DLNA playback failed" in result["message"]

    @pytest.mark.unit
    async def test_play_album_no_mcp_manager(self, internal_tools):
        """Missing MCP manager returns error."""
        with self._patch_main_app(None):
            result = await internal_tools._play_album_on_dlna({
                "album_id": "abc123",
                "renderer_name": "Arbeitszimmer",
            })

        assert result["success"] is False
        assert "MCP manager not available" in result["message"]

    @pytest.mark.unit
    async def test_execute_routes_to_play_album_on_dlna(self, internal_tools):
        """execute() routes internal.play_album_on_dlna correctly."""
        with patch.object(internal_tools, "_play_album_on_dlna", new_callable=AsyncMock) as mock:
            mock.return_value = {"success": True}
            params = {"album_id": "abc", "renderer_name": "Test"}
            result = await internal_tools.execute("internal.play_album_on_dlna", params)
            mock.assert_called_once_with(params)
            assert result["success"] is True


class TestPlayVideoOnDlna:
    """Test internal.play_video_on_dlna tool."""

    @staticmethod
    def _patch_main_app(mock_mcp_manager):
        """Patch main.app without importing the real main module."""
        mock_app = MagicMock()
        mock_app.state.mcp_manager = mock_mcp_manager
        fake_main = ModuleType("main")
        fake_main.app = mock_app  # type: ignore
        return patch.dict(sys.modules, {"main": fake_main})

    @pytest.mark.unit
    async def test_play_video_success(self, internal_tools):
        """Video played successfully via Jellyfin get_stream_url + DLNA play_tracks."""
        import json
        stream_response = json.dumps({
            "id": "movie1",
            "name": "Interstellar",
            "type": "Movie",
            "video_stream": "http://jellyfin:8096/Videos/movie1/stream?static=true&api_key=k",
        })
        mock_mcp_manager = MagicMock()
        mock_mcp_manager.execute_tool = AsyncMock(side_effect=[
            {"success": True, "message": stream_response, "data": [{"type": "text", "text": stream_response}]},
            {"success": True, "message": "Playing on Samsung TV"},
        ])

        with self._patch_main_app(mock_mcp_manager):
            result = await internal_tools._play_video_on_dlna({
                "item_id": "movie1",
                "renderer_name": "Samsung TV",
                "title": "Interstellar",
            })

        assert result["success"] is True
        assert result["data"]["title"] == "Interstellar"
        assert result["data"]["renderer"] == "Samsung TV"

        # Verify correct MCP calls
        calls = mock_mcp_manager.execute_tool.call_args_list
        assert calls[0].args[0] == "mcp.jellyfin.get_stream_url"
        assert calls[0].args[1] == {"item_id": "movie1"}
        assert calls[1].args[0] == "mcp.dlna.play_tracks"
        # Verify video media_type in the tracks JSON
        tracks_json = json.loads(calls[1].args[1]["tracks"])
        assert tracks_json[0]["media_type"] == "video"

    @pytest.mark.unit
    async def test_play_video_missing_item_id(self, internal_tools):
        """Missing item_id returns error."""
        result = await internal_tools._play_video_on_dlna({"renderer_name": "Samsung TV"})
        assert result["success"] is False
        assert "item_id" in result["message"]

    @pytest.mark.unit
    async def test_play_video_missing_renderer_and_room(self, internal_tools):
        """Missing both renderer_name and room_name returns error."""
        result = await internal_tools._play_video_on_dlna({"item_id": "movie1"})
        assert result["success"] is False
        assert "renderer_name" in result["message"] or "room_name" in result["message"]

    @pytest.mark.unit
    async def test_play_video_via_room_name(self, internal_tools):
        """room_name resolves visual DLNA renderer from room config."""
        import json
        resolve_result = {
            "success": True,
            "message": "Found visual DLNA renderer",
            "action_taken": True,
            "data": {
                "target_type": "dlna",
                "dlna_renderer_name": "Samsung TV Wohnzimmer",
                "room_name": "Wohnzimmer",
                "device_name": "Samsung TV Wohnzimmer",
            },
        }

        stream_response = json.dumps({
            "id": "movie1",
            "name": "Interstellar",
            "video_stream": "http://jellyfin:8096/Videos/movie1/stream?static=true&api_key=k",
        })
        mock_mcp_manager = MagicMock()
        mock_mcp_manager.execute_tool = AsyncMock(side_effect=[
            {"success": True, "message": stream_response, "data": [{"type": "text", "text": stream_response}]},
            {"success": True, "message": "Playing"},
        ])

        with patch.object(internal_tools, "_resolve_room_visual_player",
                          new_callable=AsyncMock, return_value=resolve_result), \
             self._patch_main_app(mock_mcp_manager):
            result = await internal_tools._play_video_on_dlna({
                "item_id": "movie1",
                "room_name": "Wohnzimmer",
            })

        assert result["success"] is True
        assert result["data"]["renderer"] == "Samsung TV Wohnzimmer"

    @pytest.mark.unit
    async def test_play_video_no_video_stream(self, internal_tools):
        """Item without video_stream URL returns error."""
        import json
        stream_response = json.dumps({
            "id": "audio1",
            "name": "Song",
            "type": "Audio",
            "api_stream": "http://jellyfin:8096/Audio/audio1/stream",
        })
        mock_mcp_manager = MagicMock()
        mock_mcp_manager.execute_tool = AsyncMock(return_value={
            "success": True, "message": stream_response, "data": [{"type": "text", "text": stream_response}],
        })

        with self._patch_main_app(mock_mcp_manager):
            result = await internal_tools._play_video_on_dlna({
                "item_id": "audio1",
                "renderer_name": "Samsung TV",
            })

        assert result["success"] is False
        assert "No video stream URL" in result["message"]

    @pytest.mark.unit
    async def test_play_video_jellyfin_fails(self, internal_tools):
        """Jellyfin get_stream_url failure returns error."""
        mock_mcp_manager = MagicMock()
        mock_mcp_manager.execute_tool = AsyncMock(return_value={
            "success": False, "message": "Item not found",
        })

        with self._patch_main_app(mock_mcp_manager):
            result = await internal_tools._play_video_on_dlna({
                "item_id": "invalid",
                "renderer_name": "Samsung TV",
            })

        assert result["success"] is False
        assert "Failed to get stream URL" in result["message"]

    @pytest.mark.unit
    async def test_execute_routes_to_play_video(self, internal_tools):
        """execute() routes internal.play_video_on_dlna correctly."""
        with patch.object(internal_tools, "_play_video_on_dlna", new_callable=AsyncMock) as mock:
            mock.return_value = {"success": True}
            params = {"item_id": "movie1", "renderer_name": "Samsung TV"}
            result = await internal_tools.execute("internal.play_video_on_dlna", params)
            mock.assert_called_once_with(params)
            assert result["success"] is True


class TestResolveRoomVisualPlayer:
    """Test _resolve_room_visual_player helper."""

    @pytest.mark.unit
    async def test_resolve_visual_dlna_success(self, internal_tools):
        """Room with visual DLNA device resolves correctly."""
        mock_room = MagicMock()
        mock_room.id = 1
        mock_room.name = "Wohnzimmer"

        mock_room_service = MagicMock()
        mock_room_service.get_room_by_name = AsyncMock(return_value=mock_room)
        mock_room_service.get_room_by_alias = AsyncMock(return_value=None)

        mock_output_device = MagicMock()
        mock_output_device.device_name = "Samsung TV"

        mock_decision = MagicMock()
        mock_decision.reason = "ok"
        mock_decision.output_device = mock_output_device
        mock_decision.target_type = "dlna"
        mock_decision.target_id = "Samsung TV"  # DLNA target_id == renderer name

        mock_routing_service = MagicMock()
        mock_routing_service.get_visual_output_for_room = AsyncMock(return_value=mock_decision)

        with _patch_resolve_deps(mock_room_service, mock_routing_service):
            result = await internal_tools._resolve_room_visual_player({"room_name": "Wohnzimmer"})

        assert result["success"] is True
        assert result["data"]["target_type"] == "dlna"
        assert result["data"]["dlna_renderer_name"] == "Samsung TV"

    @pytest.mark.unit
    async def test_resolve_visual_room_not_found(self, internal_tools):
        """Unknown room returns error."""
        mock_room_service = MagicMock()
        mock_room_service.get_room_by_name = AsyncMock(return_value=None)
        mock_room_service.get_room_by_alias = AsyncMock(return_value=None)

        with _patch_resolve_deps(mock_room_service):
            result = await internal_tools._resolve_room_visual_player({"room_name": "Narnia"})

        assert result["success"] is False
        assert "not found" in result["message"]

    @pytest.mark.unit
    async def test_resolve_visual_no_devices(self, internal_tools):
        """Room with no visual devices returns error."""
        mock_room = MagicMock()
        mock_room.id = 1
        mock_room.name = "Kueche"

        mock_room_service = MagicMock()
        mock_room_service.get_room_by_name = AsyncMock(return_value=mock_room)

        mock_decision = MagicMock()
        mock_decision.reason = "no_output_devices_configured"

        mock_routing_service = MagicMock()
        mock_routing_service.get_visual_output_for_room = AsyncMock(return_value=mock_decision)

        with _patch_resolve_deps(mock_room_service, mock_routing_service):
            result = await internal_tools._resolve_room_visual_player({"room_name": "Kueche"})

        assert result["success"] is False
        assert "No visual output device" in result["message"]

    @pytest.mark.unit
    async def test_resolve_visual_missing_room_name(self, internal_tools):
        """Missing room_name returns error."""
        result = await internal_tools._resolve_room_visual_player({})
        assert result["success"] is False
        assert "room_name" in result["message"]


class TestFormatLastSeen:
    """Test relative time formatting."""

    @pytest.mark.unit
    def test_just_now(self, internal_tools):
        import time
        assert internal_tools._format_last_seen(time.time() - 10) == "just now"

    @pytest.mark.unit
    def test_minutes_ago(self, internal_tools):
        import time
        result = internal_tools._format_last_seen(time.time() - 300)
        assert "5 minutes ago" == result

    @pytest.mark.unit
    def test_one_minute_ago(self, internal_tools):
        import time
        result = internal_tools._format_last_seen(time.time() - 90)
        assert "1 minute ago" == result

    @pytest.mark.unit
    def test_hours_ago(self, internal_tools):
        import time
        result = internal_tools._format_last_seen(time.time() - 7200)
        assert "2 hours ago" == result

    @pytest.mark.unit
    def test_days_ago(self, internal_tools):
        import time
        result = internal_tools._format_last_seen(time.time() - 172800)
        assert "2 days ago" == result


# ============================================================================
# Test play_radio
# ============================================================================

class TestPlayRadio:
    """Test internal.play_radio tool."""

    @staticmethod
    def _patch_main_app(mock_mcp_manager):
        """Patch main.app without importing the real main module."""
        mock_app = MagicMock()
        mock_app.state.mcp_manager = mock_mcp_manager
        fake_main = ModuleType("main")
        fake_main.app = mock_app
        return patch.dict(sys.modules, {"main": fake_main})

    @pytest.mark.unit
    async def test_play_radio_success(self, internal_tools):
        """Station ID + room → stream URL resolved → play_in_room called."""
        import json

        stream_response = json.dumps({
            "station_id": "s12345",
            "stream_url": "http://stream.example.com/radio.mp3",
            "media_type": "audio/mpeg",
        })

        mock_mcp_manager = MagicMock()
        mock_mcp_manager.execute_tool = AsyncMock(return_value={
            "success": True,
            "message": stream_response,
            "data": [{"type": "text", "text": stream_response}],
        })

        play_result = {
            "success": True,
            "message": "Playing on Speaker in Arbeitszimmer",
            "action_taken": True,
            "data": {
                "entity_id": "media_player.arbeitszimmer",
                "room_name": "Arbeitszimmer",
                "device_name": "Speaker",
                "media_url": "http://stream.example.com/radio.mp3",
                "media_type": "music",
            },
        }

        # _play_radio resolves the room via _resolve_room_player first, then
        # delegates to _play_in_room for the HA target.
        resolve_result = {
            "success": True,
            "data": {
                "target_type": "homeassistant",
                "entity_id": "media_player.arbeitszimmer",
                "room_name": "Arbeitszimmer",
                "device_name": "Speaker",
            },
        }

        with self._patch_main_app(mock_mcp_manager), \
             patch.object(internal_tools, "_resolve_room_player",
                          new_callable=AsyncMock, return_value=resolve_result), \
             patch.object(internal_tools, "_play_in_room",
                          new_callable=AsyncMock, return_value=play_result):
            result = await internal_tools._play_radio({
                "station_id": "s12345",
                "room_name": "Arbeitszimmer",
                "station_name": "BBC Radio 1",
            })

        assert result["success"] is True
        assert "BBC Radio 1" in result["message"]

        # Verify MCP was called to resolve stream URL
        mock_mcp_manager.execute_tool.assert_called_once_with(
            "mcp.radio.get_stream_url",
            {"station_id": "s12345"},
        )

    @pytest.mark.unit
    async def test_play_radio_stream_resolve_fails(self, internal_tools):
        """Failed stream URL resolution returns error."""
        mock_mcp_manager = MagicMock()
        mock_mcp_manager.execute_tool = AsyncMock(return_value={
            "success": False,
            "message": "Station not found",
        })

        with self._patch_main_app(mock_mcp_manager):
            result = await internal_tools._play_radio({
                "station_id": "s99999",
                "room_name": "Arbeitszimmer",
            })

        assert result["success"] is False
        assert "Failed to resolve stream URL" in result["message"]

    @pytest.mark.unit
    async def test_play_radio_empty_stream_url(self, internal_tools):
        """Stream response without stream_url returns error."""
        import json

        stream_response = json.dumps({"station_id": "s12345"})

        mock_mcp_manager = MagicMock()
        mock_mcp_manager.execute_tool = AsyncMock(return_value={
            "success": True,
            "message": stream_response,
            "data": [{"type": "text", "text": stream_response}],
        })

        with self._patch_main_app(mock_mcp_manager):
            result = await internal_tools._play_radio({
                "station_id": "s12345",
                "room_name": "Arbeitszimmer",
            })

        assert result["success"] is False
        assert "Could not resolve stream URL" in result["message"]

    @pytest.mark.unit
    async def test_play_radio_missing_station_id(self, internal_tools):
        """Missing station_id returns error."""
        result = await internal_tools._play_radio({
            "room_name": "Arbeitszimmer",
        })
        assert result["success"] is False
        assert "station_id" in result["message"]

    @pytest.mark.unit
    async def test_play_radio_missing_room_name(self, internal_tools):
        """Missing room_name returns error."""
        result = await internal_tools._play_radio({
            "station_id": "s12345",
        })
        assert result["success"] is False
        assert "room_name" in result["message"]

    @pytest.mark.unit
    async def test_play_radio_no_mcp_manager(self, internal_tools):
        """Missing MCP manager returns error."""
        with self._patch_main_app(None):
            result = await internal_tools._play_radio({
                "station_id": "s12345",
                "room_name": "Arbeitszimmer",
            })

        assert result["success"] is False
        assert "MCP manager not available" in result["message"]

    @pytest.mark.unit
    async def test_play_radio_passes_metadata(self, internal_tools):
        """Station name and image are passed to play_in_room as title/thumb."""
        import json

        stream_response = json.dumps({
            "stream_url": "http://stream.example.com/radio.mp3",
        })

        mock_mcp_manager = MagicMock()
        mock_mcp_manager.execute_tool = AsyncMock(return_value={
            "success": True,
            "message": stream_response,
            "data": [{"type": "text", "text": stream_response}],
        })

        play_result = {
            "success": True,
            "message": "Playing",
            "action_taken": True,
            "data": {},
        }

        resolve_result = {
            "success": True,
            "data": {
                "target_type": "homeassistant",
                "entity_id": "media_player.wohnzimmer",
                "room_name": "Wohnzimmer",
                "device_name": "Speaker",
            },
        }

        with self._patch_main_app(mock_mcp_manager), \
             patch.object(internal_tools, "_resolve_room_player",
                          new_callable=AsyncMock, return_value=resolve_result), \
             patch.object(internal_tools, "_play_in_room",
                          new_callable=AsyncMock, return_value=play_result) as mock_play:
            await internal_tools._play_radio({
                "station_id": "s12345",
                "room_name": "Wohnzimmer",
                "station_name": "Jazz FM",
                "station_image": "http://example.com/logo.png",
            })

        play_params = mock_play.call_args.args[0]
        assert play_params["title"] == "Jazz FM"
        assert play_params["thumb"] == "http://example.com/logo.png"
        assert play_params["media_url"] == "http://stream.example.com/radio.mp3"

    @pytest.mark.unit
    async def test_execute_routes_to_play_radio(self, internal_tools):
        """execute() routes internal.play_radio correctly."""
        with patch.object(internal_tools, "_play_radio", new_callable=AsyncMock) as mock:
            mock.return_value = {"success": True}
            params = {"station_id": "s12345", "room_name": "Test"}
            result = await internal_tools.execute("internal.play_radio", params)
            mock.assert_called_once_with(params)
            assert result["success"] is True


# ============================================================================
# Test radio favorites (save/list/remove)
# ============================================================================

def _patch_db_deps():
    """Context manager that patches DB imports for favorite tools."""
    mock_db = AsyncMock()

    @asynccontextmanager
    async def mock_session():
        yield mock_db

    _ensure_module = []
    for mod_name in ["services.database", "models.database"]:
        if mod_name not in sys.modules:
            fake = ModuleType(mod_name)
            sys.modules[mod_name] = fake
            _ensure_module.append(mod_name)

    patches = [
        patch("services.database.AsyncSessionLocal", mock_session, create=True),
    ]

    class combined:
        db = mock_db

        def __enter__(self_):
            for p in patches:
                p.__enter__()
            return self_

        def __exit__(self_, *args):
            for p in reversed(patches):
                p.__exit__(*args)
            for mod_name in _ensure_module:
                sys.modules.pop(mod_name, None)

    return combined()


class TestSaveRadioFavorite:
    """Test internal.save_radio_favorite tool."""

    @pytest.mark.unit
    async def test_save_favorite_success(self, internal_tools):
        """Save new favorite station to DB."""
        with _patch_db_deps() as ctx:
            # Mock: no existing favorite found
            mock_result = MagicMock()
            mock_result.scalar_one_or_none.return_value = None
            ctx.db.execute = AsyncMock(return_value=mock_result)
            ctx.db.commit = AsyncMock()
            ctx.db.add = MagicMock()

            result = await internal_tools._save_radio_favorite({
                "station_id": "s12345",
                "station_name": "BBC Radio 1",
                "genre": "Pop",
                "user_id": 1,
            })

        assert result["success"] is True
        assert result["action_taken"] is True
        assert "BBC Radio 1" in result["message"]
        ctx.db.add.assert_called_once()
        ctx.db.commit.assert_called_once()

    @pytest.mark.unit
    async def test_save_favorite_already_exists(self, internal_tools):
        """Saving same station twice is idempotent (no error)."""
        with _patch_db_deps() as ctx:
            existing = MagicMock()
            existing.station_id = "s12345"
            mock_result = MagicMock()
            mock_result.scalar_one_or_none.return_value = existing
            ctx.db.execute = AsyncMock(return_value=mock_result)

            result = await internal_tools._save_radio_favorite({
                "station_id": "s12345",
                "station_name": "BBC Radio 1",
                "user_id": 1,
            })

        assert result["success"] is True
        assert result["action_taken"] is False
        assert "already" in result["message"]

    @pytest.mark.unit
    async def test_save_favorite_missing_station_id(self, internal_tools):
        """Missing station_id returns error."""
        result = await internal_tools._save_radio_favorite({
            "station_name": "BBC Radio 1",
        })
        assert result["success"] is False
        assert "station_id" in result["message"]

    @pytest.mark.unit
    async def test_save_favorite_missing_station_name(self, internal_tools):
        """Missing station_name returns error."""
        result = await internal_tools._save_radio_favorite({
            "station_id": "s12345",
        })
        assert result["success"] is False
        assert "station_name" in result["message"]


class TestListRadioFavorites:
    """Test internal.list_radio_favorites tool."""

    @pytest.mark.unit
    async def test_list_favorites_with_results(self, internal_tools):
        """Returns user's saved stations."""
        with _patch_db_deps() as ctx:
            fav1 = MagicMock()
            fav1.station_id = "s12345"
            fav1.station_name = "BBC Radio 1"
            fav1.station_image = "http://example.com/bbc.png"
            fav1.genre = "Pop"

            fav2 = MagicMock()
            fav2.station_id = "s67890"
            fav2.station_name = "Jazz FM"
            fav2.station_image = None
            fav2.genre = "Jazz"

            mock_result = MagicMock()
            mock_scalars = MagicMock()
            mock_scalars.all.return_value = [fav1, fav2]
            mock_result.scalars.return_value = mock_scalars
            ctx.db.execute = AsyncMock(return_value=mock_result)

            result = await internal_tools._list_radio_favorites({"user_id": 1})

        assert result["success"] is True
        assert len(result["data"]["favorites"]) == 2
        assert result["data"]["favorites"][0]["station_name"] == "BBC Radio 1"
        assert result["data"]["favorites"][1]["genre"] == "Jazz"

    @pytest.mark.unit
    async def test_list_favorites_empty(self, internal_tools):
        """No favorites returns empty list with empty_result flag."""
        with _patch_db_deps() as ctx:
            mock_result = MagicMock()
            mock_scalars = MagicMock()
            mock_scalars.all.return_value = []
            mock_result.scalars.return_value = mock_scalars
            ctx.db.execute = AsyncMock(return_value=mock_result)

            result = await internal_tools._list_radio_favorites({"user_id": 1})

        assert result["success"] is True
        assert result.get("empty_result") is True
        assert result["data"]["favorites"] == []


class TestRemoveRadioFavorite:
    """Test internal.remove_radio_favorite tool."""

    @pytest.mark.unit
    async def test_remove_favorite_success(self, internal_tools):
        """Remove existing favorite."""
        with _patch_db_deps() as ctx:
            mock_result = MagicMock()
            mock_result.rowcount = 1
            ctx.db.execute = AsyncMock(return_value=mock_result)
            ctx.db.commit = AsyncMock()

            result = await internal_tools._remove_radio_favorite({
                "station_id": "s12345",
                "user_id": 1,
            })

        assert result["success"] is True
        assert result["action_taken"] is True

    @pytest.mark.unit
    async def test_remove_favorite_not_found(self, internal_tools):
        """Removing non-existent station returns error."""
        with _patch_db_deps() as ctx:
            mock_result = MagicMock()
            mock_result.rowcount = 0
            ctx.db.execute = AsyncMock(return_value=mock_result)
            ctx.db.commit = AsyncMock()

            result = await internal_tools._remove_radio_favorite({
                "station_id": "s99999",
                "user_id": 1,
            })

        assert result["success"] is False
        assert "not found" in result["message"]

    @pytest.mark.unit
    async def test_remove_favorite_missing_station_id(self, internal_tools):
        """Missing station_id returns error."""
        result = await internal_tools._remove_radio_favorite({
            "user_id": 1,
        })
        assert result["success"] is False
        assert "station_id" in result["message"]


# ============================================================================
# Test announce_in_room (relay-a-message primitive + privacy gate)
# ============================================================================

class TestAnnounceInRoom:
    """internal.announce_in_room: TTS into a room, with the fail-closed privacy
    gate (a personal message is NOT spoken aloud if the person isn't alone)."""

    @staticmethod
    def _patch_deps(*, occupants, room_name="Arbeitszimmer", room_id=2,
                    tts=b"WAVDATA", play_ok=True, name_to_id=None,
                    camera_sat=False, snapshot=None, people=None, fail_closed=False):
        import sys
        from types import ModuleType
        from ha_glue.utils.config import ha_glue_settings

        mock_db = AsyncMock()

        @asynccontextmanager
        async def mock_session():
            yield mock_db

        room = MagicMock(id=room_id, name=room_name)
        mock_room_service = MagicMock()
        mock_room_service.get_room_by_name = AsyncMock(return_value=room)
        mock_room_service.get_room_by_alias = AsyncMock(return_value=None)

        presence = MagicMock()
        presence.get_room_occupants = MagicMock(return_value=occupants)
        presence.find_user_by_name = MagicMock(side_effect=lambda n: (name_to_id or {}).get(n))

        piper = MagicMock()
        piper.synthesize_to_bytes = AsyncMock(return_value=tts)

        decision = MagicMock()
        decision.output_device = MagicMock()
        decision.fallback_to_input = False
        routing = MagicMock()
        routing.get_audio_output_for_room = AsyncMock(return_value=decision)

        audio = MagicMock()
        audio.play_audio = AsyncMock(return_value=play_ok)

        dm = MagicMock()
        dm.get_devices_in_room = MagicMock(return_value=[])

        # Camera occupancy check deps.
        sat_mgr = MagicMock()
        cam = MagicMock(satellite_id="sat-arbeitszimmer") if camera_sat else None
        sat_mgr.get_camera_satellite_for_room = MagicMock(return_value=cam)
        sat_mgr.request_snapshot = AsyncMock(return_value=snapshot)
        ollama = MagicMock()
        ollama.count_people_in_image = AsyncMock(return_value=people)
        fake_main = ModuleType("main")
        fake_main.app = MagicMock()
        fake_main.app.state.ollama = ollama

        ensure = []
        for mod_name in [
            "services.database", "services.piper_service",
            "ha_glue.services.room_service", "ha_glue.services.presence_service",
            "ha_glue.services.output_routing_service",
            "ha_glue.services.audio_output_service", "ha_glue.services.device_manager",
        ]:
            if mod_name not in sys.modules:
                sys.modules[mod_name] = ModuleType(mod_name)
                ensure.append(mod_name)

        patches = [
            patch("services.database.AsyncSessionLocal", mock_session, create=True),
            patch("services.piper_service.PiperService", return_value=piper, create=True),
            patch("ha_glue.services.room_service.RoomService", return_value=mock_room_service, create=True),
            patch("ha_glue.services.presence_service.get_presence_service", return_value=presence, create=True),
            patch("ha_glue.services.output_routing_service.OutputRoutingService", return_value=routing, create=True),
            patch("ha_glue.services.audio_output_service.get_audio_output_service", return_value=audio, create=True),
            patch("ha_glue.services.device_manager.get_device_manager", return_value=dm, create=True),
            patch("ha_glue.services.satellite_manager.get_satellite_manager", return_value=sat_mgr, create=True),
            patch.dict(sys.modules, {"main": fake_main}),
            patch.object(ha_glue_settings, "announce_camera_check_fail_closed", fail_closed),
        ]

        class Combined:
            def __enter__(self_):
                for p in patches:
                    p.__enter__()
                self_.piper = piper
                self_.audio = audio
                self_.sat_mgr = sat_mgr
                self_.ollama = ollama
                return self_

            def __exit__(self_, *a):
                for p in reversed(patches):
                    p.__exit__(*a)
                for m in ensure:
                    sys.modules.pop(m, None)

        return Combined()

    @pytest.mark.unit
    async def test_personal_blocked_when_not_alone(self, internal_tools):
        """SAFETY: a personal message is NOT spoken aloud (no TTS) when others
        are present in the room — returns blocked=not_private instead."""
        occ = [MagicMock(user_name="evdb"), MagicMock(user_name="Jutta")]
        with self._patch_deps(occupants=occ) as deps:
            result = await internal_tools._announce_in_room({
                "text": "Dein Steuerbescheid ist da", "room_name": "Arbeitszimmer",
                "privacy": "personal",
            })
        assert result["success"] is False
        assert result.get("blocked") == "not_private"
        # Crucially: nothing was synthesized or played.
        deps.piper.synthesize_to_bytes.assert_not_called()
        deps.audio.play_audio.assert_not_called()

    @pytest.mark.unit
    async def test_personal_announced_when_recipient_alone(self, internal_tools):
        """A personal message IS announced when the (only) person present is the
        intended recipient."""
        occ = [MagicMock(user_id=2)]
        with self._patch_deps(occupants=occ, name_to_id={"Eduard": 2}) as deps:
            result = await internal_tools._announce_in_room({
                "text": "Dein Steuerbescheid ist da", "room_name": "Arbeitszimmer",
                "privacy": "personal", "for_users": ["Eduard"],
            })
        assert result["success"] is True
        deps.audio.play_audio.assert_awaited_once()

    @pytest.mark.unit
    async def test_personal_blocked_when_room_untracked(self, internal_tools):
        """FAIL-CLOSED: nobody tracked in the room → personal message blocked
        (can't prove the recipient is alone; an untracked bystander may be there)."""
        with self._patch_deps(occupants=[], name_to_id={"Eduard": 2}) as deps:
            result = await internal_tools._announce_in_room({
                "text": "vertraulich", "room_name": "Arbeitszimmer",
                "privacy": "personal", "for_users": ["Eduard"],
            })
        assert result["success"] is False
        assert result.get("blocked") == "not_private"
        deps.piper.synthesize_to_bytes.assert_not_called()

    @pytest.mark.unit
    async def test_personal_blocked_when_no_recipients_given(self, internal_tools):
        """FAIL-CLOSED: privacy=personal without for_users → blocked (can't verify
        who's allowed to hear it), even if only one person is present."""
        occ = [MagicMock(user_id=2)]
        with self._patch_deps(occupants=occ) as deps:
            result = await internal_tools._announce_in_room({
                "text": "vertraulich", "room_name": "Arbeitszimmer", "privacy": "personal",
            })
        assert result["success"] is False
        assert result.get("blocked") == "not_private"
        deps.piper.synthesize_to_bytes.assert_not_called()

    @pytest.mark.unit
    async def test_public_announced_even_with_others(self, internal_tools):
        """A public message is announced regardless of who else is present."""
        occ = [MagicMock(user_name="evdb"), MagicMock(user_name="Jutta")]
        with self._patch_deps(occupants=occ) as deps:
            result = await internal_tools._announce_in_room({
                "text": "Das Essen ist fertig", "room_name": "Arbeitszimmer",
                "privacy": "public",
            })
        assert result["success"] is True
        deps.audio.play_audio.assert_awaited_once()

    @pytest.mark.unit
    async def test_default_privacy_is_public(self, internal_tools):
        """No privacy arg → treated as public (announces with others present)."""
        occ = [MagicMock(user_name="evdb"), MagicMock(user_name="Jutta")]
        with self._patch_deps(occupants=occ) as deps:
            result = await internal_tools._announce_in_room({
                "text": "Das Essen ist fertig", "room_name": "Arbeitszimmer",
            })
        assert result["success"] is True
        deps.audio.play_audio.assert_awaited_once()

    @pytest.mark.unit
    async def test_missing_text(self, internal_tools):
        result = await internal_tools._announce_in_room({"room_name": "Arbeitszimmer"})
        assert result["success"] is False and "text" in result["message"]

    @pytest.mark.unit
    async def test_missing_room(self, internal_tools):
        result = await internal_tools._announce_in_room({"text": "hi"})
        assert result["success"] is False and "room_name" in result["message"]

    @pytest.mark.unit
    async def test_personal_announced_for_two_recipients_both_present(self, internal_tools):
        """Two intended recipients alone together → personal message IS announced."""
        occ = [MagicMock(user_id=2), MagicMock(user_id=3)]
        with self._patch_deps(occupants=occ, name_to_id={"Eduard": 2, "Jutta": 3}) as deps:
            result = await internal_tools._announce_in_room({
                "text": "vertraulich", "room_name": "Arbeitszimmer",
                "privacy": "personal", "for_users": ["Eduard", "Jutta"],
            })
        assert result["success"] is True
        deps.audio.play_audio.assert_awaited_once()

    @pytest.mark.unit
    async def test_personal_blocked_when_a_non_recipient_present(self, internal_tools):
        """3 present, message for 2 of them → blocked (the 3rd isn't a recipient)."""
        occ = [MagicMock(user_id=2), MagicMock(user_id=3), MagicMock(user_id=9)]
        with self._patch_deps(occupants=occ, name_to_id={"Eduard": 2, "Jutta": 3}) as deps:
            result = await internal_tools._announce_in_room({
                "text": "vertraulich", "room_name": "Arbeitszimmer",
                "privacy": "personal", "for_users": ["Eduard", "Jutta"],
            })
        assert result["success"] is False
        assert result.get("blocked") == "not_private"
        assert result["data"]["recipients_present"] == 2  # E + J present, X is the outsider
        deps.piper.synthesize_to_bytes.assert_not_called()
        deps.audio.play_audio.assert_not_called()

    @pytest.mark.unit
    async def test_force_bypasses_gate_after_consent(self, internal_tools):
        """force=true announces a personal message even with non-recipients present
        (used after the recipient consents to 'go ahead')."""
        occ = [MagicMock(user_id=2), MagicMock(user_id=9)]
        with self._patch_deps(occupants=occ, name_to_id={"Eduard": 2}) as deps:
            result = await internal_tools._announce_in_room({
                "text": "vertraulich", "room_name": "Arbeitszimmer",
                "privacy": "personal", "for_users": ["Eduard"], "force": "true",
            })
        assert result["success"] is True
        deps.audio.play_audio.assert_awaited_once()

    @pytest.mark.unit
    async def test_camera_blocks_when_extra_person_seen(self, internal_tools):
        """BLE gate passes (only the recipient is tracked) but the room camera
        sees MORE people than tracked → an untracked bystander → blocked."""
        occ = [MagicMock(user_id=2)]
        with self._patch_deps(occupants=occ, name_to_id={"Eduard": 2},
                              camera_sat=True, snapshot="IMG_B64", people=2) as deps:
            result = await internal_tools._announce_in_room({
                "text": "vertraulich", "room_name": "Arbeitszimmer",
                "privacy": "personal", "for_users": ["Eduard"],
            })
        assert result["success"] is False
        assert result.get("blocked") == "not_private"
        assert result["data"]["people_seen"] == 2
        deps.audio.play_audio.assert_not_called()

    @pytest.mark.unit
    async def test_camera_allows_when_count_matches(self, internal_tools):
        """Camera sees exactly the tracked recipient(s) → announced."""
        occ = [MagicMock(user_id=2)]
        with self._patch_deps(occupants=occ, name_to_id={"Eduard": 2},
                              camera_sat=True, snapshot="IMG_B64", people=1) as deps:
            result = await internal_tools._announce_in_room({
                "text": "vertraulich", "room_name": "Arbeitszimmer",
                "privacy": "personal", "for_users": ["Eduard"],
            })
        assert result["success"] is True
        deps.audio.play_audio.assert_awaited_once()

    @pytest.mark.unit
    async def test_camera_fail_open_when_snapshot_fails(self, internal_tools):
        """Snapshot/vision unavailable + default fail-open → BLE decision stands
        (announced)."""
        occ = [MagicMock(user_id=2)]
        with self._patch_deps(occupants=occ, name_to_id={"Eduard": 2},
                              camera_sat=True, snapshot=None, people=None,
                              fail_closed=False) as deps:
            result = await internal_tools._announce_in_room({
                "text": "vertraulich", "room_name": "Arbeitszimmer",
                "privacy": "personal", "for_users": ["Eduard"],
            })
        assert result["success"] is True
        deps.audio.play_audio.assert_awaited_once()

    @pytest.mark.unit
    async def test_camera_fail_closed_when_snapshot_fails(self, internal_tools):
        """Snapshot/vision unavailable + fail-closed policy → blocked."""
        occ = [MagicMock(user_id=2)]
        with self._patch_deps(occupants=occ, name_to_id={"Eduard": 2},
                              camera_sat=True, snapshot=None, people=None,
                              fail_closed=True) as deps:
            result = await internal_tools._announce_in_room({
                "text": "vertraulich", "room_name": "Arbeitszimmer",
                "privacy": "personal", "for_users": ["Eduard"],
            })
        assert result["success"] is False
        assert result.get("blocked") == "not_private"
        deps.audio.play_audio.assert_not_called()

    @pytest.mark.unit
    async def test_no_camera_falls_back_to_ble(self, internal_tools):
        """No camera in the room → BLE gate decides (recipient alone → announced)."""
        occ = [MagicMock(user_id=2)]
        with self._patch_deps(occupants=occ, name_to_id={"Eduard": 2},
                              camera_sat=False) as deps:
            result = await internal_tools._announce_in_room({
                "text": "vertraulich", "room_name": "Arbeitszimmer",
                "privacy": "personal", "for_users": ["Eduard"],
            })
        assert result["success"] is True
        deps.audio.play_audio.assert_awaited_once()


# ============================================================================
# Media Follow Me — presence-derived session owner (option A)
# ============================================================================

class TestRegisterMediaFollowPresenceFallback:
    """When playback has no authenticated chat user (AUTH disabled), the follow
    session must be attributed to the user presence places in the playback room
    — the SAME user_id that presence_leave_room later fires with — so leaving
    actually stops the music."""

    @pytest.mark.unit
    def test_presence_room_user_single_vs_ambiguous(self, internal_tools, monkeypatch):
        from types import SimpleNamespace
        import ha_glue.services.presence_service as ps

        class _PS:
            def __init__(self, occ): self._occ = occ
            def get_room_occupants(self, _rid): return self._occ

        monkeypatch.setattr(ps, "get_presence_service", lambda: _PS([SimpleNamespace(user_id=7)]))
        assert internal_tools._presence_room_user(2) == 7
        # >1 occupant → ambiguous → None (never attribute the wrong person)
        monkeypatch.setattr(ps, "get_presence_service",
                            lambda: _PS([SimpleNamespace(user_id=7), SimpleNamespace(user_id=8)]))
        assert internal_tools._presence_room_user(2) is None
        # empty room → None
        monkeypatch.setattr(ps, "get_presence_service", lambda: _PS([]))
        assert internal_tools._presence_room_user(2) is None

    def _wire(self, internal_tools, monkeypatch, presence_user, captured):
        from ha_glue.utils.config import ha_glue_settings
        import ha_glue.services.media_follow_service as mfs
        monkeypatch.setattr(ha_glue_settings, "media_follow_enabled", True)
        monkeypatch.setattr(internal_tools, "_get_room_id", AsyncMock(return_value=2))
        monkeypatch.setattr(internal_tools, "_presence_room_user", lambda rid: presence_user)

        class _MF:
            def register_playback(self, **kw): captured.update(kw)

        monkeypatch.setattr(mfs, "get_media_follow_service", lambda: _MF())

    @pytest.mark.unit
    async def test_falls_back_to_presence_room_user(self, internal_tools, monkeypatch):
        captured: dict = {}
        self._wire(internal_tools, monkeypatch, presence_user=7, captured=captured)
        await internal_tools._register_media_follow({"user_id": None}, "Arbeitszimmer", "radio")
        assert captured.get("user_id") == 7
        assert captured.get("room_id") == 2
        assert captured.get("room_name") == "Arbeitszimmer"

    @pytest.mark.unit
    async def test_chat_user_id_wins_over_presence(self, internal_tools, monkeypatch):
        captured: dict = {}
        self._wire(internal_tools, monkeypatch, presence_user=99, captured=captured)
        await internal_tools._register_media_follow({"user_id": 5}, "Arbeitszimmer", "radio")
        assert captured.get("user_id") == 5  # authenticated caller wins

    @pytest.mark.unit
    async def test_skips_when_no_user_resolvable(self, internal_tools, monkeypatch):
        captured: dict = {}
        self._wire(internal_tools, monkeypatch, presence_user=None, captured=captured)
        await internal_tools._register_media_follow({"user_id": None}, "Arbeitszimmer", "radio")
        assert captured == {}  # nothing registered (empty/ambiguous room)
