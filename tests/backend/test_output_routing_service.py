"""Tests for OutputRoutingService.

Tests routing logic, device availability checks, priority ordering,
and fallback behavior.
"""
import sys
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

from services.output_routing_service import (
    DeviceAvailability,
    OutputDecision,
    OutputRoutingService,
)


def _make_output_device(
    *,
    renfield_device_id=None,
    ha_entity_id=None,
    priority=1,
    allow_interruption=False,
    is_enabled=True,
    device_name="Test Device",
    output_type="audio",
    tts_volume=0.5,
):
    """Create a mock RoomOutputDevice."""
    dev = MagicMock()
    dev.renfield_device_id = renfield_device_id
    dev.ha_entity_id = ha_entity_id
    dev.priority = priority
    dev.allow_interruption = allow_interruption
    dev.is_enabled = is_enabled
    dev.device_name = device_name
    dev.output_type = output_type
    dev.tts_volume = tts_volume
    dev.is_renfield_device = renfield_device_id is not None
    dev.target_id = renfield_device_id or ha_entity_id or ""
    return dev


@pytest.fixture
def mock_db_session():
    return AsyncMock()


@pytest.fixture
def mock_ha_client():
    return AsyncMock()


@pytest.fixture
def service(mock_db_session, mock_ha_client):
    with patch("services.output_routing_service.HomeAssistantClient", return_value=mock_ha_client):
        svc = OutputRoutingService(mock_db_session)
    return svc


# ============================================================================
# Routing Logic Tests
# ============================================================================

@pytest.mark.unit
class TestOutputRoutingDecisions:
    """Tests for the core routing decision logic."""

    @pytest.mark.asyncio
    async def test_no_devices_configured_returns_fallback(self, service):
        """When no output devices exist, fallback to input device."""
        service._get_output_devices = AsyncMock(return_value=[])

        result = await service.get_audio_output_for_room(room_id=1, input_device_id="sat-kitchen")

        assert isinstance(result, OutputDecision)
        assert result.fallback_to_input is True
        assert result.target_id == "sat-kitchen"
        assert result.target_type == "renfield"
        assert result.reason == "no_output_devices_configured"

    @pytest.mark.asyncio
    async def test_no_devices_configured_no_input_device(self, service):
        """When no output devices and no input device, target_id is empty."""
        service._get_output_devices = AsyncMock(return_value=[])

        result = await service.get_audio_output_for_room(room_id=1, input_device_id=None)

        assert result.fallback_to_input is True
        assert result.target_id == ""

    @pytest.mark.asyncio
    async def test_available_device_selected(self, service):
        """First available device in priority order is selected."""
        dev = _make_output_device(ha_entity_id="media_player.living_room", priority=1)
        service._get_output_devices = AsyncMock(return_value=[dev])
        service._check_device_availability = AsyncMock(return_value=DeviceAvailability.AVAILABLE)

        result = await service.get_audio_output_for_room(room_id=1)

        assert result.fallback_to_input is False
        assert result.target_id == "media_player.living_room"
        assert result.target_type == "homeassistant"
        assert result.reason == "device_available"
        assert result.availability == DeviceAvailability.AVAILABLE

    @pytest.mark.asyncio
    async def test_renfield_device_type_detected(self, service):
        """Renfield devices get target_type='renfield'."""
        dev = _make_output_device(renfield_device_id="sat-kitchen", priority=1)
        service._get_output_devices = AsyncMock(return_value=[dev])
        service._check_device_availability = AsyncMock(return_value=DeviceAvailability.AVAILABLE)

        result = await service.get_audio_output_for_room(room_id=1)

        assert result.target_type == "renfield"
        assert result.target_id == "sat-kitchen"

    @pytest.mark.asyncio
    async def test_busy_device_with_interruption_allowed(self, service):
        """Busy device is selected when allow_interruption=True."""
        dev = _make_output_device(
            ha_entity_id="media_player.bedroom",
            allow_interruption=True,
        )
        service._get_output_devices = AsyncMock(return_value=[dev])
        service._check_device_availability = AsyncMock(return_value=DeviceAvailability.BUSY)

        result = await service.get_audio_output_for_room(room_id=1)

        assert result.fallback_to_input is False
        assert result.reason == "device_busy_allowing_interruption"
        assert result.availability == DeviceAvailability.BUSY

    @pytest.mark.asyncio
    async def test_busy_device_without_interruption_skipped(self, service):
        """Busy device is skipped when allow_interruption=False."""
        dev = _make_output_device(
            ha_entity_id="media_player.bedroom",
            allow_interruption=False,
        )
        service._get_output_devices = AsyncMock(return_value=[dev])
        service._check_device_availability = AsyncMock(return_value=DeviceAvailability.BUSY)

        result = await service.get_audio_output_for_room(room_id=1)

        assert result.fallback_to_input is True
        assert result.reason == "all_devices_unavailable"

    @pytest.mark.asyncio
    async def test_off_device_skipped(self, service):
        """OFF device is skipped."""
        dev = _make_output_device(ha_entity_id="media_player.off_device")
        service._get_output_devices = AsyncMock(return_value=[dev])
        service._check_device_availability = AsyncMock(return_value=DeviceAvailability.OFF)

        result = await service.get_audio_output_for_room(room_id=1)

        assert result.fallback_to_input is True
        assert result.reason == "all_devices_unavailable"

    @pytest.mark.asyncio
    async def test_unavailable_device_skipped(self, service):
        """UNAVAILABLE device is skipped."""
        dev = _make_output_device(ha_entity_id="media_player.dead")
        service._get_output_devices = AsyncMock(return_value=[dev])
        service._check_device_availability = AsyncMock(return_value=DeviceAvailability.UNAVAILABLE)

        result = await service.get_audio_output_for_room(room_id=1)

        assert result.fallback_to_input is True

    @pytest.mark.asyncio
    async def test_priority_ordering_first_available_wins(self, service):
        """Among multiple devices, first available by priority wins."""
        dev1 = _make_output_device(ha_entity_id="media_player.priority1", priority=1)
        dev2 = _make_output_device(ha_entity_id="media_player.priority2", priority=2)
        service._get_output_devices = AsyncMock(return_value=[dev1, dev2])

        async def availability_side_effect(device):
            if device.ha_entity_id == "media_player.priority1":
                return DeviceAvailability.OFF
            return DeviceAvailability.AVAILABLE

        service._check_device_availability = AsyncMock(side_effect=availability_side_effect)

        result = await service.get_audio_output_for_room(room_id=1)

        assert result.target_id == "media_player.priority2"
        assert result.reason == "device_available"

    @pytest.mark.asyncio
    async def test_disabled_device_skipped(self, service):
        """Disabled devices are skipped entirely."""
        dev = _make_output_device(ha_entity_id="media_player.disabled", is_enabled=False)
        service._get_output_devices = AsyncMock(return_value=[dev])
        # _check_device_availability should NOT be called for disabled devices
        service._check_device_availability = AsyncMock(return_value=DeviceAvailability.AVAILABLE)

        result = await service.get_audio_output_for_room(room_id=1)

        assert result.fallback_to_input is True
        service._check_device_availability.assert_not_called()

    @pytest.mark.asyncio
    async def test_visual_output_uses_correct_type(self, service):
        """get_visual_output_for_room passes correct output_type."""
        service._get_output_devices = AsyncMock(return_value=[])

        await service.get_visual_output_for_room(room_id=1)

        service._get_output_devices.assert_called_once_with(1, "visual")


# ============================================================================
# Device Availability Check Tests
# ============================================================================

@pytest.mark.unit
class TestDeviceAvailabilityChecks:
    """Tests for device availability detection."""

    @pytest.mark.asyncio
    async def test_ha_device_idle_is_available(self, service):
        service.ha_client.get_state = AsyncMock(return_value={"state": "idle"})
        result = await service._check_ha_device_availability("media_player.test")
        assert result == DeviceAvailability.AVAILABLE

    @pytest.mark.asyncio
    async def test_ha_device_paused_is_available(self, service):
        service.ha_client.get_state = AsyncMock(return_value={"state": "paused"})
        result = await service._check_ha_device_availability("media_player.test")
        assert result == DeviceAvailability.AVAILABLE

    @pytest.mark.asyncio
    async def test_ha_device_standby_is_available(self, service):
        service.ha_client.get_state = AsyncMock(return_value={"state": "standby"})
        result = await service._check_ha_device_availability("media_player.test")
        assert result == DeviceAvailability.AVAILABLE

    @pytest.mark.asyncio
    async def test_ha_device_on_is_available(self, service):
        service.ha_client.get_state = AsyncMock(return_value={"state": "on"})
        result = await service._check_ha_device_availability("media_player.test")
        assert result == DeviceAvailability.AVAILABLE

    @pytest.mark.asyncio
    async def test_ha_device_playing_is_busy(self, service):
        service.ha_client.get_state = AsyncMock(return_value={"state": "playing"})
        result = await service._check_ha_device_availability("media_player.test")
        assert result == DeviceAvailability.BUSY

    @pytest.mark.asyncio
    async def test_ha_device_buffering_is_busy(self, service):
        service.ha_client.get_state = AsyncMock(return_value={"state": "buffering"})
        result = await service._check_ha_device_availability("media_player.test")
        assert result == DeviceAvailability.BUSY

    @pytest.mark.asyncio
    async def test_ha_device_off_is_off(self, service):
        service.ha_client.get_state = AsyncMock(return_value={"state": "off"})
        result = await service._check_ha_device_availability("media_player.test")
        assert result == DeviceAvailability.OFF

    @pytest.mark.asyncio
    async def test_ha_device_unknown_is_unavailable(self, service):
        service.ha_client.get_state = AsyncMock(return_value={"state": "unknown"})
        result = await service._check_ha_device_availability("media_player.test")
        assert result == DeviceAvailability.UNAVAILABLE

    @pytest.mark.asyncio
    async def test_ha_device_no_state_is_unavailable(self, service):
        service.ha_client.get_state = AsyncMock(return_value=None)
        result = await service._check_ha_device_availability("media_player.test")
        assert result == DeviceAvailability.UNAVAILABLE

    @pytest.mark.asyncio
    async def test_ha_device_error_is_unavailable(self, service):
        service.ha_client.get_state = AsyncMock(side_effect=Exception("Connection refused"))
        result = await service._check_ha_device_availability("media_player.test")
        assert result == DeviceAvailability.UNAVAILABLE

    @pytest.mark.asyncio
    async def test_renfield_device_idle_is_available(self, service):
        from services.device_manager import DeviceState
        mock_device = MagicMock()
        mock_device.capabilities.has_speaker = True
        mock_device.state = DeviceState.IDLE

        mock_dm = MagicMock()
        mock_dm.get_device.return_value = mock_device

        with patch("services.device_manager.get_device_manager", return_value=mock_dm):
            result = await service._check_renfield_device_availability("sat-kitchen")

        assert result == DeviceAvailability.AVAILABLE

    @pytest.mark.asyncio
    async def test_renfield_device_speaking_is_busy(self, service):
        from services.device_manager import DeviceState
        mock_device = MagicMock()
        mock_device.capabilities.has_speaker = True
        mock_device.state = DeviceState.SPEAKING

        mock_dm = MagicMock()
        mock_dm.get_device.return_value = mock_device

        with patch("services.device_manager.get_device_manager", return_value=mock_dm):
            result = await service._check_renfield_device_availability("sat-kitchen")

        assert result == DeviceAvailability.BUSY

    @pytest.mark.asyncio
    async def test_renfield_device_not_connected_is_unavailable(self, service):
        mock_dm = MagicMock()
        mock_dm.get_device.return_value = None

        with patch("services.device_manager.get_device_manager", return_value=mock_dm):
            result = await service._check_renfield_device_availability("sat-kitchen")

        assert result == DeviceAvailability.UNAVAILABLE

    @pytest.mark.asyncio
    async def test_renfield_device_no_speaker_is_unavailable(self, service):
        mock_device = MagicMock()
        mock_device.capabilities.has_speaker = False

        mock_dm = MagicMock()
        mock_dm.get_device.return_value = mock_device

        with patch("services.device_manager.get_device_manager", return_value=mock_dm):
            result = await service._check_renfield_device_availability("sat-kitchen")

        assert result == DeviceAvailability.UNAVAILABLE

    @pytest.mark.asyncio
    async def test_check_device_dispatches_to_renfield(self, service):
        """_check_device_availability routes to renfield checker for renfield devices."""
        dev = _make_output_device(renfield_device_id="sat-1")
        service._check_renfield_device_availability = AsyncMock(return_value=DeviceAvailability.AVAILABLE)

        result = await service._check_device_availability(dev)

        assert result == DeviceAvailability.AVAILABLE
        service._check_renfield_device_availability.assert_called_once_with("sat-1")

    @pytest.mark.asyncio
    async def test_check_device_dispatches_to_ha(self, service):
        """_check_device_availability routes to HA checker for HA devices."""
        dev = _make_output_device(ha_entity_id="media_player.test")
        service._check_ha_device_availability = AsyncMock(return_value=DeviceAvailability.BUSY)

        result = await service._check_device_availability(dev)

        assert result == DeviceAvailability.BUSY
        service._check_ha_device_availability.assert_called_once_with("media_player.test")


# ============================================================================
# CRUD Operation Tests
# ============================================================================

@pytest.mark.unit
class TestCRUDOperations:
    """Tests for add/update/delete output device operations."""

    @pytest.mark.asyncio
    async def test_add_device_requires_either_renfield_or_ha(self, service):
        """Must provide either renfield_device_id or ha_entity_id."""
        with pytest.raises(ValueError, match="Either renfield_device_id or ha_entity_id"):
            await service.add_output_device(room_id=1, output_type="audio")

    @pytest.mark.asyncio
    async def test_add_device_rejects_both_renfield_and_ha(self, service):
        """Cannot provide both renfield_device_id and ha_entity_id."""
        with pytest.raises(ValueError, match="Only one of"):
            await service.add_output_device(
                room_id=1,
                output_type="audio",
                renfield_device_id="sat-1",
                ha_entity_id="media_player.test",
            )

    @pytest.mark.asyncio
    async def test_get_available_ha_media_players_error_returns_empty(self, service):
        """HA errors return empty list instead of raising."""
        service.ha_client.get_entities_by_domain = AsyncMock(side_effect=Exception("HA offline"))

        result = await service.get_available_ha_media_players()

        assert result == []


# ============================================================================
# Model Enum Tests
# ============================================================================

@pytest.mark.unit
class TestEnumsAndDataclasses:

    def test_device_availability_values(self):
        assert DeviceAvailability.AVAILABLE == "available"
        assert DeviceAvailability.BUSY == "busy"
        assert DeviceAvailability.OFF == "off"
        assert DeviceAvailability.UNAVAILABLE == "unavailable"

    def test_output_decision_fields(self):
        decision = OutputDecision(
            output_device=None,
            target_id="sat-1",
            target_type="renfield",
            availability=DeviceAvailability.AVAILABLE,
            fallback_to_input=True,
            reason="test",
        )
        assert decision.target_id == "sat-1"
        assert decision.fallback_to_input is True
