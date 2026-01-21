"""
Frontend Test Fixtures

Provides fixtures for testing frontend-related functionality:
- API response mocking for frontend tests
- Browser automation helpers (for E2E)
- Component test utilities
"""

import pytest
from unittest.mock import MagicMock, AsyncMock


# ============================================================================
# API Mock Fixtures
# ============================================================================

@pytest.fixture
def mock_api_responses():
    """Mock API responses that frontend expects"""
    return {
        "rooms": [
            {"id": 1, "name": "Wohnzimmer", "alias": "wohnzimmer", "device_count": 2},
            {"id": 2, "name": "Küche", "alias": "kueche", "device_count": 1},
        ],
        "devices": [
            {"device_id": "web-living-1", "device_type": "web_panel", "is_online": True},
            {"device_id": "sat-kitchen-1", "device_type": "satellite", "is_online": True},
        ],
        "speakers": [
            {"id": 1, "name": "Max", "alias": "max", "embedding_count": 5},
        ],
        "conversations": [
            {"session_id": "sess-1", "message_count": 10, "created_at": "2024-01-15T12:00:00"},
        ],
    }


@pytest.fixture
def mock_websocket_messages():
    """Mock WebSocket messages for frontend testing"""
    return {
        "register_ack": {
            "type": "register_ack",
            "success": True,
            "device_id": "test-device",
            "room_id": 1,
            "capabilities": {"has_microphone": True, "has_speaker": True}
        },
        "state_idle": {"type": "state", "state": "idle"},
        "state_listening": {"type": "state", "state": "listening"},
        "state_processing": {"type": "state", "state": "processing"},
        "state_speaking": {"type": "state", "state": "speaking"},
        "transcription": {
            "type": "transcription",
            "text": "Schalte das Licht ein",
            "session_id": "test-session"
        },
        "response_text": {
            "type": "response_text",
            "text": "Das Licht wurde eingeschaltet.",
            "session_id": "test-session"
        },
        "tts_audio": {
            "type": "tts_audio",
            "audio": "UklGRi...",  # Base64 WAV header
            "is_final": True,
            "session_id": "test-session"
        },
        "session_end": {
            "type": "session_end",
            "session_id": "test-session",
            "reason": "complete"
        },
        "error": {
            "type": "error",
            "code": 1001,
            "message": "Test error message"
        }
    }


# ============================================================================
# Frontend Configuration Fixtures
# ============================================================================

@pytest.fixture
def frontend_config():
    """Frontend configuration for testing"""
    return {
        "api_url": "http://localhost:8000",
        "ws_url": "ws://localhost:8000/ws",
        "device_ws_url": "ws://localhost:8000/ws/device",
    }


# ============================================================================
# UI State Fixtures
# ============================================================================

@pytest.fixture
def initial_ui_state():
    """Initial UI state for component testing"""
    return {
        "isConnected": False,
        "currentRoom": None,
        "deviceId": None,
        "deviceType": "web_browser",
        "messages": [],
        "isRecording": False,
        "isSpeaking": False,
    }


@pytest.fixture
def connected_ui_state():
    """Connected UI state for component testing"""
    return {
        "isConnected": True,
        "currentRoom": {"id": 1, "name": "Wohnzimmer"},
        "deviceId": "web-test-123",
        "deviceType": "web_panel",
        "messages": [
            {"role": "user", "content": "Hallo"},
            {"role": "assistant", "content": "Hallo! Wie kann ich helfen?"}
        ],
        "isRecording": False,
        "isSpeaking": False,
    }
