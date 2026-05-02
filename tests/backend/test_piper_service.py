"""Tests for PiperService (in-process piper-tts bindings).

After the Phase A voice-pipeline migration, PiperService loads voice models
in-process via `piper.voice.PiperVoice.load(...)` instead of shelling out to
the `piper` CLI per request. These tests mock the in-process API.
"""
import sys
from unittest.mock import AsyncMock, MagicMock, patch

# Pre-mock modules not available in test environment. piper.voice is imported
# at module-load time by piper_service so a real stub must answer attribute
# access for `PiperVoice`.
_missing_stubs = [
    "asyncpg", "faster_whisper", "speechbrain",
    "speechbrain.inference", "speechbrain.inference.speaker",
    "openwakeword", "openwakeword.model",
]
for _mod in _missing_stubs:
    if _mod not in sys.modules:
        sys.modules[_mod] = MagicMock()

# piper / piper.voice need to be MagicMock-shaped so `from piper.voice import PiperVoice`
# works and `PiperVoice.load(...)` can be patched per-test.
if "piper" not in sys.modules:
    sys.modules["piper"] = MagicMock()
if "piper.voice" not in sys.modules:
    piper_voice_mod = MagicMock()
    piper_voice_mod.PiperVoice = MagicMock()
    sys.modules["piper.voice"] = piper_voice_mod

import pytest

from services.piper_service import PiperService


def _make_mock_settings(
    piper_default_voice="de_DE-thorsten-high",
    piper_voice_map=None,
    default_language="de",
    tts_cache_size=0,
    tts_max_concurrent=4,
):
    s = MagicMock()
    s.piper_default_voice = piper_default_voice
    s.piper_voice_map = piper_voice_map or {"de": "de_DE-thorsten-high", "en": "en_US-amy-medium"}
    s.default_language = default_language
    s.tts_cache_size = tts_cache_size
    s.tts_max_concurrent = tts_max_concurrent
    return s


@pytest.fixture
def mock_settings():
    return _make_mock_settings()


@pytest.fixture
def service(mock_settings):
    """Service with PIPER_AVAILABLE=True (piper.voice import succeeded)."""
    with patch("services.piper_service.settings", mock_settings), \
         patch("services.piper_service.PIPER_AVAILABLE", True):
        svc = PiperService()
    return svc


@pytest.fixture
def service_unavailable(mock_settings):
    """Service with PIPER_AVAILABLE=False (piper-tts not installed)."""
    with patch("services.piper_service.settings", mock_settings), \
         patch("services.piper_service.PIPER_AVAILABLE", False):
        svc = PiperService()
    return svc


@pytest.fixture
def service_cached(mock_settings):
    """Service with a 4-entry TTS cache enabled."""
    mock_settings.tts_cache_size = 4
    with patch("services.piper_service.settings", mock_settings), \
         patch("services.piper_service.PIPER_AVAILABLE", True):
        svc = PiperService()
    return svc


@pytest.fixture
def fake_voice():
    """A PiperVoice-like mock whose synthesize() writes a minimal WAV header."""
    voice = MagicMock()

    def _synthesize(text, wav_file):
        # Real PiperVoice.synthesize writes frames into the wave.Wave_write
        # object. For tests we just write a 4-byte sentinel so the resulting
        # file isn't empty.
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(22050)
        wav_file.writeframes(b"\x00\x00\x00\x00")

    voice.synthesize = MagicMock(side_effect=_synthesize)
    return voice


# ============================================================================
# Initialization Tests
# ============================================================================

@pytest.mark.unit
class TestPiperServiceInit:

    def test_init_with_piper_available(self, service):
        """Service is available when piper-tts package is installed."""
        assert service.available is True

    def test_init_with_piper_unavailable(self, service_unavailable):
        """Service is not available when piper-tts is missing."""
        assert service_unavailable.available is False

    def test_init_sets_voice_map(self, service):
        assert service.voice_map == {"de": "de_DE-thorsten-high", "en": "en_US-amy-medium"}

    def test_init_sets_default_voice(self, service):
        assert service.default_voice == "de_DE-thorsten-high"

    def test_init_sets_default_language(self, service):
        assert service.default_language == "de"

    def test_init_starts_with_empty_voice_cache(self, service):
        assert service._voice_cache == {}


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
        result = service._get_voice_for_language("fr")
        assert result == "de_DE-thorsten-high"  # Falls back to default_voice

    def test_get_voice_for_none_uses_default_language(self, service):
        result = service._get_voice_for_language(None)
        assert result == "de_DE-thorsten-high"

    def test_get_voice_case_insensitive(self, service):
        result = service._get_voice_for_language("DE")
        assert result == "de_DE-thorsten-high"

    def test_get_model_path(self, service):
        path = service._get_model_path("de_DE-thorsten-high")
        assert path == "/usr/share/piper/voices/de_DE-thorsten-high.onnx"


# ============================================================================
# Voice Loading + Caching
# ============================================================================

@pytest.mark.unit
class TestVoiceLoading:

    def test_load_voice_caches_result(self, service, fake_voice):
        """Subsequent calls reuse the cached PiperVoice instance."""
        with patch("services.piper_service.Path") as mock_path, \
             patch("services.piper_service.PiperVoice") as mock_pv:
            mock_path.return_value.exists.return_value = True
            mock_pv.load.return_value = fake_voice

            v1 = service._load_voice("de_DE-thorsten-high")
            v2 = service._load_voice("de_DE-thorsten-high")

        assert v1 is fake_voice
        assert v2 is fake_voice
        mock_pv.load.assert_called_once()

    def test_load_voice_returns_none_when_model_missing(self, service):
        """Missing .onnx file → returns None."""
        with patch("services.piper_service.Path") as mock_path:
            mock_path.return_value.exists.return_value = False
            result = service._load_voice("nonexistent-voice")

        assert result is None

    def test_load_voice_returns_none_on_exception(self, service):
        """PiperVoice.load() raising → returns None, doesn't crash."""
        with patch("services.piper_service.Path") as mock_path, \
             patch("services.piper_service.PiperVoice") as mock_pv:
            mock_path.return_value.exists.return_value = True
            mock_pv.load.side_effect = RuntimeError("ONNX session init failed")

            result = service._load_voice("broken-voice")

        assert result is None

    def test_load_voice_returns_none_when_unavailable(self, service_unavailable):
        """When PIPER_AVAILABLE=False, _load_voice short-circuits."""
        result = service_unavailable._load_voice("de_DE-thorsten-high")
        assert result is None


# ============================================================================
# Synthesis Tests
# ============================================================================

@pytest.mark.unit
class TestSynthesis:

    @pytest.mark.asyncio
    async def test_synthesize_to_file_success(self, service, fake_voice, tmp_path):
        """Successful synthesis writes a WAV and returns True."""
        output_path = str(tmp_path / "output.wav")
        with patch.object(service, "_load_voice", return_value=fake_voice):
            result = await service.synthesize_to_file("Hallo Welt", output_path)

        assert result is True
        fake_voice.synthesize.assert_called_once()
        call_args = fake_voice.synthesize.call_args[0]
        assert call_args[0] == "Hallo Welt"
        assert tmp_path.joinpath("output.wav").stat().st_size > 0

    @pytest.mark.asyncio
    async def test_synthesize_to_file_uses_correct_voice_for_language(self, service, fake_voice, tmp_path):
        """Correct voice is loaded based on language parameter."""
        output_path = str(tmp_path / "output.wav")
        with patch.object(service, "_load_voice", return_value=fake_voice) as mock_load:
            await service.synthesize_to_file("Hello world", output_path, language="en")

        mock_load.assert_called_once_with("en_US-amy-medium")

    @pytest.mark.asyncio
    async def test_synthesize_to_file_returns_false_when_voice_missing(self, service, tmp_path):
        """When _load_voice returns None, synthesize returns False."""
        output_path = str(tmp_path / "output.wav")
        with patch.object(service, "_load_voice", return_value=None):
            result = await service.synthesize_to_file("Hallo", output_path)

        assert result is False

    @pytest.mark.asyncio
    async def test_synthesize_to_file_returns_false_on_exception(self, service, tmp_path):
        """Exception during synthesize() returns False."""
        output_path = str(tmp_path / "output.wav")
        bad_voice = MagicMock()
        bad_voice.synthesize.side_effect = RuntimeError("inference error")
        with patch.object(service, "_load_voice", return_value=bad_voice):
            result = await service.synthesize_to_file("Hallo", output_path)

        assert result is False

    @pytest.mark.asyncio
    async def test_synthesize_to_file_when_unavailable(self, service_unavailable, tmp_path):
        result = await service_unavailable.synthesize_to_file("Hello", str(tmp_path / "out.wav"))
        assert result is False

    @pytest.mark.asyncio
    async def test_synthesize_to_bytes_success(self, service, fake_voice):
        """synthesize_to_bytes returns audio bytes when synthesis succeeds."""
        with patch.object(service, "_load_voice", return_value=fake_voice):
            result = await service.synthesize_to_bytes("Hallo")

        # WAV file should have at least the header (44 bytes) plus our sentinel
        assert len(result) >= 44
        assert result[:4] == b"RIFF"

    @pytest.mark.asyncio
    async def test_synthesize_to_bytes_returns_empty_when_voice_missing(self, service):
        with patch.object(service, "_load_voice", return_value=None):
            result = await service.synthesize_to_bytes("Hallo")
        assert result == b""

    @pytest.mark.asyncio
    async def test_synthesize_to_bytes_when_unavailable(self, service_unavailable):
        result = await service_unavailable.synthesize_to_bytes("Hello")
        assert result == b""


# ============================================================================
# TTS LRU Cache (Phase B)
# ============================================================================

@pytest.mark.unit
class TestTtsLruCache:

    @pytest.mark.asyncio
    async def test_repeat_text_hits_cache_and_skips_synthesis(self, service_cached, fake_voice):
        """Same (voice, text) on second call returns cached bytes without re-running synthesize."""
        with patch.object(service_cached, "_load_voice", return_value=fake_voice):
            first = await service_cached.synthesize_to_bytes("Verstanden")
            second = await service_cached.synthesize_to_bytes("Verstanden")

        assert first == second
        assert fake_voice.synthesize.call_count == 1
        stats = service_cached.cache_stats()
        assert stats["hits"] == 1
        assert stats["misses"] == 1
        assert stats["size"] == 1

    @pytest.mark.asyncio
    async def test_different_text_misses_cache(self, service_cached, fake_voice):
        with patch.object(service_cached, "_load_voice", return_value=fake_voice):
            await service_cached.synthesize_to_bytes("Verstanden")
            await service_cached.synthesize_to_bytes("Bestätigt")

        assert fake_voice.synthesize.call_count == 2
        assert service_cached.cache_stats()["size"] == 2

    @pytest.mark.asyncio
    async def test_different_language_partitions_cache(self, service_cached, fake_voice):
        """Same text in different languages must not collide — voice is part of the key."""
        with patch.object(service_cached, "_load_voice", return_value=fake_voice):
            await service_cached.synthesize_to_bytes("OK", language="de")
            await service_cached.synthesize_to_bytes("OK", language="en")

        assert fake_voice.synthesize.call_count == 2
        assert service_cached.cache_stats()["size"] == 2

    @pytest.mark.asyncio
    async def test_lru_eviction_at_capacity(self, service_cached, fake_voice):
        """Cache evicts least-recently-used entry when capacity exceeded."""
        with patch.object(service_cached, "_load_voice", return_value=fake_voice):
            for text in ("a", "b", "c", "d", "e"):  # capacity=4 → "a" evicted
                await service_cached.synthesize_to_bytes(text)

            assert service_cached.cache_stats()["size"] == 4

            # "a" was evicted → re-synthesize on next call
            count_before = fake_voice.synthesize.call_count
            await service_cached.synthesize_to_bytes("a")
            assert fake_voice.synthesize.call_count == count_before + 1

    @pytest.mark.asyncio
    async def test_disabled_cache_does_not_store(self, service, fake_voice):
        """tts_cache_size=0 disables caching entirely."""
        with patch.object(service, "_load_voice", return_value=fake_voice):
            await service.synthesize_to_bytes("Verstanden")
            await service.synthesize_to_bytes("Verstanden")

        assert fake_voice.synthesize.call_count == 2
        assert service.cache_stats()["size"] == 0

    @pytest.mark.asyncio
    async def test_cache_hit_writes_file(self, service_cached, fake_voice, tmp_path):
        """Cache hit on synthesize_to_file still produces a valid WAV on disk."""
        path1 = str(tmp_path / "first.wav")
        path2 = str(tmp_path / "second.wav")
        with patch.object(service_cached, "_load_voice", return_value=fake_voice):
            await service_cached.synthesize_to_file("Verstanden", path1)
            await service_cached.synthesize_to_file("Verstanden", path2)

        assert (tmp_path / "first.wav").read_bytes() == (tmp_path / "second.wav").read_bytes()
        assert fake_voice.synthesize.call_count == 1


# ============================================================================
# ensure_model_downloaded Tests
# ============================================================================

@pytest.mark.unit
class TestEnsureModelDownloaded:

    def test_noop_when_available(self, service):
        """Voice models are baked into the image; nothing to do."""
        service.ensure_model_downloaded()  # Should not raise

    def test_noop_when_unavailable(self, service_unavailable):
        service_unavailable.ensure_model_downloaded()  # Should not raise
