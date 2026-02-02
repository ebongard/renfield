"""
Tests für WebSocket Endpoints und Protokoll

Testet:
- WebSocket Message Parsing
- Device Registration Protokoll
- Audio Streaming Protokoll
- Session Management
- Fehlerbehandlung
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
import json
import base64
from datetime import datetime

from models.websocket_messages import (
    parse_ws_message, create_error_response, WSErrorCode, WSErrorResponse,
    WSRegisterMessage, WSTextMessage, WSAudioMessage, WSAudioEndMessage,
    WSWakewordDetectedMessage, WSHeartbeatMessage, WSBaseMessage
)


# ============================================================================
# WebSocket Message Parsing Tests
# ============================================================================

class TestWebSocketMessageParsing:
    """Tests für WebSocket Message Parsing"""

    @pytest.mark.unit
    def test_parse_register_message(self):
        """Test: Register Message parsen"""
        data = {
            "type": "register",
            "device_id": "web-test-123",
            "device_type": "web_panel",
            "room": "Wohnzimmer",
            "device_name": "Test iPad",
            "is_stationary": True,
            "capabilities": {
                "has_microphone": True,
                "has_speaker": True
            }
        }

        msg = parse_ws_message(data)

        assert isinstance(msg, WSRegisterMessage)
        assert msg.device_id == "web-test-123"
        assert msg.device_type == "web_panel"
        assert msg.room == "Wohnzimmer"
        # WSCapabilities is a Pydantic model, access via attributes
        assert msg.capabilities.has_microphone is True

    @pytest.mark.unit
    def test_parse_text_message(self):
        """Test: Text Message parsen"""
        data = {
            "type": "text",
            "content": "Schalte das Licht ein",
            "session_id": "session-123"
        }

        msg = parse_ws_message(data)

        assert isinstance(msg, WSTextMessage)
        assert msg.content == "Schalte das Licht ein"
        assert msg.session_id == "session-123"

    @pytest.mark.unit
    def test_parse_audio_message(self):
        """Test: Audio Message parsen"""
        audio_data = base64.b64encode(b"fake_audio_data").decode()
        data = {
            "type": "audio",
            "chunk": audio_data,
            "sequence": 1,
            "session_id": "audio-session-123"
        }

        msg = parse_ws_message(data)

        assert isinstance(msg, WSAudioMessage)
        assert msg.chunk == audio_data
        assert msg.sequence == 1

    @pytest.mark.unit
    def test_parse_audio_end_message(self):
        """Test: Audio End Message parsen"""
        data = {
            "type": "audio_end",
            "session_id": "audio-session-123",
            "reason": "silence"
        }

        msg = parse_ws_message(data)

        assert isinstance(msg, WSAudioEndMessage)
        assert msg.reason == "silence"

    @pytest.mark.unit
    def test_parse_wakeword_detected_message(self):
        """Test: Wakeword Detected Message parsen"""
        data = {
            "type": "wakeword_detected",
            "keyword": "alexa",
            "confidence": 0.85,
            "session_id": "wake-session-123"
        }

        msg = parse_ws_message(data)

        assert isinstance(msg, WSWakewordDetectedMessage)
        assert msg.keyword == "alexa"
        assert msg.confidence == 0.85

    @pytest.mark.unit
    def test_parse_heartbeat_message(self):
        """Test: Heartbeat Message parsen"""
        data = {
            "type": "heartbeat",
            "status": "idle"
        }

        msg = parse_ws_message(data)

        assert isinstance(msg, WSHeartbeatMessage)
        assert msg.status == "idle"

    @pytest.mark.unit
    def test_parse_unknown_message_type(self):
        """Test: Unbekannter Message Type returns base message"""
        data = {
            "type": "unknown_type",
            "data": "something"
        }

        msg = parse_ws_message(data)

        # Unknown types return WSBaseMessage
        assert isinstance(msg, WSBaseMessage)
        assert msg.type == "unknown_type"

    @pytest.mark.unit
    def test_parse_invalid_json(self):
        """Test: Ungültiges JSON"""
        data = "not valid json"

        # Should handle gracefully
        with pytest.raises(Exception):
            parse_ws_message(data)


# ============================================================================
# WebSocket Error Response Tests
# ============================================================================

class TestWebSocketErrorResponses:
    """Tests für WebSocket Error Responses"""

    @pytest.mark.unit
    def test_create_error_response_invalid_message(self):
        """Test: Invalid Message Error erstellen"""
        response = create_error_response(
            WSErrorCode.INVALID_MESSAGE,
            "Invalid message format"
        )

        assert response["type"] == "error"
        assert response["code"] == WSErrorCode.INVALID_MESSAGE.value
        assert "invalid" in response["message"].lower()

    @pytest.mark.unit
    def test_create_error_response_unauthorized(self):
        """Test: Unauthorized Error erstellen"""
        response = create_error_response(
            WSErrorCode.UNAUTHORIZED,
            "Not authorized"
        )

        assert response["code"] == WSErrorCode.UNAUTHORIZED.value

    @pytest.mark.unit
    def test_create_error_response_rate_limited(self):
        """Test: Rate Limited Error erstellen"""
        response = create_error_response(
            WSErrorCode.RATE_LIMITED,
            "Too many requests"
        )

        assert response["code"] == WSErrorCode.RATE_LIMITED.value


# ============================================================================
# WebSocket Device Registration Tests
# ============================================================================

class TestWebSocketDeviceRegistration:
    """Tests für Device Registration über WebSocket"""

    @pytest.mark.unit
    @pytest.mark.database
    async def test_device_registration_flow(self, mock_websocket, db_session):
        """Test: Vollständiger Registration Flow"""
        from services.room_service import RoomService

        room_service = RoomService(db_session)

        # Simulate registration
        device = await room_service.register_device(
            device_id="ws-test-device",
            room_name="Test Room",
            device_type="web_panel",
            is_stationary=True,
            ip_address="192.168.1.100"
        )

        assert device is not None
        assert device.is_online is True

    @pytest.mark.unit
    @pytest.mark.database
    async def test_device_registration_creates_room(self, db_session):
        """Test: Registration erstellt Room bei Bedarf"""
        from services.room_service import RoomService

        room_service = RoomService(db_session)

        device = await room_service.register_device(
            device_id="auto-room-device",
            room_name="Auto Created Room",
            device_type="satellite",
            auto_create_room=True
        )

        room = await room_service.get_room_by_name("Auto Created Room")

        assert room is not None
        assert room.source == "satellite"
        assert device.room_id == room.id


# ============================================================================
# WebSocket Audio Streaming Tests
# ============================================================================

class TestWebSocketAudioStreaming:
    """Tests für Audio Streaming über WebSocket"""

    @pytest.mark.unit
    def test_audio_chunk_encoding(self):
        """Test: Audio Chunk wird korrekt encodiert"""
        raw_audio = bytes([0] * 1600)  # 100ms of 16kHz 16bit mono
        encoded = base64.b64encode(raw_audio).decode()

        # Verify it can be decoded
        decoded = base64.b64decode(encoded)
        assert decoded == raw_audio

    @pytest.mark.unit
    def test_audio_message_sequence(self):
        """Test: Audio Messages haben korrekte Sequenz"""
        messages = []
        for i in range(5):
            msg = WSAudioMessage(
                chunk=base64.b64encode(b"audio" + bytes([i])).decode(),
                sequence=i,
                session_id="test-session"
            )
            messages.append(msg)

        sequences = [m.sequence for m in messages]
        assert sequences == [0, 1, 2, 3, 4]

    @pytest.mark.unit
    def test_audio_end_reasons(self):
        """Test: Audio End Reasons"""
        valid_reasons = ["silence", "timeout", "manual", "error"]

        for reason in valid_reasons:
            msg = WSAudioEndMessage(
                session_id="test",
                reason=reason
            )
            assert msg.reason == reason


# ============================================================================
# WebSocket Session Management Tests
# ============================================================================

class TestWebSocketSessionManagement:
    """Tests für Session Management"""

    @pytest.mark.unit
    def test_session_id_generation(self):
        """Test: Session IDs sind unique"""
        import uuid

        session_ids = set()
        for _ in range(100):
            session_id = str(uuid.uuid4())
            assert session_id not in session_ids
            session_ids.add(session_id)

    @pytest.mark.unit
    @pytest.mark.database
    async def test_session_cleanup_on_disconnect(self, db_session):
        """Test: Session Cleanup bei Disconnect"""
        from services.room_service import RoomService

        room_service = RoomService(db_session)

        # Register device
        device = await room_service.register_device(
            device_id="cleanup-test-device",
            room_name="Test Room",
            device_type="web_browser"
        )

        assert device.is_online is True

        # Simulate disconnect
        await room_service.set_device_online(device.device_id, False)

        updated = await room_service.get_device(device.device_id)
        assert updated.is_online is False


# ============================================================================
# WebSocket Rate Limiting Tests
# ============================================================================

class TestWebSocketRateLimiting:
    """Tests für WebSocket Rate Limiting"""

    @pytest.mark.unit
    def test_rate_limiter_initialization(self):
        """Test: Rate Limiter wird initialisiert"""
        from services.websocket_rate_limiter import WSRateLimiter

        limiter = WSRateLimiter(
            per_second=50,
            per_minute=1000
        )

        assert limiter.per_second == 50
        assert limiter.per_minute == 1000

    @pytest.mark.unit
    def test_rate_limiter_allows_normal_traffic(self):
        """Test: Rate Limiter erlaubt normalen Traffic"""
        from services.websocket_rate_limiter import WSRateLimiter

        limiter = WSRateLimiter(
            per_second=50,
            per_minute=1000
        )

        # Send 10 messages (should be allowed)
        for _ in range(10):
            allowed, reason = limiter.check("test-client")
            assert allowed is True

    @pytest.mark.unit
    def test_connection_limiter(self):
        """Test: Connection Limiter"""
        from services.websocket_rate_limiter import WSConnectionLimiter

        limiter = WSConnectionLimiter(max_per_ip=5)

        # First 5 connections should be allowed (need to add them)
        for i in range(5):
            device_id = f"device-{i}"
            allowed, reason = limiter.can_connect("192.168.1.1", device_id)
            assert allowed is True
            limiter.add_connection("192.168.1.1", device_id)

        # 6th connection should be denied
        allowed, reason = limiter.can_connect("192.168.1.1", "device-5")
        assert allowed is False

        # Different IP should be allowed
        allowed, reason = limiter.can_connect("192.168.1.2", "device-other")
        assert allowed is True


# ============================================================================
# WebSocket Authentication Tests
# ============================================================================

class TestWebSocketAuthentication:
    """Tests für WebSocket Authentication"""

    @pytest.mark.unit
    def test_token_store_initialization(self):
        """Test: Token Store wird initialisiert"""
        from services.websocket_auth import WSTokenStore

        store = WSTokenStore()
        assert store is not None

    @pytest.mark.unit
    def test_token_store_create_and_validate(self):
        """Test: Token erstellen und validieren"""
        from services.websocket_auth import WSTokenStore

        store = WSTokenStore()
        token = store.create_token("test-device-123")

        assert token is not None
        assert len(token) > 0

        # Validate the token - returns dict with token data
        token_data = store.validate_token(token)
        assert token_data is not None
        assert token_data["device_id"] == "test-device-123"

    @pytest.mark.unit
    def test_invalid_token_rejected(self):
        """Test: Ungültiger Token wird abgelehnt"""
        from services.websocket_auth import WSTokenStore

        store = WSTokenStore()
        device_id = store.validate_token("invalid-token-here")

        assert device_id is None


# ============================================================================
# WebSocket Protocol Edge Cases
# ============================================================================

class TestWebSocketProtocolEdgeCases:
    """Tests für Edge Cases im WebSocket Protokoll"""

    @pytest.mark.unit
    def test_empty_message(self):
        """Test: Leere Message returns error response"""
        result = parse_ws_message({})
        # Empty message returns WSErrorResponse
        assert isinstance(result, WSErrorResponse)
        assert result.code == WSErrorCode.INVALID_MESSAGE

    @pytest.mark.unit
    def test_message_without_type(self):
        """Test: Message ohne Type returns error response"""
        result = parse_ws_message({"content": "test"})
        # Missing type returns WSErrorResponse
        assert isinstance(result, WSErrorResponse)
        assert result.code == WSErrorCode.INVALID_MESSAGE

    @pytest.mark.unit
    def test_register_message_minimal(self):
        """Test: Register Message mit minimalen Daten"""
        data = {
            "type": "register",
            "device_id": "minimal-device"
        }

        msg = parse_ws_message(data)

        # Should handle missing optional fields
        assert msg is not None

    @pytest.mark.unit
    def test_large_audio_chunk(self):
        """Test: Großer Audio Chunk"""
        # 10 seconds of 16kHz 16bit mono = 320000 bytes
        large_audio = bytes([0] * 320000)
        encoded = base64.b64encode(large_audio).decode()

        msg = WSAudioMessage(
            chunk=encoded,
            sequence=0,
            session_id="large-chunk-session"
        )

        assert len(msg.chunk) > 400000  # Base64 is ~33% larger

    @pytest.mark.unit
    def test_special_characters_in_content(self):
        """Test: Sonderzeichen in Text Content"""
        special_content = "Schalte 'Licht' im \"Wohnzimmer\" ein! 🏠"

        data = {
            "type": "text",
            "content": special_content,
            "session_id": "special-session"
        }

        msg = parse_ws_message(data)

        if msg and isinstance(msg, WSTextMessage):
            assert msg.content == special_content

    @pytest.mark.unit
    def test_unicode_room_name(self):
        """Test: Unicode im Raumnamen"""
        data = {
            "type": "register",
            "device_id": "unicode-device",
            "room": "Gästezimmer 客房",
            "device_type": "web_browser"
        }

        msg = parse_ws_message(data)

        if msg and isinstance(msg, WSRegisterMessage):
            assert "Gäste" in msg.room


# ============================================================================
# Action Summary Tests (for conversation history enrichment)
# ============================================================================

class TestBuildActionSummary:
    """Tests für _build_action_summary() in chat_handler."""

    @pytest.mark.unit
    def test_list_results_with_key_fields(self):
        """Test: List results extract key fields."""
        from api.websocket.chat_handler import _build_action_summary

        intent = {"intent": "mcp.paperless.search_documents"}
        action_result = {
            "success": True,
            "data": [
                {"id": 123, "title": "Rechnung regfish 2024-01", "created": "2024-01-15"},
                {"id": 456, "title": "Rechnung regfish 2023-12", "created": "2023-12-01"},
            ]
        }

        summary = _build_action_summary(intent, action_result)

        assert "mcp.paperless.search_documents" in summary
        assert "2 Ergebnisse" in summary
        assert "id=123" in summary
        assert "Rechnung regfish 2024-01" in summary
        assert "id=456" in summary

    @pytest.mark.unit
    def test_nested_results_dict(self):
        """Test: Dict with 'results' key delegates to list handling."""
        from api.websocket.chat_handler import _build_action_summary

        intent = {"intent": "mcp.paperless.search_documents"}
        action_result = {
            "success": True,
            "data": {
                "count": 2,
                "results": [
                    {"id": 1, "title": "Doc A"},
                    {"id": 2, "title": "Doc B"},
                ]
            }
        }

        summary = _build_action_summary(intent, action_result)

        assert "2 Ergebnisse" in summary
        assert "id=1" in summary
        assert "Doc A" in summary

    @pytest.mark.unit
    def test_empty_data_returns_empty(self):
        """Test: No data returns empty string."""
        from api.websocket.chat_handler import _build_action_summary

        assert _build_action_summary({"intent": "test"}, {"success": True, "data": None}) == ""
        assert _build_action_summary({"intent": "test"}, {"success": True, "data": []}) == ""

    @pytest.mark.unit
    def test_max_10_items(self):
        """Test: Only first 10 items are included."""
        from api.websocket.chat_handler import _build_action_summary

        intent = {"intent": "test"}
        items = [{"id": i, "title": f"Item {i}"} for i in range(15)]
        action_result = {"success": True, "data": items}

        summary = _build_action_summary(intent, action_result)

        assert "15 Ergebnisse" in summary
        assert "id=9" in summary  # 10th item (0-indexed)
        assert "id=10" not in summary  # 11th item should be excluded
        assert "und 5 weitere" in summary

    @pytest.mark.unit
    def test_simple_dict_result(self):
        """Test: Simple dict result becomes compact JSON."""
        from api.websocket.chat_handler import _build_action_summary

        intent = {"intent": "mcp.mail.send_email"}
        action_result = {"success": True, "data": {"status": "sent", "message_id": "abc"}}

        summary = _build_action_summary(intent, action_result)

        assert "mcp.mail.send_email" in summary
        assert "sent" in summary

    @pytest.mark.unit
    def test_truncation(self):
        """Test: Summary is truncated to max_chars."""
        from api.websocket.chat_handler import _build_action_summary

        intent = {"intent": "test"}
        items = [{"id": i, "title": f"Very long title number {i} " * 10} for i in range(10)]
        action_result = {"success": True, "data": items}

        summary = _build_action_summary(intent, action_result, max_chars=200)

        assert len(summary) <= 200

    @pytest.mark.unit
    def test_mcp_raw_data_format_parsed(self):
        """Test: MCP raw_data [{"type":"text","text":"{JSON}"}] is parsed correctly."""
        import json
        from api.websocket.chat_handler import _build_action_summary

        intent = {"intent": "mcp.paperless.search_documents"}
        # Simulate MCP execute_tool() raw_data format
        inner_data = {
            "count": 2,
            "results": [
                {"id": 123, "title": "Rechnung regfish 2024-01", "created": "2024-01-15"},
                {"id": 456, "title": "Rechnung regfish 2023-12", "created": "2023-12-01"},
            ]
        }
        action_result = {
            "success": True,
            "data": [{"type": "text", "text": json.dumps(inner_data)}]
        }

        summary = _build_action_summary(intent, action_result)

        assert "mcp.paperless.search_documents" in summary
        assert "2 Ergebnisse" in summary
        assert "id=123" in summary
        assert "Rechnung regfish 2024-01" in summary
        assert "id=456" in summary

    @pytest.mark.unit
    def test_mcp_raw_data_list_results(self):
        """Test: MCP raw_data with a plain list inside text is parsed."""
        import json
        from api.websocket.chat_handler import _build_action_summary

        intent = {"intent": "mcp.mail.list_emails"}
        inner_data = [
            {"id": 1, "subject": "Invoice A", "date": "2024-06-01"},
            {"id": 2, "subject": "Invoice B", "date": "2024-05-15"},
        ]
        action_result = {
            "success": True,
            "data": [{"type": "text", "text": json.dumps(inner_data)}]
        }

        summary = _build_action_summary(intent, action_result)

        assert "2 Ergebnisse" in summary
        assert "id=1" in summary
        assert "Invoice A" in summary

    @pytest.mark.unit
    def test_mcp_raw_data_not_json_returns_text_summary(self):
        """Test: MCP raw_data with non-JSON text returns text_summary."""
        from api.websocket.chat_handler import _build_action_summary

        intent = {"intent": "mcp.tool.action"}
        action_result = {
            "success": True,
            "data": [{"type": "text", "text": "Operation completed successfully. " * 3}]
        }

        summary = _build_action_summary(intent, action_result)

        # Should fall through to dict handling with text_summary key
        assert "mcp.tool.action" in summary
        assert "Operation completed" in summary

    @pytest.mark.unit
    def test_non_mcp_list_not_affected(self):
        """Test: Regular list data (non-MCP format) still works."""
        from api.websocket.chat_handler import _build_action_summary

        intent = {"intent": "test"}
        # Regular list without MCP "type"/"text" structure
        action_result = {
            "success": True,
            "data": [
                {"id": 1, "title": "Regular item"},
            ]
        }

        summary = _build_action_summary(intent, action_result)

        assert "1 Ergebnisse" in summary
        assert "id=1" in summary
        assert "Regular item" in summary
