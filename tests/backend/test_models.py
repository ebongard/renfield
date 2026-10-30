"""
Tests for the database models

Covers:
- Model instantiation and defaults
- Relationships and foreign keys
- Property methods
- Validation
"""

from datetime import datetime

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from models.database import (
    DEFAULT_CAPABILITIES,
    DEVICE_TYPE_SATELLITE,
    DEVICE_TYPE_WEB_BROWSER,
    DEVICE_TYPE_WEB_KIOSK,
    DEVICE_TYPE_WEB_PANEL,
    DEVICE_TYPE_WEB_TABLET,
    DEVICE_TYPES,
    OUTPUT_TYPE_AUDIO,
    CameraEvent,
    Conversation,
    HomeAssistantEntity,
    Message,
    Room,
    RoomDevice,
    RoomOutputDevice,
    Speaker,
    SpeakerEmbedding,
    Task,
)

# ============================================================================
# Room Model Tests
# ============================================================================

class TestRoomModel:
    """Tests for the Room model"""

    @pytest.mark.database
    async def test_room_creation(self, db_session: AsyncSession):
        """Test: a room can be created"""
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
        """Test: Room with a Home Assistant area ID"""
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
        """Test: the satellites property returns only satellites"""
        # Add several device types
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

        # Reload with relationships
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
        """Test: the online_devices property returns only online devices"""
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
    """Tests for the RoomDevice model"""

    @pytest.mark.database
    async def test_device_creation(self, db_session: AsyncSession, test_room: Room):
        """Test: a device can be created"""
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
        """Test: Device capability property methods"""
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
        """Test: the DEVICE_TYPES constant contains every type"""
        assert DEVICE_TYPE_SATELLITE in DEVICE_TYPES
        assert DEVICE_TYPE_WEB_PANEL in DEVICE_TYPES
        assert DEVICE_TYPE_WEB_TABLET in DEVICE_TYPES
        assert DEVICE_TYPE_WEB_BROWSER in DEVICE_TYPES
        assert DEVICE_TYPE_WEB_KIOSK in DEVICE_TYPES
        assert len(DEVICE_TYPES) == 5

    @pytest.mark.unit
    def test_default_capabilities_for_all_types(self):
        """Test: DEFAULT_CAPABILITIES is defined for every device type"""
        for device_type in DEVICE_TYPES:
            assert device_type in DEFAULT_CAPABILITIES
            caps = DEFAULT_CAPABILITIES[device_type]
            assert "has_microphone" in caps
            assert "has_speaker" in caps


# ============================================================================
# RoomOutputDevice Model Tests
# ============================================================================

class TestRoomOutputDeviceModel:
    """Tests for the RoomOutputDevice model"""

    @pytest.mark.database
    async def test_output_device_with_renfield_device(
        self, db_session: AsyncSession, test_room: Room, test_device: RoomDevice
    ):
        """Test: Output device with a Renfield device"""
        output = RoomOutputDevice(
            room_id=test_room.id,
            output_provider="renfield",
            output_target_id=test_device.device_id,
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
        """Test: Output device with a Home Assistant entity"""
        output = RoomOutputDevice(
            room_id=test_room.id,
            output_provider="homeassistant",
            output_target_id="media_player.sonos_living",
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
        """Test: output devices are sorted by priority"""
        output1 = RoomOutputDevice(
            room_id=test_room.id,
            output_provider="homeassistant",
            output_target_id="media_player.low_prio",
            priority=3
        )
        output2 = RoomOutputDevice(
            room_id=test_room.id,
            output_provider="homeassistant",
            output_target_id="media_player.high_prio",
            priority=1
        )
        output3 = RoomOutputDevice(
            room_id=test_room.id,
            output_provider="homeassistant",
            output_target_id="media_player.mid_prio",
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


@pytest.mark.unit
class TestRoomOutputDevicePairProperties:
    """target_type / target_id / is_*_device derive ONLY from the generic
    (output_provider, output_target_id) pair after the legacy brand columns were
    dropped (docs/design/output-providers.md, migration pc20260617b)."""

    def test_renfield(self):
        d = RoomOutputDevice(output_provider="renfield", output_target_id="sat-1")
        assert d.target_type == "renfield"
        assert d.target_id == "sat-1"
        assert d.is_renfield_device is True
        assert d.is_ha_device is False
        assert d.is_dlna_device is False

    def test_homeassistant(self):
        d = RoomOutputDevice(
            output_provider="homeassistant", output_target_id="media_player.x"
        )
        assert d.target_type == "homeassistant"
        assert d.target_id == "media_player.x"
        assert d.is_ha_device is True
        assert d.is_renfield_device is False
        assert d.is_dlna_device is False

    def test_dlna(self):
        d = RoomOutputDevice(output_provider="dlna", output_target_id="HiFiBerry")
        assert d.target_type == "dlna"
        assert d.target_id == "HiFiBerry"
        assert d.is_dlna_device is True
        assert d.is_renfield_device is False
        assert d.is_ha_device is False

    def test_samsung_pair_only(self):
        d = RoomOutputDevice(output_provider="samsung", output_target_id="192.168.1.47")
        assert d.target_type == "samsung"
        assert d.target_id == "192.168.1.47"
        # No is_* flag matches a non-legacy provider.
        assert d.is_renfield_device is False
        assert d.is_ha_device is False
        assert d.is_dlna_device is False

    def test_sonos_pair_only(self):
        d = RoomOutputDevice(output_provider="sonos", output_target_id="RINCON_1")
        assert d.target_type == "sonos"
        assert d.target_id == "RINCON_1"
        assert d.is_dlna_device is False

    def test_empty_row_defaults(self):
        d = RoomOutputDevice()
        assert d.target_id == ""
        assert d.target_type == "renfield"
        assert d.is_renfield_device is False  # output_provider is None, not "renfield"


# ============================================================================
# Speaker Model Tests
# ============================================================================

class TestSpeakerModel:
    """Tests for the Speaker model"""

    @pytest.mark.database
    async def test_speaker_creation(self, db_session: AsyncSession):
        """Test: a speaker can be created"""
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
        """Test: Speaker-to-embeddings relationship"""
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
        """Test: embeddings are deleted when a speaker is deleted"""
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
    """Tests for the Conversation model"""

    @pytest.mark.database
    async def test_conversation_creation(self, db_session: AsyncSession):
        """Test: a conversation can be created"""
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
        """Test: Conversation-to-messages relationship"""
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
        """Test: Message with metadata JSON"""
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
    """Tests for the Task model"""

    @pytest.mark.database
    async def test_task_creation(self, db_session: AsyncSession):
        """Test: a task can be created"""
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
        """Test: a task status can be updated"""
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
    """Tests for the CameraEvent model"""

    @pytest.mark.database
    async def test_camera_event_creation(self, db_session: AsyncSession):
        """Test: a camera event can be created"""
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
    """Tests for the HomeAssistantEntity model"""

    @pytest.mark.database
    async def test_ha_entity_creation(self, db_session: AsyncSession):
        """Test: an HA entity can be created"""
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
        """Test: entity_id must be unique"""
        entity1 = HomeAssistantEntity(
            entity_id="switch.test",
            friendly_name="Test Switch",
            domain="switch"
        )

        db_session.add(entity1)
        await db_session.commit()

        # Attempt a duplicate entity_id
        entity2 = HomeAssistantEntity(
            entity_id="switch.test",
            friendly_name="Another Switch",
            domain="switch"
        )

        db_session.add(entity2)

        with pytest.raises(IntegrityError):
            await db_session.commit()


# ============================================================================
# PDF-Split Model Tests (documents lineage columns + pdf_split_proposals)
# ============================================================================

from models.database import (
    DOC_STATUS_SPLIT_ARCHIVED,
    PDF_SPLIT_PROPOSAL_APPROVED,
    PDF_SPLIT_PROPOSAL_PENDING,
    Document,
    PdfSplitProposal,
)


def _split_doc(filename: str, file_hash: str, **over) -> Document:
    defaults = dict(
        filename=filename,
        file_path=f"/uploads/{file_hash}.pdf",
        file_hash=file_hash,
        status="completed",
        circle_tier=0,
    )
    defaults.update(over)
    return Document(**defaults)


class TestPdfSplitModels:
    """Constraint-level coverage on REAL Postgres (pg_db_session — flush only,
    rollback-isolated): the SET-NULL lineage FK, the proposals CASCADE, and
    the one-pending-proposal-per-document partial unique index (declared in
    the ORM __table_args__ so create_all enforces it on fresh installs too)."""

    @pytest.mark.database
    @pytest.mark.asyncio
    async def test_split_lineage_fk_set_null_on_parent_delete(self, pg_db_session):
        parent = _split_doc("stapel.pdf", "h_parent", status=DOC_STATUS_SPLIT_ARCHIVED)
        pg_db_session.add(parent)
        await pg_db_session.flush()
        child = _split_doc(
            "stapel_teil01.pdf", "h_child", split_from_document_id=parent.id
        )
        pg_db_session.add(child)
        await pg_db_session.flush()

        # Deleting the archived parent must NOT cascade into the ingested
        # child — the lineage pointer just clears.
        await pg_db_session.delete(parent)
        await pg_db_session.flush()
        await pg_db_session.refresh(child)

        assert child.split_from_document_id is None

    @pytest.mark.database
    @pytest.mark.asyncio
    async def test_proposals_cascade_with_document(self, pg_db_session):
        doc = _split_doc("stapel2.pdf", "h_prop_doc")
        pg_db_session.add(doc)
        await pg_db_session.flush()
        pg_db_session.add(
            PdfSplitProposal(
                document_id=doc.id,
                status=PDF_SPLIT_PROPOSAL_PENDING,
                proposal=[{"start_page": 1, "end_page": 1}],
                page_count=1,
            )
        )
        await pg_db_session.flush()

        await pg_db_session.delete(doc)
        await pg_db_session.flush()

        remaining = (
            await pg_db_session.execute(
                select(PdfSplitProposal).where(PdfSplitProposal.document_id == doc.id)
            )
        ).scalars().all()
        assert remaining == []

    @pytest.mark.database
    @pytest.mark.asyncio
    async def test_one_pending_proposal_per_document(self, pg_db_session):
        from sqlalchemy.exc import IntegrityError

        doc = _split_doc("stapel3.pdf", "h_uniq_doc")
        pg_db_session.add(doc)
        await pg_db_session.flush()

        def _proposal(status):
            return PdfSplitProposal(
                document_id=doc.id,
                status=status,
                proposal=[{"start_page": 1, "end_page": 1}],
                page_count=1,
            )

        # pending + APPROVED coexist (the partial predicate only covers pending)
        pg_db_session.add(_proposal(PDF_SPLIT_PROPOSAL_APPROVED))
        pg_db_session.add(_proposal(PDF_SPLIT_PROPOSAL_PENDING))
        await pg_db_session.flush()

        # a SECOND pending proposal for the same document violates the
        # partial unique index
        pg_db_session.add(_proposal(PDF_SPLIT_PROPOSAL_PENDING))
        with pytest.raises(IntegrityError):
            await pg_db_session.flush()
