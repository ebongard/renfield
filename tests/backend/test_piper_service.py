"""Tests for PiperService.

Tests TTS initialization, voice selection, synthesis methods,
and availability checks.
"""
import sys
from unittest.mock import MagicMock

# Pre-mock modules not available in test environment
_missing_stubs = [
    "asyncpg", "whisper", "piper", "piper.voice", "speechbrain",
    "speechbrain.inference", "speechbrain.inference.speaker",
    "openwakeword", "openwakeword.model",
]
for _mod in _missing_stubs:
    if _mod not in sys.modules:
        sys.modules[_mod] = MagicMock()

from unittest.mock import AsyncMock, patch

import pytest

from services.piper_service import PiperService


def _make_mock_settings(
    piper_voice="de_DE-thorsten-high",
    piper_voice_map=None,
    default_language="de",
):
    s = MagicMock()
    s.piper_voice = piper_voice
    s.piper_voice_map = piper_voice_map or {"de": "de_DE-thorsten-high", "en": "en_US-amy-medium"}
    s.default_language = default_language
    return s


@pytest.fixture
def mock_settings():
    return _make_mock_settings()


@pytest.fixture
def service(mock_settings):
    with patch("services.piper_service.settings", mock_settings), \
         patch("services.piper_service.subprocess") as mock_subprocess:
        # Simulate piper being available
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_subprocess.run.return_value = mock_result
        svc = PiperService()
    return svc


@pytest.fixture
def service_unavailable(mock_settings):
    with patch("services.piper_service.settings", mock_settings), \
         patch("services.piper_service.subprocess") as mock_subprocess:
        # Simulate piper NOT being available
        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_subprocess.run.return_value = mock_result
        svc = PiperService()
    return svc


# ============================================================================
# Initialization Tests
# ============================================================================

@pytest.mark.unit
class TestPiperServiceInit:

    def test_init_with_piper_available(self, service):
        """Service is available when piper binary exists."""
        assert service.available is True

    def test_init_with_piper_unavailable(self, service_unavailable):
        """Service is not available when piper binary missing."""
        assert service_unavailable.available is False

    def test_init_sets_voice_map(self, service):
        """Voice map is loaded from settings."""
        assert service.voice_map == {"de": "de_DE-thorsten-high", "en": "en_US-amy-medium"}

    def test_init_sets_default_voice(self, service):
        """Default voice is loaded from settings."""
        assert service.default_voice == "de_DE-thorsten-high"

    def test_init_sets_default_language(self, service):
        assert service.default_language == "de"

    def test_check_piper_handles_exception(self):
        """Graceful handling when subprocess.run raises."""
        mock_s = _make_mock_settings()
        with patch("services.piper_service.settings", mock_s), \
             patch("services.piper_service.subprocess") as mock_sub:
            mock_sub.run.side_effect = FileNotFoundError("which not found")
            svc = PiperService()
        assert svc.available is False


# ============================================================================
# Voice Selection Tests
# ============================================================================

@pytest.mark.unit
class TestVoiceSelection:

    def test_get_voice_for_german(self, service):
        assert service._get_voice_for_language("de") == "de_DE-thorsten-high"

    def test_get_voice_for_english(self, service):
        assert service._get_voice_for_language("en") == "en_US-amy-medium"

    def test_get_voice_for_unknown_language_falls_back(self, service):
        """Unknown language falls back to default voice."""
        result = service._get_voice_for_language("fr")
        assert result == "de_DE-thorsten-high"  # Falls back to default_voice

    def test_get_voice_for_none_uses_default_language(self, service):
        """None language uses default_language setting."""
        result = service._get_voice_for_language(None)
        assert result == "de_DE-thorsten-high"  # de is default_language

    def test_get_voice_case_insensitive(self, service):
        """Language lookup is case-insensitive."""
        result = service._get_voice_for_language("DE")
        assert result == "de_DE-thorsten-high"

    def test_get_model_path(self, service):
        """Model path follows expected convention."""
        path = service._get_model_path("de_DE-thorsten-high")
        assert path == "/usr/share/piper/voices/de_DE-thorsten-high.onnx"


# ============================================================================
# Synthesis Tests
# ============================================================================

@pytest.mark.unit
class TestSynthesis:

    @pytest.mark.asyncio
    async def test_synthesize_to_file_success(self, service, tmp_path):
        """Successful synthesis writes file and returns True."""
        output_path = str(tmp_path / "output.wav")

        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.communicate.return_value = (b"", b"")

        with patch("services.piper_service.subprocess.Popen", return_value=mock_proc) as mock_popen:
            result = await service.synthesize_to_file("Hallo Welt", output_path)

        assert result is True
        mock_popen.assert_called_once()
        # Verify command includes model and output_file
        call_args = mock_popen.call_args[0][0]
        assert "piper" in call_args
        assert "--model" in call_args
        assert "--output_file" in call_args

    @pytest.mark.asyncio
    async def test_synthesize_to_file_failure(self, service, tmp_path):
        """Failed synthesis returns False."""
        output_path = str(tmp_path / "output.wav")

        mock_proc = MagicMock()
        mock_proc.returncode = 1
        mock_proc.communicate.return_value = (b"", b"Error: model not found")

        with patch("services.piper_service.subprocess.Popen", return_value=mock_proc):
            result = await service.synthesize_to_file("Hallo", output_path)

        assert result is False

    @pytest.mark.asyncio
    async def test_synthesize_to_file_exception(self, service, tmp_path):
        """Exception during synthesis returns False."""
        output_path = str(tmp_path / "output.wav")

        with patch("services.piper_service.subprocess.Popen", side_effect=OSError("spawn failed")):
            result = await service.synthesize_to_file("Hallo", output_path)

        assert result is False

    @pytest.mark.asyncio
    async def test_synthesize_to_file_when_unavailable(self, service_unavailable, tmp_path):
        """Returns False immediately when piper is not available."""
        result = await service_unavailable.synthesize_to_file("Hello", str(tmp_path / "out.wav"))
        assert result is False

    @pytest.mark.asyncio
    async def test_synthesize_to_file_uses_correct_voice_for_language(self, service, tmp_path):
        """Correct voice model is used based on language parameter."""
        output_path = str(tmp_path / "output.wav")

        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.communicate.return_value = (b"", b"")

        with patch("services.piper_service.subprocess.Popen", return_value=mock_proc) as mock_popen:
            await service.synthesize_to_file("Hello world", output_path, language="en")

        call_args = mock_popen.call_args[0][0]
        # Should use en_US-amy-medium model
        assert "/usr/share/piper/voices/en_US-amy-medium.onnx" in call_args

    @pytest.mark.asyncio
    async def test_synthesize_to_bytes_success(self, service, tmp_path):
        """synthesize_to_bytes returns audio bytes on success."""
        fake_wav = b"RIFF\x00\x00\x00\x00WAVEfmt "

        with patch.object(service, "synthesize_to_file", new_callable=AsyncMock) as mock_synth:
            # Simulate synthesize_to_file writing content
            async def write_and_return(text, path, language=None):
                from pathlib import Path
                Path(path).write_bytes(fake_wav)
                return True
            mock_synth.side_effect = write_and_return

            result = await service.synthesize_to_bytes("Hallo")

        assert result == fake_wav

    @pytest.mark.asyncio
    async def test_synthesize_to_bytes_failure_returns_empty(self, service):
        """synthesize_to_bytes returns empty bytes on failure."""
        with patch.object(service, "synthesize_to_file", new_callable=AsyncMock, return_value=False):
            result = await service.synthesize_to_bytes("Hallo")

        assert result == b""

    @pytest.mark.asyncio
    async def test_synthesize_to_bytes_when_unavailable(self, service_unavailable):
        """Returns empty bytes when piper unavailable."""
        result = await service_unavailable.synthesize_to_bytes("Hello")
        assert result == b""

    @pytest.mark.asyncio
    async def test_synthesize_sends_text_via_stdin(self, service, tmp_path):
        """Text is passed to piper via stdin."""
        output_path = str(tmp_path / "output.wav")

        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.communicate.return_value = (b"", b"")

        with patch("services.piper_service.subprocess.Popen", return_value=mock_proc):
            await service.synthesize_to_file("Hallo Welt", output_path)

        mock_proc.communicate.assert_called_once_with(input=b"Hallo Welt")


# ============================================================================
# ensure_model_downloaded Tests
# ============================================================================

@pytest.mark.unit
class TestEnsureModelDownloaded:

    def test_noop_when_available(self, service):
        """Does nothing when piper is available (piper auto-downloads)."""
        service.ensure_model_downloaded()  # Should not raise

    def test_noop_when_unavailable(self, service_unavailable):
        """Does nothing when piper is unavailable."""
        service_unavailable.ensure_model_downloaded()  # Should not raise
