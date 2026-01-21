"""
Tests für RoomService

Testet:
- Room CRUD Operationen
- Device Registration und Management
- Room Name Normalisierung
- Home Assistant Sync
- IP-basierte Room Detection
"""

import pytest
from datetime import datetime
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from services.room_service import RoomService, normalize_room_name, generate_device_id
from models.database import (
    Room, RoomDevice,
    DEVICE_TYPE_SATELLITE, DEVICE_TYPE_WEB_PANEL, DEVICE_TYPE_WEB_BROWSER
)


# ============================================================================
# normalize_room_name Tests
# ============================================================================

class TestNormalizeRoomName:
    """Tests für normalize_room_name Funktion"""

    @pytest.mark.unit
    def test_basic_normalization(self):
        """Test: Einfache Normalisierung"""
        assert normalize_room_name("Wohnzimmer") == "wohnzimmer"
        assert normalize_room_name("Living Room") == "livingroom"

    @pytest.mark.unit
    def test_german_umlauts(self):
        """Test: Deutsche Umlaute werden ersetzt"""
        assert normalize_room_name("Küche") == "kueche"
        assert normalize_room_name("Gästezimmer") == "gaestezimmer"
        assert normalize_room_name("Büro") == "buero"
        assert normalize_room_name("Straße") == "strasse"

    @pytest.mark.unit
    def test_uppercase_umlauts(self):
        """Test: Großbuchstaben-Umlaute"""
        assert normalize_room_name("KÜCHE") == "kueche"
        assert normalize_room_name("BÜRO") == "buero"
        assert normalize_room_name("Österreich") == "oesterreich"

    @pytest.mark.unit
    def test_special_characters_removed(self):
        """Test: Sonderzeichen werden entfernt"""
        assert normalize_room_name("Bad/WC") == "badwc"
        assert normalize_room_name("Kinder-Zimmer") == "kinderzimmer"
        assert normalize_room_name("Zimmer #1") == "zimmer1"

    @pytest.mark.unit
    def test_whitespace_handling(self):
        """Test: Whitespace wird entfernt"""
        assert normalize_room_name("  Wohnzimmer  ") == "wohnzimmer"
        assert normalize_room_name("Living Room") == "livingroom"

    @pytest.mark.unit
    def test_empty_string(self):
        """Test: Leerer String"""
        assert normalize_room_name("") == ""
        assert normalize_room_name("   ") == ""


# ============================================================================
# generate_device_id Tests
# ============================================================================

class TestGenerateDeviceId:
    """Tests für generate_device_id Funktion"""

    @pytest.mark.unit
    def test_satellite_prefix(self):
        """Test: Satellite bekommt 'sat' Prefix"""
        device_id = generate_device_id(DEVICE_TYPE_SATELLITE, "Wohnzimmer", "main")
        assert device_id == "sat-wohnzimmer-main"

    @pytest.mark.unit
    def test_web_panel_prefix(self):
        """Test: Web Panel bekommt 'panel' Prefix"""
        device_id = generate_device_id(DEVICE_TYPE_WEB_PANEL, "Küche", "ipad1")
        assert device_id == "panel-kueche-ipad1"

    @pytest.mark.unit
    def test_web_browser_prefix(self):
        """Test: Web Browser bekommt 'web' Prefix"""
        device_id = generate_device_id(DEVICE_TYPE_WEB_BROWSER, "Office", "chrome")
        assert device_id == "web-office-chrome"

    @pytest.mark.unit
    def test_auto_suffix_generation(self):
        """Test: Suffix wird automatisch generiert wenn nicht angegeben"""
        device_id = generate_device_id(DEVICE_TYPE_SATELLITE, "Wohnzimmer")
        assert device_id.startswith("sat-wohnzimmer-")
        assert len(device_id.split("-")[-1]) == 6  # UUID hex suffix

    @pytest.mark.unit
    def test_long_room_name_truncated(self):
        """Test: Lange Raumnamen werden gekürzt"""
        long_name = "Super Extra Langes Wohnzimmer Mit Vielen Worten"
        device_id = generate_device_id(DEVICE_TYPE_WEB_BROWSER, long_name, "test")
        # Room alias sollte maximal 20 Zeichen sein
        parts = device_id.split("-")
        assert len(parts[1]) <= 20


# ============================================================================
# RoomService CRUD Tests
# ============================================================================

class TestRoomServiceCRUD:
    """Tests für Room CRUD Operationen"""

    @pytest.mark.database
    async def test_create_room(self, room_service: RoomService):
        """Test: Room erstellen"""
        room = await room_service.create_room(
            name="Wohnzimmer",
            source="renfield",
            icon="mdi:sofa"
        )

        assert room.id is not None
        assert room.name == "Wohnzimmer"
        assert room.alias == "wohnzimmer"
        assert room.source == "renfield"
        assert room.icon == "mdi:sofa"

    @pytest.mark.database
    async def test_create_room_with_ha_area(self, room_service: RoomService):
        """Test: Room mit HA Area erstellen"""
        room = await room_service.create_room(
            name="Küche",
            source="homeassistant",
            ha_area_id="kitchen_area"
        )

        assert room.ha_area_id == "kitchen_area"
        assert room.alias == "kueche"

    @pytest.mark.database
    async def test_get_room(self, room_service: RoomService, test_room: Room):
        """Test: Room nach ID laden"""
        room = await room_service.get_room(test_room.id)

        assert room is not None
        assert room.id == test_room.id
        assert room.name == test_room.name

    @pytest.mark.database
    async def test_get_room_not_found(self, room_service: RoomService):
        """Test: Nicht existierender Room"""
        room = await room_service.get_room(99999)
        assert room is None

    @pytest.mark.database
    async def test_get_room_by_name(self, room_service: RoomService, test_room: Room):
        """Test: Room nach Namen laden"""
        room = await room_service.get_room_by_name(test_room.name)

        assert room is not None
        assert room.name == test_room.name

    @pytest.mark.database
    async def test_get_room_by_alias(self, room_service: RoomService, test_room: Room):
        """Test: Room nach Alias laden"""
        room = await room_service.get_room_by_alias("wohnzimmer")

        assert room is not None
        assert room.alias == "wohnzimmer"

    @pytest.mark.database
    async def test_get_room_by_alias_normalized(self, room_service: RoomService):
        """Test: Room-Suche nach Alias normalisiert Input"""
        room = await room_service.create_room(name="Gästezimmer")
        found = await room_service.get_room_by_alias("Gästezimmer")

        assert found is not None
        assert found.id == room.id

    @pytest.mark.database
    async def test_get_all_rooms(self, room_service: RoomService):
        """Test: Alle Rooms laden"""
        await room_service.create_room(name="Room A")
        await room_service.create_room(name="Room B")
        await room_service.create_room(name="Room C")

        rooms = await room_service.get_all_rooms()

        assert len(rooms) >= 3
        names = [r.name for r in rooms]
        assert "Room A" in names
        assert "Room B" in names
        assert "Room C" in names

    @pytest.mark.database
    async def test_update_room(self, room_service: RoomService, test_room: Room):
        """Test: Room aktualisieren"""
        updated = await room_service.update_room(
            room_id=test_room.id,
            name="Neuer Name",
            icon="mdi:lamp"
        )

        assert updated is not None
        assert updated.name == "Neuer Name"
        assert updated.alias == "neuername"
        assert updated.icon == "mdi:lamp"

    @pytest.mark.database
    async def test_update_room_not_found(self, room_service: RoomService):
        """Test: Update für nicht existierenden Room"""
        result = await room_service.update_room(room_id=99999, name="Test")
        assert result is None

    @pytest.mark.database
    async def test_delete_room(self, room_service: RoomService):
        """Test: Room löschen"""
        room = await room_service.create_room(name="To Delete")
        deleted = await room_service.delete_room(room.id)

        assert deleted is True

        check = await room_service.get_room(room.id)
        assert check is None

    @pytest.mark.database
    async def test_delete_room_not_found(self, room_service: RoomService):
        """Test: Löschen nicht existierender Room"""
        deleted = await room_service.delete_room(99999)
        assert deleted is False


# ============================================================================
# RoomService Device Operations Tests
# ============================================================================

class TestRoomServiceDeviceOperations:
    """Tests für Device Registration und Management"""

    @pytest.mark.database
    async def test_register_device(self, room_service: RoomService, test_room: Room):
        """Test: Device registrieren"""
        device = await room_service.register_device(
            device_id="test-device-123",
            room_name=test_room.name,
            device_type=DEVICE_TYPE_WEB_BROWSER,
            device_name="Test Browser",
            is_stationary=False
        )

        assert device is not None
        assert device.device_id == "test-device-123"
        assert device.room_id == test_room.id
        assert device.is_online is True

    @pytest.mark.database
    async def test_register_device_auto_create_room(self, room_service: RoomService):
        """Test: Device mit Auto-Room-Erstellung"""
        device = await room_service.register_device(
            device_id="auto-room-device",
            room_name="Auto Created Room",
            device_type=DEVICE_TYPE_SATELLITE,
            auto_create_room=True
        )

        assert device is not None

        room = await room_service.get_room_by_name("Auto Created Room")
        assert room is not None
        assert room.source == "satellite"

    @pytest.mark.database
    async def test_register_device_no_auto_create(self, room_service: RoomService):
        """Test: Device ohne Auto-Room-Erstellung"""
        device = await room_service.register_device(
            device_id="no-auto-device",
            room_name="Non Existing Room",
            device_type=DEVICE_TYPE_WEB_BROWSER,
            auto_create_room=False
        )

        assert device is None

    @pytest.mark.database
    async def test_register_device_update_existing(
        self, room_service: RoomService, test_room: Room
    ):
        """Test: Existierendes Device wird aktualisiert"""
        # Erste Registrierung
        device1 = await room_service.register_device(
            device_id="update-device",
            room_name=test_room.name,
            device_type=DEVICE_TYPE_WEB_BROWSER,
            device_name="First Name"
        )

        # Zweite Registrierung mit gleichem device_id
        device2 = await room_service.register_device(
            device_id="update-device",
            room_name=test_room.name,
            device_type=DEVICE_TYPE_WEB_BROWSER,
            device_name="Updated Name"
        )

        assert device1.id == device2.id
        assert device2.device_name == "Updated Name"

    @pytest.mark.database
    async def test_get_device(self, room_service: RoomService, test_device: RoomDevice):
        """Test: Device nach ID laden"""
        device = await room_service.get_device(test_device.device_id)

        assert device is not None
        assert device.device_id == test_device.device_id

    @pytest.mark.database
    async def test_get_devices_in_room(
        self, room_service: RoomService, test_room: Room, test_device: RoomDevice
    ):
        """Test: Alle Devices in Room laden"""
        devices = await room_service.get_devices_in_room(test_room.id)

        assert len(devices) >= 1
        device_ids = [d.device_id for d in devices]
        assert test_device.device_id in device_ids

    @pytest.mark.database
    async def test_set_device_online(
        self, room_service: RoomService, test_device: RoomDevice
    ):
        """Test: Device Online-Status setzen"""
        await room_service.set_device_online(test_device.device_id, False)

        device = await room_service.get_device(test_device.device_id)
        assert device.is_online is False

        await room_service.set_device_online(test_device.device_id, True)
        device = await room_service.get_device(test_device.device_id)
        assert device.is_online is True

    @pytest.mark.database
    async def test_move_device_to_room(self, room_service: RoomService, test_device: RoomDevice):
        """Test: Device in anderen Room verschieben"""
        new_room = await room_service.create_room(name="New Room")

        moved = await room_service.move_device_to_room(
            test_device.device_id,
            new_room.id
        )

        assert moved is not None
        assert moved.room_id == new_room.id

    @pytest.mark.database
    async def test_unregister_device(
        self, room_service: RoomService, test_room: Room
    ):
        """Test: Device entfernen"""
        device = await room_service.register_device(
            device_id="to-delete-device",
            room_name=test_room.name,
            device_type=DEVICE_TYPE_WEB_BROWSER
        )

        deleted = await room_service.unregister_device(device.device_id)
        assert deleted is True

        check = await room_service.get_device(device.device_id)
        assert check is None


# ============================================================================
# RoomService IP Detection Tests
# ============================================================================

class TestRoomServiceIPDetection:
    """Tests für IP-basierte Room Detection"""

    @pytest.mark.database
    async def test_get_stationary_device_by_ip(
        self, room_service: RoomService, test_room: Room
    ):
        """Test: Stationäres Device nach IP finden"""
        device = await room_service.register_device(
            device_id="stationary-ip-device",
            room_name=test_room.name,
            device_type=DEVICE_TYPE_WEB_PANEL,
            is_stationary=True,
            ip_address="192.168.1.100"
        )

        found = await room_service.get_stationary_device_by_ip("192.168.1.100")

        assert found is not None
        assert found.device_id == device.device_id

    @pytest.mark.database
    async def test_get_stationary_device_by_ip_not_stationary(
        self, room_service: RoomService, test_room: Room
    ):
        """Test: Nicht-stationäres Device wird nicht gefunden"""
        await room_service.register_device(
            device_id="mobile-ip-device",
            room_name=test_room.name,
            device_type=DEVICE_TYPE_WEB_BROWSER,
            is_stationary=False,
            ip_address="192.168.1.101"
        )

        found = await room_service.get_stationary_device_by_ip("192.168.1.101")
        assert found is None

    @pytest.mark.database
    async def test_get_room_context_by_ip(
        self, room_service: RoomService, test_room: Room
    ):
        """Test: Room Context nach IP ermitteln"""
        await room_service.register_device(
            device_id="context-device",
            room_name=test_room.name,
            device_type=DEVICE_TYPE_WEB_PANEL,
            device_name="iPad Wohnzimmer",
            is_stationary=True,
            ip_address="192.168.1.200"
        )

        context = await room_service.get_room_context_by_ip("192.168.1.200")

        assert context is not None
        assert context["room_name"] == test_room.name
        assert context["room_id"] == test_room.id
        assert context["device_id"] == "context-device"
        assert context["auto_detected"] is True

    @pytest.mark.database
    async def test_get_room_context_by_ip_not_found(self, room_service: RoomService):
        """Test: Kein Room Context für unbekannte IP"""
        context = await room_service.get_room_context_by_ip("10.0.0.1")
        assert context is None


# ============================================================================
# RoomService Home Assistant Sync Tests
# ============================================================================

class TestRoomServiceHASync:
    """Tests für Home Assistant Sync Operationen"""

    @pytest.mark.database
    async def test_link_to_ha_area(self, room_service: RoomService, test_room: Room):
        """Test: Room mit HA Area verknüpfen"""
        linked = await room_service.link_to_ha_area(test_room.id, "ha_area_123")

        assert linked is not None
        assert linked.ha_area_id == "ha_area_123"
        assert linked.last_synced_at is not None

    @pytest.mark.database
    async def test_unlink_from_ha(self, room_service: RoomService):
        """Test: HA Verknüpfung entfernen"""
        room = await room_service.create_room(
            name="HA Room",
            ha_area_id="to_unlink"
        )

        unlinked = await room_service.unlink_from_ha(room.id)

        assert unlinked is not None
        assert unlinked.ha_area_id is None
        assert unlinked.last_synced_at is None

    @pytest.mark.database
    async def test_import_ha_areas_new(self, room_service: RoomService, sample_ha_areas):
        """Test: HA Areas importieren (neue Rooms)"""
        results = await room_service.import_ha_areas(sample_ha_areas)

        assert results["imported"] == 3
        assert results["skipped"] == 0
        assert results["linked"] == 0

        # Verify rooms created
        room = await room_service.get_room_by_ha_area_id("living_room")
        assert room is not None
        assert room.name == "Wohnzimmer"

    @pytest.mark.database
    async def test_import_ha_areas_skip_existing(
        self, room_service: RoomService, sample_ha_areas
    ):
        """Test: HA Import überspringt existierende Rooms"""
        # Create room with same name
        await room_service.create_room(name="Wohnzimmer")

        results = await room_service.import_ha_areas(
            sample_ha_areas,
            conflict_resolution="skip"
        )

        assert results["skipped"] == 1  # Wohnzimmer skipped
        assert results["imported"] == 2

    @pytest.mark.database
    async def test_import_ha_areas_link_existing(
        self, room_service: RoomService, sample_ha_areas
    ):
        """Test: HA Import verknüpft existierende Rooms"""
        existing = await room_service.create_room(name="Wohnzimmer")

        results = await room_service.import_ha_areas(
            sample_ha_areas,
            conflict_resolution="link"
        )

        assert results["linked"] == 1
        assert results["imported"] == 2

        # Verify link
        room = await room_service.get_room(existing.id)
        assert room.ha_area_id == "living_room"

    @pytest.mark.database
    async def test_get_room_by_ha_area_id(self, room_service: RoomService):
        """Test: Room nach HA Area ID laden"""
        room = await room_service.create_room(
            name="HA Room",
            ha_area_id="unique_ha_area"
        )

        found = await room_service.get_room_by_ha_area_id("unique_ha_area")

        assert found is not None
        assert found.id == room.id


# ============================================================================
# RoomService Helper Methods Tests
# ============================================================================

class TestRoomServiceHelpers:
    """Tests für Helper-Methoden"""

    @pytest.mark.database
    async def test_room_to_dict(self, room_service: RoomService, test_room: Room):
        """Test: Room zu Dictionary konvertieren"""
        room = await room_service.get_room(test_room.id)
        data = room_service.room_to_dict(room)

        assert data["id"] == room.id
        assert data["name"] == room.name
        assert data["alias"] == room.alias
        assert "devices" in data
        assert "satellites" in data  # Legacy compatibility

    @pytest.mark.database
    async def test_device_to_dict(
        self, room_service: RoomService, test_device: RoomDevice
    ):
        """Test: Device zu Dictionary konvertieren"""
        device = await room_service.get_device(test_device.device_id)
        data = room_service.device_to_dict(device)

        assert data["device_id"] == device.device_id
        assert data["device_type"] == device.device_type
        assert "capabilities" in data
        assert "room_name" in data
