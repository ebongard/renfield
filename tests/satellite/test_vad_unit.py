"""
VoiceActivityDetector Unit Tests (RMS backend)

Tests for renfield_satellite.audio.vad.VoiceActivityDetector with RMS backend:
- Constructor with backend=RMS creates detector
- _rms_detect() on silent bytes returns False
- _rms_detect() on loud bytes returns True
- Threshold boundary: RMS exactly at threshold
- get_available_backends() always includes RMS
- is_speech() delegates to correct backend
"""

import numpy as np
import pytest
from unittest.mock import patch

from renfield_satellite.audio.vad import VoiceActivityDetector, VADBackend


class TestVADConstructor:
    """Tests for VoiceActivityDetector construction with RMS backend."""

    @pytest.mark.satellite
    def test_constructor_rms_backend(self):
        """Constructor with backend=RMS creates a functional detector."""
        vad = VoiceActivityDetector(
            sample_rate=16000,
            backend=VADBackend.RMS,
            rms_threshold=500.0,
        )

        assert vad.backend == VADBackend.RMS
        assert vad.sample_rate == 16000
        assert vad.rms_threshold == 500.0

    @pytest.mark.satellite
    def test_constructor_default_is_rms(self):
        """Default constructor creates an RMS backend detector."""
        vad = VoiceActivityDetector()

        assert vad.backend == VADBackend.RMS

    @pytest.mark.satellite
    def test_constructor_custom_threshold(self):
        """Custom rms_threshold is stored correctly."""
        vad = VoiceActivityDetector(rms_threshold=1000.0)
        assert vad.rms_threshold == 1000.0


class TestRMSDetect:
    """Tests for VoiceActivityDetector._rms_detect() method."""

    @pytest.mark.satellite
    def test_rms_detect_silent_bytes_returns_false(self):
        """_rms_detect() on all-zero (silent) bytes returns False."""
        vad = VoiceActivityDetector(rms_threshold=500.0)

        silence = np.zeros(640, dtype=np.int16).tobytes()
        result = vad._rms_detect(silence)

        assert bool(result) is False

    @pytest.mark.satellite
    def test_rms_detect_loud_bytes_returns_true(self):
        """_rms_detect() on high-amplitude bytes returns True."""
        vad = VoiceActivityDetector(rms_threshold=500.0)

        loud = np.full(640, 10000, dtype=np.int16).tobytes()
        result = vad._rms_detect(loud)

        assert bool(result) is True

    @pytest.mark.satellite
    def test_rms_detect_barely_below_threshold(self):
        """_rms_detect() with RMS just below threshold returns False."""
        threshold = 500.0
        vad = VoiceActivityDetector(rms_threshold=threshold)

        # Create audio with RMS just below threshold
        # For constant signal, RMS = abs(value), so use value < threshold
        quiet = np.full(640, 400, dtype=np.int16).tobytes()
        result = vad._rms_detect(quiet)

        assert bool(result) is False

    @pytest.mark.satellite
    def test_rms_detect_at_exact_threshold(self):
        """_rms_detect() with RMS exactly at threshold returns True (>= comparison)."""
        threshold = 500.0
        vad = VoiceActivityDetector(rms_threshold=threshold)

        # For constant int16 signal of value V, RMS = V
        at_threshold = np.full(640, int(threshold), dtype=np.int16).tobytes()
        result = vad._rms_detect(at_threshold)

        assert bool(result) is True

    @pytest.mark.satellite
    def test_rms_detect_just_above_threshold(self):
        """_rms_detect() with RMS just above threshold returns True."""
        threshold = 500.0
        vad = VoiceActivityDetector(rms_threshold=threshold)

        above = np.full(640, 600, dtype=np.int16).tobytes()
        result = vad._rms_detect(above)

        assert bool(result) is True

    @pytest.mark.satellite
    def test_rms_detect_empty_bytes_returns_false(self):
        """_rms_detect() on empty bytes returns False (graceful error handling)."""
        vad = VoiceActivityDetector(rms_threshold=500.0)
        result = vad._rms_detect(b"")
        assert bool(result) is False

    @pytest.mark.satellite
    def test_rms_detect_negative_amplitude(self):
        """_rms_detect() works correctly with negative amplitude samples."""
        vad = VoiceActivityDetector(rms_threshold=500.0)

        # Negative values have the same RMS as positive
        negative_loud = np.full(640, -10000, dtype=np.int16).tobytes()
        result = vad._rms_detect(negative_loud)

        assert bool(result) is True


class TestAvailableBackends:
    """Tests for VoiceActivityDetector.get_available_backends()."""

    @pytest.mark.satellite
    def test_rms_always_in_available_backends(self):
        """get_available_backends() always includes RMS."""
        backends = VoiceActivityDetector.get_available_backends()
        assert VADBackend.RMS in backends

    @pytest.mark.satellite
    def test_available_backends_returns_list(self):
        """get_available_backends() returns a list of VADBackend enums."""
        backends = VoiceActivityDetector.get_available_backends()

        assert isinstance(backends, list)
        assert len(backends) >= 1
        for b in backends:
            assert isinstance(b, VADBackend)

    @pytest.mark.satellite
    def test_rms_is_first_in_available_backends(self):
        """RMS is the first backend in the available list."""
        backends = VoiceActivityDetector.get_available_backends()
        assert backends[0] == VADBackend.RMS


class TestIsSpeechDelegation:
    """Tests that is_speech() delegates to the correct backend."""

    @pytest.mark.satellite
    def test_is_speech_delegates_to_rms_detect(self):
        """is_speech() on RMS backend delegates to _rms_detect()."""
        vad = VoiceActivityDetector(backend=VADBackend.RMS, rms_threshold=500.0)

        # Silent => False
        silence = np.zeros(640, dtype=np.int16).tobytes()
        assert bool(vad.is_speech(silence)) is False

        # Loud => True
        loud = np.full(640, 10000, dtype=np.int16).tobytes()
        assert bool(vad.is_speech(loud)) is True

    @pytest.mark.satellite
    def test_is_speech_rms_backend_calls_rms_detect(self):
        """is_speech() with RMS backend calls _rms_detect() internally."""
        vad = VoiceActivityDetector(backend=VADBackend.RMS, rms_threshold=500.0)

        test_bytes = np.full(640, 10000, dtype=np.int16).tobytes()

        with patch.object(vad, '_rms_detect', wraps=vad._rms_detect) as mock_rms:
            vad.is_speech(test_bytes)
            mock_rms.assert_called_once_with(test_bytes)

    @pytest.mark.satellite
    def test_is_speech_webrtc_fallback_to_rms_when_unavailable(self):
        """When WebRTC is requested but unavailable, backend falls back to RMS."""
        # WebRTC may or may not be installed, but we can test the fallback logic
        # by checking that the backend attribute is either WEBRTC or RMS after init.
        vad = VoiceActivityDetector(backend=VADBackend.WEBRTC)

        # If webrtcvad is not installed, it should have fallen back to RMS
        from renfield_satellite.audio.vad import WEBRTC_AVAILABLE
        if not WEBRTC_AVAILABLE:
            assert vad.backend == VADBackend.RMS
        else:
            assert vad.backend == VADBackend.WEBRTC


class TestVADBackendEnum:
    """Tests for VADBackend enum values."""

    @pytest.mark.satellite
    def test_backend_enum_values(self):
        """VADBackend enum has expected string values."""
        assert VADBackend.RMS.value == "rms"
        assert VADBackend.WEBRTC.value == "webrtc"
        assert VADBackend.SILERO.value == "silero"

    @pytest.mark.satellite
    def test_backend_enum_is_string(self):
        """VADBackend values can be used as strings."""
        assert str(VADBackend.RMS) == "VADBackend.RMS"
        # The value itself is a string due to str(Enum)
        assert VADBackend.RMS == "rms"
