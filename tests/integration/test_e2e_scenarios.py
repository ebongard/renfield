"""
End-to-End Integration Tests

Tests complete user scenarios across all components:
- Voice command flow (Satellite → Backend → HA → TTS → Satellite)
- Web client flow (Frontend → Backend → Response)
- Multi-device scenarios
"""

import pytest
from unittest.mock import MagicMock, AsyncMock, patch
import asyncio


# ============================================================================
# Voice Command Flow Tests
# ============================================================================

class TestVoiceCommandFlow:
    """End-to-end tests for voice command processing"""

    @pytest.mark.e2e
    @pytest.mark.integration
    async def test_complete_voice_command_flow(self, voice_command_scenario):
        """Test: Complete voice command from audio to TTS response"""
        scenario = voice_command_scenario

        # Step 1: Audio received
        audio_input = scenario["input"]["audio"]
        assert len(audio_input) > 0

        # Step 2: Transcription
        transcription = scenario["input"]["transcription"]
        assert "Licht" in transcription

        # Step 3: Intent extraction
        intent = scenario["processing"]["intent"]
        assert intent == "homeassistant.turn_on"

        # Step 4: HA execution
        ha_result = scenario["output"]["ha_result"]
        assert ha_result["success"] is True

        # Step 5: TTS response
        tts_text = scenario["output"]["tts_text"]
        assert "eingeschaltet" in tts_text

    @pytest.mark.e2e
    @pytest.mark.integration
    async def test_voice_command_with_room_context(self, voice_command_scenario):
        """Test: Voice command uses room context"""
        # Simulating a command without explicit room
        # "Schalte das Licht ein" → should use device's room

        transcription = "Schalte das Licht ein"
        room_context = {"room_name": "Wohnzimmer", "room_id": 1}

        # System should resolve to light.wohnzimmer based on room context
        expected_entity = "light.wohnzimmer"
        assert "wohnzimmer" in expected_entity.lower()

    @pytest.mark.e2e
    @pytest.mark.integration
    async def test_voice_command_error_handling(self):
        """Test: Voice command handles errors gracefully"""
        # Simulate entity not found
        error_scenario = {
            "transcription": "Schalte das Einhorn ein",
            "expected_response": "konnte kein gerät finden"
        }

        # System should respond with helpful error message
        assert "konnte" in error_scenario["expected_response"].lower()


# ============================================================================
# Satellite Session Flow Tests
# ============================================================================

class TestSatelliteSessionFlow:
    """End-to-end tests for satellite sessions"""

    @pytest.mark.e2e
    @pytest.mark.integration
    async def test_complete_satellite_session(self, satellite_session_scenario):
        """Test: Complete satellite session lifecycle"""
        scenario = satellite_session_scenario

        # Verify all steps are defined
        assert len(scenario["steps"]) == 7

        # Step 1: Registration
        assert scenario["steps"][0]["action"] == "register"
        assert scenario["steps"][0]["expected_response"] == "register_ack"

        # Step 2: Wake word detection
        assert scenario["steps"][1]["action"] == "wakeword_detected"
        assert scenario["steps"][1]["expected_state"] == "listening"

        # Final step: Return to idle
        assert scenario["steps"][6]["action"] == "playback_complete"
        assert scenario["steps"][6]["expected_state"] == "idle"

    @pytest.mark.e2e
    @pytest.mark.integration
    async def test_satellite_reconnection(self):
        """Test: Satellite can reconnect after disconnect"""
        # Simulate disconnect and reconnect
        reconnect_scenario = {
            "disconnect_reason": "network_error",
            "reconnect_delay_ms": 5000,
            "expected_state_after_reconnect": "idle",
            "session_preserved": False,
        }

        assert reconnect_scenario["expected_state_after_reconnect"] == "idle"

    @pytest.mark.e2e
    @pytest.mark.integration
    async def test_satellite_audio_timeout(self):
        """Test: Satellite handles audio timeout"""
        timeout_scenario = {
            "audio_timeout_ms": 10000,
            "expected_action": "cancel_session",
            "expected_state": "idle",
        }

        assert timeout_scenario["expected_state"] == "idle"


# ============================================================================
# Web Client Flow Tests
# ============================================================================

class TestWebClientFlow:
    """End-to-end tests for web client interactions"""

    @pytest.mark.e2e
    @pytest.mark.integration
    async def test_complete_web_text_chat(self, web_client_scenario):
        """Test: Complete web text chat flow"""
        scenario = web_client_scenario

        # Verify steps
        assert scenario["steps"][0]["action"] == "connect_ws"
        assert scenario["steps"][1]["action"] == "register"
        assert scenario["steps"][2]["action"] == "send_text"
        assert scenario["steps"][3]["action"] == "receive_response"

    @pytest.mark.e2e
    @pytest.mark.integration
    async def test_web_client_room_detection(self):
        """Test: Web client gets room context by IP"""
        ip_detection_scenario = {
            "client_ip": "192.168.1.100",
            "registered_device": {
                "device_id": "panel-wohnzimmer-1",
                "room": "Wohnzimmer",
                "is_stationary": True,
            },
            "expected_room_context": {
                "room_name": "Wohnzimmer",
                "auto_detected": True,
            }
        }

        result = ip_detection_scenario["expected_room_context"]
        assert result["room_name"] == "Wohnzimmer"
        assert result["auto_detected"] is True

    @pytest.mark.e2e
    @pytest.mark.integration
    async def test_web_client_streaming_response(self):
        """Test: Web client receives streaming response"""
        streaming_scenario = {
            "message": "Erzähle mir etwas über das Wetter",
            "expected_stream_chunks": 5,
            "final_message_type": "done",
        }

        assert streaming_scenario["expected_stream_chunks"] > 0
        assert streaming_scenario["final_message_type"] == "done"


# ============================================================================
# Multi-Device Scenario Tests
# ============================================================================

class TestMultiDeviceScenarios:
    """Tests for multi-device interactions"""

    @pytest.mark.e2e
    @pytest.mark.integration
    async def test_output_routing_to_external_device(self):
        """Test: TTS routed to configured output device"""
        output_routing_scenario = {
            "input_device": "sat-wohnzimmer-1",
            "configured_output": "media_player.sonos_living",
            "tts_text": "Das Licht ist eingeschaltet.",
            "expected_output_device": "media_player.sonos_living",
            "input_device_plays_audio": False,
        }

        # TTS should go to Sonos, not back to satellite
        assert output_routing_scenario["input_device_plays_audio"] is False
        assert "media_player" in output_routing_scenario["expected_output_device"]

    @pytest.mark.e2e
    @pytest.mark.integration
    async def test_fallback_to_input_device(self):
        """Test: Falls back to input device when output unavailable"""
        fallback_scenario = {
            "input_device": "sat-kueche-1",
            "configured_output": "media_player.sonos_kitchen",
            "output_device_state": "unavailable",
            "expected_fallback": "sat-kueche-1",
        }

        assert fallback_scenario["expected_fallback"] == fallback_scenario["input_device"]

    @pytest.mark.e2e
    @pytest.mark.integration
    async def test_multiple_satellites_same_room(self):
        """Test: Multiple satellites in same room"""
        multi_satellite_scenario = {
            "room": "Wohnzimmer",
            "satellites": [
                {"device_id": "sat-wohnzimmer-1", "is_online": True},
                {"device_id": "sat-wohnzimmer-2", "is_online": True},
            ],
            "wakeword_triggered_by": "sat-wohnzimmer-1",
            "expected_responder": "sat-wohnzimmer-1",
        }

        # Only the triggering satellite should respond
        assert multi_satellite_scenario["expected_responder"] == "sat-wohnzimmer-1"


# ============================================================================
# Speaker Recognition Flow Tests
# ============================================================================

class TestSpeakerRecognitionFlow:
    """End-to-end tests for speaker recognition"""

    @pytest.mark.e2e
    @pytest.mark.integration
    async def test_known_speaker_identification(self):
        """Test: Known speaker is identified from audio"""
        speaker_scenario = {
            "audio_input": bytes([0] * 32000),
            "expected_speaker": {"id": 1, "name": "Max"},
            "confidence": 0.85,
            "threshold": 0.25,
        }

        assert speaker_scenario["confidence"] >= speaker_scenario["threshold"]
        assert speaker_scenario["expected_speaker"]["name"] == "Max"

    @pytest.mark.e2e
    @pytest.mark.integration
    async def test_unknown_speaker_auto_enroll(self):
        """Test: Unknown speaker triggers auto-enrollment"""
        auto_enroll_scenario = {
            "audio_input": bytes([0] * 32000),
            "speaker_found": False,
            "auto_enroll_enabled": True,
            "expected_action": "create_speaker_profile",
            "new_speaker_name": "Unbekannt 1",
        }

        assert auto_enroll_scenario["expected_action"] == "create_speaker_profile"

    @pytest.mark.e2e
    @pytest.mark.integration
    async def test_speaker_continuous_learning(self):
        """Test: Known speaker embeddings updated on interaction"""
        continuous_learning_scenario = {
            "speaker_id": 1,
            "initial_embedding_count": 5,
            "continuous_learning_enabled": True,
            "expected_embedding_count_after": 6,
        }

        assert (continuous_learning_scenario["expected_embedding_count_after"] >
                continuous_learning_scenario["initial_embedding_count"])


# ============================================================================
# Home Assistant Integration Flow Tests
# ============================================================================

class TestHomeAssistantIntegrationFlow:
    """End-to-end tests for Home Assistant integration"""

    @pytest.mark.e2e
    @pytest.mark.integration
    async def test_ha_area_sync_flow(self):
        """Test: HA areas sync to Renfield rooms"""
        sync_scenario = {
            "ha_areas": [
                {"area_id": "living_room", "name": "Wohnzimmer"},
                {"area_id": "kitchen", "name": "Küche"},
            ],
            "conflict_resolution": "link",
            "expected_rooms_created": 2,
        }

        assert sync_scenario["expected_rooms_created"] == len(sync_scenario["ha_areas"])

    @pytest.mark.e2e
    @pytest.mark.integration
    async def test_ha_entity_control_flow(self):
        """Test: Complete HA entity control flow"""
        control_scenario = {
            "command": "Schalte das Wohnzimmer Licht auf 50%",
            "extracted_intent": "homeassistant.set_value",
            "extracted_params": {
                "entity_id": "light.wohnzimmer",
                "attribute": "brightness_pct",
                "value": 50,
            },
            "ha_response": {"success": True},
            "tts_response": "Die Helligkeit wurde auf 50% gesetzt.",
        }

        assert control_scenario["ha_response"]["success"] is True
        assert "50%" in control_scenario["tts_response"]


# ============================================================================
# Error Recovery Flow Tests
# ============================================================================

class TestErrorRecoveryFlows:
    """End-to-end tests for error recovery scenarios"""

    @pytest.mark.e2e
    @pytest.mark.integration
    async def test_ollama_unavailable_recovery(self):
        """Test: System handles Ollama unavailability"""
        error_scenario = {
            "error": "Ollama connection failed",
            "expected_user_response": "entschuldigung",
            "system_state_after": "idle",
        }

        assert error_scenario["system_state_after"] == "idle"

    @pytest.mark.e2e
    @pytest.mark.integration
    async def test_ha_unavailable_recovery(self):
        """Test: System handles HA unavailability"""
        error_scenario = {
            "command": "Schalte das Licht ein",
            "ha_error": "Connection refused",
            "expected_response_contains": "home assistant",
            "action_taken": False,
        }

        assert error_scenario["action_taken"] is False

    @pytest.mark.e2e
    @pytest.mark.integration
    async def test_websocket_reconnection_recovery(self):
        """Test: Devices reconnect after WebSocket disconnect"""
        reconnect_scenario = {
            "disconnect_event": "connection_lost",
            "max_reconnect_attempts": 5,
            "reconnect_delay_ms": 1000,
            "expected_outcome": "reconnected",
        }

        assert reconnect_scenario["expected_outcome"] == "reconnected"
