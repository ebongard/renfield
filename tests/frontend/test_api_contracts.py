"""
Frontend API Contract Tests

Tests that verify the API responses match what the frontend expects.
These tests ensure backend changes don't break the frontend.
"""

import pytest
from unittest.mock import patch, AsyncMock


# ============================================================================
# Rooms API Contract Tests
# ============================================================================

class TestRoomsAPIContract:
    """Tests for Rooms API contract that frontend depends on"""

    @pytest.mark.frontend
    def test_rooms_list_response_structure(self, mock_api_responses):
        """Test: Rooms list has expected structure"""
        rooms = mock_api_responses["rooms"]

        for room in rooms:
            assert "id" in room
            assert "name" in room
            assert "alias" in room
            assert isinstance(room["id"], int)
            assert isinstance(room["name"], str)

    @pytest.mark.frontend
    def test_room_device_count_field(self, mock_api_responses):
        """Test: Room has device_count for UI display"""
        rooms = mock_api_responses["rooms"]

        for room in rooms:
            assert "device_count" in room
            assert isinstance(room["device_count"], int)
            assert room["device_count"] >= 0


# ============================================================================
# Devices API Contract Tests
# ============================================================================

class TestDevicesAPIContract:
    """Tests for Devices API contract"""

    @pytest.mark.frontend
    def test_device_response_structure(self, mock_api_responses):
        """Test: Device response has expected fields"""
        devices = mock_api_responses["devices"]

        required_fields = ["device_id", "device_type", "is_online"]

        for device in devices:
            for field in required_fields:
                assert field in device, f"Missing field: {field}"

    @pytest.mark.frontend
    def test_device_type_values(self, mock_api_responses):
        """Test: Device types are valid values"""
        valid_types = ["satellite", "web_panel", "web_tablet", "web_browser", "web_kiosk"]
        devices = mock_api_responses["devices"]

        for device in devices:
            assert device["device_type"] in valid_types


# ============================================================================
# Speakers API Contract Tests
# ============================================================================

class TestSpeakersAPIContract:
    """Tests for Speakers API contract"""

    @pytest.mark.frontend
    def test_speaker_response_structure(self, mock_api_responses):
        """Test: Speaker response has expected fields"""
        speakers = mock_api_responses["speakers"]

        for speaker in speakers:
            assert "id" in speaker
            assert "name" in speaker
            assert "alias" in speaker

    @pytest.mark.frontend
    def test_speaker_embedding_count(self, mock_api_responses):
        """Test: Speaker has embedding_count for enrollment status"""
        speakers = mock_api_responses["speakers"]

        for speaker in speakers:
            assert "embedding_count" in speaker
            assert isinstance(speaker["embedding_count"], int)


# ============================================================================
# Conversations API Contract Tests
# ============================================================================

class TestConversationsAPIContract:
    """Tests for Conversations API contract"""

    @pytest.mark.frontend
    def test_conversation_response_structure(self, mock_api_responses):
        """Test: Conversation response has expected fields"""
        conversations = mock_api_responses["conversations"]

        for conv in conversations:
            assert "session_id" in conv
            assert "message_count" in conv

    @pytest.mark.frontend
    def test_conversation_has_timestamp(self, mock_api_responses):
        """Test: Conversation has timestamp for sorting"""
        conversations = mock_api_responses["conversations"]

        for conv in conversations:
            assert "created_at" in conv


# ============================================================================
# WebSocket Message Contract Tests
# ============================================================================

class TestWebSocketMessageContract:
    """Tests for WebSocket message contracts"""

    @pytest.mark.frontend
    def test_register_ack_structure(self, mock_websocket_messages):
        """Test: register_ack has required fields"""
        msg = mock_websocket_messages["register_ack"]

        assert msg["type"] == "register_ack"
        assert "success" in msg
        assert "device_id" in msg

    @pytest.mark.frontend
    def test_state_message_structure(self, mock_websocket_messages):
        """Test: state message has state field"""
        for key in ["state_idle", "state_listening", "state_processing", "state_speaking"]:
            msg = mock_websocket_messages[key]
            assert msg["type"] == "state"
            assert "state" in msg

    @pytest.mark.frontend
    def test_state_values(self, mock_websocket_messages):
        """Test: state values are valid"""
        valid_states = ["idle", "listening", "processing", "speaking"]

        assert mock_websocket_messages["state_idle"]["state"] == "idle"
        assert mock_websocket_messages["state_listening"]["state"] == "listening"
        assert mock_websocket_messages["state_processing"]["state"] == "processing"
        assert mock_websocket_messages["state_speaking"]["state"] == "speaking"

    @pytest.mark.frontend
    def test_transcription_message_structure(self, mock_websocket_messages):
        """Test: transcription message has text"""
        msg = mock_websocket_messages["transcription"]

        assert msg["type"] == "transcription"
        assert "text" in msg
        assert "session_id" in msg

    @pytest.mark.frontend
    def test_response_text_structure(self, mock_websocket_messages):
        """Test: response_text message structure"""
        msg = mock_websocket_messages["response_text"]

        assert msg["type"] == "response_text"
        assert "text" in msg
        assert "session_id" in msg

    @pytest.mark.frontend
    def test_tts_audio_structure(self, mock_websocket_messages):
        """Test: tts_audio message structure"""
        msg = mock_websocket_messages["tts_audio"]

        assert msg["type"] == "tts_audio"
        assert "audio" in msg  # Base64 encoded
        assert "is_final" in msg
        assert "session_id" in msg

    @pytest.mark.frontend
    def test_session_end_structure(self, mock_websocket_messages):
        """Test: session_end message structure"""
        msg = mock_websocket_messages["session_end"]

        assert msg["type"] == "session_end"
        assert "session_id" in msg
        assert "reason" in msg

    @pytest.mark.frontend
    def test_error_message_structure(self, mock_websocket_messages):
        """Test: error message structure"""
        msg = mock_websocket_messages["error"]

        assert msg["type"] == "error"
        assert "code" in msg
        assert "message" in msg


# ============================================================================
# UI State Contract Tests
# ============================================================================

class TestUIStateContract:
    """Tests for UI state management contracts"""

    @pytest.mark.frontend
    def test_initial_state_fields(self, initial_ui_state):
        """Test: Initial UI state has all required fields"""
        required_fields = [
            "isConnected",
            "currentRoom",
            "deviceId",
            "deviceType",
            "messages",
            "isRecording",
            "isSpeaking"
        ]

        for field in required_fields:
            assert field in initial_ui_state

    @pytest.mark.frontend
    def test_initial_state_defaults(self, initial_ui_state):
        """Test: Initial state has correct defaults"""
        assert initial_ui_state["isConnected"] is False
        assert initial_ui_state["currentRoom"] is None
        assert initial_ui_state["messages"] == []
        assert initial_ui_state["isRecording"] is False

    @pytest.mark.frontend
    def test_connected_state_fields(self, connected_ui_state):
        """Test: Connected state has room and device info"""
        assert connected_ui_state["isConnected"] is True
        assert connected_ui_state["currentRoom"] is not None
        assert connected_ui_state["deviceId"] is not None

    @pytest.mark.frontend
    def test_messages_structure(self, connected_ui_state):
        """Test: Messages have role and content"""
        messages = connected_ui_state["messages"]

        for msg in messages:
            assert "role" in msg
            assert "content" in msg
            assert msg["role"] in ["user", "assistant"]
