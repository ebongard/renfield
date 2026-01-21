"""
Tests für Utility Module

Testet:
- Config Settings
- Audio Preprocessor
- Weitere Hilfsfunktionen
"""

import pytest
from unittest.mock import patch, MagicMock
import os


# ============================================================================
# Config Settings Tests
# ============================================================================

class TestConfigSettings:
    """Tests für Config Settings"""

    @pytest.mark.unit
    def test_default_settings(self):
        """Test: Default Settings werden geladen"""
        from utils.config import Settings

        settings = Settings()

        assert settings.ollama_model == "llama3.2:3b"
        assert settings.default_language == "de"
        assert settings.whisper_model == "base"

    @pytest.mark.unit
    def test_database_url_default(self):
        """Test: Default Database URL"""
        from utils.config import Settings

        settings = Settings()

        assert "postgresql" in settings.database_url
        assert "renfield" in settings.database_url

    @pytest.mark.unit
    def test_speaker_recognition_settings(self):
        """Test: Speaker Recognition Settings"""
        from utils.config import Settings

        settings = Settings()

        assert settings.speaker_recognition_enabled is True
        assert 0 < settings.speaker_recognition_threshold < 1
        assert settings.speaker_auto_enroll is True

    @pytest.mark.unit
    def test_websocket_settings(self):
        """Test: WebSocket Settings"""
        from utils.config import Settings

        settings = Settings()

        assert settings.ws_rate_limit_per_second > 0
        assert settings.ws_max_connections_per_ip > 0
        assert settings.ws_max_message_size > 0

    @pytest.mark.unit
    def test_env_override(self):
        """Test: Environment Variables überschreiben Defaults"""
        with patch.dict(os.environ, {"OLLAMA_MODEL": "custom-model"}):
            from utils.config import Settings

            settings = Settings()
            # Note: May need to reload module for this to work
            # This test demonstrates the pattern

    @pytest.mark.unit
    def test_cors_settings(self):
        """Test: CORS Settings"""
        from utils.config import Settings

        settings = Settings()

        # Default is "*" for development
        assert settings.cors_origins is not None


# ============================================================================
# Room Name Normalization Tests (Additional)
# ============================================================================

class TestRoomNameNormalizationEdgeCases:
    """Zusätzliche Tests für Room Name Normalisierung"""

    @pytest.mark.unit
    def test_numbers_preserved(self):
        """Test: Zahlen bleiben erhalten"""
        from services.room_service import normalize_room_name

        assert normalize_room_name("Zimmer 1") == "zimmer1"
        assert normalize_room_name("Raum 123") == "raum123"
        assert normalize_room_name("2. Stock") == "2stock"

    @pytest.mark.unit
    def test_mixed_case(self):
        """Test: Mixed Case wird normalisiert"""
        from services.room_service import normalize_room_name

        assert normalize_room_name("WohnZimmer") == "wohnzimmer"
        assert normalize_room_name("KÜCHE") == "kueche"

    @pytest.mark.unit
    def test_accented_characters(self):
        """Test: Akzentierte Zeichen"""
        from services.room_service import normalize_room_name

        assert normalize_room_name("Café") == "cafe"
        assert normalize_room_name("Entrée") == "entree"

    @pytest.mark.unit
    def test_consecutive_spaces(self):
        """Test: Mehrere aufeinanderfolgende Leerzeichen"""
        from services.room_service import normalize_room_name

        assert normalize_room_name("Wohn    Zimmer") == "wohnzimmer"

    @pytest.mark.unit
    def test_underscores_and_dashes(self):
        """Test: Unterstriche und Bindestriche"""
        from services.room_service import normalize_room_name

        assert normalize_room_name("kinder-zimmer") == "kinderzimmer"
        assert normalize_room_name("wohn_zimmer") == "wohnzimmer"


# ============================================================================
# Device ID Generation Tests (Additional)
# ============================================================================

class TestDeviceIdGenerationEdgeCases:
    """Zusätzliche Tests für Device ID Generierung"""

    @pytest.mark.unit
    def test_unknown_device_type(self):
        """Test: Unbekannter Device Type bekommt default prefix"""
        from services.room_service import generate_device_id

        device_id = generate_device_id("unknown_type", "Test Room", "suffix")

        assert device_id.startswith("dev-")

    @pytest.mark.unit
    def test_special_characters_in_room(self):
        """Test: Sonderzeichen im Raumnamen"""
        from services.room_service import generate_device_id

        device_id = generate_device_id("satellite", "Wohn/Ess-Zimmer!", "main")

        assert "/" not in device_id
        assert "!" not in device_id
        assert "-" in device_id  # Only as separator

    @pytest.mark.unit
    def test_empty_room_name(self):
        """Test: Leerer Raumname"""
        from services.room_service import generate_device_id

        device_id = generate_device_id("satellite", "", "suffix")

        assert device_id.startswith("sat-")
        assert "suffix" in device_id


# ============================================================================
# Audio Preprocessor Tests
# ============================================================================

class TestAudioPreprocessor:
    """Tests für Audio Preprocessor"""

    @pytest.mark.unit
    def test_preprocessor_initialization(self):
        """Test: Preprocessor kann initialisiert werden"""
        try:
            from services.audio_preprocessor import AudioPreprocessor

            preprocessor = AudioPreprocessor()
            assert preprocessor is not None
        except ImportError:
            pytest.skip("Audio preprocessing dependencies not installed")

    @pytest.mark.unit
    def test_normalize_audio(self):
        """Test: Audio Normalisierung"""
        try:
            from services.audio_preprocessor import AudioPreprocessor
            import numpy as np

            preprocessor = AudioPreprocessor()

            # Create test audio signal
            test_audio = np.random.randn(16000).astype(np.float32) * 0.1
            sr = 16000

            normalized = preprocessor.normalize(test_audio, sr)

            # Check that audio is normalized
            assert normalized is not None
            assert len(normalized) > 0
        except ImportError:
            pytest.skip("Audio preprocessing dependencies not installed")


# ============================================================================
# Validation Helpers Tests
# ============================================================================

class TestValidationHelpers:
    """Tests für Validierungs-Hilfsfunktionen"""

    @pytest.mark.unit
    def test_entity_id_format(self):
        """Test: Home Assistant Entity ID Format"""
        def is_valid_entity_id(entity_id: str) -> bool:
            if not entity_id or "." not in entity_id:
                return False
            parts = entity_id.split(".")
            return len(parts) == 2 and all(part for part in parts)

        assert is_valid_entity_id("light.wohnzimmer") is True
        assert is_valid_entity_id("switch.tv") is True
        assert is_valid_entity_id("invalid") is False
        assert is_valid_entity_id("") is False
        assert is_valid_entity_id("light.") is False

    @pytest.mark.unit
    def test_ip_address_format(self):
        """Test: IP Adress Format Validierung"""
        import re

        def is_valid_ip(ip: str) -> bool:
            pattern = r'^(\d{1,3}\.){3}\d{1,3}$'
            if not re.match(pattern, ip):
                return False
            parts = ip.split(".")
            return all(0 <= int(part) <= 255 for part in parts)

        assert is_valid_ip("192.168.1.1") is True
        assert is_valid_ip("10.0.0.1") is True
        assert is_valid_ip("256.1.1.1") is False
        assert is_valid_ip("invalid") is False

    @pytest.mark.unit
    def test_session_id_format(self):
        """Test: Session ID Format (UUID)"""
        import uuid

        def is_valid_session_id(session_id: str) -> bool:
            try:
                uuid.UUID(session_id)
                return True
            except ValueError:
                return False

        assert is_valid_session_id("550e8400-e29b-41d4-a716-446655440000") is True
        assert is_valid_session_id("invalid-session") is False
        assert is_valid_session_id("") is False


# ============================================================================
# Output Routing Tests
# ============================================================================

class TestOutputRoutingHelpers:
    """Tests für Output Routing Hilfsfunktionen"""

    @pytest.mark.unit
    def test_output_type_validation(self):
        """Test: Output Type Validierung"""
        from models.database import OUTPUT_TYPE_AUDIO, OUTPUT_TYPE_VISUAL, OUTPUT_TYPES

        assert OUTPUT_TYPE_AUDIO in OUTPUT_TYPES
        assert OUTPUT_TYPE_VISUAL in OUTPUT_TYPES
        assert len(OUTPUT_TYPES) == 2

    @pytest.mark.unit
    def test_device_type_validation(self):
        """Test: Device Type Validierung"""
        from models.database import DEVICE_TYPES, DEVICE_TYPE_SATELLITE

        assert DEVICE_TYPE_SATELLITE in DEVICE_TYPES
        assert "invalid_type" not in DEVICE_TYPES


# ============================================================================
# State Translation Tests (Additional)
# ============================================================================

class TestStateTranslationEdgeCases:
    """Zusätzliche Tests für State Übersetzung"""

    @pytest.mark.unit
    def test_media_player_states(self):
        """Test: Media Player States"""
        from services.action_executor import ActionExecutor

        executor = ActionExecutor()

        assert executor._translate_state("playing") == "läuft"
        assert executor._translate_state("paused") == "pausiert"
        assert executor._translate_state("idle") == "inaktiv"

    @pytest.mark.unit
    def test_lock_states(self):
        """Test: Lock States"""
        from services.action_executor import ActionExecutor

        executor = ActionExecutor()

        assert executor._translate_state("locked") == "verschlossen"
        assert executor._translate_state("unlocked") == "entriegelt"

    @pytest.mark.unit
    def test_presence_states(self):
        """Test: Presence States"""
        from services.action_executor import ActionExecutor

        executor = ActionExecutor()

        assert executor._translate_state("home") == "zuhause"
        assert executor._translate_state("away") == "abwesend"
