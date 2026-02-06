"""
Pytest Fixtures für Renfield Backend Tests

Bietet:
- In-Memory SQLite Datenbank für isolierte Tests
- Mock-Services für externe Abhängigkeiten
- FastAPI TestClient für API-Tests
- Async Support
"""

from collections.abc import AsyncGenerator
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

# Renfield Imports
from models.database import (
    DEFAULT_CAPABILITIES,
    DEVICE_TYPE_SATELLITE,
    DEVICE_TYPE_WEB_BROWSER,
    Base,
    Conversation,
    Document,
    KnowledgeBase,
    Message,
    Role,
    Room,
    RoomDevice,
    Speaker,
    User,
)

# ============================================================================
# Database Fixtures
# ============================================================================

# SQLite async engine for testing
TEST_DATABASE_URL = "sqlite+aiosqlite:///:memory:"


@pytest.fixture
async def async_engine():
    """Create async SQLite engine for tests"""
    engine = create_async_engine(
        TEST_DATABASE_URL,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        echo=False
    )

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    yield engine

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)

    await engine.dispose()


@pytest.fixture
async def db_session(async_engine) -> AsyncGenerator[AsyncSession, None]:
    """Create async database session for tests"""
    async_session_maker = async_sessionmaker(
        async_engine,
        class_=AsyncSession,
        expire_on_commit=False
    )

    async with async_session_maker() as session:
        yield session
        await session.rollback()


# ============================================================================
# Sample Data Fixtures
# ============================================================================

@pytest.fixture
def sample_room_data():
    """Sample room data for tests"""
    return {
        "name": "Wohnzimmer",
        "alias": "wohnzimmer",
        "source": "renfield",
        "ha_area_id": None,
        "icon": "mdi:sofa"
    }


@pytest.fixture
def sample_device_data():
    """Sample device data for tests"""
    return {
        "device_id": "web-wohnzimmer-abc123",
        "device_type": DEVICE_TYPE_WEB_BROWSER,
        "device_name": "Test Browser",
        "capabilities": DEFAULT_CAPABILITIES[DEVICE_TYPE_WEB_BROWSER],
        "is_stationary": False,
        "ip_address": "192.168.1.100",
        "user_agent": "Mozilla/5.0 Test"
    }


@pytest.fixture
def sample_satellite_data():
    """Sample satellite device data"""
    return {
        "device_id": "sat-wohnzimmer-main",
        "device_type": DEVICE_TYPE_SATELLITE,
        "device_name": "Living Room Satellite",
        "capabilities": DEFAULT_CAPABILITIES[DEVICE_TYPE_SATELLITE],
        "is_stationary": True,
        "ip_address": "192.168.1.50"
    }


@pytest.fixture
def sample_speaker_data():
    """Sample speaker data for tests"""
    return {
        "name": "Max Mustermann",
        "alias": "max",
        "is_admin": False
    }


@pytest.fixture
def sample_conversation_data():
    """Sample conversation data for tests"""
    return {
        "session_id": "test-session-123"
    }


@pytest.fixture
def sample_message_data():
    """Sample message data for tests"""
    return {
        "role": "user",
        "content": "Schalte das Licht ein",
        "message_metadata": {"intent": "homeassistant.turn_on"}
    }


@pytest.fixture
def sample_intent_data():
    """Sample intent data for tests"""
    return {
        "intent": "homeassistant.turn_on",
        "parameters": {
            "entity_id": "light.wohnzimmer",
            "name": "Wohnzimmer Licht"
        },
        "confidence": 0.95
    }


@pytest.fixture
def sample_ha_areas():
    """Sample Home Assistant areas for tests"""
    return [
        {"area_id": "living_room", "name": "Wohnzimmer", "icon": "mdi:sofa"},
        {"area_id": "kitchen", "name": "Küche", "icon": "mdi:pot"},
        {"area_id": "bedroom", "name": "Schlafzimmer", "icon": "mdi:bed"},
    ]


# ============================================================================
# Database Object Fixtures
# ============================================================================

@pytest.fixture
async def test_room(db_session: AsyncSession, sample_room_data) -> Room:
    """Create a test room in database"""
    room = Room(**sample_room_data)
    db_session.add(room)
    await db_session.commit()
    await db_session.refresh(room)
    return room


@pytest.fixture
async def test_device(db_session: AsyncSession, test_room: Room, sample_device_data) -> RoomDevice:
    """Create a test device in database"""
    device = RoomDevice(
        room_id=test_room.id,
        **sample_device_data
    )
    db_session.add(device)
    await db_session.commit()
    await db_session.refresh(device)
    return device


@pytest.fixture
async def test_satellite(db_session: AsyncSession, test_room: Room, sample_satellite_data) -> RoomDevice:
    """Create a test satellite in database"""
    satellite = RoomDevice(
        room_id=test_room.id,
        **sample_satellite_data
    )
    db_session.add(satellite)
    await db_session.commit()
    await db_session.refresh(satellite)
    return satellite


@pytest.fixture
async def test_speaker(db_session: AsyncSession, sample_speaker_data) -> Speaker:
    """Create a test speaker in database"""
    speaker = Speaker(**sample_speaker_data)
    db_session.add(speaker)
    await db_session.commit()
    await db_session.refresh(speaker)
    return speaker


@pytest.fixture
async def test_conversation(db_session: AsyncSession, sample_conversation_data) -> Conversation:
    """Create a test conversation in database"""
    conversation = Conversation(**sample_conversation_data)
    db_session.add(conversation)
    await db_session.commit()
    await db_session.refresh(conversation)
    return conversation


@pytest.fixture
async def test_message(db_session: AsyncSession, test_conversation: Conversation, sample_message_data) -> Message:
    """Create a test message in database"""
    message = Message(
        conversation_id=test_conversation.id,
        **sample_message_data
    )
    db_session.add(message)
    await db_session.commit()
    await db_session.refresh(message)
    return message


@pytest.fixture
def sample_knowledge_base_data():
    """Sample knowledge base data for tests"""
    return {
        "name": "Test Knowledge Base",
        "description": "A test knowledge base for unit tests",
        "is_active": True,
        "is_public": False
    }


@pytest.fixture
def sample_document_data():
    """Sample document data for tests"""
    return {
        "filename": "test_document.pdf",
        "title": "Test Document",
        "file_path": "/tmp/test_document.pdf",
        "file_type": "pdf",
        "file_size": 12345,
        "file_hash": "abc123def456",
        "status": "completed",
        "chunk_count": 5,
        "page_count": 3
    }


@pytest.fixture
def sample_role_data():
    """Sample role data for tests"""
    return {
        "name": "TestRole",
        "description": "A test role",
        "permissions": ["kb.all", "ha.full", "admin"],
        "is_system": False
    }


@pytest.fixture
def sample_user_data():
    """Sample user data for tests"""
    return {
        "username": "testuser",
        "email": "test@example.com",
        "password_hash": "$2b$12$LQv3c1yqBWVHxkd0LHAkCOYz6TtxMQJqhN8/X4wPpY/ABCDEFGH",  # Fake hash
        "is_active": True
    }


@pytest.fixture
async def test_role(db_session: AsyncSession, sample_role_data) -> Role:
    """Create a test role in database"""
    role = Role(**sample_role_data)
    db_session.add(role)
    await db_session.commit()
    await db_session.refresh(role)
    return role


@pytest.fixture
async def test_user(db_session: AsyncSession, test_role: Role, sample_user_data) -> User:
    """Create a test user in database"""
    user = User(
        role_id=test_role.id,
        **sample_user_data
    )
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user


@pytest.fixture
async def test_knowledge_base(db_session: AsyncSession, sample_knowledge_base_data) -> KnowledgeBase:
    """Create a test knowledge base in database"""
    kb = KnowledgeBase(**sample_knowledge_base_data)
    db_session.add(kb)
    await db_session.commit()
    await db_session.refresh(kb)
    return kb


@pytest.fixture
async def test_knowledge_base_with_owner(
    db_session: AsyncSession,
    test_user: User,
    sample_knowledge_base_data
) -> KnowledgeBase:
    """Create a test knowledge base with an owner"""
    kb = KnowledgeBase(
        owner_id=test_user.id,
        **sample_knowledge_base_data
    )
    db_session.add(kb)
    await db_session.commit()
    await db_session.refresh(kb)
    return kb


@pytest.fixture
async def test_document(
    db_session: AsyncSession,
    test_knowledge_base: KnowledgeBase,
    sample_document_data
) -> Document:
    """Create a test document in database"""
    document = Document(
        knowledge_base_id=test_knowledge_base.id,
        **sample_document_data
    )
    db_session.add(document)
    await db_session.commit()
    await db_session.refresh(document)
    return document


# ============================================================================
# Mock Service Fixtures
# ============================================================================

@pytest.fixture
def mock_ha_client():
    """Mock Home Assistant client"""
    client = AsyncMock()

    # Default responses
    client.get_state.return_value = {
        "state": "off",
        "attributes": {"friendly_name": "Wohnzimmer Licht"}
    }
    client.turn_on.return_value = True
    client.turn_off.return_value = True
    client.toggle.return_value = True
    client.search_entities.return_value = [
        {"entity_id": "light.wohnzimmer", "friendly_name": "Wohnzimmer Licht"}
    ]
    client.get_all_entities.return_value = [
        {"entity_id": "light.wohnzimmer", "state": "off", "attributes": {"friendly_name": "Wohnzimmer Licht"}},
        {"entity_id": "switch.fernseher", "state": "on", "attributes": {"friendly_name": "Fernseher"}},
    ]
    client.get_keywords.return_value = ["wohnzimmer", "licht", "fernseher", "küche"]
    client.is_configured.return_value = True
    client.get_areas.return_value = [
        {"area_id": "living_room", "name": "Wohnzimmer"},
        {"area_id": "kitchen", "name": "Küche"}
    ]

    return client


@pytest.fixture
def mock_ollama_client():
    """Mock Ollama client"""
    client = AsyncMock()

    client.generate.return_value = {
        "response": "Das Licht wurde eingeschaltet."
    }
    client.chat.return_value = {
        "message": {"content": "Das Licht wurde eingeschaltet."}
    }

    return client


@pytest.fixture
def mock_whisper_service():
    """Mock Whisper STT service"""
    service = AsyncMock()

    service.transcribe.return_value = {
        "text": "Schalte das Licht im Wohnzimmer ein",
        "language": "de"
    }
    service.transcribe_with_speaker.return_value = {
        "text": "Schalte das Licht ein",
        "language": "de",
        "speaker_id": 1,
        "speaker_name": "Max"
    }

    return service


@pytest.fixture
def mock_piper_service():
    """Mock Piper TTS service"""
    service = MagicMock()

    # Return fake audio bytes
    service.synthesize.return_value = b"RIFF" + b"\x00" * 100
    service.synthesize_async = AsyncMock(return_value=b"RIFF" + b"\x00" * 100)

    return service


@pytest.fixture
def mock_frigate_client():
    """Mock Frigate client"""
    client = AsyncMock()

    client.get_events.return_value = [
        {
            "id": "event-1",
            "camera": "front_door",
            "label": "person",
            "confidence": 0.85,
            "timestamp": datetime.utcnow().isoformat()
        }
    ]
    client.get_snapshot.return_value = b"\x89PNG" + b"\x00" * 100

    return client


@pytest.fixture
def mock_n8n_client():
    """Mock n8n client"""
    client = AsyncMock()

    client.trigger_workflow.return_value = {
        "success": True,
        "executionId": "exec-123"
    }

    return client


@pytest.fixture
def mock_speaker_service():
    """Mock speaker recognition service"""
    service = AsyncMock()

    service.extract_embedding.return_value = [0.1] * 192  # 192-dimensional embedding
    service.identify_speaker.return_value = {
        "speaker_id": 1,
        "speaker_name": "Max",
        "confidence": 0.85
    }
    service.enroll_speaker.return_value = True

    return service


@pytest.fixture
def mock_plugin_registry():
    """Mock plugin registry"""
    registry = MagicMock()

    registry.get_plugin_for_intent.return_value = None
    registry.get_all_intents.return_value = []
    registry.generate_llm_prompt.return_value = ""

    return registry


# ============================================================================
# Service Fixtures
# ============================================================================

@pytest.fixture
def room_service(db_session: AsyncSession):
    """Create RoomService with test database"""
    from services.room_service import RoomService
    return RoomService(db_session)


@pytest.fixture
def mock_mcp_manager():
    """Create a mock MCP manager"""
    manager = AsyncMock()
    manager.execute_tool.return_value = {
        "success": True,
        "message": "MCP tool executed",
        "action_taken": True,
    }
    return manager


@pytest.fixture
def action_executor(mock_plugin_registry, mock_mcp_manager):
    """Create ActionExecutor with mocked dependencies"""
    from services.action_executor import ActionExecutor

    executor = ActionExecutor(
        plugin_registry=mock_plugin_registry,
        mcp_manager=mock_mcp_manager,
    )

    return executor


# ============================================================================
# FastAPI Test Client Fixtures
# ============================================================================

@pytest.fixture
def override_get_db(db_session: AsyncSession):
    """Override database dependency for FastAPI"""
    async def _override():
        yield db_session
    return _override


@pytest.fixture
async def app_with_test_db(override_get_db, mock_ha_client):
    """FastAPI app with test database and mocked services"""
    from main import app
    from services.database import get_db

    # Override database
    app.dependency_overrides[get_db] = override_get_db

    yield app

    # Cleanup
    app.dependency_overrides.clear()


@pytest.fixture
async def async_client(app_with_test_db) -> AsyncGenerator[AsyncClient, None]:
    """Async HTTP client for API tests"""
    transport = ASGITransport(app=app_with_test_db)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


# ============================================================================
# WebSocket Test Fixtures
# ============================================================================

@pytest.fixture
def mock_websocket():
    """Mock WebSocket connection"""
    ws = AsyncMock()
    ws.accept = AsyncMock()
    ws.send_json = AsyncMock()
    ws.send_bytes = AsyncMock()
    ws.receive_json = AsyncMock()
    ws.receive_bytes = AsyncMock()
    ws.close = AsyncMock()
    ws.client = MagicMock()
    ws.client.host = "127.0.0.1"

    return ws


# ============================================================================
# Utility Fixtures
# ============================================================================

@pytest.fixture
def freeze_time():
    """Fixture for mocking datetime.utcnow()"""
    frozen_time = datetime(2024, 1, 15, 12, 0, 0)

    with patch('datetime.datetime') as mock_datetime:
        mock_datetime.utcnow.return_value = frozen_time
        mock_datetime.side_effect = lambda *args, **kwargs: datetime(*args, **kwargs)
        yield frozen_time


@pytest.fixture
def mock_settings():
    """Mock settings for tests"""
    from utils.config import Settings

    return Settings(
        database_url="sqlite:///:memory:",
        redis_url="redis://localhost:6379",
        ollama_url="http://localhost:11434",
        ollama_model="llama3.2:3b",
        home_assistant_url="http://localhost:8123",
        home_assistant_token="test_token",
        speaker_recognition_enabled=True,
        speaker_auto_enroll=True,
        ws_auth_enabled=False,
        ws_rate_limit_enabled=False
    )
