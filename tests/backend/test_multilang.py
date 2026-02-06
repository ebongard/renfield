"""
Tests for Multi-Language Support

Tests:
- STT with language parameter
- TTS with language parameter
- Preferences API (get/set language)
- Satellite language support
"""

from io import BytesIO
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import AsyncClient

# ============================================================================
# Fixtures
# ============================================================================

@pytest.fixture
def mock_audio_file():
    """Create a mock audio file"""
    # Minimal WAV header + some data
    wav_header = b'RIFF' + b'\x24\x00\x00\x00' + b'WAVE'
    wav_header += b'fmt ' + b'\x10\x00\x00\x00'  # fmt chunk
    wav_header += b'\x01\x00\x01\x00'  # PCM, 1 channel
    wav_header += b'\x44\xac\x00\x00'  # 44100 sample rate
    wav_header += b'\x88\x58\x01\x00'  # byte rate
    wav_header += b'\x02\x00\x10\x00'  # block align, bits per sample
    wav_header += b'data' + b'\x00\x00\x00\x00'  # data chunk

    return wav_header + b'\x00' * 1000


@pytest.fixture
def mock_whisper_service():
    """Mock Whisper service"""
    mock = MagicMock()
    mock.transcribe_bytes = AsyncMock(return_value="Test Transcription")
    mock.transcribe_bytes_with_speaker = AsyncMock(return_value={
        "text": "Test Transcription",
        "speaker_id": 1,
        "speaker_name": "Max",
        "speaker_alias": "max",
        "speaker_confidence": 0.85
    })
    return mock


@pytest.fixture
def mock_piper_service():
    """Mock Piper service"""
    mock = MagicMock()
    mock.synthesize_to_bytes = AsyncMock(
        return_value=b'RIFF' + b'\x00' * 100
    )
    return mock


# ============================================================================
# STT Language Tests
# ============================================================================

class TestSTTLanguage:
    """Tests for STT with language parameter"""

    @pytest.mark.integration
    async def test_stt_with_language_german(
        self,
        async_client: AsyncClient,
        mock_audio_file,
        mock_whisper_service
    ):
        """Test STT with German language parameter"""
        import api.routes.voice as voice_module
        original_service = voice_module.whisper_service

        try:
            voice_module.whisper_service = mock_whisper_service

            with patch.object(voice_module, 'settings') as mock_settings:
                mock_settings.speaker_recognition_enabled = False
                mock_settings.default_language = "de"
                mock_settings.supported_languages_list = ["de", "en"]

                files = {"audio": ("test.wav", BytesIO(mock_audio_file), "audio/wav")}
                response = await async_client.post(
                    "/api/voice/stt?language=de",
                    files=files
                )
        finally:
            voice_module.whisper_service = original_service

        assert response.status_code == 200
        data = response.json()
        assert data["language"] == "de"
        # Verify language was passed to transcribe
        mock_whisper_service.transcribe_bytes.assert_called_once()

    @pytest.mark.integration
    async def test_stt_with_language_english(
        self,
        async_client: AsyncClient,
        mock_audio_file,
        mock_whisper_service
    ):
        """Test STT with English language parameter"""
        import api.routes.voice as voice_module
        original_service = voice_module.whisper_service

        try:
            voice_module.whisper_service = mock_whisper_service

            with patch.object(voice_module, 'settings') as mock_settings:
                mock_settings.speaker_recognition_enabled = False
                mock_settings.default_language = "de"
                mock_settings.supported_languages_list = ["de", "en"]

                files = {"audio": ("test.wav", BytesIO(mock_audio_file), "audio/wav")}
                response = await async_client.post(
                    "/api/voice/stt?language=en",
                    files=files
                )
        finally:
            voice_module.whisper_service = original_service

        assert response.status_code == 200
        data = response.json()
        assert data["language"] == "en"

    @pytest.mark.integration
    async def test_stt_with_unsupported_language_fallback(
        self,
        async_client: AsyncClient,
        mock_audio_file,
        mock_whisper_service
    ):
        """Test STT with unsupported language falls back to default"""
        import api.routes.voice as voice_module
        original_service = voice_module.whisper_service

        try:
            voice_module.whisper_service = mock_whisper_service

            with patch.object(voice_module, 'settings') as mock_settings:
                mock_settings.speaker_recognition_enabled = False
                mock_settings.default_language = "de"
                mock_settings.supported_languages_list = ["de", "en"]

                files = {"audio": ("test.wav", BytesIO(mock_audio_file), "audio/wav")}
                response = await async_client.post(
                    "/api/voice/stt?language=fr",  # Unsupported
                    files=files
                )
        finally:
            voice_module.whisper_service = original_service

        assert response.status_code == 200
        data = response.json()
        assert data["language"] == "de"  # Fallback to default


# ============================================================================
# TTS Language Tests
# ============================================================================

class TestTTSLanguage:
    """Tests for TTS with language parameter"""

    @pytest.mark.integration
    async def test_tts_with_language_german(
        self,
        async_client: AsyncClient,
        mock_piper_service
    ):
        """Test TTS with German language"""
        import api.routes.voice as voice_module
        original_service = voice_module.piper_service

        try:
            voice_module.piper_service = mock_piper_service

            with patch.object(voice_module, 'settings') as mock_settings:
                mock_settings.default_language = "de"
                mock_settings.supported_languages_list = ["de", "en"]

                response = await async_client.post(
                    "/api/voice/tts",
                    json={"text": "Hallo Welt", "language": "de"}
                )
        finally:
            voice_module.piper_service = original_service

        assert response.status_code == 200
        assert response.headers["content-type"] == "audio/wav"
        # Verify language was passed to synthesize
        mock_piper_service.synthesize_to_bytes.assert_called_once_with(
            "Hallo Welt", language="de"
        )

    @pytest.mark.integration
    async def test_tts_with_language_english(
        self,
        async_client: AsyncClient,
        mock_piper_service
    ):
        """Test TTS with English language"""
        import api.routes.voice as voice_module
        original_service = voice_module.piper_service

        try:
            voice_module.piper_service = mock_piper_service

            with patch.object(voice_module, 'settings') as mock_settings:
                mock_settings.default_language = "de"
                mock_settings.supported_languages_list = ["de", "en"]

                response = await async_client.post(
                    "/api/voice/tts",
                    json={"text": "Hello World", "language": "en"}
                )
        finally:
            voice_module.piper_service = original_service

        assert response.status_code == 200
        mock_piper_service.synthesize_to_bytes.assert_called_once_with(
            "Hello World", language="en"
        )

    @pytest.mark.integration
    async def test_tts_without_language_uses_default(
        self,
        async_client: AsyncClient,
        mock_piper_service
    ):
        """Test TTS without language parameter uses default"""
        import api.routes.voice as voice_module
        original_service = voice_module.piper_service

        try:
            voice_module.piper_service = mock_piper_service

            with patch.object(voice_module, 'settings') as mock_settings:
                mock_settings.default_language = "de"
                mock_settings.supported_languages_list = ["de", "en"]

                response = await async_client.post(
                    "/api/voice/tts",
                    json={"text": "Test"}
                )
        finally:
            voice_module.piper_service = original_service

        assert response.status_code == 200
        # Should use None (which falls back to default in the service)
        mock_piper_service.synthesize_to_bytes.assert_called_once_with(
            "Test", language=None
        )


# ============================================================================
# Preferences API Tests
# ============================================================================

class TestPreferencesAPI:
    """Tests for Preferences API"""

    @pytest.mark.integration
    async def test_get_language_unauthenticated(self, async_client: AsyncClient):
        """Test getting language when not authenticated returns default"""
        with patch('api.routes.preferences.settings') as mock_settings:
            mock_settings.default_language = "de"

            response = await async_client.get("/api/preferences/language")

        assert response.status_code == 200
        data = response.json()
        assert data["language"] == "de"

    @pytest.mark.integration
    async def test_set_language_requires_auth(self, async_client: AsyncClient):
        """Test setting language requires authentication"""
        response = await async_client.put(
            "/api/preferences/language",
            json={"language": "en"}
        )

        # Should return 401 or 403 when not authenticated
        assert response.status_code in [401, 403]

    @pytest.mark.integration
    async def test_get_all_preferences(self, async_client: AsyncClient):
        """Test getting all preferences"""
        with patch('api.routes.preferences.settings') as mock_settings:
            mock_settings.default_language = "de"
            mock_settings.supported_languages_list = ["de", "en"]

            response = await async_client.get("/api/preferences")

        assert response.status_code == 200
        data = response.json()
        assert "language" in data
        assert "supported_languages" in data
        assert data["supported_languages"] == ["de", "en"]


# ============================================================================
# PiperService Multi-Voice Tests
# ============================================================================

class TestPiperServiceMultiVoice:
    """Tests for PiperService multi-voice support"""

    @pytest.mark.unit
    def test_voice_map_parsing(self):
        """Test PIPER_VOICES environment variable parsing"""
        from utils.config import Settings

        with patch.dict('os.environ', {
            'PIPER_VOICES': 'de:de_DE-thorsten-high,en:en_US-amy-medium'
        }):
            settings = Settings()
            voice_map = settings.piper_voice_map

        assert voice_map["de"] == "de_DE-thorsten-high"
        assert voice_map["en"] == "en_US-amy-medium"

    @pytest.mark.unit
    def test_voice_for_language_selection(self):
        """Test voice selection based on language"""
        from services.piper_service import PiperService

        with patch.object(PiperService, '_check_piper_available', return_value=True):
            with patch('services.piper_service.settings') as mock_settings:
                mock_settings.piper_voice = "de_DE-thorsten-high"
                mock_settings.piper_voice_map = {
                    "de": "de_DE-thorsten-high",
                    "en": "en_US-amy-medium"
                }
                mock_settings.default_language = "de"

                service = PiperService()

                # German voice
                de_voice = service._get_voice_for_language("de")
                assert de_voice == "de_DE-thorsten-high"

                # English voice
                en_voice = service._get_voice_for_language("en")
                assert en_voice == "en_US-amy-medium"

                # Unknown language falls back to default
                unknown_voice = service._get_voice_for_language("fr")
                assert unknown_voice == "de_DE-thorsten-high"


# ============================================================================
# Satellite Language Tests
# ============================================================================

class TestSatelliteLanguage:
    """Tests for satellite language support"""

    @pytest.mark.unit
    def test_satellite_info_has_language(self):
        """Test SatelliteInfo dataclass has language field"""
        from unittest.mock import MagicMock

        from services.satellite_manager import SatelliteCapabilities, SatelliteInfo

        mock_websocket = MagicMock()
        caps = SatelliteCapabilities()

        sat = SatelliteInfo(
            satellite_id="sat-test",
            room="Test Room",
            websocket=mock_websocket,
            capabilities=caps,
            language="en"
        )

        assert sat.language == "en"

    @pytest.mark.unit
    def test_satellite_info_default_language(self):
        """Test SatelliteInfo defaults to German"""
        from unittest.mock import MagicMock

        from services.satellite_manager import SatelliteCapabilities, SatelliteInfo

        mock_websocket = MagicMock()
        caps = SatelliteCapabilities()

        sat = SatelliteInfo(
            satellite_id="sat-test",
            room="Test Room",
            websocket=mock_websocket,
            capabilities=caps
        )

        assert sat.language == "de"

    @pytest.mark.unit
    async def test_satellite_registration_with_language(self):
        """Test satellite registration stores language"""
        from unittest.mock import AsyncMock, MagicMock

        from services.satellite_manager import SatelliteManager

        manager = SatelliteManager()
        mock_websocket = MagicMock()
        mock_websocket.close = AsyncMock()

        success = await manager.register(
            satellite_id="sat-test",
            room="Test Room",
            websocket=mock_websocket,
            capabilities={},
            language="en"
        )

        assert success is True
        assert manager.satellites["sat-test"].language == "en"


# ============================================================================
# Config Tests
# ============================================================================

class TestMultiLangConfig:
    """Tests for multi-language configuration"""

    @pytest.mark.unit
    def test_supported_languages_parsing(self):
        """Test SUPPORTED_LANGUAGES environment variable parsing"""
        from utils.config import Settings

        with patch.dict('os.environ', {
            'SUPPORTED_LANGUAGES': 'de,en,fr'
        }):
            settings = Settings()
            languages = settings.supported_languages_list

        assert languages == ["de", "en", "fr"]

    @pytest.mark.unit
    def test_default_language_setting(self):
        """Test DEFAULT_LANGUAGE setting"""
        from utils.config import Settings

        with patch.dict('os.environ', {
            'DEFAULT_LANGUAGE': 'en'
        }):
            settings = Settings()

        assert settings.default_language == "en"
