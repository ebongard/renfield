"""
Satellite Test Fixtures

Provides fixtures for testing satellite functionality:
- Mock hardware interfaces (LEDs, buttons, microphone)
- Mock network connections
- Audio test data
- Wake word detection mocks
"""

import pytest
from unittest.mock import MagicMock, AsyncMock, patch
import asyncio


# ============================================================================
# Hardware Mock Fixtures
# ============================================================================

@pytest.fixture
def mock_led_controller():
    """Mock LED controller for ReSpeaker HAT"""
    controller = MagicMock()
    controller.set_color = MagicMock()
    controller.set_all = MagicMock()
    controller.off = MagicMock()
    controller.breathing = MagicMock()
    controller.spin = MagicMock()
    controller.pulse = MagicMock()
    return controller


@pytest.fixture
def mock_button():
    """Mock GPIO button"""
    button = MagicMock()
    button.is_pressed = False
    button.wait_for_press = MagicMock()
    button.when_pressed = None
    button.when_released = None
    return button


@pytest.fixture
def mock_microphone():
    """Mock microphone capture"""
    mic = MagicMock()
    mic.start = MagicMock()
    mic.stop = MagicMock()
    mic.is_recording = False

    # Generate fake audio frames
    def fake_read():
        return bytes([0] * 3200)  # 100ms of 16kHz 16-bit mono

    mic.read = fake_read
    return mic


@pytest.fixture
def mock_speaker():
    """Mock speaker playback"""
    speaker = MagicMock()
    speaker.play = AsyncMock()
    speaker.play_file = AsyncMock()
    speaker.stop = MagicMock()
    speaker.is_playing = False
    speaker.set_volume = MagicMock()
    return speaker


# ============================================================================
# Wake Word Mock Fixtures
# ============================================================================

@pytest.fixture
def mock_wakeword_detector():
    """Mock OpenWakeWord detector"""
    detector = MagicMock()
    detector.detect = MagicMock(return_value=False)
    detector.get_confidence = MagicMock(return_value=0.0)
    detector.reset = MagicMock()

    return detector


@pytest.fixture
def wakeword_detection_event():
    """Simulated wake word detection"""
    return {
        "keyword": "alexa",
        "confidence": 0.85,
        "timestamp": 1705320000.0
    }


# ============================================================================
# Network Mock Fixtures
# ============================================================================

@pytest.fixture
def mock_websocket_client():
    """Mock WebSocket client for satellite"""
    client = AsyncMock()
    client.connect = AsyncMock()
    client.disconnect = AsyncMock()
    client.send = AsyncMock()
    client.receive = AsyncMock()
    client.is_connected = False

    return client


@pytest.fixture
def mock_zeroconf_discovery():
    """Mock Zeroconf service discovery"""
    return {
        "host": "192.168.1.100",
        "port": 8000,
        "name": "renfield-backend",
        "properties": {
            "version": "1.0",
            "ws_path": "/ws/satellite"
        }
    }


# ============================================================================
# Satellite Configuration Fixtures
# ============================================================================

@pytest.fixture
def satellite_config():
    """Sample satellite configuration"""
    return {
        "satellite_id": "sat-wohnzimmer-main",
        "room": "Wohnzimmer",
        "backend_url": "ws://192.168.1.100:8000/ws/satellite",
        "wake_word": "alexa",
        "wake_word_threshold": 0.5,
        "sample_rate": 16000,
        "chunk_size": 1600,
        "led_enabled": True,
        "button_enabled": True,
    }


@pytest.fixture
def satellite_capabilities():
    """Satellite device capabilities"""
    return {
        "has_microphone": True,
        "has_speaker": True,
        "has_wakeword": True,
        "wakeword_method": "openwakeword",
        "has_display": False,
        "has_leds": True,
        "led_count": 3,
        "has_button": True,
    }


# ============================================================================
# Audio Test Data Fixtures
# ============================================================================

@pytest.fixture
def sample_audio_frames():
    """Generate sample audio frames for testing"""
    frames = []
    for i in range(10):
        # 100ms frames of silence with some variation
        frame = bytes([i % 256] * 3200)
        frames.append(frame)
    return frames


@pytest.fixture
def sample_speech_audio():
    """Sample speech audio data"""
    # Simulate 2 seconds of "speech" (just non-zero data)
    import random
    random.seed(42)
    return bytes([random.randint(0, 255) for _ in range(64000)])


# ============================================================================
# State Machine Fixtures
# ============================================================================

@pytest.fixture
def satellite_states():
    """Valid satellite states"""
    return ["idle", "listening", "processing", "speaking", "error"]


@pytest.fixture
def satellite_state_transitions():
    """Valid state transitions"""
    return {
        "idle": ["listening"],
        "listening": ["processing", "idle"],
        "processing": ["speaking", "idle", "error"],
        "speaking": ["idle"],
        "error": ["idle"],
    }


# ============================================================================
# WebSocket Message Fixtures
# ============================================================================

@pytest.fixture
def satellite_register_message(satellite_config, satellite_capabilities):
    """Registration message from satellite"""
    return {
        "type": "register",
        "satellite_id": satellite_config["satellite_id"],
        "room": satellite_config["room"],
        "capabilities": satellite_capabilities
    }


@pytest.fixture
def satellite_audio_message():
    """Audio chunk message from satellite"""
    import base64
    return {
        "type": "audio",
        "chunk": base64.b64encode(bytes([0] * 3200)).decode(),
        "sequence": 0,
        "session_id": "session-123"
    }


@pytest.fixture
def satellite_wakeword_message(wakeword_detection_event):
    """Wake word detection message"""
    return {
        "type": "wakeword_detected",
        "keyword": wakeword_detection_event["keyword"],
        "confidence": wakeword_detection_event["confidence"],
        "session_id": "session-456"
    }


@pytest.fixture
def backend_response_messages():
    """Backend response messages for satellite"""
    return {
        "register_ack": {
            "type": "register_ack",
            "success": True,
            "config": {
                "wake_words": ["alexa"],
                "threshold": 0.5
            }
        },
        "state_listening": {"type": "state", "state": "listening"},
        "state_processing": {"type": "state", "state": "processing"},
        "state_speaking": {"type": "state", "state": "speaking"},
        "state_idle": {"type": "state", "state": "idle"},
        "transcription": {
            "type": "transcription",
            "session_id": "session-123",
            "text": "Schalte das Licht ein"
        },
        "tts_audio": {
            "type": "tts_audio",
            "session_id": "session-123",
            "audio": "UklGRi...",  # Base64 WAV
            "is_final": True
        }
    }
