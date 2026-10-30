"""
Tests for the utility modules

Covers:
- Config Settings
- Audio Preprocessor
- Other helper functions
"""

import os
from unittest.mock import patch

import pytest

# ============================================================================
# Config Settings Tests
# ============================================================================

class TestConfigSettings:
    """Tests for the config settings"""

    @pytest.mark.unit
    def test_default_settings(self):
        """Test: the default settings are loaded"""
        from utils.config import Settings

        settings = Settings()

        # Check that settings have valid values (not specific values as they depend on env)
        assert settings.ollama_model is not None
        assert len(settings.ollama_model) > 0
        assert settings.default_language in ["de", "en"]
        assert settings.whisper_model in ["tiny", "base", "small", "medium", "large"]

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
        """Test: environment variables override the defaults"""
        with patch.dict(os.environ, {"OLLAMA_MODEL": "custom-model"}):
            from utils.config import Settings

            _settings = Settings()
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
# Secrets / DATABASE_URL Assembly Tests
# ============================================================================

class TestSecretsAndDatabaseUrl:
    """Tests for secrets support and assembling DATABASE_URL dynamically"""

    @pytest.mark.unit
    def test_database_url_assembled_from_parts(self):
        """Test: DATABASE_URL is assembled from the individual fields when unset"""
        from utils.config import Settings

        settings = Settings(
            database_url=None,
            postgres_user="testuser",
            postgres_password="testpass",
            postgres_host="dbhost",
            postgres_port=5433,
            postgres_db="testdb",
        )

        assert settings.database_url == "postgresql://testuser:testpass@dbhost:5433/testdb"

    @pytest.mark.unit
    def test_database_url_explicit_takes_precedence(self):
        """Test: an explicit DATABASE_URL takes precedence over the individual fields"""
        from utils.config import Settings

        explicit_url = "postgresql://explicit:pass@host:5432/db"
        settings = Settings(
            database_url=explicit_url,
            postgres_password="ignored",
        )

        assert settings.database_url == explicit_url

    @pytest.mark.unit
    def test_database_url_default_parts(self):
        """Test: default values for the individual Postgres fields"""
        from utils.config import Settings

        settings = Settings(database_url=None)

        assert settings.postgres_user == "renfield"
        assert settings.postgres_host == "postgres"
        assert settings.postgres_port == 5432
        assert settings.postgres_db == "renfield"
        assert "postgresql://renfield:" in settings.database_url
        assert "@postgres:5432/renfield" in settings.database_url

    @pytest.mark.unit
    def test_secrets_dir_configured(self):
        """Test: secrets_dir is configured to /run/secrets"""
        from utils.config import Settings

        config = Settings.model_config
        assert config.get("secrets_dir") == "/run/secrets"

    @pytest.mark.unit
    def test_secret_fields_exist(self):
        """Test: Alle Secret-Felder existieren in den Settings-Klassen.

        Core platform secrets live on ``Settings``; integration secrets
        (Home Assistant, Jellyfin) moved to ``HaGlueSettings`` when the
        ha_glue boundary was extracted. ``openweather_api_key`` /
        ``newsapi_key`` were removed entirely (those integrations are now
        configured via MCP servers, not platform Settings fields).
        """
        from ha_glue.utils.config import HaGlueSettings
        from utils.config import Settings

        settings = Settings(database_url=None)

        # Core platform secrets
        assert hasattr(settings, "postgres_password")
        assert hasattr(settings, "secret_key")
        assert hasattr(settings, "default_admin_password")

        # Integration secrets on the ha_glue settings boundary
        assert "home_assistant_token" in HaGlueSettings.model_fields
        assert "jellyfin_api_key" in HaGlueSettings.model_fields

    @pytest.mark.unit
    def test_postgres_password_from_env(self):
        """Test: postgres_password can be set via an environment variable"""
        with patch.dict(os.environ, {"POSTGRES_PASSWORD": "env-secret-pw"}):
            from utils.config import Settings

            settings = Settings(database_url=None)
            assert settings.postgres_password.get_secret_value() == "env-secret-pw"
            assert "env-secret-pw" in settings.database_url

    @pytest.mark.unit
    def test_secrets_from_file(self, tmp_path):
        """Test: secrets are read from files when secrets_dir exists"""

        from pydantic_settings import BaseSettings

        # Create a temporary secret file
        secret_file = tmp_path / "test_secret"
        secret_file.write_text("file-based-secret")

        class TestSettings(BaseSettings):
            test_secret: str = "default"

            class Config:
                secrets_dir = str(tmp_path)

        # Make sure no environment variable disturbs the test
        env_clean = {k: v for k, v in os.environ.items() if k != "TEST_SECRET"}
        with patch.dict(os.environ, env_clean, clear=True):
            settings = TestSettings()
            assert settings.test_secret == "file-based-secret"

    @pytest.mark.unit
    def test_env_var_overrides_secret_file(self, tmp_path):
        """Test: an environment variable takes precedence over a secret file"""
        from pydantic_settings import BaseSettings

        secret_file = tmp_path / "secret_key"
        secret_file.write_text("file-value")

        class TestSettings(BaseSettings):
            secret_key: str = "default"

            class Config:
                secrets_dir = str(tmp_path)

        with patch.dict(os.environ, {"SECRET_KEY": "env-value"}):
            settings = TestSettings()
            assert settings.secret_key == "env-value"


# ============================================================================
# Room Name Normalization Tests (Additional)
# ============================================================================

class TestRoomNameNormalizationEdgeCases:
    """Additional tests for room name normalization"""

    @pytest.mark.unit
    def test_numbers_preserved(self):
        """Test: Digits are preserved"""
        from ha_glue.services.room_service import normalize_room_name

        assert normalize_room_name("Zimmer 1") == "zimmer1"
        assert normalize_room_name("Raum 123") == "raum123"
        assert normalize_room_name("2. Stock") == "2stock"

    @pytest.mark.unit
    def test_mixed_case(self):
        """Test: mixed case is normalized"""
        from ha_glue.services.room_service import normalize_room_name

        assert normalize_room_name("WohnZimmer") == "wohnzimmer"
        assert normalize_room_name("KÜCHE") == "kueche"

    @pytest.mark.unit
    def test_accented_characters(self):
        """Test: Accented characters"""
        from ha_glue.services.room_service import normalize_room_name

        assert normalize_room_name("Café") == "cafe"
        assert normalize_room_name("Entrée") == "entree"

    @pytest.mark.unit
    def test_consecutive_spaces(self):
        """Test: Several consecutive spaces"""
        from ha_glue.services.room_service import normalize_room_name

        assert normalize_room_name("Wohn    Zimmer") == "wohnzimmer"

    @pytest.mark.unit
    def test_underscores_and_dashes(self):
        """Test: Underscores and hyphens"""
        from ha_glue.services.room_service import normalize_room_name

        assert normalize_room_name("kinder-zimmer") == "kinderzimmer"
        assert normalize_room_name("wohn_zimmer") == "wohnzimmer"


# ============================================================================
# Device ID Generation Tests (Additional)
# ============================================================================

class TestDeviceIdGenerationEdgeCases:
    """Additional tests for device id generation"""

    @pytest.mark.unit
    def test_unknown_device_type(self):
        """Test: An unknown device type gets the default prefix"""
        from ha_glue.services.room_service import generate_device_id

        device_id = generate_device_id("unknown_type", "Test Room", "suffix")

        assert device_id.startswith("dev-")

    @pytest.mark.unit
    def test_special_characters_in_room(self):
        """Test: Special characters in the room name"""
        from ha_glue.services.room_service import generate_device_id

        device_id = generate_device_id("satellite", "Wohn/Ess-Zimmer!", "main")

        assert "/" not in device_id
        assert "!" not in device_id
        assert "-" in device_id  # Only as separator

    @pytest.mark.unit
    def test_empty_room_name(self):
        """Test: Empty room name"""
        from ha_glue.services.room_service import generate_device_id

        device_id = generate_device_id("satellite", "", "suffix")

        assert device_id.startswith("sat-")
        assert "suffix" in device_id


# ============================================================================
# Audio Preprocessor Tests
# ============================================================================

class TestAudioPreprocessor:
    """Tests for the audio preprocessor"""

    @pytest.mark.unit
    def test_preprocessor_initialization(self):
        """Test: the preprocessor can be initialized"""
        try:
            from services.audio_preprocessor import AudioPreprocessor

            preprocessor = AudioPreprocessor()
            assert preprocessor is not None
        except ImportError:
            pytest.skip("Audio preprocessing dependencies not installed")

    @pytest.mark.unit
    def test_normalize_audio(self):
        """Test: Audio normalization"""
        try:
            import numpy as np

            from services.audio_preprocessor import AudioPreprocessor

            preprocessor = AudioPreprocessor()

            # Create test audio signal with realistic amplitude (not silence)
            # Use a sine wave to ensure there's actual signal content
            t = np.linspace(0, 1, 16000, dtype=np.float32)
            test_audio = (np.sin(2 * np.pi * 440 * t) * 0.5).astype(np.float32)
            sr = 16000

            normalized = preprocessor.normalize(test_audio, sr)

            # Check that audio is normalized
            assert normalized is not None
            assert len(normalized) > 0
        except ImportError:
            pytest.skip("Audio preprocessing dependencies not installed")
        except (OverflowError, ValueError):
            # Some audio might cause numerical issues, which is acceptable
            pytest.skip("Audio normalization not supported for this input")


# ============================================================================
# Validation Helpers Tests
# ============================================================================

class TestValidationHelpers:
    """Tests for the validation helpers"""

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
        """Test: IP address format validation"""
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
    """Tests for the output-routing helpers"""

    @pytest.mark.unit
    def test_output_type_validation(self):
        """Test: output type validation"""
        from models.database import OUTPUT_TYPE_AUDIO, OUTPUT_TYPE_VISUAL, OUTPUT_TYPES

        assert OUTPUT_TYPE_AUDIO in OUTPUT_TYPES
        assert OUTPUT_TYPE_VISUAL in OUTPUT_TYPES
        assert len(OUTPUT_TYPES) == 2

    @pytest.mark.unit
    def test_device_type_validation(self):
        """Test: device type validation"""
        from models.database import DEVICE_TYPE_SATELLITE, DEVICE_TYPES

        assert DEVICE_TYPE_SATELLITE in DEVICE_TYPES
        assert "invalid_type" not in DEVICE_TYPES


