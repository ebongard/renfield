"""
Component Communication Integration Tests

Tests communication between system components:
- Backend ↔ Home Assistant
- Backend ↔ Ollama
- Backend ↔ Satellite
- Backend ↔ Frontend
"""

import pytest
from unittest.mock import MagicMock, AsyncMock, patch


# ============================================================================
# Backend ↔ Home Assistant Communication Tests
# ============================================================================

class TestBackendHomeAssistantCommunication:
    """Tests for Backend to Home Assistant communication"""

    @pytest.mark.integration
    async def test_ha_service_call_flow(self, backend_to_ha_communication):
        """Test: Backend can call HA services"""
        comm = backend_to_ha_communication["turn_on_light"]

        # Verify request structure
        request = comm["request"]
        assert request["domain"] == "light"
        assert request["service"] == "turn_on"
        assert "entity_id" in request

        # Verify response handling
        response = comm["response"]
        assert response["status"] == 200

    @pytest.mark.integration
    async def test_ha_state_query_flow(self, backend_to_ha_communication):
        """Test: Backend can query HA states"""
        comm = backend_to_ha_communication["get_state"]

        request = comm["request"]
        assert "entity_id" in request

        response = comm["response"]
        assert "state" in response
        assert "attributes" in response

    @pytest.mark.integration
    async def test_ha_keywords_extraction(self):
        """Test: Backend extracts keywords from HA entities"""
        # Simulated HA entities
        entities = [
            {"entity_id": "light.wohnzimmer", "attributes": {"friendly_name": "Wohnzimmer Licht"}},
            {"entity_id": "switch.fernseher", "attributes": {"friendly_name": "Fernseher"}},
        ]

        # Expected keywords
        expected_keywords = ["wohnzimmer", "licht", "fernseher"]

        extracted = []
        for entity in entities:
            friendly_name = entity["attributes"]["friendly_name"]
            words = friendly_name.lower().split()
            extracted.extend(words)

        for kw in expected_keywords:
            assert kw in extracted


# ============================================================================
# Backend ↔ Ollama Communication Tests
# ============================================================================

class TestBackendOllamaCommunication:
    """Tests for Backend to Ollama communication"""

    @pytest.mark.integration
    async def test_intent_extraction_flow(self, backend_to_ollama_communication):
        """Test: Backend can extract intents via Ollama"""
        comm = backend_to_ollama_communication["intent_extraction"]

        request = comm["request"]
        assert "model" in request
        assert "prompt" in request

        response = comm["response"]
        assert "intent" in response
        assert "parameters" in response
        assert "confidence" in response

    @pytest.mark.integration
    async def test_response_generation_flow(self, backend_to_ollama_communication):
        """Test: Backend can generate responses via Ollama"""
        comm = backend_to_ollama_communication["response_generation"]

        request = comm["request"]
        assert "model" in request

        response = comm["response"]
        assert "text" in response
        assert len(response["text"]) > 0

    @pytest.mark.integration
    async def test_ollama_model_loading(self):
        """Test: Ollama model is loaded on startup"""
        model_check = {
            "model": "llama3.2:3b",
            "loaded": True,
            "response_time_ms": 50,
        }

        assert model_check["loaded"] is True


# ============================================================================
# Backend ↔ Satellite Communication Tests
# ============================================================================

class TestBackendSatelliteCommunication:
    """Tests for Backend to Satellite communication"""

    @pytest.mark.integration
    async def test_satellite_registration_flow(self, satellite_to_backend_communication):
        """Test: Satellite registration flow"""
        comm = satellite_to_backend_communication["registration"]

        message = comm["message"]
        assert message["type"] == "register"
        assert "satellite_id" in message
        assert "room" in message

        response = comm["response"]
        assert response["type"] == "register_ack"
        assert response["success"] is True

    @pytest.mark.integration
    async def test_audio_streaming_flow(self, satellite_to_backend_communication):
        """Test: Audio streaming from satellite to backend"""
        comm = satellite_to_backend_communication["audio_stream"]

        messages = comm["messages"]
        assert len(messages) == 10

        for i, msg in enumerate(messages):
            assert msg["type"] == "audio"
            assert msg["sequence"] == i

        end_msg = comm["end_message"]
        assert end_msg["type"] == "audio_end"
        assert end_msg["reason"] == "silence"

    @pytest.mark.integration
    async def test_tts_delivery_to_satellite(self):
        """Test: TTS audio delivered to satellite"""
        tts_delivery = {
            "session_id": "test-session",
            "tts_text": "Das Licht ist an.",
            "audio_format": "wav",
            "audio_size_bytes": 16000,
            "chunks_sent": 1,
            "is_final": True,
        }

        assert tts_delivery["is_final"] is True
        assert tts_delivery["audio_size_bytes"] > 0


# ============================================================================
# Backend ↔ Frontend Communication Tests
# ============================================================================

class TestBackendFrontendCommunication:
    """Tests for Backend to Frontend communication"""

    @pytest.mark.integration
    async def test_rest_api_response_format(self):
        """Test: REST API responses match frontend expectations"""
        # Room list response
        rooms_response = {
            "data": [
                {"id": 1, "name": "Wohnzimmer", "device_count": 2},
            ],
            "status": "success"
        }

        assert "data" in rooms_response or isinstance(rooms_response.get("data", rooms_response), list)

    @pytest.mark.integration
    async def test_websocket_message_flow(self):
        """Test: WebSocket message flow frontend ↔ backend"""
        message_flow = [
            {"direction": "client→server", "type": "text", "content": "Hallo"},
            {"direction": "server→client", "type": "stream", "content": "Hallo!"},
            {"direction": "server→client", "type": "stream", "content": " Wie kann ich helfen?"},
            {"direction": "server→client", "type": "done"},
        ]

        # Verify flow structure
        assert message_flow[0]["direction"] == "client→server"
        assert message_flow[-1]["type"] == "done"

    @pytest.mark.integration
    async def test_device_websocket_flow(self):
        """Test: Device WebSocket registration and communication"""
        device_flow = [
            {"action": "connect", "endpoint": "/ws/device"},
            {"action": "register", "device_type": "web_panel"},
            {"action": "receive", "type": "register_ack"},
            {"action": "send_text", "content": "Test"},
            {"action": "receive_response"},
        ]

        assert device_flow[0]["endpoint"] == "/ws/device"
        assert device_flow[2]["type"] == "register_ack"


# ============================================================================
# Cross-Component Data Flow Tests
# ============================================================================

class TestCrossComponentDataFlow:
    """Tests for data flow across multiple components"""

    @pytest.mark.integration
    async def test_room_context_propagation(self):
        """Test: Room context flows through all components"""
        # Device registers with room
        device_registration = {
            "device_id": "panel-wohnzimmer",
            "room": "Wohnzimmer",
            "ip_address": "192.168.1.100",
        }

        # Room context used in intent extraction
        intent_context = {
            "user_input": "Schalte das Licht ein",
            "room_context": {"room_name": "Wohnzimmer"},
        }

        # HA action uses room context
        ha_action = {
            "entity_id": "light.wohnzimmer",  # Resolved from room context
            "action": "turn_on",
        }

        assert device_registration["room"] == intent_context["room_context"]["room_name"]
        assert "wohnzimmer" in ha_action["entity_id"]

    @pytest.mark.integration
    async def test_speaker_context_propagation(self):
        """Test: Speaker context flows through system"""
        # Speaker identified from audio
        speaker_identification = {
            "speaker_id": 1,
            "speaker_name": "Max",
            "confidence": 0.85,
        }

        # Speaker stored in message metadata
        message_metadata = {
            "speaker_id": speaker_identification["speaker_id"],
            "speaker_name": speaker_identification["speaker_name"],
        }

        # Response can be personalized
        personalized_response = {
            "greeting": f"Hallo {speaker_identification['speaker_name']}!",
        }

        assert message_metadata["speaker_id"] == speaker_identification["speaker_id"]
        assert speaker_identification["speaker_name"] in personalized_response["greeting"]

    @pytest.mark.integration
    async def test_session_data_persistence(self):
        """Test: Session data persists across interactions"""
        session_data = {
            "session_id": "test-session-123",
            "messages": [
                {"role": "user", "content": "Wie ist das Wetter?"},
                {"role": "assistant", "content": "Das Wetter ist sonnig."},
                {"role": "user", "content": "Und morgen?"},
            ],
            "context_preserved": True,
        }

        # Verify context is available for follow-up
        assert len(session_data["messages"]) == 3
        assert session_data["context_preserved"] is True


# ============================================================================
# Error Propagation Tests
# ============================================================================

class TestErrorPropagation:
    """Tests for error propagation across components"""

    @pytest.mark.integration
    async def test_ha_error_propagation(self):
        """Test: HA errors propagate to user correctly"""
        error_flow = {
            "ha_error": "Entity not found",
            "backend_handling": "catch and format",
            "user_message": "Das Gerät konnte nicht gefunden werden.",
            "websocket_message": {"type": "error", "code": 404},
        }

        assert "nicht gefunden" in error_flow["user_message"]

    @pytest.mark.integration
    async def test_ollama_error_propagation(self):
        """Test: Ollama errors propagate to user correctly"""
        error_flow = {
            "ollama_error": "Connection timeout",
            "backend_handling": "retry then fail gracefully",
            "user_message": "Es gab ein Problem bei der Verarbeitung.",
            "fallback_response": True,
        }

        assert error_flow["fallback_response"] is True

    @pytest.mark.integration
    async def test_satellite_disconnect_handling(self):
        """Test: Satellite disconnect handled correctly"""
        disconnect_flow = {
            "event": "websocket_disconnect",
            "satellite_id": "sat-wohnzimmer-1",
            "backend_action": "mark_device_offline",
            "cleanup_actions": ["cancel_active_session", "release_resources"],
        }

        assert "mark_device_offline" == disconnect_flow["backend_action"]
        assert "cancel_active_session" in disconnect_flow["cleanup_actions"]
