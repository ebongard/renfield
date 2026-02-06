"""Tests for AudioOutputService.

Tests TTS audio delivery to Renfield devices (WebSocket) and
Home Assistant media players (HA service calls), caching, and cleanup.
"""
import sys
import time
from unittest.mock import MagicMock

# Pre-mock modules not available in test environment
_missing_stubs = [
    "asyncpg", "whisper", "piper", "piper.voice", "speechbrain",
    "speechbrain.inference", "speechbrain.inference.speaker",
    "openwakeword", "openwakeword.model",
]
for _mod in _missing_stubs:
    if _mod not in sys.modules:
        sys.modules[_mod] = MagicMock()

from unittest.mock import AsyncMock, patch

import pytest

from services.audio_output_service import AudioOutputService, get_audio_output_service


def _make_output_device(
    *,
    renfield_device_id=None,
    ha_entity_id=None,
    tts_volume=0.5,
):
    """Create a mock RoomOutputDevice."""
    dev = MagicMock()
    dev.renfield_device_id = renfield_device_id
    dev.ha_entity_id = ha_entity_id
    dev.tts_volume = tts_volume
    dev.is_renfield_device = renfield_device_id is not None
    return dev


@pytest.fixture
def mock_ha_client():
    return AsyncMock()


@pytest.fixture
def mock_settings():
    s = MagicMock()
    s.advertise_host = "renfield.local"
    s.advertise_port = 8000
    s.backend_internal_url = "http://backend:8000"
    return s


@pytest.fixture
def service(mock_ha_client, mock_settings, tmp_path):
    with patch("services.audio_output_service.HomeAssistantClient", return_value=mock_ha_client), \
         patch("services.audio_output_service.settings", mock_settings):
        svc = AudioOutputService()
        # Use tmp_path for cache to avoid filesystem side effects
        svc.TTS_CACHE_DIR = tmp_path
    return svc


# ============================================================================
# Play on Renfield Device Tests
# ============================================================================

@pytest.mark.unit
class TestPlayOnRenfieldDevice:

    @pytest.mark.asyncio
    async def test_play_on_connected_device(self, service):
        """Audio is sent to a connected Renfield device."""
        mock_device = MagicMock()
        mock_device.capabilities.has_speaker = True

        mock_dm = MagicMock()
        mock_dm.get_device.return_value = mock_device
        mock_dm.send_tts_audio = AsyncMock()

        with patch("services.audio_output_service.get_device_manager", return_value=mock_dm):
            result = await service._play_on_renfield_device(
                audio_bytes=b"fake-audio",
                device_id="sat-kitchen",
                session_id="sess-1",
            )

        assert result is True
        mock_dm.send_tts_audio.assert_called_once_with(
            session_id="sess-1",
            audio_bytes=b"fake-audio",
            is_final=True,
        )

    @pytest.mark.asyncio
    async def test_play_on_disconnected_device(self, service):
        """Returns False when device is not connected."""
        mock_dm = MagicMock()
        mock_dm.get_device.return_value = None

        with patch("services.audio_output_service.get_device_manager", return_value=mock_dm):
            result = await service._play_on_renfield_device(
                audio_bytes=b"audio", device_id="sat-gone", session_id="sess-1"
            )

        assert result is False

    @pytest.mark.asyncio
    async def test_play_on_device_without_speaker(self, service):
        """Returns False when device has no speaker."""
        mock_device = MagicMock()
        mock_device.capabilities.has_speaker = False

        mock_dm = MagicMock()
        mock_dm.get_device.return_value = mock_device

        with patch("services.audio_output_service.get_device_manager", return_value=mock_dm):
            result = await service._play_on_renfield_device(
                audio_bytes=b"audio", device_id="sat-nospeaker", session_id="sess-1"
            )

        assert result is False

    @pytest.mark.asyncio
    async def test_play_on_device_send_error(self, service):
        """Returns False when WebSocket send raises."""
        mock_device = MagicMock()
        mock_device.capabilities.has_speaker = True

        mock_dm = MagicMock()
        mock_dm.get_device.return_value = mock_device
        mock_dm.send_tts_audio = AsyncMock(side_effect=Exception("WS closed"))

        with patch("services.audio_output_service.get_device_manager", return_value=mock_dm):
            result = await service._play_on_renfield_device(
                audio_bytes=b"audio", device_id="sat-err", session_id="sess-1"
            )

        assert result is False


# ============================================================================
# Play on HA Media Player Tests
# ============================================================================

@pytest.mark.unit
class TestPlayOnHAMediaPlayer:

    @pytest.mark.asyncio
    async def test_play_on_ha_player_success(self, service, mock_ha_client):
        """Successful playback on HA media player."""
        mock_ha_client.get_state = AsyncMock(
            return_value={"attributes": {"volume_level": 0.3}}
        )
        mock_ha_client.call_service = AsyncMock(return_value=True)

        device = _make_output_device(ha_entity_id="media_player.living", tts_volume=0.5)
        result = await service.play_audio(
            audio_bytes=b"wav-data",
            output_device=device,
            session_id="sess-1",
        )

        assert result is True
        # Volume was changed, so call_service should be called for volume_set + play_media
        assert mock_ha_client.call_service.call_count >= 1

    @pytest.mark.asyncio
    async def test_play_on_ha_player_no_volume_change(self, service, mock_ha_client):
        """No volume_set call when volume already matches tts_volume."""
        mock_ha_client.get_state = AsyncMock(
            return_value={"attributes": {"volume_level": 0.5}}
        )
        mock_ha_client.call_service = AsyncMock(return_value=True)

        result = await service._play_on_ha_media_player(
            audio_bytes=b"wav-data",
            entity_id="media_player.living",
            tts_volume=0.5,
            session_id="sess-1",
        )

        assert result is True
        # Only play_media should be called, not volume_set
        calls = mock_ha_client.call_service.call_args_list
        services_called = [c.kwargs.get("service") or c[1].get("service", c[0][1] if len(c[0]) > 1 else None) for c in calls]
        assert "volume_set" not in services_called

    @pytest.mark.asyncio
    async def test_play_on_ha_player_null_volume(self, service, mock_ha_client):
        """No volume adjustment when tts_volume is None."""
        mock_ha_client.call_service = AsyncMock(return_value=True)

        result = await service._play_on_ha_media_player(
            audio_bytes=b"wav-data",
            entity_id="media_player.living",
            tts_volume=None,
            session_id="sess-1",
        )

        assert result is True
        # get_state should not be called when tts_volume is None
        mock_ha_client.get_state.assert_not_called()

    @pytest.mark.asyncio
    async def test_play_on_ha_player_failure(self, service, mock_ha_client):
        """Returns False when HA service call fails."""
        mock_ha_client.call_service = AsyncMock(side_effect=Exception("HA unreachable"))

        result = await service._play_on_ha_media_player(
            audio_bytes=b"wav-data",
            entity_id="media_player.dead",
            tts_volume=None,
            session_id="sess-1",
        )

        assert result is False


# ============================================================================
# play_audio Dispatch Tests
# ============================================================================

@pytest.mark.unit
class TestPlayAudioDispatch:

    @pytest.mark.asyncio
    async def test_dispatches_to_renfield(self, service):
        """play_audio routes to _play_on_renfield_device for Renfield devices."""
        service._play_on_renfield_device = AsyncMock(return_value=True)
        device = _make_output_device(renfield_device_id="sat-1")

        result = await service.play_audio(b"audio", device, "sess-1")

        assert result is True
        service._play_on_renfield_device.assert_called_once()

    @pytest.mark.asyncio
    async def test_dispatches_to_ha(self, service):
        """play_audio routes to _play_on_ha_media_player for HA devices."""
        service._play_on_ha_media_player = AsyncMock(return_value=True)
        device = _make_output_device(ha_entity_id="media_player.test")

        result = await service.play_audio(b"audio", device, "sess-1")

        assert result is True
        service._play_on_ha_media_player.assert_called_once()


# ============================================================================
# Cache Tests
# ============================================================================

@pytest.mark.unit
class TestTTSCache:

    def test_get_cached_audio_existing_file(self, service, tmp_path):
        """get_cached_audio returns bytes for existing file."""
        audio_id = "tts_test_abc123"
        audio_file = tmp_path / f"{audio_id}.wav"
        audio_file.write_bytes(b"cached-wav-data")

        result = service.get_cached_audio(audio_id)

        assert result == b"cached-wav-data"

    def test_get_cached_audio_missing_file(self, service):
        """get_cached_audio returns None for missing file."""
        result = service.get_cached_audio("nonexistent_id")
        assert result is None

    @pytest.mark.asyncio
    async def test_cleanup_removes_old_files(self, service, tmp_path):
        """Old cache files are removed during cleanup."""
        old_file = tmp_path / "tts_old_file.wav"
        old_file.write_bytes(b"old-data")

        # Make file appear old by setting last_cleanup to 0 and file mtime far in past
        import os
        old_time = time.time() - 999
        os.utime(old_file, (old_time, old_time))

        service._last_cleanup = 0
        service.CACHE_CLEANUP_INTERVAL = 0  # Always run
        service.CACHE_MAX_AGE = 60  # 1 minute

        await service._cleanup_old_cache_files()

        assert not old_file.exists()

    @pytest.mark.asyncio
    async def test_cleanup_skipped_if_recent(self, service, tmp_path):
        """Cleanup is skipped if run recently."""
        old_file = tmp_path / "tts_should_remain.wav"
        old_file.write_bytes(b"data")

        import os
        old_time = time.time() - 999
        os.utime(old_file, (old_time, old_time))

        service._last_cleanup = time.time()  # Just ran
        service.CACHE_CLEANUP_INTERVAL = 9999

        await service._cleanup_old_cache_files()

        assert old_file.exists()  # Should NOT have been cleaned up


# ============================================================================
# Backend URL Tests
# ============================================================================

@pytest.mark.unit
class TestBackendURL:

    def test_url_with_advertise_host(self):
        """Uses advertise_host when set."""
        mock_s = MagicMock()
        mock_s.advertise_host = "renfield.local"
        mock_s.advertise_port = 8000

        with patch("services.audio_output_service.settings", mock_s), \
             patch("services.audio_output_service.HomeAssistantClient"):
            svc = AudioOutputService()
            url = svc._get_backend_url()

        assert url == "http://renfield.local:8000"

    def test_url_falls_back_to_internal(self):
        """Falls back to backend_internal_url when advertise_host is None."""
        mock_s = MagicMock()
        mock_s.advertise_host = None
        mock_s.backend_internal_url = "http://backend:8000"

        with patch("services.audio_output_service.settings", mock_s), \
             patch("services.audio_output_service.HomeAssistantClient"):
            svc = AudioOutputService()
            url = svc._get_backend_url()

        assert url == "http://backend:8000"


# ============================================================================
# Singleton Tests
# ============================================================================

@pytest.mark.unit
class TestSingleton:

    def test_get_audio_output_service_returns_instance(self):
        """get_audio_output_service returns an AudioOutputService."""
        import services.audio_output_service as mod
        # Reset singleton
        mod._audio_output_service = None

        with patch("services.audio_output_service.HomeAssistantClient"), \
             patch("services.audio_output_service.settings") as mock_s:
            mock_s.advertise_host = None
            mock_s.backend_internal_url = "http://backend:8000"
            svc = get_audio_output_service()

        assert isinstance(svc, AudioOutputService)
        # Reset to avoid leaking state
        mod._audio_output_service = None

    def test_get_audio_output_service_returns_same_instance(self):
        """Repeated calls return the same singleton instance."""
        import services.audio_output_service as mod
        mod._audio_output_service = None

        with patch("services.audio_output_service.HomeAssistantClient"), \
             patch("services.audio_output_service.settings") as mock_s:
            mock_s.advertise_host = None
            mock_s.backend_internal_url = "http://backend:8000"
            svc1 = get_audio_output_service()
            svc2 = get_audio_output_service()

        assert svc1 is svc2
        mod._audio_output_service = None
