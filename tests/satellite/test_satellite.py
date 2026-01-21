"""
Satellite Core Tests

Tests for the main satellite functionality:
- State machine
- Wake word detection integration
- Audio streaming
- Backend communication
"""

import pytest
from unittest.mock import MagicMock, AsyncMock, patch
import asyncio


# ============================================================================
# State Machine Tests
# ============================================================================

class TestSatelliteStateMachine:
    """Tests for satellite state machine"""

    @pytest.mark.satellite
    def test_valid_states(self, satellite_states):
        """Test: All valid states are defined"""
        expected = ["idle", "listening", "processing", "speaking", "error"]
        assert satellite_states == expected

    @pytest.mark.satellite
    def test_idle_to_listening_transition(self, satellite_state_transitions):
        """Test: idle can transition to listening"""
        assert "listening" in satellite_state_transitions["idle"]

    @pytest.mark.satellite
    def test_listening_transitions(self, satellite_state_transitions):
        """Test: listening can transition to processing or idle"""
        transitions = satellite_state_transitions["listening"]
        assert "processing" in transitions
        assert "idle" in transitions

    @pytest.mark.satellite
    def test_processing_transitions(self, satellite_state_transitions):
        """Test: processing can transition to speaking, idle, or error"""
        transitions = satellite_state_transitions["processing"]
        assert "speaking" in transitions
        assert "idle" in transitions
        assert "error" in transitions

    @pytest.mark.satellite
    def test_speaking_to_idle_transition(self, satellite_state_transitions):
        """Test: speaking transitions to idle"""
        assert "idle" in satellite_state_transitions["speaking"]

    @pytest.mark.satellite
    def test_error_recovery(self, satellite_state_transitions):
        """Test: error can recover to idle"""
        assert "idle" in satellite_state_transitions["error"]


# ============================================================================
# Wake Word Detection Tests
# ============================================================================

class TestWakeWordDetection:
    """Tests for wake word detection"""

    @pytest.mark.satellite
    def test_detector_initialization(self, mock_wakeword_detector):
        """Test: Detector can be initialized"""
        assert mock_wakeword_detector is not None

    @pytest.mark.satellite
    def test_no_detection_returns_false(self, mock_wakeword_detector):
        """Test: No wake word returns False"""
        result = mock_wakeword_detector.detect(bytes([0] * 3200))
        assert result is False

    @pytest.mark.satellite
    def test_detection_event_structure(self, wakeword_detection_event):
        """Test: Detection event has required fields"""
        assert "keyword" in wakeword_detection_event
        assert "confidence" in wakeword_detection_event
        assert "timestamp" in wakeword_detection_event

    @pytest.mark.satellite
    def test_confidence_threshold(self, wakeword_detection_event, satellite_config):
        """Test: Detection confidence exceeds threshold"""
        confidence = wakeword_detection_event["confidence"]
        threshold = satellite_config["wake_word_threshold"]
        assert confidence >= threshold

    @pytest.mark.satellite
    def test_detector_reset(self, mock_wakeword_detector):
        """Test: Detector can be reset"""
        mock_wakeword_detector.reset()
        mock_wakeword_detector.reset.assert_called_once()


# ============================================================================
# Audio Capture Tests
# ============================================================================

class TestAudioCapture:
    """Tests for audio capture"""

    @pytest.mark.satellite
    def test_microphone_start_stop(self, mock_microphone):
        """Test: Microphone can start and stop"""
        mock_microphone.start()
        mock_microphone.start.assert_called_once()

        mock_microphone.stop()
        mock_microphone.stop.assert_called_once()

    @pytest.mark.satellite
    def test_audio_frame_size(self, mock_microphone, satellite_config):
        """Test: Audio frames have correct size"""
        frame = mock_microphone.read()

        # 100ms at 16kHz, 16-bit mono = 3200 bytes
        expected_size = int(satellite_config["sample_rate"] * 0.1 * 2)
        assert len(frame) == expected_size

    @pytest.mark.satellite
    def test_audio_frames_sequence(self, sample_audio_frames):
        """Test: Audio frames can be collected in sequence"""
        assert len(sample_audio_frames) == 10
        for frame in sample_audio_frames:
            assert len(frame) == 3200


# ============================================================================
# Audio Playback Tests
# ============================================================================

class TestAudioPlayback:
    """Tests for audio playback"""

    @pytest.mark.satellite
    async def test_speaker_play(self, mock_speaker):
        """Test: Speaker can play audio"""
        audio_data = bytes([0] * 6400)
        await mock_speaker.play(audio_data)
        mock_speaker.play.assert_called_once_with(audio_data)

    @pytest.mark.satellite
    def test_speaker_volume_control(self, mock_speaker):
        """Test: Speaker volume can be set"""
        mock_speaker.set_volume(0.5)
        mock_speaker.set_volume.assert_called_once_with(0.5)


# ============================================================================
# LED Control Tests
# ============================================================================

class TestLEDControl:
    """Tests for LED control"""

    @pytest.mark.satellite
    def test_led_set_color(self, mock_led_controller):
        """Test: LED color can be set"""
        mock_led_controller.set_color(0, (255, 0, 0))  # Red
        mock_led_controller.set_color.assert_called_once()

    @pytest.mark.satellite
    def test_led_off(self, mock_led_controller):
        """Test: LEDs can be turned off"""
        mock_led_controller.off()
        mock_led_controller.off.assert_called_once()

    @pytest.mark.satellite
    def test_led_breathing_effect(self, mock_led_controller):
        """Test: Breathing effect can be triggered"""
        mock_led_controller.breathing((0, 0, 255))  # Blue
        mock_led_controller.breathing.assert_called_once()


# ============================================================================
# Configuration Tests
# ============================================================================

class TestSatelliteConfiguration:
    """Tests for satellite configuration"""

    @pytest.mark.satellite
    def test_config_has_satellite_id(self, satellite_config):
        """Test: Config has satellite_id"""
        assert "satellite_id" in satellite_config
        assert satellite_config["satellite_id"].startswith("sat-")

    @pytest.mark.satellite
    def test_config_has_room(self, satellite_config):
        """Test: Config has room assignment"""
        assert "room" in satellite_config
        assert len(satellite_config["room"]) > 0

    @pytest.mark.satellite
    def test_config_audio_settings(self, satellite_config):
        """Test: Config has audio settings"""
        assert satellite_config["sample_rate"] == 16000
        assert satellite_config["chunk_size"] == 1600

    @pytest.mark.satellite
    def test_config_wake_word_settings(self, satellite_config):
        """Test: Config has wake word settings"""
        assert "wake_word" in satellite_config
        assert "wake_word_threshold" in satellite_config
        assert 0 < satellite_config["wake_word_threshold"] < 1


# ============================================================================
# Capabilities Tests
# ============================================================================

class TestSatelliteCapabilities:
    """Tests for satellite capabilities"""

    @pytest.mark.satellite
    def test_has_audio_capabilities(self, satellite_capabilities):
        """Test: Satellite has audio capabilities"""
        assert satellite_capabilities["has_microphone"] is True
        assert satellite_capabilities["has_speaker"] is True

    @pytest.mark.satellite
    def test_has_wakeword_capability(self, satellite_capabilities):
        """Test: Satellite has wake word capability"""
        assert satellite_capabilities["has_wakeword"] is True
        assert satellite_capabilities["wakeword_method"] == "openwakeword"

    @pytest.mark.satellite
    def test_has_led_capability(self, satellite_capabilities):
        """Test: Satellite has LED capability"""
        assert satellite_capabilities["has_leds"] is True
        assert satellite_capabilities["led_count"] == 3

    @pytest.mark.satellite
    def test_no_display_capability(self, satellite_capabilities):
        """Test: Satellite has no display"""
        assert satellite_capabilities["has_display"] is False


# ============================================================================
# WebSocket Communication Tests
# ============================================================================

class TestSatelliteWebSocket:
    """Tests for satellite WebSocket communication"""

    @pytest.mark.satellite
    def test_register_message_structure(self, satellite_register_message):
        """Test: Register message has correct structure"""
        msg = satellite_register_message

        assert msg["type"] == "register"
        assert "satellite_id" in msg
        assert "room" in msg
        assert "capabilities" in msg

    @pytest.mark.satellite
    def test_audio_message_structure(self, satellite_audio_message):
        """Test: Audio message has correct structure"""
        msg = satellite_audio_message

        assert msg["type"] == "audio"
        assert "chunk" in msg
        assert "sequence" in msg
        assert "session_id" in msg

    @pytest.mark.satellite
    def test_wakeword_message_structure(self, satellite_wakeword_message):
        """Test: Wake word message has correct structure"""
        msg = satellite_wakeword_message

        assert msg["type"] == "wakeword_detected"
        assert "keyword" in msg
        assert "confidence" in msg
        assert "session_id" in msg

    @pytest.mark.satellite
    async def test_websocket_send(self, mock_websocket_client, satellite_register_message):
        """Test: WebSocket can send messages"""
        await mock_websocket_client.send(satellite_register_message)
        mock_websocket_client.send.assert_called_once()


# ============================================================================
# Backend Response Handling Tests
# ============================================================================

class TestBackendResponseHandling:
    """Tests for handling backend responses"""

    @pytest.mark.satellite
    def test_register_ack_success(self, backend_response_messages):
        """Test: Register ack indicates success"""
        msg = backend_response_messages["register_ack"]

        assert msg["type"] == "register_ack"
        assert msg["success"] is True
        assert "config" in msg

    @pytest.mark.satellite
    def test_state_change_messages(self, backend_response_messages):
        """Test: State change messages are valid"""
        states = ["listening", "processing", "speaking", "idle"]

        for state in states:
            msg = backend_response_messages[f"state_{state}"]
            assert msg["type"] == "state"
            assert msg["state"] == state

    @pytest.mark.satellite
    def test_transcription_message(self, backend_response_messages):
        """Test: Transcription message has text"""
        msg = backend_response_messages["transcription"]

        assert msg["type"] == "transcription"
        assert "text" in msg
        assert len(msg["text"]) > 0

    @pytest.mark.satellite
    def test_tts_audio_message(self, backend_response_messages):
        """Test: TTS audio message has audio data"""
        msg = backend_response_messages["tts_audio"]

        assert msg["type"] == "tts_audio"
        assert "audio" in msg
        assert "is_final" in msg


# ============================================================================
# Zeroconf Discovery Tests
# ============================================================================

class TestZeroconfDiscovery:
    """Tests for Zeroconf service discovery"""

    @pytest.mark.satellite
    def test_discovery_result_structure(self, mock_zeroconf_discovery):
        """Test: Discovery result has required fields"""
        result = mock_zeroconf_discovery

        assert "host" in result
        assert "port" in result
        assert "name" in result

    @pytest.mark.satellite
    def test_discovery_provides_ws_path(self, mock_zeroconf_discovery):
        """Test: Discovery provides WebSocket path"""
        props = mock_zeroconf_discovery["properties"]
        assert "ws_path" in props
        assert props["ws_path"] == "/ws/satellite"
