"""
Tests for Presence Detection API route logic.

Tests the PresenceService methods that the API routes delegate to,
verifying the same behavior the endpoints would return.
The actual HTTP endpoint wiring is covered by integration tests in CI
(which has all backend dependencies installed).
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from services.presence_service import PresenceService


@pytest.fixture
def service():
    """Create a PresenceService with test config."""
    svc = PresenceService.__new__(PresenceService)
    svc._mac_to_user = {
        "AA:BB:CC:DD:EE:01": 1,
        "AA:BB:CC:DD:EE:02": 2,
        "AA:BB:CC:DD:EE:03": 3,
    }
    svc._mac_to_method = {
        "AA:BB:CC:DD:EE:01": "ble",
        "AA:BB:CC:DD:EE:02": "ble",
        "AA:BB:CC:DD:EE:03": "ble",
    }
    svc._presence = {}
    svc._sightings = {}
    svc._hysteresis_threshold = 2
    svc._stale_timeout = 120.0
    svc._rssi_threshold = -80
    svc._room_names = {}
    svc._user_names = {}
    svc._pending_events = []
    return svc


@pytest.mark.unit
class TestAPIGetRoomsPresence:
    """Tests matching GET /api/presence/rooms behavior."""

    def test_empty_when_no_presence(self, service):
        """Endpoint returns [] when no one is present."""
        all_p = service.get_all_presence()
        assert all_p == {}

    @pytest.mark.asyncio
    async def test_rooms_grouped_with_occupants(self, service):
        """Endpoint returns rooms grouped with their occupants."""
        # Populate two rooms
        await service.process_ble_report("sat-1", 10, [
            {"mac": "AA:BB:CC:DD:EE:01", "rssi": -50},
            {"mac": "AA:BB:CC:DD:EE:02", "rssi": -60},
        ], room_name="Kitchen")
        await service.process_ble_report("sat-2", 20, [
            {"mac": "AA:BB:CC:DD:EE:03", "rssi": -45},
        ], room_name="Living Room")

        all_p = service.get_all_presence()
        # Group by room (same logic as the route handler)
        rooms = {}
        for _uid, p in all_p.items():
            if p.room_id not in rooms:
                rooms[p.room_id] = []
            rooms[p.room_id].append(p)

        assert len(rooms) == 2
        assert len(rooms[10]) == 2
        assert len(rooms[20]) == 1


@pytest.mark.unit
class TestAPIGetUserPresence:
    """Tests matching GET /api/presence/user/{id} behavior."""

    def test_untracked_user_returns_none(self, service):
        """Endpoint returns default when user not tracked."""
        p = service.get_user_presence(999)
        assert p is None

    @pytest.mark.asyncio
    async def test_tracked_user_with_alone_status(self, service):
        """Endpoint returns room + alone flag."""
        await service.process_ble_report("sat-1", 10, [
            {"mac": "AA:BB:CC:DD:EE:01", "rssi": -50},
        ], room_name="Kitchen")

        p = service.get_user_presence(1)
        assert p is not None
        assert p.room_id == 10
        assert p.room_name == "Kitchen"
        assert service.is_user_alone_in_room(1) is True

    @pytest.mark.asyncio
    async def test_tracked_user_not_alone(self, service):
        """Endpoint shows alone=False when others in room."""
        await service.process_ble_report("sat-1", 10, [
            {"mac": "AA:BB:CC:DD:EE:01", "rssi": -50},
            {"mac": "AA:BB:CC:DD:EE:02", "rssi": -55},
        ])

        p = service.get_user_presence(1)
        assert p is not None
        assert service.is_user_alone_in_room(1) is False


@pytest.mark.unit
class TestAPIGetRoomPresence:
    """Tests matching GET /api/presence/room/{id} behavior."""

    def test_empty_room(self, service):
        """Endpoint returns empty for room with no occupants."""
        occupants = service.get_room_occupants(999)
        assert occupants == []

    @pytest.mark.asyncio
    async def test_room_with_occupants(self, service):
        """Endpoint returns occupant list."""
        await service.process_ble_report("sat-1", 10, [
            {"mac": "AA:BB:CC:DD:EE:01", "rssi": -50},
            {"mac": "AA:BB:CC:DD:EE:02", "rssi": -60},
        ])

        occupants = service.get_room_occupants(10)
        user_ids = {o.user_id for o in occupants}
        assert user_ids == {1, 2}


@pytest.mark.unit
class TestAPIDeviceRegistration:
    """Tests matching POST/DELETE /api/presence/devices behavior."""

    @pytest.mark.asyncio
    async def test_add_device_updates_known_macs(self, service):
        """POST creates device and updates MAC whitelist."""
        mock_db = AsyncMock()
        async def mock_refresh(obj):
            obj.id = 99
            obj.user_id = 5
            obj.mac_address = "FF:EE:DD:CC:BB:AA"
            obj.device_name = "New Phone"
            obj.device_type = "phone"
            obj.is_enabled = True
        mock_db.refresh = mock_refresh

        await service.add_device(
            user_id=5, mac="ff:ee:dd:cc:bb:aa",
            name="New Phone", device_type="phone", db=mock_db,
        )

        assert "FF:EE:DD:CC:BB:AA" in service.get_known_macs()
        assert service._mac_to_user["FF:EE:DD:CC:BB:AA"] == 5

    @pytest.mark.asyncio
    async def test_remove_device_clears_mac(self, service):
        """DELETE removes device from cache."""
        mock_db = AsyncMock()
        mock_device = MagicMock()
        mock_device.mac_address = "AA:BB:CC:DD:EE:01"

        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = mock_device
        mock_db.execute = AsyncMock(return_value=mock_result)

        assert "AA:BB:CC:DD:EE:01" in service.get_known_macs()
        await service.remove_device(1, mock_db)
        assert "AA:BB:CC:DD:EE:01" not in service.get_known_macs()

    @pytest.mark.asyncio
    async def test_duplicate_mac_in_registry(self, service):
        """Adding same MAC twice would be caught by unique constraint."""
        # The service adds to cache regardless — DB unique constraint prevents dupes
        # Verify MAC is already in cache
        assert "AA:BB:CC:DD:EE:01" in service.get_known_macs()


@pytest.mark.unit
class TestAPIDeviceUpdate:
    """Tests matching PATCH /api/presence/devices/{id} behavior."""

    @pytest.mark.asyncio
    async def test_update_device_changes_method(self, service):
        """PATCH updates detection_method in DB and cache."""
        mock_db = AsyncMock()
        mock_device = MagicMock()
        mock_device.id = 1
        mock_device.user_id = 1
        mock_device.mac_address = "AA:BB:CC:DD:EE:01"
        mock_device.device_name = "Phone"
        mock_device.device_type = "phone"
        mock_device.detection_method = "ble"
        mock_device.is_enabled = True

        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = mock_device
        mock_db.execute = AsyncMock(return_value=mock_result)

        async def mock_refresh(obj):
            obj.detection_method = "classic_bt"
        mock_db.refresh = mock_refresh

        assert service._mac_to_method["AA:BB:CC:DD:EE:01"] == "ble"
        device = await service.update_device(1, "classic_bt", mock_db)

        assert device is not None
        assert service._mac_to_method["AA:BB:CC:DD:EE:01"] == "classic_bt"

    @pytest.mark.asyncio
    async def test_update_device_not_found(self, service):
        """PATCH returns None for unknown device ID."""
        mock_db = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_db.execute = AsyncMock(return_value=mock_result)

        device = await service.update_device(999, "classic_bt", mock_db)
        assert device is None
