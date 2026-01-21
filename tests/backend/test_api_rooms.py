"""
Tests für Rooms API Routes

Testet:
- Room CRUD Endpoints
- Device Management Endpoints
- Home Assistant Sync Endpoints
- Output Device Endpoints
"""

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from models.database import (
    Room, RoomDevice, RoomOutputDevice,
    DEVICE_TYPE_SATELLITE, DEVICE_TYPE_WEB_BROWSER, DEVICE_TYPE_WEB_PANEL
)


# ============================================================================
# Room CRUD Endpoint Tests
# ============================================================================

class TestRoomCRUDEndpoints:
    """Tests für Room CRUD Endpoints"""

    @pytest.mark.integration
    async def test_create_room(self, async_client: AsyncClient):
        """Test: POST /api/rooms - Room erstellen"""
        response = await async_client.post(
            "/api/rooms",
            json={
                "name": "Test Wohnzimmer",
                "icon": "mdi:sofa"
            }
        )

        assert response.status_code == 200
        data = response.json()
        assert data["name"] == "Test Wohnzimmer"
        assert data["alias"] == "testwohnzimmer"
        assert data["icon"] == "mdi:sofa"
        assert data["source"] == "renfield"

    @pytest.mark.integration
    async def test_create_room_duplicate_name(
        self, async_client: AsyncClient, test_room: Room
    ):
        """Test: Doppelter Room-Name gibt Fehler"""
        response = await async_client.post(
            "/api/rooms",
            json={"name": test_room.name}
        )

        assert response.status_code == 400
        assert "already exists" in response.json()["detail"].lower()

    @pytest.mark.integration
    async def test_get_rooms(self, async_client: AsyncClient, test_room: Room):
        """Test: GET /api/rooms - Alle Rooms laden"""
        response = await async_client.get("/api/rooms")

        assert response.status_code == 200
        data = response.json()
        assert isinstance(data, list)
        assert len(data) >= 1

        room_names = [r["name"] for r in data]
        assert test_room.name in room_names

    @pytest.mark.integration
    async def test_get_room_by_id(self, async_client: AsyncClient, test_room: Room):
        """Test: GET /api/rooms/{id} - Room nach ID laden"""
        response = await async_client.get(f"/api/rooms/{test_room.id}")

        assert response.status_code == 200
        data = response.json()
        assert data["id"] == test_room.id
        assert data["name"] == test_room.name

    @pytest.mark.integration
    async def test_get_room_not_found(self, async_client: AsyncClient):
        """Test: Room nicht gefunden"""
        response = await async_client.get("/api/rooms/99999")

        assert response.status_code == 404

    @pytest.mark.integration
    async def test_update_room(self, async_client: AsyncClient, test_room: Room):
        """Test: PATCH /api/rooms/{id} - Room aktualisieren"""
        response = await async_client.patch(
            f"/api/rooms/{test_room.id}",
            json={
                "name": "Neuer Name",
                "icon": "mdi:lamp"
            }
        )

        assert response.status_code == 200
        data = response.json()
        assert data["name"] == "Neuer Name"
        assert data["alias"] == "neuername"
        assert data["icon"] == "mdi:lamp"

    @pytest.mark.integration
    async def test_delete_room(self, async_client: AsyncClient, db_session: AsyncSession):
        """Test: DELETE /api/rooms/{id} - Room löschen"""
        # Create room to delete
        room = Room(name="To Delete", alias="todelete")
        db_session.add(room)
        await db_session.commit()
        await db_session.refresh(room)

        response = await async_client.delete(f"/api/rooms/{room.id}")

        assert response.status_code == 200
        # Message includes room name
        assert "deleted" in response.json()["message"].lower()


# ============================================================================
# Room Device Endpoint Tests
# ============================================================================

class TestRoomDeviceEndpoints:
    """Tests für Room Device Endpoints"""

    @pytest.mark.integration
    async def test_get_devices_in_room(
        self, async_client: AsyncClient, test_room: Room, test_device: RoomDevice
    ):
        """Test: GET /api/rooms/{id}/devices - Devices im Room laden"""
        response = await async_client.get(f"/api/rooms/{test_room.id}/devices")

        assert response.status_code == 200
        data = response.json()
        assert isinstance(data, list)
        assert len(data) >= 1

        device_ids = [d["device_id"] for d in data]
        assert test_device.device_id in device_ids

    @pytest.mark.integration
    async def test_register_device_to_room(
        self, async_client: AsyncClient, test_room: Room
    ):
        """Test: POST /api/rooms/{id}/devices - Device registrieren"""
        response = await async_client.post(
            f"/api/rooms/{test_room.id}/devices",
            json={
                "device_id": "new-test-device",
                "device_type": "web_browser",
                "device_name": "New Browser Device",
                "is_stationary": False
            }
        )

        assert response.status_code == 200
        data = response.json()
        assert data["device_id"] == "new-test-device"
        assert data["device_type"] == "web_browser"
        assert data["room_id"] == test_room.id

    @pytest.mark.integration
    async def test_delete_device(
        self, async_client: AsyncClient, test_room: Room, db_session: AsyncSession
    ):
        """Test: DELETE /api/rooms/devices/{device_id} - Device löschen"""
        # Create device to delete
        device = RoomDevice(
            room_id=test_room.id,
            device_id="to-delete-device",
            device_type=DEVICE_TYPE_WEB_BROWSER
        )
        db_session.add(device)
        await db_session.commit()

        response = await async_client.delete("/api/rooms/devices/to-delete-device")

        assert response.status_code == 200
        # Message includes device_id
        assert "deleted" in response.json()["message"].lower()

    @pytest.mark.integration
    async def test_move_device_to_room(
        self, async_client: AsyncClient, test_device: RoomDevice, db_session: AsyncSession
    ):
        """Test: PATCH /api/rooms/devices/{device_id}/room/{room_id} - Device verschieben"""
        # Create target room
        target_room = Room(name="Target Room", alias="targetroom")
        db_session.add(target_room)
        await db_session.commit()
        await db_session.refresh(target_room)

        response = await async_client.patch(
            f"/api/rooms/devices/{test_device.device_id}/room/{target_room.id}"
        )

        assert response.status_code == 200
        data = response.json()
        assert data["room_id"] == target_room.id


# ============================================================================
# Home Assistant Sync Endpoint Tests
# ============================================================================

class TestHASyncEndpoints:
    """Tests für Home Assistant Sync Endpoints"""

    @pytest.mark.integration
    async def test_link_room_to_ha_area(
        self, async_client: AsyncClient, test_room: Room
    ):
        """Test: POST /api/rooms/{id}/link/{ha_area_id} - Room mit HA Area verknüpfen"""
        response = await async_client.post(
            f"/api/rooms/{test_room.id}/link/test_area_123"
        )

        assert response.status_code == 200
        data = response.json()
        assert data["ha_area_id"] == "test_area_123"

    @pytest.mark.integration
    async def test_unlink_room_from_ha(
        self, async_client: AsyncClient, db_session: AsyncSession
    ):
        """Test: DELETE /api/rooms/{id}/link - HA Verknüpfung entfernen"""
        # Create linked room
        room = Room(
            name="HA Linked Room",
            alias="halinkedroom",
            ha_area_id="to_unlink_area"
        )
        db_session.add(room)
        await db_session.commit()
        await db_session.refresh(room)

        response = await async_client.delete(f"/api/rooms/{room.id}/link")

        assert response.status_code == 200
        data = response.json()
        assert data["ha_area_id"] is None


# ============================================================================
# Output Device Endpoint Tests
# ============================================================================

class TestOutputDeviceEndpoints:
    """Tests für Output Device Endpoints"""

    @pytest.mark.integration
    async def test_get_output_devices(
        self, async_client: AsyncClient, test_room: Room
    ):
        """Test: GET /api/rooms/{id}/output-devices - Output Devices laden"""
        response = await async_client.get(f"/api/rooms/{test_room.id}/output-devices")

        assert response.status_code == 200
        data = response.json()
        assert isinstance(data, list)

    @pytest.mark.integration
    async def test_add_output_device_ha_entity(
        self, async_client: AsyncClient, test_room: Room
    ):
        """Test: POST /api/rooms/{id}/output-devices - HA Output Device hinzufügen"""
        response = await async_client.post(
            f"/api/rooms/{test_room.id}/output-devices",
            json={
                "ha_entity_id": "media_player.sonos_living",
                "output_type": "audio",
                "priority": 1,
                "allow_interruption": False,
                "tts_volume": 0.5,
                "device_name": "Sonos Speaker"
            }
        )

        assert response.status_code == 200
        data = response.json()
        assert data["ha_entity_id"] == "media_player.sonos_living"
        assert data["priority"] == 1

    @pytest.mark.integration
    async def test_add_output_device_renfield_device(
        self, async_client: AsyncClient, test_room: Room, test_device: RoomDevice
    ):
        """Test: POST /api/rooms/{id}/output-devices - Renfield Output Device hinzufügen"""
        response = await async_client.post(
            f"/api/rooms/{test_room.id}/output-devices",
            json={
                "renfield_device_id": test_device.device_id,
                "output_type": "audio",
                "priority": 2
            }
        )

        assert response.status_code == 200
        data = response.json()
        assert data["renfield_device_id"] == test_device.device_id

    @pytest.mark.integration
    async def test_update_output_device(
        self, async_client: AsyncClient, test_room: Room, db_session: AsyncSession
    ):
        """Test: PATCH /api/rooms/output-devices/{id} - Output Device aktualisieren"""
        # Create output device
        output = RoomOutputDevice(
            room_id=test_room.id,
            ha_entity_id="media_player.test",
            priority=1
        )
        db_session.add(output)
        await db_session.commit()
        await db_session.refresh(output)

        response = await async_client.patch(
            f"/api/rooms/output-devices/{output.id}",
            json={
                "priority": 5,
                "allow_interruption": True,
                "tts_volume": 0.8
            }
        )

        assert response.status_code == 200
        data = response.json()
        assert data["priority"] == 5
        assert data["allow_interruption"] is True
        assert data["tts_volume"] == 0.8

    @pytest.mark.integration
    async def test_delete_output_device(
        self, async_client: AsyncClient, test_room: Room, db_session: AsyncSession
    ):
        """Test: DELETE /api/rooms/output-devices/{id} - Output Device löschen"""
        # Create output device
        output = RoomOutputDevice(
            room_id=test_room.id,
            ha_entity_id="media_player.to_delete",
            priority=1
        )
        db_session.add(output)
        await db_session.commit()
        await db_session.refresh(output)

        response = await async_client.delete(f"/api/rooms/output-devices/{output.id}")

        assert response.status_code == 200

    @pytest.mark.integration
    async def test_reorder_output_devices(
        self, async_client: AsyncClient, test_room: Room, db_session: AsyncSession
    ):
        """Test: POST /api/rooms/{id}/output-devices/reorder - Prioritäten neu ordnen"""
        # Create multiple output devices
        output1 = RoomOutputDevice(
            room_id=test_room.id,
            ha_entity_id="media_player.first",
            priority=1
        )
        output2 = RoomOutputDevice(
            room_id=test_room.id,
            ha_entity_id="media_player.second",
            priority=2
        )
        db_session.add_all([output1, output2])
        await db_session.commit()
        await db_session.refresh(output1)
        await db_session.refresh(output2)

        # Reorder: swap priorities
        response = await async_client.post(
            f"/api/rooms/{test_room.id}/output-devices/reorder",
            json={"device_ids": [output2.id, output1.id]}
        )

        assert response.status_code == 200
        data = response.json()

        # Verify new order
        priorities = {d["id"]: d["priority"] for d in data}
        assert priorities[output2.id] == 1
        assert priorities[output1.id] == 2


# ============================================================================
# Connected Devices Endpoint Tests
# ============================================================================

class TestConnectedDevicesEndpoints:
    """Tests für Connected Devices Endpoints"""

    @pytest.mark.integration
    async def test_get_connected_devices(self, async_client: AsyncClient):
        """Test: GET /api/rooms/devices/connected - Verbundene Geräte"""
        response = await async_client.get("/api/rooms/devices/connected")

        assert response.status_code == 200
        data = response.json()
        # Might be empty if no devices connected via WebSocket
        assert isinstance(data, list)


# ============================================================================
# Error Handling Tests
# ============================================================================

class TestRoomAPIErrorHandling:
    """Tests für Fehlerbehandlung"""

    @pytest.mark.integration
    async def test_invalid_room_id(self, async_client: AsyncClient):
        """Test: Ungültige Room ID"""
        response = await async_client.get("/api/rooms/invalid")

        assert response.status_code == 422  # Validation error

    @pytest.mark.integration
    async def test_invalid_device_type(
        self, async_client: AsyncClient, test_room: Room
    ):
        """Test: Ungültiger Device Type"""
        response = await async_client.post(
            f"/api/rooms/{test_room.id}/devices",
            json={
                "device_id": "invalid-type-device",
                "device_type": "invalid_type"
            }
        )

        # Should still work but with default type
        assert response.status_code in [200, 422]

    @pytest.mark.integration
    async def test_missing_required_fields(self, async_client: AsyncClient):
        """Test: Fehlende Pflichtfelder"""
        response = await async_client.post(
            "/api/rooms",
            json={}  # Missing 'name'
        )

        assert response.status_code == 422

    @pytest.mark.integration
    async def test_output_device_missing_target(
        self, async_client: AsyncClient, test_room: Room
    ):
        """Test: Output Device ohne renfield_device_id und ha_entity_id"""
        response = await async_client.post(
            f"/api/rooms/{test_room.id}/output-devices",
            json={
                "output_type": "audio",
                "priority": 1
            }
        )

        assert response.status_code == 400
