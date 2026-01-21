"""
Tests für Database Models

Testet:
- Model-Instanziierung und Defaults
- Relationships und Foreign Keys
- Property-Methoden
- Validierung
"""

import pytest
from datetime import datetime
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.database import (
    Room, RoomDevice, RoomOutputDevice,
    Speaker, SpeakerEmbedding,
    Conversation, Message, Task, CameraEvent, HomeAssistantEntity,
    DEVICE_TYPE_SATELLITE, DEVICE_TYPE_WEB_PANEL, DEVICE_TYPE_WEB_BROWSER,
    DEVICE_TYPE_WEB_TABLET, DEVICE_TYPE_WEB_KIOSK, DEVICE_TYPES,
    DEFAULT_CAPABILITIES, OUTPUT_TYPE_AUDIO, OUTPUT_TYPE_VISUAL
)


# ============================================================================
# Room Model Tests
# ============================================================================

class TestRoomModel:
    """Tests für Room Model"""

    @pytest.mark.database
    async def test_room_creation(self, db_session: AsyncSession):
        """Test: Room kann erstellt werden"""
        room = Room(
            name="Wohnzimmer",
            alias="wohnzimmer",
            source="renfield"
        )

        db_session.add(room)
        await db_session.commit()
        await db_session.refresh(room)

        assert room.id is not None
        assert room.name == "Wohnzimmer"
        assert room.alias == "wohnzimmer"
        assert room.source == "renfield"
        assert room.created_at is not None

    @pytest.mark.database
    async def test_room_with_ha_area(self, db_session: AsyncSession):
        """Test: Room mit Home Assistant Area ID"""
        room = Room(
            name="Küche",
            alias="kueche",
            source="homeassistant",
            ha_area_id="kitchen_area_123"
        )

        db_session.add(room)
        await db_session.commit()
        await db_session.refresh(room)

        assert room.ha_area_id == "kitchen_area_123"
        assert room.source == "homeassistant"

    @pytest.mark.database
    async def test_room_satellites_property(self, db_session: AsyncSession, test_room: Room):
        """Test: satellites Property gibt nur Satellites zurück"""
        # Füge verschiedene Device-Typen hinzu
        satellite = RoomDevice(
            room_id=test_room.id,
            device_id="sat-test-1",
            device_type=DEVICE_TYPE_SATELLITE,
            is_online=True
        )
        web_panel = RoomDevice(
            room_id=test_room.id,
            device_id="panel-test-1",
            device_type=DEVICE_TYPE_WEB_PANEL,
            is_online=True
        )

        db_session.add_all([satellite, web_panel])
        await db_session.commit()

        # Neu laden mit relationships
        result = await db_session.execute(
            select(Room).where(Room.id == test_room.id)
        )
        room = result.scalar_one()
        await db_session.refresh(room, ["devices"])

        satellites = room.satellites
        assert len(satellites) == 1
        assert satellites[0].device_type == DEVICE_TYPE_SATELLITE

    @pytest.mark.database
    async def test_room_online_devices_property(self, db_session: AsyncSession, test_room: Room):
        """Test: online_devices Property gibt nur Online-Geräte zurück"""
        online_device = RoomDevice(
            room_id=test_room.id,
            device_id="dev-online",
            device_type=DEVICE_TYPE_WEB_BROWSER,
            is_online=True
        )
        offline_device = RoomDevice(
            room_id=test_room.id,
            device_id="dev-offline",
            device_type=DEVICE_TYPE_WEB_BROWSER,
            is_online=False
        )

        db_session.add_all([online_device, offline_device])
        await db_session.commit()

        result = await db_session.execute(
            select(Room).where(Room.id == test_room.id)
        )
        room = result.scalar_one()
        await db_session.refresh(room, ["devices"])

        online = room.online_devices
        assert len(online) == 1
        assert online[0].is_online is True


# ============================================================================
# RoomDevice Model Tests
# ============================================================================

class TestRoomDeviceModel:
    """Tests für RoomDevice Model"""

    @pytest.mark.database
    async def test_device_creation(self, db_session: AsyncSession, test_room: Room):
        """Test: Device kann erstellt werden"""
        device = RoomDevice(
            room_id=test_room.id,
            device_id="test-device-123",
            device_type=DEVICE_TYPE_WEB_BROWSER,
            device_name="Test Browser",
            is_online=True
        )

        db_session.add(device)
        await db_session.commit()
        await db_session.refresh(device)

        assert device.id is not None
        assert device.device_id == "test-device-123"
        assert device.room_id == test_room.id

    @pytest.mark.database
    async def test_device_capabilities(self, db_session: AsyncSession, test_room: Room):
        """Test: Device Capabilities JSON"""
        capabilities = {
            "has_microphone": True,
            "has_speaker": True,
            "has_display": True
        }

        device = RoomDevice(
            room_id=test_room.id,
            device_id="cap-device",
            device_type=DEVICE_TYPE_WEB_PANEL,
            capabilities=capabilities
        )

        db_session.add(device)
        await db_session.commit()
        await db_session.refresh(device)

        assert device.capabilities == capabilities
        assert device.has_capability("has_microphone") is True
        assert device.has_capability("has_wakeword") is False

    @pytest.mark.database
    async def test_device_capability_properties(self, db_session: AsyncSession, test_room: Room):
        """Test: Device Capability Property-Methoden"""
        device = RoomDevice(
            room_id=test_room.id,
            device_id="prop-device",
            device_type=DEVICE_TYPE_SATELLITE,
            capabilities=DEFAULT_CAPABILITIES[DEVICE_TYPE_SATELLITE]
        )

        db_session.add(device)
        await db_session.commit()
        await db_session.refresh(device)

        assert device.can_record_audio is True
        assert device.can_play_audio is True
        assert device.has_wakeword is True
        assert device.can_show_display is False

    @pytest.mark.unit
    def test_device_types_constant(self):
        """Test: DEVICE_TYPES Konstante enthält alle Typen"""
        assert DEVICE_TYPE_SATELLITE in DEVICE_TYPES
        assert DEVICE_TYPE_WEB_PANEL in DEVICE_TYPES
        assert DEVICE_TYPE_WEB_TABLET in DEVICE_TYPES
        assert DEVICE_TYPE_WEB_BROWSER in DEVICE_TYPES
        assert DEVICE_TYPE_WEB_KIOSK in DEVICE_TYPES
        assert len(DEVICE_TYPES) == 5

    @pytest.mark.unit
    def test_default_capabilities_for_all_types(self):
        """Test: DEFAULT_CAPABILITIES für alle Device-Typen definiert"""
        for device_type in DEVICE_TYPES:
            assert device_type in DEFAULT_CAPABILITIES
            caps = DEFAULT_CAPABILITIES[device_type]
            assert "has_microphone" in caps
            assert "has_speaker" in caps


# ============================================================================
# RoomOutputDevice Model Tests
# ============================================================================

class TestRoomOutputDeviceModel:
    """Tests für RoomOutputDevice Model"""

    @pytest.mark.database
    async def test_output_device_with_renfield_device(
        self, db_session: AsyncSession, test_room: Room, test_device: RoomDevice
    ):
        """Test: Output Device mit Renfield Device"""
        output = RoomOutputDevice(
            room_id=test_room.id,
            renfield_device_id=test_device.device_id,
            output_type=OUTPUT_TYPE_AUDIO,
            priority=1,
            allow_interruption=False,
            tts_volume=0.5
        )

        db_session.add(output)
        await db_session.commit()
        await db_session.refresh(output)

        assert output.is_renfield_device is True
        assert output.is_ha_device is False
        assert output.target_id == test_device.device_id

    @pytest.mark.database
    async def test_output_device_with_ha_entity(self, db_session: AsyncSession, test_room: Room):
        """Test: Output Device mit Home Assistant Entity"""
        output = RoomOutputDevice(
            room_id=test_room.id,
            ha_entity_id="media_player.sonos_living",
            output_type=OUTPUT_TYPE_AUDIO,
            priority=2,
            allow_interruption=True,
            tts_volume=0.7
        )

        db_session.add(output)
        await db_session.commit()
        await db_session.refresh(output)

        assert output.is_renfield_device is False
        assert output.is_ha_device is True
        assert output.target_id == "media_player.sonos_living"

    @pytest.mark.database
    async def test_output_device_priority_ordering(self, db_session: AsyncSession, test_room: Room):
        """Test: Output Devices werden nach Priorität sortiert"""
        output1 = RoomOutputDevice(
            room_id=test_room.id,
            ha_entity_id="media_player.low_prio",
            priority=3
        )
        output2 = RoomOutputDevice(
            room_id=test_room.id,
            ha_entity_id="media_player.high_prio",
            priority=1
        )
        output3 = RoomOutputDevice(
            room_id=test_room.id,
            ha_entity_id="media_player.mid_prio",
            priority=2
        )

        db_session.add_all([output1, output2, output3])
        await db_session.commit()

        # Refresh room with output_devices
        result = await db_session.execute(
            select(Room).where(Room.id == test_room.id)
        )
        room = result.scalar_one()
        await db_session.refresh(room, ["output_devices"])

        priorities = [od.priority for od in room.output_devices]
        assert priorities == [1, 2, 3]


# ============================================================================
# Speaker Model Tests
# ============================================================================

class TestSpeakerModel:
    """Tests für Speaker Model"""

    @pytest.mark.database
    async def test_speaker_creation(self, db_session: AsyncSession):
        """Test: Speaker kann erstellt werden"""
        speaker = Speaker(
            name="Max Mustermann",
            alias="max",
            is_admin=False
        )

        db_session.add(speaker)
        await db_session.commit()
        await db_session.refresh(speaker)

        assert speaker.id is not None
        assert speaker.name == "Max Mustermann"
        assert speaker.alias == "max"
        assert speaker.is_admin is False

    @pytest.mark.database
    async def test_speaker_with_embeddings(self, db_session: AsyncSession, test_speaker: Speaker):
        """Test: Speaker mit Embeddings Relationship"""
        embedding1 = SpeakerEmbedding(
            speaker_id=test_speaker.id,
            embedding="base64_encoded_data_1",
            sample_duration=3000
        )
        embedding2 = SpeakerEmbedding(
            speaker_id=test_speaker.id,
            embedding="base64_encoded_data_2",
            sample_duration=2500
        )

        db_session.add_all([embedding1, embedding2])
        await db_session.commit()

        result = await db_session.execute(
            select(Speaker).where(Speaker.id == test_speaker.id)
        )
        speaker = result.scalar_one()
        await db_session.refresh(speaker, ["embeddings"])

        assert len(speaker.embeddings) == 2

    @pytest.mark.database
    async def test_speaker_cascade_delete(self, db_session: AsyncSession):
        """Test: Embeddings werden bei Speaker-Löschung gelöscht"""
        speaker = Speaker(name="To Delete", alias="delete_me")
        db_session.add(speaker)
        await db_session.commit()
        await db_session.refresh(speaker)

        embedding = SpeakerEmbedding(
            speaker_id=speaker.id,
            embedding="to_be_deleted"
        )
        db_session.add(embedding)
        await db_session.commit()

        # Delete speaker
        await db_session.delete(speaker)
        await db_session.commit()

        # Check embedding is also deleted
        result = await db_session.execute(
            select(SpeakerEmbedding).where(SpeakerEmbedding.speaker_id == speaker.id)
        )
        embeddings = result.scalars().all()
        assert len(embeddings) == 0


# ============================================================================
# Conversation & Message Model Tests
# ============================================================================

class TestConversationModel:
    """Tests für Conversation Model"""

    @pytest.mark.database
    async def test_conversation_creation(self, db_session: AsyncSession):
        """Test: Conversation kann erstellt werden"""
        conv = Conversation(session_id="unique-session-123")

        db_session.add(conv)
        await db_session.commit()
        await db_session.refresh(conv)

        assert conv.id is not None
        assert conv.session_id == "unique-session-123"
        assert conv.created_at is not None

    @pytest.mark.database
    async def test_conversation_with_messages(
        self, db_session: AsyncSession, test_conversation: Conversation
    ):
        """Test: Conversation mit Messages Relationship"""
        msg1 = Message(
            conversation_id=test_conversation.id,
            role="user",
            content="Hallo"
        )
        msg2 = Message(
            conversation_id=test_conversation.id,
            role="assistant",
            content="Hallo! Wie kann ich helfen?"
        )

        db_session.add_all([msg1, msg2])
        await db_session.commit()

        result = await db_session.execute(
            select(Conversation).where(Conversation.id == test_conversation.id)
        )
        conv = result.scalar_one()
        await db_session.refresh(conv, ["messages"])

        assert len(conv.messages) == 2

    @pytest.mark.database
    async def test_message_with_metadata(self, db_session: AsyncSession, test_conversation: Conversation):
        """Test: Message mit Metadata JSON"""
        metadata = {
            "intent": "homeassistant.turn_on",
            "confidence": 0.95,
            "speaker_id": 1
        }

        msg = Message(
            conversation_id=test_conversation.id,
            role="user",
            content="Licht an",
            message_metadata=metadata
        )

        db_session.add(msg)
        await db_session.commit()
        await db_session.refresh(msg)

        assert msg.message_metadata == metadata
        assert msg.message_metadata["confidence"] == 0.95


# ============================================================================
# Task Model Tests
# ============================================================================

class TestTaskModel:
    """Tests für Task Model"""

    @pytest.mark.database
    async def test_task_creation(self, db_session: AsyncSession):
        """Test: Task kann erstellt werden"""
        task = Task(
            title="Test Task",
            description="A test task description",
            task_type="homeassistant",
            status="pending",
            priority=1,
            parameters={"entity_id": "light.test"}
        )

        db_session.add(task)
        await db_session.commit()
        await db_session.refresh(task)

        assert task.id is not None
        assert task.status == "pending"
        assert task.parameters["entity_id"] == "light.test"

    @pytest.mark.database
    async def test_task_status_update(self, db_session: AsyncSession):
        """Test: Task Status kann aktualisiert werden"""
        task = Task(
            title="Update Task",
            task_type="n8n",
            status="pending"
        )

        db_session.add(task)
        await db_session.commit()

        task.status = "completed"
        task.completed_at = datetime.utcnow()
        task.result = {"success": True}

        await db_session.commit()
        await db_session.refresh(task)

        assert task.status == "completed"
        assert task.completed_at is not None
        assert task.result["success"] is True


# ============================================================================
# CameraEvent Model Tests
# ============================================================================

class TestCameraEventModel:
    """Tests für CameraEvent Model"""

    @pytest.mark.database
    async def test_camera_event_creation(self, db_session: AsyncSession):
        """Test: CameraEvent kann erstellt werden"""
        event = CameraEvent(
            camera_name="front_door",
            event_type="person",
            confidence=85,
            snapshot_path="/snapshots/event1.jpg",
            event_metadata={"zone": "entrance"}
        )

        db_session.add(event)
        await db_session.commit()
        await db_session.refresh(event)

        assert event.id is not None
        assert event.camera_name == "front_door"
        assert event.event_type == "person"
        assert event.confidence == 85
        assert event.notified is False


# ============================================================================
# HomeAssistantEntity Model Tests
# ============================================================================

class TestHomeAssistantEntityModel:
    """Tests für HomeAssistantEntity Model"""

    @pytest.mark.database
    async def test_ha_entity_creation(self, db_session: AsyncSession):
        """Test: HA Entity kann erstellt werden"""
        entity = HomeAssistantEntity(
            entity_id="light.wohnzimmer",
            friendly_name="Wohnzimmer Licht",
            domain="light",
            state="off",
            attributes={"brightness": 255, "color_temp": 370}
        )

        db_session.add(entity)
        await db_session.commit()
        await db_session.refresh(entity)

        assert entity.id is not None
        assert entity.entity_id == "light.wohnzimmer"
        assert entity.domain == "light"
        assert entity.attributes["brightness"] == 255

    @pytest.mark.database
    async def test_ha_entity_unique_constraint(self, db_session: AsyncSession):
        """Test: entity_id muss eindeutig sein"""
        entity1 = HomeAssistantEntity(
            entity_id="switch.test",
            friendly_name="Test Switch",
            domain="switch"
        )

        db_session.add(entity1)
        await db_session.commit()

        # Versuche doppelte entity_id
        entity2 = HomeAssistantEntity(
            entity_id="switch.test",
            friendly_name="Another Switch",
            domain="switch"
        )

        db_session.add(entity2)

        with pytest.raises(Exception):  # IntegrityError
            await db_session.commit()
