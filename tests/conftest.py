"""
Root Conftest - Shared Fixtures for All Tests

Provides:
- Common utilities shared across backend, frontend, satellite tests
- Integration test helpers
- Environment configuration
"""

import pytest
import os
import sys
from pathlib import Path

# Add project paths to sys.path for imports
PROJECT_ROOT = Path(__file__).parent.parent
SRC_PATH = PROJECT_ROOT / "src"
BACKEND_PATH = SRC_PATH / "backend"
FRONTEND_PATH = SRC_PATH / "frontend"
SATELLITE_PATH = SRC_PATH / "satellite"

sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(SRC_PATH))
sys.path.insert(0, str(BACKEND_PATH))
sys.path.insert(0, str(SATELLITE_PATH))


# ============================================================================
# Environment Fixtures
# ============================================================================

@pytest.fixture(scope="session")
def project_root():
    """Return project root path"""
    return PROJECT_ROOT


@pytest.fixture(scope="session")
def src_path():
    """Return src path"""
    return SRC_PATH


@pytest.fixture(scope="session")
def backend_path():
    """Return backend path (src/backend)"""
    return BACKEND_PATH


@pytest.fixture(scope="session")
def frontend_path():
    """Return frontend path (src/frontend)"""
    return FRONTEND_PATH


@pytest.fixture(scope="session")
def satellite_path():
    """Return satellite path (src/satellite)"""
    return SATELLITE_PATH


# ============================================================================
# Test Environment Configuration
# ============================================================================

@pytest.fixture(scope="session", autouse=True)
def setup_test_environment():
    """Setup test environment variables"""
    # Set test-specific environment variables
    os.environ.setdefault("TESTING", "1")
    os.environ.setdefault("LOG_LEVEL", "WARNING")

    yield

    # Cleanup if needed


# ============================================================================
# Shared Mock Factories
# ============================================================================

@pytest.fixture
def mock_http_response():
    """Factory for creating mock HTTP responses"""
    from unittest.mock import MagicMock

    def _create_response(status_code=200, json_data=None, content=None):
        response = MagicMock()
        response.status_code = status_code
        response.json.return_value = json_data or {}
        response.content = content or b""
        response.raise_for_status = MagicMock()
        return response

    return _create_response


@pytest.fixture
def mock_websocket_factory():
    """Factory for creating mock WebSocket connections"""
    from unittest.mock import AsyncMock, MagicMock

    def _create_websocket(client_ip="127.0.0.1"):
        ws = AsyncMock()
        ws.accept = AsyncMock()
        ws.send_json = AsyncMock()
        ws.send_bytes = AsyncMock()
        ws.receive_json = AsyncMock()
        ws.receive_bytes = AsyncMock()
        ws.close = AsyncMock()
        ws.client = MagicMock()
        ws.client.host = client_ip
        return ws

    return _create_websocket


# ============================================================================
# Common Test Data
# ============================================================================

@pytest.fixture
def sample_audio_data():
    """Generate sample audio data for testing"""
    import base64

    # 100ms of 16kHz 16-bit mono silence
    raw_audio = bytes([0] * 3200)
    return {
        "raw": raw_audio,
        "base64": base64.b64encode(raw_audio).decode(),
        "sample_rate": 16000,
        "channels": 1,
        "duration_ms": 100
    }


@pytest.fixture
def sample_home_assistant_entities():
    """Sample Home Assistant entities for testing"""
    return [
        {
            "entity_id": "light.wohnzimmer",
            "state": "off",
            "attributes": {
                "friendly_name": "Wohnzimmer Licht",
                "brightness": 255,
                "supported_features": 41
            }
        },
        {
            "entity_id": "switch.fernseher",
            "state": "on",
            "attributes": {
                "friendly_name": "Fernseher",
                "icon": "mdi:television"
            }
        },
        {
            "entity_id": "climate.heizung",
            "state": "heat",
            "attributes": {
                "friendly_name": "Heizung Wohnzimmer",
                "temperature": 21.5,
                "current_temperature": 20.2
            }
        },
        {
            "entity_id": "media_player.sonos",
            "state": "idle",
            "attributes": {
                "friendly_name": "Sonos Speaker",
                "volume_level": 0.5
            }
        }
    ]


@pytest.fixture
def sample_rooms():
    """Sample room data for testing"""
    return [
        {"name": "Wohnzimmer", "alias": "wohnzimmer", "icon": "mdi:sofa"},
        {"name": "Küche", "alias": "kueche", "icon": "mdi:pot"},
        {"name": "Schlafzimmer", "alias": "schlafzimmer", "icon": "mdi:bed"},
        {"name": "Büro", "alias": "buero", "icon": "mdi:desk"},
    ]


@pytest.fixture
def sample_intents():
    """Sample intents for testing"""
    return [
        {
            "intent": "homeassistant.turn_on",
            "parameters": {"entity_id": "light.wohnzimmer"},
            "confidence": 0.95
        },
        {
            "intent": "homeassistant.turn_off",
            "parameters": {"entity_id": "switch.fernseher"},
            "confidence": 0.92
        },
        {
            "intent": "homeassistant.get_state",
            "parameters": {"entity_id": "climate.heizung"},
            "confidence": 0.88
        },
        {
            "intent": "general.conversation",
            "parameters": {},
            "confidence": 0.75
        }
    ]
