"""
Integration Test Fixtures

Provides fixtures for cross-component integration testing:
- Full system setup helpers
- Multi-component communication mocks
- End-to-end test utilities
"""

import pytest
from unittest.mock import MagicMock, AsyncMock, patch
import asyncio


# ============================================================================
# Full System Fixtures
# ============================================================================

@pytest.fixture
def system_config():
    """Full system configuration for integration tests"""
    return {
        "backend": {
            "url": "http://localhost:8000",
            "ws_url": "ws://localhost:8000/ws",
            "satellite_ws_url": "ws://localhost:8000/ws/satellite",
            "device_ws_url": "ws://localhost:8000/ws/device",
        },
        "ollama": {
            "url": "http://localhost:11434",
            "model": "llama3.2:3b",
        },
        "home_assistant": {
            "url": "http://localhost:8123",
            "token": "test_token",
        },
        "database": {
            "url": "postgresql://renfield:test@localhost:5432/renfield_test",
        },
    }


@pytest.fixture
def mock_full_backend():
    """Mock full backend for integration tests"""
    backend = MagicMock()

    # API endpoints
    backend.get_rooms = AsyncMock(return_value=[
        {"id": 1, "name": "Wohnzimmer"},
        {"id": 2, "name": "Küche"}
    ])
    backend.get_devices = AsyncMock(return_value=[
        {"device_id": "sat-1", "is_online": True}
    ])

    # WebSocket
    backend.ws_connect = AsyncMock()
    backend.ws_send = AsyncMock()
    backend.ws_receive = AsyncMock()

    return backend


# ============================================================================
# End-to-End Scenario Fixtures
# ============================================================================

@pytest.fixture
def voice_command_scenario():
    """Complete voice command scenario"""
    return {
        "input": {
            "audio": bytes([0] * 32000),  # 1 second of audio
            "transcription": "Schalte das Licht im Wohnzimmer ein",
        },
        "processing": {
            "intent": "homeassistant.turn_on",
            "entity_id": "light.wohnzimmer",
            "confidence": 0.95,
        },
        "output": {
            "ha_result": {"success": True},
            "tts_text": "Das Wohnzimmer Licht ist jetzt eingeschaltet.",
            "tts_audio": bytes([0] * 16000),  # 0.5 seconds response
        }
    }


@pytest.fixture
def satellite_session_scenario():
    """Complete satellite session scenario"""
    return {
        "satellite_id": "sat-wohnzimmer-main",
        "room": "Wohnzimmer",
        "steps": [
            {"action": "register", "expected_response": "register_ack"},
            {"action": "wakeword_detected", "expected_state": "listening"},
            {"action": "audio_stream", "chunk_count": 20},
            {"action": "audio_end", "expected_state": "processing"},
            {"action": "receive_transcription", "text": "Schalte das Licht ein"},
            {"action": "receive_tts", "expected_state": "speaking"},
            {"action": "playback_complete", "expected_state": "idle"},
        ]
    }


@pytest.fixture
def web_client_scenario():
    """Complete web client scenario"""
    return {
        "device_id": "web-browser-123",
        "device_type": "web_browser",
        "steps": [
            {"action": "connect_ws"},
            {"action": "register"},
            {"action": "send_text", "content": "Wie ist das Wetter?"},
            {"action": "receive_response"},
        ]
    }


# ============================================================================
# Multi-Component Communication Fixtures
# ============================================================================

@pytest.fixture
def backend_to_ha_communication():
    """Backend to Home Assistant communication mock"""
    return {
        "turn_on_light": {
            "request": {
                "domain": "light",
                "service": "turn_on",
                "entity_id": "light.wohnzimmer"
            },
            "response": {"status": 200}
        },
        "get_state": {
            "request": {"entity_id": "light.wohnzimmer"},
            "response": {
                "state": "on",
                "attributes": {"brightness": 255}
            }
        }
    }


@pytest.fixture
def backend_to_ollama_communication():
    """Backend to Ollama communication mock"""
    return {
        "intent_extraction": {
            "request": {
                "model": "llama3.2:3b",
                "prompt": "Extract intent from: Schalte das Licht ein"
            },
            "response": {
                "intent": "homeassistant.turn_on",
                "parameters": {"entity_id": "light.wohnzimmer"},
                "confidence": 0.95
            }
        },
        "response_generation": {
            "request": {
                "model": "llama3.2:3b",
                "prompt": "Generate response for action result"
            },
            "response": {
                "text": "Das Licht wurde eingeschaltet."
            }
        }
    }


@pytest.fixture
def satellite_to_backend_communication():
    """Satellite to Backend communication mock"""
    return {
        "registration": {
            "message": {
                "type": "register",
                "satellite_id": "sat-1",
                "room": "Wohnzimmer"
            },
            "response": {
                "type": "register_ack",
                "success": True
            }
        },
        "audio_stream": {
            "messages": [
                {"type": "audio", "chunk": "base64...", "sequence": i}
                for i in range(10)
            ],
            "end_message": {"type": "audio_end", "reason": "silence"}
        }
    }


# ============================================================================
# Test Database Fixtures
# ============================================================================

@pytest.fixture
async def integration_test_db():
    """Setup integration test database"""
    # This would create a test database with seed data
    db_config = {
        "rooms": [
            {"id": 1, "name": "Wohnzimmer", "alias": "wohnzimmer"},
            {"id": 2, "name": "Küche", "alias": "kueche"},
            {"id": 3, "name": "Schlafzimmer", "alias": "schlafzimmer"},
        ],
        "devices": [
            {"device_id": "sat-1", "room_id": 1, "device_type": "satellite"},
            {"device_id": "web-1", "room_id": 1, "device_type": "web_panel"},
        ],
        "speakers": [
            {"id": 1, "name": "Max", "alias": "max"},
        ],
        "conversations": [
            {"session_id": "test-session-1", "message_count": 5},
        ]
    }

    yield db_config

    # Cleanup would happen here


# ============================================================================
# Network Simulation Fixtures
# ============================================================================

@pytest.fixture
def network_latency_simulation():
    """Simulate network latency for integration tests"""
    async def add_latency(delay_ms: int = 50):
        await asyncio.sleep(delay_ms / 1000)

    return add_latency


@pytest.fixture
def network_failure_simulation():
    """Simulate network failures for integration tests"""
    class NetworkFailure:
        def __init__(self):
            self.fail_count = 0
            self.fail_every_n = 0

        def should_fail(self) -> bool:
            if self.fail_every_n == 0:
                return False
            self.fail_count += 1
            return self.fail_count % self.fail_every_n == 0

        def set_failure_rate(self, every_n: int):
            self.fail_every_n = every_n
            self.fail_count = 0

    return NetworkFailure()
