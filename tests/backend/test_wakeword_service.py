"""Tests for WakeWordService.

Tests wake word detection initialization, model loading, audio processing,
keyword management, threshold/cooldown configuration, and cleanup.
"""
import sys
from unittest.mock import MagicMock

# Pre-mock modules not available in test environment
_missing_stubs = [
    "asyncpg", "whisper", "piper", "piper.voice", "speechbrain",
    "speechbrain.inference", "speechbrain.inference.speaker",
    "openwakeword", "openwakeword.model",
    "zeroconf", "zeroconf.asyncio",
]
for _mod in _missing_stubs:
    if _mod not in sys.modules:
        sys.modules[_mod] = MagicMock()

import struct
from unittest.mock import patch

import numpy as np
import pytest


def _make_mock_settings(
    wake_word_default="alexa",
    wake_word_threshold=0.5,
    wake_word_cooldown_ms=2000,
):
    s = MagicMock()
    s.wake_word_default = wake_word_default
    s.wake_word_threshold = wake_word_threshold
    s.wake_word_cooldown_ms = wake_word_cooldown_ms
    return s


def _make_audio_bytes(num_samples=1280):
    """Create fake 16-bit PCM audio bytes."""
    return struct.pack(f"<{num_samples}h", *([0] * num_samples))


@pytest.fixture
def mock_settings():
    return _make_mock_settings()


@pytest.fixture
def service(mock_settings):
    with patch("services.wakeword_service.settings", mock_settings):
        from services.wakeword_service import WakeWordService
        svc = WakeWordService()
    return svc


# ============================================================================
# Initialization Tests
# ============================================================================

@pytest.mark.unit
class TestWakeWordServiceInit:

    def test_init_defaults(self, service):
        """Service initializes with correct default values."""
        assert service.model is None
        assert service.keywords == ["alexa"]
        assert service.threshold == 0.5
        assert service.cooldown_ms == 2000
        assert service._loaded is False

    def test_available_property_when_available(self, service):
        """available property reflects OPENWAKEWORD_AVAILABLE."""
        with patch("services.wakeword_service.OPENWAKEWORD_AVAILABLE", True):
            assert service.available is True

    def test_available_property_when_unavailable(self, service):
        """available property is False when library not installed."""
        with patch("services.wakeword_service.OPENWAKEWORD_AVAILABLE", False):
            assert service.available is False

    def test_available_keywords_list(self, service):
        """AVAILABLE_KEYWORDS contains expected wake words."""
        assert "alexa" in service.AVAILABLE_KEYWORDS
        assert "hey_jarvis" in service.AVAILABLE_KEYWORDS
        assert "hey_mycroft" in service.AVAILABLE_KEYWORDS
        assert len(service.AVAILABLE_KEYWORDS) >= 4

    def test_init_custom_default_keyword(self):
        """Service uses custom default from settings."""
        settings = _make_mock_settings(wake_word_default="hey_jarvis")
        with patch("services.wakeword_service.settings", settings):
            from services.wakeword_service import WakeWordService
            svc = WakeWordService()
        assert svc.keywords == ["hey_jarvis"]


# ============================================================================
# Model Loading Tests
# ============================================================================

@pytest.mark.unit
class TestModelLoading:

    def test_load_model_success(self, mock_settings):
        """Model loads successfully with valid keywords."""
        mock_model_cls = MagicMock()
        mock_model_instance = MagicMock()
        mock_model_cls.return_value = mock_model_instance

        with patch("services.wakeword_service.settings", mock_settings), \
             patch("services.wakeword_service.OPENWAKEWORD_AVAILABLE", True), \
             patch("services.wakeword_service.Model", mock_model_cls):
            from services.wakeword_service import WakeWordService
            svc = WakeWordService()
            result = svc.load_model()

        assert result is True
        assert svc._loaded is True
        assert svc.model is mock_model_instance
        mock_model_cls.assert_called_once()

    def test_load_model_not_available(self, mock_settings):
        """load_model returns False when OpenWakeWord is not installed."""
        with patch("services.wakeword_service.settings", mock_settings), \
             patch("services.wakeword_service.OPENWAKEWORD_AVAILABLE", False):
            from services.wakeword_service import WakeWordService
            svc = WakeWordService()
            result = svc.load_model()

        assert result is False
        assert svc._loaded is False

    def test_load_model_already_loaded(self, mock_settings):
        """load_model returns True without reloading when already loaded."""
        mock_model_cls = MagicMock()

        with patch("services.wakeword_service.settings", mock_settings), \
             patch("services.wakeword_service.OPENWAKEWORD_AVAILABLE", True), \
             patch("services.wakeword_service.Model", mock_model_cls):
            from services.wakeword_service import WakeWordService
            svc = WakeWordService()
            svc.load_model()
            mock_model_cls.reset_mock()

            # Second call should not reload
            result = svc.load_model()

        assert result is True
        mock_model_cls.assert_not_called()

    def test_load_model_failure(self, mock_settings):
        """load_model returns False when Model() raises."""
        mock_model_cls = MagicMock(side_effect=RuntimeError("ONNX error"))

        with patch("services.wakeword_service.settings", mock_settings), \
             patch("services.wakeword_service.OPENWAKEWORD_AVAILABLE", True), \
             patch("services.wakeword_service.Model", mock_model_cls):
            from services.wakeword_service import WakeWordService
            svc = WakeWordService()
            result = svc.load_model()

        assert result is False
        assert svc._loaded is False

    def test_load_model_with_custom_keywords(self, mock_settings):
        """load_model accepts and filters custom keywords."""
        mock_model_cls = MagicMock()

        with patch("services.wakeword_service.settings", mock_settings), \
             patch("services.wakeword_service.OPENWAKEWORD_AVAILABLE", True), \
             patch("services.wakeword_service.Model", mock_model_cls):
            from services.wakeword_service import WakeWordService
            svc = WakeWordService()
            result = svc.load_model(keywords=["hey_jarvis", "timer"])

        assert result is True
        assert svc.keywords == ["hey_jarvis", "timer"]

    def test_load_model_invalid_keywords_fallback(self, mock_settings):
        """Invalid keywords fall back to default."""
        mock_model_cls = MagicMock()

        with patch("services.wakeword_service.settings", mock_settings), \
             patch("services.wakeword_service.OPENWAKEWORD_AVAILABLE", True), \
             patch("services.wakeword_service.Model", mock_model_cls):
            from services.wakeword_service import WakeWordService
            svc = WakeWordService()
            result = svc.load_model(keywords=["nonexistent_keyword"])

        assert result is True
        assert svc.keywords == ["alexa"]  # Falls back to default


# ============================================================================
# Audio Processing Tests
# ============================================================================

@pytest.mark.unit
class TestAudioProcessing:

    def test_process_audio_model_not_loaded(self, service):
        """Returns error dict when model is not loaded."""
        result = service.process_audio_chunk(_make_audio_bytes())
        assert result["detected"] is False
        assert "error" in result
        assert "not loaded" in result["error"]

    def test_process_audio_no_detection(self, mock_settings):
        """Returns detected=False when score is below threshold."""
        mock_model = MagicMock()
        mock_model.predict.return_value = {"alexa": 0.1}

        with patch("services.wakeword_service.settings", mock_settings), \
             patch("services.wakeword_service.OPENWAKEWORD_AVAILABLE", True):
            from services.wakeword_service import WakeWordService
            svc = WakeWordService()
            svc.model = mock_model
            svc._loaded = True

            result = svc.process_audio_chunk(_make_audio_bytes())

        assert result["detected"] is False
        assert "error" not in result

    def test_process_audio_detection(self, mock_settings):
        """Returns detected=True with keyword and score when above threshold."""
        mock_model = MagicMock()
        mock_model.predict.return_value = {"alexa": 0.85}

        with patch("services.wakeword_service.settings", mock_settings), \
             patch("services.wakeword_service.OPENWAKEWORD_AVAILABLE", True):
            from services.wakeword_service import WakeWordService
            svc = WakeWordService()
            svc.model = mock_model
            svc._loaded = True
            svc.threshold = 0.5

            result = svc.process_audio_chunk(_make_audio_bytes())

        assert result["detected"] is True
        assert result["keyword"] == "alexa"
        assert result["score"] == pytest.approx(0.85)

    def test_process_audio_cooldown(self, mock_settings):
        """Detection is suppressed during cooldown period."""
        mock_model = MagicMock()
        mock_model.predict.return_value = {"alexa": 0.85}

        with patch("services.wakeword_service.settings", mock_settings), \
             patch("services.wakeword_service.OPENWAKEWORD_AVAILABLE", True):
            from services.wakeword_service import WakeWordService
            svc = WakeWordService()
            svc.model = mock_model
            svc._loaded = True
            svc.threshold = 0.5
            svc.cooldown_ms = 5000

            # First detection should succeed
            result1 = svc.process_audio_chunk(_make_audio_bytes())
            assert result1["detected"] is True

            # Second immediate detection should be suppressed by cooldown
            result2 = svc.process_audio_chunk(_make_audio_bytes())
            assert result2["detected"] is False

    def test_process_audio_multiple_keywords(self, mock_settings):
        """Detects the first keyword above threshold from multiple."""
        mock_model = MagicMock()
        mock_model.predict.return_value = {"alexa": 0.2, "hey_jarvis": 0.9}

        with patch("services.wakeword_service.settings", mock_settings), \
             patch("services.wakeword_service.OPENWAKEWORD_AVAILABLE", True):
            from services.wakeword_service import WakeWordService
            svc = WakeWordService()
            svc.model = mock_model
            svc._loaded = True
            svc.keywords = ["alexa", "hey_jarvis"]
            svc.threshold = 0.5

            result = svc.process_audio_chunk(_make_audio_bytes())

        assert result["detected"] is True
        assert result["keyword"] == "hey_jarvis"

    def test_process_audio_exception_handling(self, mock_settings):
        """Returns error dict when processing raises exception."""
        mock_model = MagicMock()
        mock_model.predict.side_effect = RuntimeError("inference failed")

        with patch("services.wakeword_service.settings", mock_settings), \
             patch("services.wakeword_service.OPENWAKEWORD_AVAILABLE", True):
            from services.wakeword_service import WakeWordService
            svc = WakeWordService()
            svc.model = mock_model
            svc._loaded = True

            result = svc.process_audio_chunk(_make_audio_bytes())

        assert result["detected"] is False
        assert "error" in result

    def test_process_audio_converts_to_float32(self, mock_settings):
        """Audio bytes are converted to float32 before prediction."""
        mock_model = MagicMock()
        mock_model.predict.return_value = {"alexa": 0.0}

        with patch("services.wakeword_service.settings", mock_settings), \
             patch("services.wakeword_service.OPENWAKEWORD_AVAILABLE", True):
            from services.wakeword_service import WakeWordService
            svc = WakeWordService()
            svc.model = mock_model
            svc._loaded = True

            svc.process_audio_chunk(_make_audio_bytes(10))

        # Verify predict was called with a float32 numpy array
        call_args = mock_model.predict.call_args[0][0]
        assert isinstance(call_args, np.ndarray)
        assert call_args.dtype == np.float32


# ============================================================================
# Keyword Management Tests
# ============================================================================

@pytest.mark.unit
class TestKeywordManagement:

    def test_set_keywords_valid(self, mock_settings):
        """set_keywords changes active keywords and reloads model."""
        mock_model_cls = MagicMock()

        with patch("services.wakeword_service.settings", mock_settings), \
             patch("services.wakeword_service.OPENWAKEWORD_AVAILABLE", True), \
             patch("services.wakeword_service.Model", mock_model_cls):
            from services.wakeword_service import WakeWordService
            svc = WakeWordService()
            result = svc.set_keywords(["hey_jarvis", "timer"])

        assert result is True
        assert svc.keywords == ["hey_jarvis", "timer"]

    def test_set_keywords_invalid(self, service):
        """set_keywords returns False for invalid keywords."""
        result = service.set_keywords(["nonexistent"])
        assert result is False


# ============================================================================
# Configuration Tests
# ============================================================================

@pytest.mark.unit
class TestConfiguration:

    def test_set_threshold(self, service):
        """set_threshold clamps to valid range."""
        service.set_threshold(0.8)
        assert service.threshold == pytest.approx(0.8)

    def test_set_threshold_clamps_high(self, service):
        """Threshold > 1.0 is clamped to 1.0."""
        service.set_threshold(1.5)
        assert service.threshold == pytest.approx(1.0)

    def test_set_threshold_clamps_low(self, service):
        """Threshold < 0.0 is clamped to 0.0."""
        service.set_threshold(-0.5)
        assert service.threshold == pytest.approx(0.0)

    def test_set_cooldown(self, service):
        """set_cooldown updates cooldown_ms."""
        service.set_cooldown(3000)
        assert service.cooldown_ms == 3000

    def test_set_cooldown_clamps_negative(self, service):
        """Negative cooldown is clamped to 0."""
        service.set_cooldown(-100)
        assert service.cooldown_ms == 0

    def test_reset_clears_detection_times(self, service):
        """reset() clears last detection times."""
        service._last_detection_time["alexa"] = 12345.0
        service.reset()
        assert service._last_detection_time == {}

    def test_get_status(self, service):
        """get_status returns complete status dict."""
        status = service.get_status()
        assert "available" in status
        assert "loaded" in status
        assert "keywords" in status
        assert "threshold" in status
        assert "cooldown_ms" in status
        assert "available_keywords" in status
        assert status["loaded"] is False
        assert status["keywords"] == ["alexa"]


# ============================================================================
# Singleton Tests
# ============================================================================

@pytest.mark.unit
class TestSingleton:

    def test_get_wakeword_service_returns_instance(self, mock_settings):
        """get_wakeword_service returns a WakeWordService."""
        with patch("services.wakeword_service.settings", mock_settings), \
             patch("services.wakeword_service._wakeword_service", None):
            from services.wakeword_service import get_wakeword_service
            svc = get_wakeword_service()
        from services.wakeword_service import WakeWordService
        assert isinstance(svc, WakeWordService)

    def test_get_wakeword_service_returns_same_instance(self, mock_settings):
        """get_wakeword_service returns singleton."""
        with patch("services.wakeword_service.settings", mock_settings), \
             patch("services.wakeword_service._wakeword_service", None):
            from services.wakeword_service import get_wakeword_service
            svc1 = get_wakeword_service()
            svc2 = get_wakeword_service()
        assert svc1 is svc2
