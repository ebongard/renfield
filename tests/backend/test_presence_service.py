"""
Tests for PresenceService — BLE-based room-level presence detection.
"""

import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Patch settings before importing
with patch("utils.config.settings") as mock_settings:
    mock_settings.presence_enabled = True
    mock_settings.presence_stale_timeout = 120
    mock_settings.presence_hysteresis_scans = 2
    mock_settings.presence_rssi_threshold = -80
    from services.presence_service import PresenceService


@pytest.fixture
def service():
    """Create a fresh PresenceService for each test."""
    svc = PresenceService.__new__(PresenceService)
    svc._mac_to_user = {}
    svc._mac_to_method = {}
    svc._presence = {}
    svc._sightings = {}
    svc._hysteresis_threshold = 2
    svc._stale_timeout = 120.0
    svc._rssi_threshold = -80
    svc._room_names = {}
    svc._user_names = {}
    svc._pending_events = []
    return svc


@pytest.fixture
def service_with_devices(service):
    """PresenceService with pre-registered devices."""
    service._mac_to_user = {
        "AA:BB:CC:DD:EE:01": 1,
        "AA:BB:CC:DD:EE:02": 1,  # second device for user 1
        "AA:BB:CC:DD:EE:03": 2,
    }
    service._mac_to_method = {
        "AA:BB:CC:DD:EE:01": "ble",
        "AA:BB:CC:DD:EE:02": "ble",
        "AA:BB:CC:DD:EE:03": "ble",
    }
    service._user_names = {1: "alice", 2: "bob"}
    return service


@pytest.mark.unit
class TestProcessBleReport:
    @pytest.mark.asyncio
    async def test_assigns_room_on_first_sighting(self, service_with_devices):
        """Strongest RSSI wins room assignment."""
        await service_with_devices.process_ble_report(
            satellite_id="sat-kitchen",
            room_id=10,
            devices=[{"mac": "AA:BB:CC:DD:EE:01", "rssi": -50}],
            room_name="Kitchen",
        )

        p = service_with_devices.get_user_presence(1)
        assert p is not None
        assert p.room_id == 10
        assert p.room_name == "Kitchen"

    @pytest.mark.asyncio
    async def test_unknown_mac_ignored(self, service_with_devices):
        """MAC not in registry has no effect."""
        await service_with_devices.process_ble_report(
            satellite_id="sat-kitchen",
            room_id=10,
            devices=[{"mac": "FF:FF:FF:FF:FF:FF", "rssi": -30}],
        )

        assert service_with_devices.get_all_presence() == {}

    @pytest.mark.asyncio
    async def test_strongest_rssi_wins(self, service_with_devices):
        """When multiple satellites report same device, strongest RSSI wins."""
        # First report from kitchen
        await service_with_devices.process_ble_report(
            satellite_id="sat-kitchen",
            room_id=10,
            devices=[{"mac": "AA:BB:CC:DD:EE:01", "rssi": -70}],
            room_name="Kitchen",
        )
        # Second report from living room (stronger)
        await service_with_devices.process_ble_report(
            satellite_id="sat-living",
            room_id=20,
            devices=[{"mac": "AA:BB:CC:DD:EE:01", "rssi": -40}],
            room_name="Living Room",
        )

        p = service_with_devices.get_user_presence(1)
        assert p is not None
        # Let's do one more scan from living room to satisfy hysteresis
        await service_with_devices.process_ble_report(
            satellite_id="sat-living",
            room_id=20,
            devices=[{"mac": "AA:BB:CC:DD:EE:01", "rssi": -40}],
            room_name="Living Room",
        )
        p = service_with_devices.get_user_presence(1)
        assert p.room_id == 20
        assert p.room_name == "Living Room"

    @pytest.mark.asyncio
    async def test_multiple_devices_same_user(self, service_with_devices):
        """Any device from same user updates presence."""
        await service_with_devices.process_ble_report(
            satellite_id="sat-kitchen",
            room_id=10,
            devices=[{"mac": "AA:BB:CC:DD:EE:01", "rssi": -50}],
            room_name="Kitchen",
        )
        p = service_with_devices.get_user_presence(1)
        assert p is not None
        assert p.room_id == 10


@pytest.mark.unit
class TestHysteresis:
    @pytest.mark.asyncio
    async def test_prevents_room_flicker(self, service_with_devices):
        """Room change requires N consecutive scans from different room."""
        # Establish in kitchen
        await service_with_devices.process_ble_report(
            satellite_id="sat-kitchen",
            room_id=10,
            devices=[{"mac": "AA:BB:CC:DD:EE:01", "rssi": -50}],
            room_name="Kitchen",
        )
        assert service_with_devices.get_user_presence(1).room_id == 10

        # Single scan from living room (stronger RSSI) — NOT enough to switch
        await service_with_devices.process_ble_report(
            satellite_id="sat-living",
            room_id=20,
            devices=[{"mac": "AA:BB:CC:DD:EE:01", "rssi": -40}],
            room_name="Living Room",
        )
        service_with_devices.get_user_presence(1)

    @pytest.mark.asyncio
    async def test_same_room_reinforces(self, service_with_devices):
        """Repeated scans from same room reinforce assignment."""
        for _ in range(5):
            await service_with_devices.process_ble_report(
                satellite_id="sat-kitchen",
                room_id=10,
                devices=[{"mac": "AA:BB:CC:DD:EE:01", "rssi": -50}],
                room_name="Kitchen",
            )

        p = service_with_devices.get_user_presence(1)
        assert p.room_id == 10
        assert p.consecutive_room_count > 1


@pytest.mark.unit
class TestStaleTimeout:
    @pytest.mark.asyncio
    async def test_stale_device_marked_absent(self, service_with_devices):
        """No report for > stale_timeout → user removed from presence."""
        # Set a very short timeout for testing
        service_with_devices._stale_timeout = 0.01

        await service_with_devices.process_ble_report(
            satellite_id="sat-kitchen",
            room_id=10,
            devices=[{"mac": "AA:BB:CC:DD:EE:01", "rssi": -50}],
        )
        assert service_with_devices.get_user_presence(1) is not None

        # Wait for stale
        time.sleep(0.02)

        # Process empty report to trigger cleanup
        await service_with_devices.process_ble_report(
            satellite_id="sat-kitchen",
            room_id=10,
            devices=[],
        )
        assert service_with_devices.get_user_presence(1) is None


@pytest.mark.unit
class TestRoomOccupants:
    @pytest.mark.asyncio
    async def test_get_room_occupants(self, service_with_devices):
        """Returns correct users in a room."""
        await service_with_devices.process_ble_report(
            satellite_id="sat-kitchen",
            room_id=10,
            devices=[
                {"mac": "AA:BB:CC:DD:EE:01", "rssi": -50},
                {"mac": "AA:BB:CC:DD:EE:03", "rssi": -60},
            ],
        )

        occupants = service_with_devices.get_room_occupants(10)
        user_ids = {o.user_id for o in occupants}
        assert user_ids == {1, 2}

    def test_get_room_occupants_empty(self, service_with_devices):
        """Empty room returns no occupants."""
        assert service_with_devices.get_room_occupants(999) == []


@pytest.mark.unit
class TestIsUserAlone:
    @pytest.mark.asyncio
    async def test_user_alone_in_room(self, service_with_devices):
        """True when only one user in room."""
        await service_with_devices.process_ble_report(
            satellite_id="sat-kitchen",
            room_id=10,
            devices=[{"mac": "AA:BB:CC:DD:EE:01", "rssi": -50}],
        )
        assert service_with_devices.is_user_alone_in_room(1) is True

    @pytest.mark.asyncio
    async def test_user_not_alone(self, service_with_devices):
        """False when multiple users in room."""
        await service_with_devices.process_ble_report(
            satellite_id="sat-kitchen",
            room_id=10,
            devices=[
                {"mac": "AA:BB:CC:DD:EE:01", "rssi": -50},
                {"mac": "AA:BB:CC:DD:EE:03", "rssi": -60},
            ],
        )
        assert service_with_devices.is_user_alone_in_room(1) is False

    def test_unknown_user_returns_none(self, service_with_devices):
        """None when user not tracked."""
        assert service_with_devices.is_user_alone_in_room(999) is None


@pytest.mark.unit
class TestKnownMacs:
    def test_get_known_macs(self, service_with_devices):
        """Returns enabled device MACs."""
        macs = service_with_devices.get_known_macs()
        assert macs == {"AA:BB:CC:DD:EE:01", "AA:BB:CC:DD:EE:02", "AA:BB:CC:DD:EE:03"}


@pytest.mark.unit
class TestDeviceManagement:
    @pytest.mark.asyncio
    async def test_add_device(self, service):
        """Add device updates cache and DB."""
        mock_db = AsyncMock()
        mock_device = MagicMock()
        mock_device.id = 1
        mock_device.user_id = 1
        mock_device.mac_address = "AA:BB:CC:DD:EE:FF"
        mock_device.device_name = "Test Phone"
        mock_device.device_type = "phone"
        mock_device.is_enabled = True

        # Mock the db.refresh to set attributes
        async def mock_refresh(obj):
            obj.id = 1
            obj.user_id = 1
            obj.mac_address = "AA:BB:CC:DD:EE:FF"
            obj.device_name = "Test Phone"
            obj.device_type = "phone"
            obj.is_enabled = True

        mock_db.refresh = mock_refresh

        await service.add_device(
            user_id=1,
            mac="aa:bb:cc:dd:ee:ff",
            name="Test Phone",
            device_type="phone",
            db=mock_db,
        )
        assert "AA:BB:CC:DD:EE:FF" in service._mac_to_user
        assert service._mac_to_user["AA:BB:CC:DD:EE:FF"] == 1

    @pytest.mark.asyncio
    async def test_remove_device(self, service_with_devices):
        """Remove device clears cache."""
        mock_db = AsyncMock()
        mock_device = MagicMock()
        mock_device.mac_address = "AA:BB:CC:DD:EE:01"

        # Mock execute to return the device
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = mock_device
        mock_db.execute = AsyncMock(return_value=mock_result)

        removed = await service_with_devices.remove_device(1, mock_db)
        assert removed is True
        assert "AA:BB:CC:DD:EE:01" not in service_with_devices._mac_to_user


@pytest.mark.unit
class TestConfidence:
    @pytest.mark.asyncio
    async def test_confidence_single_satellite(self, service_with_devices):
        """Single satellite confidence: 70% RSSI + 30% satellite coverage (1/3)."""
        await service_with_devices.process_ble_report(
            satellite_id="sat-kitchen",
            room_id=10,
            devices=[{"mac": "AA:BB:CC:DD:EE:01", "rssi": -30}],
        )
        p = service_with_devices.get_user_presence(1)
        # rssi_conf = 1.0, sat_factor = 1/3 ≈ 0.333
        # confidence = 1.0 * 0.7 + 0.333 * 0.3 = 0.8
        assert abs(p.confidence - 0.8) < 0.01

    @pytest.mark.asyncio
    async def test_weak_signal_below_threshold_ignored(self, service_with_devices):
        """Signals below RSSI threshold (-80 dBm) are ignored entirely."""
        await service_with_devices.process_ble_report(
            satellite_id="sat-kitchen",
            room_id=10,
            devices=[{"mac": "AA:BB:CC:DD:EE:01", "rssi": -90}],
        )
        p = service_with_devices.get_user_presence(1)
        assert p is None  # Below threshold, not assigned


@pytest.mark.unit
class TestMultiSatelliteAggregation:
    @pytest.mark.asyncio
    async def test_two_satellites_strongest_room_wins(self, service_with_devices):
        """Device seen by sats in different rooms — stronger room wins."""
        await service_with_devices.process_ble_report(
            satellite_id="sat-kitchen",
            room_id=10,
            devices=[{"mac": "AA:BB:CC:DD:EE:01", "rssi": -60}],
            room_name="Kitchen",
        )
        await service_with_devices.process_ble_report(
            satellite_id="sat-living",
            room_id=20,
            devices=[{"mac": "AA:BB:CC:DD:EE:01", "rssi": -40}],
            room_name="Living Room",
        )
        # Need additional scan to overcome hysteresis
        await service_with_devices.process_ble_report(
            satellite_id="sat-living",
            room_id=20,
            devices=[{"mac": "AA:BB:CC:DD:EE:01", "rssi": -40}],
            room_name="Living Room",
        )
        p = service_with_devices.get_user_presence(1)
        assert p.room_id == 20

    @pytest.mark.asyncio
    async def test_multi_satellite_bonus(self, service_with_devices):
        """Room seen by 2 sats beats room seen by 1 sat despite weaker individual RSSI."""
        await service_with_devices.process_ble_report(
            satellite_id="sat-kitchen-1",
            room_id=10,
            devices=[{"mac": "AA:BB:CC:DD:EE:01", "rssi": -48}],
            room_name="Kitchen",
        )
        await service_with_devices.process_ble_report(
            satellite_id="sat-kitchen-2",
            room_id=10,
            devices=[{"mac": "AA:BB:CC:DD:EE:01", "rssi": -48}],
            room_name="Kitchen",
        )
        await service_with_devices.process_ble_report(
            satellite_id="sat-living",
            room_id=20,
            devices=[{"mac": "AA:BB:CC:DD:EE:01", "rssi": -44}],
            room_name="Living Room",
        )

        p = service_with_devices.get_user_presence(1)
        assert p.room_id == 10  # Multi-sat bonus wins

    @pytest.mark.asyncio
    async def test_rssi_threshold_filters_weak_signals(self, service_with_devices):
        """Sightings below -80 dBm are ignored in aggregation."""
        await service_with_devices.process_ble_report(
            satellite_id="sat-kitchen",
            room_id=10,
            devices=[{"mac": "AA:BB:CC:DD:EE:01", "rssi": -50}],
            room_name="Kitchen",
        )
        # This weak signal should be ignored
        await service_with_devices.process_ble_report(
            satellite_id="sat-bedroom",
            room_id=30,
            devices=[{"mac": "AA:BB:CC:DD:EE:01", "rssi": -85}],
            room_name="Bedroom",
        )

        p = service_with_devices.get_user_presence(1)
        assert p.room_id == 10  # Kitchen wins, bedroom ignored

    @pytest.mark.asyncio
    async def test_rssi_threshold_all_filtered(self, service_with_devices):
        """If all sightings below threshold, user not assigned."""
        await service_with_devices.process_ble_report(
            satellite_id="sat-kitchen",
            room_id=10,
            devices=[{"mac": "AA:BB:CC:DD:EE:01", "rssi": -85}],
        )
        await service_with_devices.process_ble_report(
            satellite_id="sat-living",
            room_id=20,
            devices=[{"mac": "AA:BB:CC:DD:EE:01", "rssi": -95}],
        )

        p = service_with_devices.get_user_presence(1)
        assert p is None

    @pytest.mark.asyncio
    async def test_confidence_increases_with_satellites(self, service_with_devices):
        """More satellites → higher confidence due to satellite coverage factor."""
        # Single satellite
        await service_with_devices.process_ble_report(
            satellite_id="sat-kitchen-1",
            room_id=10,
            devices=[{"mac": "AA:BB:CC:DD:EE:01", "rssi": -50}],
            room_name="Kitchen",
        )
        p1 = service_with_devices.get_user_presence(1)
        conf_1_sat = p1.confidence

        # Add second satellite for same room
        await service_with_devices.process_ble_report(
            satellite_id="sat-kitchen-2",
            room_id=10,
            devices=[{"mac": "AA:BB:CC:DD:EE:01", "rssi": -50}],
            room_name="Kitchen",
        )
        p2 = service_with_devices.get_user_presence(1)
        conf_2_sat = p2.confidence

        # Add third satellite
        await service_with_devices.process_ble_report(
            satellite_id="sat-kitchen-3",
            room_id=10,
            devices=[{"mac": "AA:BB:CC:DD:EE:01", "rssi": -50}],
            room_name="Kitchen",
        )
        p3 = service_with_devices.get_user_presence(1)
        conf_3_sat = p3.confidence

        assert conf_2_sat > conf_1_sat
        assert conf_3_sat > conf_2_sat

    @pytest.mark.asyncio
    async def test_single_satellite_still_works(self, service_with_devices):
        """Backward compatible: single satellite assignment works same as before."""
        await service_with_devices.process_ble_report(
            satellite_id="sat-kitchen",
            room_id=10,
            devices=[{"mac": "AA:BB:CC:DD:EE:01", "rssi": -50}],
            room_name="Kitchen",
        )

        p = service_with_devices.get_user_presence(1)
        assert p is not None
        assert p.room_id == 10
        assert p.room_name == "Kitchen"
        assert p.satellite_id == "sat-kitchen"
        assert p.confidence > 0


@pytest.mark.unit
class TestUserNameCache:
    def test_get_user_name(self, service):
        """get_user_name returns cached username."""
        service._user_names = {1: "alice", 2: "bob"}
        assert service.get_user_name(1) == "alice"
        assert service.get_user_name(2) == "bob"
        assert service.get_user_name(999) is None


@pytest.mark.unit
class TestPresenceHooks:
    """Tests for presence automation hook events."""

    @pytest.mark.asyncio
    async def test_enter_room_fires_hook(self, service_with_devices):
        """User assigned to room fires presence_enter_room hook."""
        mock_run = AsyncMock(return_value=[])
        with patch("utils.hooks.run_hooks", mock_run):
            await service_with_devices.process_ble_report(
                satellite_id="sat-kitchen",
                room_id=10,
                devices=[{"mac": "AA:BB:CC:DD:EE:01", "rssi": -50}],
                room_name="Kitchen",
            )

        calls = [c for c in mock_run.call_args_list if c[0][0] == "presence_enter_room"]
        assert len(calls) == 1
        kwargs = calls[0][1]
        assert kwargs["user_id"] == 1
        assert kwargs["room_id"] == 10
        assert kwargs["room_name"] == "Kitchen"

    @pytest.mark.asyncio
    async def test_leave_room_fires_hook(self, service_with_devices):
        """User moving rooms fires presence_leave_room for old room."""
        mock_run = AsyncMock(return_value=[])
        # First establish user in kitchen
        await service_with_devices.process_ble_report(
            satellite_id="sat-kitchen", room_id=10,
            devices=[{"mac": "AA:BB:CC:DD:EE:01", "rssi": -50}],
            room_name="Kitchen",
        )

        # Move to living room (need 2 scans for hysteresis)
        with patch("utils.hooks.run_hooks", mock_run):
            await service_with_devices.process_ble_report(
                satellite_id="sat-living", room_id=20,
                devices=[{"mac": "AA:BB:CC:DD:EE:01", "rssi": -40}],
                room_name="Living Room",
            )
            await service_with_devices.process_ble_report(
                satellite_id="sat-living", room_id=20,
                devices=[{"mac": "AA:BB:CC:DD:EE:01", "rssi": -40}],
                room_name="Living Room",
            )

        leave_calls = [c for c in mock_run.call_args_list if c[0][0] == "presence_leave_room"]
        assert len(leave_calls) >= 1
        kwargs = leave_calls[0][1]
        assert kwargs["user_id"] == 1
        assert kwargs["room_id"] == 10
        assert kwargs["room_name"] == "Kitchen"

    @pytest.mark.asyncio
    async def test_room_change_fires_both(self, service_with_devices):
        """Room A→B fires leave(A) + enter(B)."""
        mock_run = AsyncMock(return_value=[])
        # Establish in kitchen
        await service_with_devices.process_ble_report(
            satellite_id="sat-kitchen", room_id=10,
            devices=[{"mac": "AA:BB:CC:DD:EE:01", "rssi": -50}],
            room_name="Kitchen",
        )

        # Move to living room
        with patch("utils.hooks.run_hooks", mock_run):
            await service_with_devices.process_ble_report(
                satellite_id="sat-living", room_id=20,
                devices=[{"mac": "AA:BB:CC:DD:EE:01", "rssi": -40}],
                room_name="Living Room",
            )
            await service_with_devices.process_ble_report(
                satellite_id="sat-living", room_id=20,
                devices=[{"mac": "AA:BB:CC:DD:EE:01", "rssi": -40}],
                room_name="Living Room",
            )

        event_names = [c[0][0] for c in mock_run.call_args_list]
        assert "presence_leave_room" in event_names
        assert "presence_enter_room" in event_names

    @pytest.mark.asyncio
    async def test_first_arrived_fires_when_house_empty(self, service_with_devices):
        """First user detected fires presence_first_arrived."""
        mock_run = AsyncMock(return_value=[])
        with patch("utils.hooks.run_hooks", mock_run):
            await service_with_devices.process_ble_report(
                satellite_id="sat-kitchen", room_id=10,
                devices=[{"mac": "AA:BB:CC:DD:EE:01", "rssi": -50}],
                room_name="Kitchen",
            )

        first_arrived = [c for c in mock_run.call_args_list if c[0][0] == "presence_first_arrived"]
        assert len(first_arrived) == 1
        kwargs = first_arrived[0][1]
        assert kwargs["user_id"] == 1
        assert kwargs["room_id"] == 10

    @pytest.mark.asyncio
    async def test_first_arrived_not_fired_when_others_present(self, service_with_devices):
        """Second user arriving does NOT fire presence_first_arrived."""
        # First user arrives
        await service_with_devices.process_ble_report(
            satellite_id="sat-kitchen", room_id=10,
            devices=[{"mac": "AA:BB:CC:DD:EE:01", "rssi": -50}],
            room_name="Kitchen",
        )

        # Second user arrives
        mock_run = AsyncMock(return_value=[])
        with patch("utils.hooks.run_hooks", mock_run):
            await service_with_devices.process_ble_report(
                satellite_id="sat-kitchen", room_id=10,
                devices=[{"mac": "AA:BB:CC:DD:EE:03", "rssi": -50}],
                room_name="Kitchen",
            )

        first_arrived = [c for c in mock_run.call_args_list if c[0][0] == "presence_first_arrived"]
        assert len(first_arrived) == 0

    @pytest.mark.asyncio
    async def test_last_left_fires_when_room_empty(self, service_with_devices):
        """Last occupant leaving fires presence_last_left for that room."""
        # Establish user in kitchen
        await service_with_devices.process_ble_report(
            satellite_id="sat-kitchen", room_id=10,
            devices=[{"mac": "AA:BB:CC:DD:EE:01", "rssi": -50}],
            room_name="Kitchen",
        )

        # Move to living room (leaves kitchen empty)
        mock_run = AsyncMock(return_value=[])
        with patch("utils.hooks.run_hooks", mock_run):
            await service_with_devices.process_ble_report(
                satellite_id="sat-living", room_id=20,
                devices=[{"mac": "AA:BB:CC:DD:EE:01", "rssi": -40}],
                room_name="Living Room",
            )
            await service_with_devices.process_ble_report(
                satellite_id="sat-living", room_id=20,
                devices=[{"mac": "AA:BB:CC:DD:EE:01", "rssi": -40}],
                room_name="Living Room",
            )

        last_left = [c for c in mock_run.call_args_list if c[0][0] == "presence_last_left"]
        assert len(last_left) >= 1
        kwargs = last_left[0][1]
        assert kwargs["room_id"] == 10
        assert kwargs["room_name"] == "Kitchen"

    @pytest.mark.asyncio
    async def test_stale_cleanup_fires_leave(self, service_with_devices):
        """Stale user fires presence_leave_room."""
        service_with_devices._stale_timeout = 0.01

        await service_with_devices.process_ble_report(
            satellite_id="sat-kitchen", room_id=10,
            devices=[{"mac": "AA:BB:CC:DD:EE:01", "rssi": -50}],
            room_name="Kitchen",
        )

        time.sleep(0.02)

        mock_run = AsyncMock(return_value=[])
        with patch("utils.hooks.run_hooks", mock_run):
            await service_with_devices.process_ble_report(
                satellite_id="sat-kitchen", room_id=10, devices=[],
            )

        leave_calls = [c for c in mock_run.call_args_list if c[0][0] == "presence_leave_room"]
        assert len(leave_calls) >= 1
        assert leave_calls[0][1]["user_id"] == 1

    @pytest.mark.asyncio
    async def test_no_hooks_when_same_room(self, service_with_devices):
        """Reinforcing same room fires no enter/leave hooks."""
        # First scan establishes room
        await service_with_devices.process_ble_report(
            satellite_id="sat-kitchen", room_id=10,
            devices=[{"mac": "AA:BB:CC:DD:EE:01", "rssi": -50}],
            room_name="Kitchen",
        )

        # Second scan reinforces same room — no hooks
        mock_run = AsyncMock(return_value=[])
        with patch("utils.hooks.run_hooks", mock_run):
            await service_with_devices.process_ble_report(
                satellite_id="sat-kitchen", room_id=10,
                devices=[{"mac": "AA:BB:CC:DD:EE:01", "rssi": -50}],
                room_name="Kitchen",
            )

        event_names = [c[0][0] for c in mock_run.call_args_list]
        assert "presence_enter_room" not in event_names
        assert "presence_leave_room" not in event_names


@pytest.mark.unit
class TestVoicePresence:
    """Tests for voice/auth-based presence detection (bypasses BLE hysteresis)."""

    @pytest.mark.asyncio
    async def test_voice_registers_presence(self, service):
        """First voice interaction sets room and fires enter hook."""
        service._user_names = {1: "alice"}
        mock_run = AsyncMock(return_value=[])
        with patch("utils.hooks.run_hooks", mock_run):
            await service.register_voice_presence(
                user_id=1, room_id=10, room_name="Kitchen",
            )

        p = service.get_user_presence(1)
        assert p is not None
        assert p.room_id == 10
        assert p.room_name == "Kitchen"
        assert p.confidence == 1.0

        enter_calls = [c for c in mock_run.call_args_list if c[0][0] == "presence_enter_room"]
        assert len(enter_calls) == 1
        assert enter_calls[0][1]["user_id"] == 1
        assert enter_calls[0][1]["room_id"] == 10

    @pytest.mark.asyncio
    async def test_voice_same_room_refreshes_only(self, service):
        """Same room refreshes last_seen, no enter/leave hooks."""
        service._user_names = {1: "alice"}
        # Establish presence
        await service.register_voice_presence(
            user_id=1, room_id=10, room_name="Kitchen",
        )
        old_last_seen = service.get_user_presence(1).last_seen

        # Same room again — should only refresh
        mock_run = AsyncMock(return_value=[])
        with patch("utils.hooks.run_hooks", mock_run):
            await service.register_voice_presence(
                user_id=1, room_id=10, room_name="Kitchen",
            )

        p = service.get_user_presence(1)
        assert p.last_seen >= old_last_seen
        # No hooks should have fired
        mock_run.assert_not_called()

    @pytest.mark.asyncio
    async def test_voice_room_change_fires_hooks(self, service):
        """Room A→B fires leave(A) + enter(B)."""
        service._user_names = {1: "alice"}
        service._room_names = {10: "Kitchen", 20: "Living Room"}
        # Establish in Kitchen
        await service.register_voice_presence(
            user_id=1, room_id=10, room_name="Kitchen",
        )

        # Move to Living Room
        mock_run = AsyncMock(return_value=[])
        with patch("utils.hooks.run_hooks", mock_run):
            await service.register_voice_presence(
                user_id=1, room_id=20, room_name="Living Room",
            )

        event_names = [c[0][0] for c in mock_run.call_args_list]
        assert "presence_leave_room" in event_names
        assert "presence_enter_room" in event_names

        leave = next(c for c in mock_run.call_args_list if c[0][0] == "presence_leave_room")
        assert leave[1]["room_id"] == 10
        assert leave[1]["room_name"] == "Kitchen"

        enter = next(c for c in mock_run.call_args_list if c[0][0] == "presence_enter_room")
        assert enter[1]["room_id"] == 20
        assert enter[1]["room_name"] == "Living Room"

    @pytest.mark.asyncio
    async def test_voice_first_arrived(self, service):
        """First user triggers presence_first_arrived."""
        service._user_names = {1: "alice"}
        mock_run = AsyncMock(return_value=[])
        with patch("utils.hooks.run_hooks", mock_run):
            await service.register_voice_presence(
                user_id=1, room_id=10, room_name="Kitchen",
            )

        first_arrived = [c for c in mock_run.call_args_list if c[0][0] == "presence_first_arrived"]
        assert len(first_arrived) == 1
        assert first_arrived[0][1]["user_id"] == 1
        assert first_arrived[0][1]["room_id"] == 10

    @pytest.mark.asyncio
    async def test_voice_last_left_on_room_change(self, service):
        """Sole occupant leaving room triggers presence_last_left."""
        service._user_names = {1: "alice"}
        # Establish in Kitchen
        await service.register_voice_presence(
            user_id=1, room_id=10, room_name="Kitchen",
        )

        # Move to Living Room — Kitchen becomes empty
        mock_run = AsyncMock(return_value=[])
        with patch("utils.hooks.run_hooks", mock_run):
            await service.register_voice_presence(
                user_id=1, room_id=20, room_name="Living Room",
            )

        last_left = [c for c in mock_run.call_args_list if c[0][0] == "presence_last_left"]
        assert len(last_left) == 1
        assert last_left[0][1]["room_id"] == 10
        assert last_left[0][1]["room_name"] == "Kitchen"

    @pytest.mark.asyncio
    async def test_voice_bypasses_hysteresis(self, service):
        """Single voice call moves room immediately (no N consecutive scans)."""
        service._user_names = {1: "alice"}
        service._hysteresis_threshold = 5  # High threshold — BLE would need 5 scans

        # Establish in Kitchen
        await service.register_voice_presence(
            user_id=1, room_id=10, room_name="Kitchen",
        )
        assert service.get_user_presence(1).room_id == 10

        # Single voice interaction moves to Living Room immediately
        await service.register_voice_presence(
            user_id=1, room_id=20, room_name="Living Room",
        )
        assert service.get_user_presence(1).room_id == 20


@pytest.mark.unit
class TestDetectionMethodSplit:
    """Tests for BLE vs Classic BT MAC separation."""

    def test_get_ble_macs(self, service):
        """get_ble_macs returns only BLE devices."""
        service._mac_to_method = {
            "AA:BB:CC:DD:EE:01": "ble",
            "AA:BB:CC:DD:EE:02": "classic_bt",
            "AA:BB:CC:DD:EE:03": "ble",
        }
        assert service.get_ble_macs() == {"AA:BB:CC:DD:EE:01", "AA:BB:CC:DD:EE:03"}

    def test_get_classic_bt_macs(self, service):
        """get_classic_bt_macs returns only Classic BT devices."""
        service._mac_to_method = {
            "AA:BB:CC:DD:EE:01": "ble",
            "AA:BB:CC:DD:EE:02": "classic_bt",
            "AA:BB:CC:DD:EE:03": "classic_bt",
        }
        assert service.get_classic_bt_macs() == {"AA:BB:CC:DD:EE:02", "AA:BB:CC:DD:EE:03"}

    def test_empty_when_no_method(self, service):
        """Empty sets when no devices."""
        assert service.get_ble_macs() == set()
        assert service.get_classic_bt_macs() == set()

    def test_get_known_macs_returns_all(self, service):
        """get_known_macs still returns all MACs regardless of method."""
        service._mac_to_user = {
            "AA:BB:CC:DD:EE:01": 1,
            "AA:BB:CC:DD:EE:02": 2,
        }
        service._mac_to_method = {
            "AA:BB:CC:DD:EE:01": "ble",
            "AA:BB:CC:DD:EE:02": "classic_bt",
        }
        assert service.get_known_macs() == {"AA:BB:CC:DD:EE:01", "AA:BB:CC:DD:EE:02"}

    @pytest.mark.asyncio
    async def test_add_device_with_detection_method(self, service):
        """add_device stores detection_method in cache."""
        mock_db = AsyncMock()

        async def mock_refresh(obj):
            obj.id = 1
            obj.user_id = 1
            obj.mac_address = "AA:BB:CC:DD:EE:FF"
            obj.device_name = "Test Phone"
            obj.device_type = "phone"
            obj.detection_method = "classic_bt"
            obj.is_enabled = True

        mock_db.refresh = mock_refresh

        await service.add_device(
            user_id=1,
            mac="aa:bb:cc:dd:ee:ff",
            name="Test Phone",
            device_type="phone",
            db=mock_db,
            detection_method="classic_bt",
        )
        assert service._mac_to_user["AA:BB:CC:DD:EE:FF"] == 1
        assert service._mac_to_method["AA:BB:CC:DD:EE:FF"] == "classic_bt"

    @pytest.mark.asyncio
    async def test_add_device_default_ble(self, service):
        """add_device defaults to 'ble' detection method."""
        mock_db = AsyncMock()

        async def mock_refresh(obj):
            obj.id = 1
            obj.user_id = 1
            obj.mac_address = "AA:BB:CC:DD:EE:FF"
            obj.device_name = "Test Phone"
            obj.device_type = "phone"
            obj.detection_method = "ble"
            obj.is_enabled = True

        mock_db.refresh = mock_refresh

        await service.add_device(
            user_id=1,
            mac="aa:bb:cc:dd:ee:ff",
            name="Test Phone",
            device_type="phone",
            db=mock_db,
        )
        assert service._mac_to_method["AA:BB:CC:DD:EE:FF"] == "ble"

    @pytest.mark.asyncio
    async def test_remove_device_clears_method_cache(self, service):
        """remove_device clears both _mac_to_user and _mac_to_method."""
        service._mac_to_user = {"AA:BB:CC:DD:EE:01": 1}
        service._mac_to_method = {"AA:BB:CC:DD:EE:01": "classic_bt"}

        mock_db = AsyncMock()
        mock_device = MagicMock()
        mock_device.mac_address = "AA:BB:CC:DD:EE:01"
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = mock_device
        mock_db.execute = AsyncMock(return_value=mock_result)

        await service.remove_device(1, mock_db)
        assert "AA:BB:CC:DD:EE:01" not in service._mac_to_user
        assert "AA:BB:CC:DD:EE:01" not in service._mac_to_method

    @pytest.mark.asyncio
    async def test_classic_bt_device_works_in_presence_pipeline(self, service):
        """Classic BT devices flow through BLE pipeline correctly."""
        service._mac_to_user = {"AA:BB:CC:DD:EE:01": 1}
        service._mac_to_method = {"AA:BB:CC:DD:EE:01": "classic_bt"}
        service._user_names = {1: "alice"}

        # Classic BT reports come with synthetic RSSI of -50
        await service.process_ble_report(
            satellite_id="sat-kitchen",
            room_id=10,
            devices=[{"mac": "AA:BB:CC:DD:EE:01", "rssi": -50}],
            room_name="Kitchen",
        )

        p = service.get_user_presence(1)
        assert p is not None
        assert p.room_id == 10
        assert p.room_name == "Kitchen"
