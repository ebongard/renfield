"""
Tests für Backend Services

Testet:
- OllamaService (LLM Chat, Intent Extraction)
- RAGService (Document Processing, Search)
- SpeakerService (Voice Embeddings, Identification)
- ActionExecutor (Intent Execution)
- AudioPreprocessor (Audio Processing)
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import datetime
import numpy as np


# ============================================================================
# OllamaService Tests
# ============================================================================

class TestOllamaService:
    """Tests für OllamaService"""

    @pytest.fixture
    def mock_ollama_client(self):
        """Mock Ollama Client"""
        with patch('services.ollama_service.ollama') as mock:
            mock_client = MagicMock()

            # Mock for list models
            mock_model = MagicMock()
            mock_model.model = "llama3.2:3b"
            mock_list = MagicMock()
            mock_list.models = [mock_model]
            mock_client.list = AsyncMock(return_value=mock_list)

            # Mock for chat
            mock_response = MagicMock()
            mock_response.message = MagicMock()
            mock_response.message.content = "Test response"
            mock_client.chat = AsyncMock(return_value=mock_response)

            mock.AsyncClient = MagicMock(return_value=mock_client)
            yield mock_client

    @pytest.mark.unit
    async def test_chat_simple(self, mock_ollama_client):
        """Testet einfachen Chat"""
        from services.ollama_service import OllamaService

        with patch('services.ollama_service.settings') as mock_settings:
            mock_settings.ollama_url = "http://localhost:11434"
            mock_settings.ollama_model = "llama3.2:3b"
            mock_settings.ollama_chat_model = "llama3.2:3b"
            mock_settings.ollama_rag_model = "llama3.2:3b"
            mock_settings.ollama_embed_model = "nomic-embed-text"
            mock_settings.ollama_intent_model = "llama3.2:3b"

            service = OllamaService()
            service.client = mock_ollama_client

            response = await service.chat("Hallo, wie geht es dir?")

        assert response == "Test response"

    @pytest.mark.unit
    async def test_chat_with_history(self, mock_ollama_client):
        """Testet Chat mit Historie"""
        from services.ollama_service import OllamaService

        with patch('services.ollama_service.settings') as mock_settings:
            mock_settings.ollama_url = "http://localhost:11434"
            mock_settings.ollama_model = "llama3.2:3b"
            mock_settings.ollama_chat_model = "llama3.2:3b"
            mock_settings.ollama_rag_model = "llama3.2:3b"
            mock_settings.ollama_embed_model = "nomic-embed-text"
            mock_settings.ollama_intent_model = "llama3.2:3b"

            service = OllamaService()
            service.client = mock_ollama_client

            history = [
                {"role": "user", "content": "Schalte das Licht ein"},
                {"role": "assistant", "content": "Ich habe das Licht eingeschaltet."}
            ]

            response = await service.chat("Und jetzt aus", history=history)

        assert response == "Test response"

    @pytest.mark.unit
    async def test_ensure_model_loaded(self, mock_ollama_client):
        """Testet Model-Loading"""
        from services.ollama_service import OllamaService

        with patch('services.ollama_service.settings') as mock_settings:
            mock_settings.ollama_url = "http://localhost:11434"
            mock_settings.ollama_model = "llama3.2:3b"
            mock_settings.ollama_chat_model = "llama3.2:3b"
            mock_settings.ollama_rag_model = "llama3.2:3b"
            mock_settings.ollama_embed_model = "nomic-embed-text"
            mock_settings.ollama_intent_model = "llama3.2:3b"

            service = OllamaService()
            service.client = mock_ollama_client

            await service.ensure_model_loaded()

        mock_ollama_client.list.assert_called_once()


# ============================================================================
# RAGService Tests
# ============================================================================

class TestRAGService:
    """Tests für RAGService"""

    @pytest.mark.unit
    async def test_get_embedding(self, db_session):
        """Testet Embedding-Generierung"""
        from services.rag_service import RAGService

        # Create mock client
        mock_client = MagicMock()
        mock_response = MagicMock()
        mock_response.embedding = [0.1] * 768  # 768 dimensions
        mock_client.embeddings = AsyncMock(return_value=mock_response)

        with patch('services.rag_service.settings') as mock_settings:
            mock_settings.ollama_url = "http://localhost:11434"
            mock_settings.ollama_embed_model = "nomic-embed-text"

            service = RAGService(db_session)
            # Set the mock client directly
            service._ollama_client = mock_client

            embedding = await service.get_embedding("Test text for embedding")

        assert len(embedding) == 768
        assert all(isinstance(x, float) for x in embedding)

    @pytest.mark.database
    async def test_ingest_document_missing_file(self, db_session):
        """Testet Dokument-Ingestion mit fehlender Datei"""
        from services.rag_service import RAGService

        with patch('services.rag_service.settings') as mock_settings:
            mock_settings.ollama_url = "http://localhost:11434"
            mock_settings.ollama_embed_model = "nomic-embed-text"

            service = RAGService(db_session)

            with patch.object(service.processor, 'process_document') as mock_process:
                mock_process.return_value = {
                    "status": "failed",
                    "error": "File not found"
                }

                doc = await service.ingest_document("/nonexistent/file.pdf")

        assert doc.status == "failed"
        assert "not found" in doc.error_message.lower()


# ============================================================================
# SpeakerService Tests
# ============================================================================

class TestSpeakerService:
    """Tests für SpeakerService"""

    @pytest.mark.unit
    def test_speaker_service_availability_check(self):
        """Testet Verfügbarkeitsprüfung"""
        with patch('services.speaker_service.SPEECHBRAIN_AVAILABLE', True):
            from services.speaker_service import SpeakerService

            with patch('services.speaker_service.settings') as mock_settings:
                mock_settings.speaker_recognition_device = 'cpu'
                mock_settings.speaker_recognition_threshold = 0.25

                service = SpeakerService()

        # Service should be initialized
        assert service.similarity_threshold == 0.25

    @pytest.mark.unit
    def test_speaker_service_not_available(self):
        """Testet wenn SpeechBrain nicht verfügbar"""
        # Import and test directly
        from services.speaker_service import SpeakerService, SPEECHBRAIN_AVAILABLE

        with patch('services.speaker_service.settings') as mock_settings:
            mock_settings.speaker_recognition_device = 'cpu'
            mock_settings.speaker_recognition_threshold = 0.25

            service = SpeakerService()

        # is_available() returns SPEECHBRAIN_AVAILABLE status
        assert service.is_available() == SPEECHBRAIN_AVAILABLE

    @pytest.mark.unit
    def test_embedding_to_base64(self):
        """Testet Embedding zu Base64 Konvertierung"""
        from services.speaker_service import SpeakerService

        with patch('services.speaker_service.settings') as mock_settings:
            mock_settings.speaker_recognition_device = 'cpu'
            mock_settings.speaker_recognition_threshold = 0.25

            service = SpeakerService()

            # Create test embedding
            embedding = np.array([0.1, 0.2, 0.3, 0.4])
            base64_str = service.embedding_to_base64(embedding)

        assert isinstance(base64_str, str)
        assert len(base64_str) > 0

    @pytest.mark.unit
    def test_embedding_from_base64(self):
        """Testet Base64 zu Embedding Konvertierung"""
        from services.speaker_service import SpeakerService

        with patch('services.speaker_service.settings') as mock_settings:
            mock_settings.speaker_recognition_device = 'cpu'
            mock_settings.speaker_recognition_threshold = 0.25

            service = SpeakerService()

            # Create test embedding and convert
            original = np.array([0.1, 0.2, 0.3, 0.4])
            base64_str = service.embedding_to_base64(original)
            recovered = service.embedding_from_base64(base64_str)

        np.testing.assert_array_almost_equal(original, recovered)

    @pytest.mark.unit
    def test_compute_similarity(self):
        """Testet Cosine Similarity Berechnung"""
        from services.speaker_service import SpeakerService

        with patch('services.speaker_service.settings') as mock_settings:
            mock_settings.speaker_recognition_device = 'cpu'
            mock_settings.speaker_recognition_threshold = 0.25

            service = SpeakerService()

            # Same vectors should have similarity of 1
            v1 = np.array([1.0, 0.0, 0.0])
            v2 = np.array([1.0, 0.0, 0.0])
            similarity = service.compute_similarity(v1, v2)

        assert abs(similarity - 1.0) < 0.001

    @pytest.mark.unit
    def test_compute_similarity_orthogonal(self):
        """Testet Cosine Similarity für orthogonale Vektoren"""
        from services.speaker_service import SpeakerService

        with patch('services.speaker_service.settings') as mock_settings:
            mock_settings.speaker_recognition_device = 'cpu'
            mock_settings.speaker_recognition_threshold = 0.25

            service = SpeakerService()

            # Orthogonal vectors should have similarity of 0
            v1 = np.array([1.0, 0.0, 0.0])
            v2 = np.array([0.0, 1.0, 0.0])
            similarity = service.compute_similarity(v1, v2)

        assert abs(similarity) < 0.001


# ============================================================================
# ActionExecutor Tests
# ============================================================================

class TestActionExecutor:
    """Tests für ActionExecutor"""

    @pytest.fixture
    def mock_ha_client(self):
        """Mock Home Assistant Client"""
        client = AsyncMock()
        client.turn_on = AsyncMock(return_value=True)
        client.turn_off = AsyncMock(return_value=True)
        client.toggle = AsyncMock(return_value=True)
        client.get_state = AsyncMock(return_value={
            "state": "on",
            "attributes": {"friendly_name": "Test Light"}
        })
        return client

    @pytest.mark.unit
    async def test_execute_turn_on(self, mock_ha_client):
        """Testet Turn On Ausführung"""
        from services.action_executor import ActionExecutor

        executor = ActionExecutor()
        executor.ha_client = mock_ha_client

        result = await executor.execute({
            "intent": "homeassistant.turn_on",
            "parameters": {"entity_id": "light.test"},
            "confidence": 0.9
        })

        assert result["success"] is True
        mock_ha_client.turn_on.assert_called_once_with("light.test")

    @pytest.mark.unit
    async def test_execute_turn_off(self, mock_ha_client):
        """Testet Turn Off Ausführung"""
        from services.action_executor import ActionExecutor

        executor = ActionExecutor()
        executor.ha_client = mock_ha_client

        result = await executor.execute({
            "intent": "homeassistant.turn_off",
            "parameters": {"entity_id": "light.test"},
            "confidence": 0.9
        })

        assert result["success"] is True
        mock_ha_client.turn_off.assert_called_once_with("light.test")

    @pytest.mark.unit
    async def test_execute_get_state(self, mock_ha_client):
        """Testet Get State Ausführung"""
        from services.action_executor import ActionExecutor

        executor = ActionExecutor()
        executor.ha_client = mock_ha_client

        result = await executor.execute({
            "intent": "homeassistant.get_state",
            "parameters": {"entity_id": "light.test"},
            "confidence": 0.9
        })

        assert result["success"] is True

    @pytest.mark.unit
    async def test_execute_conversation_intent(self, mock_ha_client):
        """Testet dass Conversation Intent keine Aktion ausführt"""
        from services.action_executor import ActionExecutor

        executor = ActionExecutor()
        executor.ha_client = mock_ha_client

        result = await executor.execute({
            "intent": "general.conversation",
            "parameters": {},
            "confidence": 0.5
        })

        assert result["success"] is True
        assert result.get("action_taken") is False


# ============================================================================
# Audio Preprocessor Tests
# ============================================================================

class TestAudioPreprocessor:
    """Tests für AudioPreprocessor"""

    @pytest.mark.unit
    def test_audio_preprocessor_init(self):
        """Testet Initialisierung"""
        from services.audio_preprocessor import AudioPreprocessor

        preprocessor = AudioPreprocessor()

        assert preprocessor is not None
        assert preprocessor.sample_rate == 16000

    @pytest.mark.unit
    def test_normalize_audio(self):
        """Testet Audio-Normalisierung"""
        from services.audio_preprocessor import AudioPreprocessor

        preprocessor = AudioPreprocessor()

        # Create test audio (quiet signal)
        audio = np.array([0.01, 0.02, -0.01, -0.02] * 1000, dtype=np.float32)

        normalized = preprocessor.normalize(audio)

        # Normalized audio should have higher amplitude
        assert np.max(np.abs(normalized)) > np.max(np.abs(audio))


# ============================================================================
# DeviceManager Tests
# ============================================================================

class TestDeviceManager:
    """Tests für DeviceManager"""

    @pytest.mark.unit
    def test_device_manager_init(self):
        """Testet Initialisierung"""
        from services.device_manager import DeviceManager

        manager = DeviceManager()

        assert manager is not None
        assert manager.devices == {}

    @pytest.mark.unit
    async def test_register_device(self):
        """Testet Geräte-Registrierung"""
        from services.device_manager import DeviceManager

        manager = DeviceManager()
        device_id = "test-device-123"

        # Create a mock WebSocket
        mock_ws = AsyncMock()
        mock_ws.send_json = AsyncMock()

        result = await manager.register(
            device_id=device_id,
            device_type="web_browser",
            room=None,
            websocket=mock_ws,
            capabilities={"has_microphone": True}
        )

        # Register returns the ConnectedDevice
        assert result is not None
        assert device_id in manager.devices

    @pytest.mark.unit
    async def test_unregister_device(self):
        """Testet Geräte-Abmeldung"""
        from services.device_manager import DeviceManager

        manager = DeviceManager()
        device_id = "test-device-456"

        # Create a mock WebSocket
        mock_ws = AsyncMock()
        mock_ws.send_json = AsyncMock()

        await manager.register(
            device_id=device_id,
            device_type="web_browser",
            room=None,
            websocket=mock_ws,
            capabilities={}
        )

        await manager.unregister(device_id)

        assert device_id not in manager.devices

    @pytest.mark.unit
    async def test_get_devices_by_room(self):
        """Testet Geräte nach Raum filtern"""
        from services.device_manager import DeviceManager

        manager = DeviceManager()

        # Create mock WebSockets
        mock_ws1 = AsyncMock()
        mock_ws2 = AsyncMock()
        mock_ws3 = AsyncMock()
        mock_ws1.send_json = AsyncMock()
        mock_ws2.send_json = AsyncMock()
        mock_ws3.send_json = AsyncMock()

        # Register devices in different rooms
        await manager.register("dev1", "web_browser", "Living Room", mock_ws1, {})
        await manager.register("dev2", "web_panel", "Living Room", mock_ws2, {})
        await manager.register("dev3", "web_browser", "Kitchen", mock_ws3, {})

        living_room_devices = manager.get_devices_in_room("Living Room")

        assert len(living_room_devices) == 2


# ============================================================================
# RoomService Tests
# ============================================================================

class TestRoomServiceUnit:
    """Unit Tests für RoomService"""

    @pytest.mark.database
    async def test_create_room(self, db_session):
        """Testet Raum-Erstellung"""
        from services.room_service import RoomService

        # Create a fresh session to avoid transaction issues
        service = RoomService(db_session)

        room = await service.create_room(
            name="Test Room Unit Create",
            source="renfield"
        )

        # Flush and refresh to ensure the room is persisted
        await db_session.flush()

        assert room.id is not None
        assert room.name == "Test Room Unit Create"
        # Alias is auto-generated from name
        assert room.alias == "testroomunitcreate"

    @pytest.mark.database
    async def test_get_room_by_alias(self, db_session, test_room):
        """Testet Raum-Abfrage nach Alias"""
        from services.room_service import RoomService

        # Flush to ensure test_room is in the database
        await db_session.flush()

        service = RoomService(db_session)

        room = await service.get_room_by_alias(test_room.alias)

        assert room is not None
        assert room.id == test_room.id

    @pytest.mark.database
    async def test_get_all_rooms(self, db_session, test_room):
        """Testet Raum-Liste"""
        from services.room_service import RoomService

        # Flush to ensure test_room is in the database
        await db_session.flush()

        service = RoomService(db_session)

        rooms = await service.get_all_rooms()

        assert len(rooms) >= 1
        assert any(r.id == test_room.id for r in rooms)
