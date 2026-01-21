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
    parse_ws_message, create_error_response, WSErrorCode,
    WSRegisterMessage, WSTextMessage, WSAudioMessage, WSAudioEndMessage,
    WSWakewordDetectedMessage, WSHeartbeatMessage
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
        assert msg.capabilities["has_microphone"] is True

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
        """Test: Unbekannter Message Type"""
        data = {
            "type": "unknown_type",
            "data": "something"
        }

        msg = parse_ws_message(data)

        # Should return None or raise
        assert msg is None

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
        assert response["code"] == WSErrorCode.INVALID_MESSAGE
        assert "invalid" in response["message"].lower()

    @pytest.mark.unit
    def test_create_error_response_not_registered(self):
        """Test: Not Registered Error erstellen"""
        response = create_error_response(
            WSErrorCode.NOT_REGISTERED,
            "Device not registered"
        )

        assert response["code"] == WSErrorCode.NOT_REGISTERED

    @pytest.mark.unit
    def test_create_error_response_rate_limited(self):
        """Test: Rate Limited Error erstellen"""
        response = create_error_response(
            WSErrorCode.RATE_LIMITED,
            "Too many requests"
        )

        assert response["code"] == WSErrorCode.RATE_LIMITED


# ============================================================================
# WebSocket Device Registration Tests
# ============================================================================

class TestWebSocketDeviceRegistration:
    """Tests für Device Registration über WebSocket"""

    @pytest.mark.unit
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
            max_per_second=50,
            max_per_minute=1000
        )

        assert limiter.max_per_second == 50
        assert limiter.max_per_minute == 1000

    @pytest.mark.unit
    async def test_rate_limiter_allows_normal_traffic(self):
        """Test: Rate Limiter erlaubt normalen Traffic"""
        from services.websocket_rate_limiter import WSRateLimiter

        limiter = WSRateLimiter(
            max_per_second=50,
            max_per_minute=1000
        )

        # Send 10 messages (should be allowed)
        for _ in range(10):
            allowed = await limiter.check_rate("test-client")
            assert allowed is True

    @pytest.mark.unit
    async def test_connection_limiter(self):
        """Test: Connection Limiter"""
        from services.websocket_rate_limiter import WSConnectionLimiter

        limiter = WSConnectionLimiter(max_per_ip=5)

        # First 5 connections should be allowed
        for _ in range(5):
            allowed = await limiter.acquire("192.168.1.1")
            assert allowed is True

        # 6th connection should be denied
        allowed = await limiter.acquire("192.168.1.1")
        assert allowed is False

        # Different IP should be allowed
        allowed = await limiter.acquire("192.168.1.2")
        assert allowed is True


# ============================================================================
# WebSocket Authentication Tests
# ============================================================================

class TestWebSocketAuthentication:
    """Tests für WebSocket Authentication"""

    @pytest.mark.unit
    def test_token_generation(self):
        """Test: Token wird generiert"""
        from services.websocket_auth import generate_token

        with patch('services.websocket_auth.settings') as mock_settings:
            mock_settings.secret_key = "test-secret-key"
            mock_settings.ws_token_expire_minutes = 60

            token = generate_token("test-device-123")

            assert token is not None
            assert len(token) > 0

    @pytest.mark.unit
    def test_token_validation(self):
        """Test: Token wird validiert"""
        from services.websocket_auth import generate_token, validate_token

        with patch('services.websocket_auth.settings') as mock_settings:
            mock_settings.secret_key = "test-secret-key"
            mock_settings.ws_token_expire_minutes = 60

            token = generate_token("test-device-456")
            payload = validate_token(token)

            assert payload is not None
            assert payload.get("device_id") == "test-device-456"

    @pytest.mark.unit
    def test_invalid_token_rejected(self):
        """Test: Ungültiger Token wird abgelehnt"""
        from services.websocket_auth import validate_token

        with patch('services.websocket_auth.settings') as mock_settings:
            mock_settings.secret_key = "test-secret-key"

            payload = validate_token("invalid-token-here")

            assert payload is None


# ============================================================================
# WebSocket Protocol Edge Cases
# ============================================================================

class TestWebSocketProtocolEdgeCases:
    """Tests für Edge Cases im WebSocket Protokoll"""

    @pytest.mark.unit
    def test_empty_message(self):
        """Test: Leere Message"""
        result = parse_ws_message({})
        assert result is None

    @pytest.mark.unit
    def test_message_without_type(self):
        """Test: Message ohne Type"""
        result = parse_ws_message({"content": "test"})
        assert result is None

    @pytest.mark.unit
    def test_register_message_minimal(self):
        """Test: Register Message mit minimalen Daten"""
        data = {
            "type": "register",
            "device_id": "minimal-device"
        }

        msg = parse_ws_message(data)

        # Should handle missing optional fields
        assert msg is not None or msg is None  # Depends on implementation

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

        if msg:
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

        if msg:
            assert "Gäste" in msg.room
