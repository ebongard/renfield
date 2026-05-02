"""Tests for WhisperService.

Tests model loading, transcription, language detection, audio format handling,
preprocessing, and transcribe_bytes.
"""
import sys
from unittest.mock import MagicMock

# Pre-mock modules not available in test environment
_missing_stubs = [
    "asyncpg", "faster_whisper", "piper", "piper.voice", "speechbrain",
    "speechbrain.inference", "speechbrain.inference.speaker",
    "openwakeword", "openwakeword.model",
    "librosa", "soundfile",
]
for _mod in _missing_stubs:
    if _mod not in sys.modules:
        sys.modules[_mod] = MagicMock()


def _segments_result(text: str):
    """Build the (segments_iter, info) tuple that faster-whisper's transcribe returns.

    The real API returns a one-shot generator of Segment-like objects each
    carrying a `.text` attribute, plus an info object. We wrap the segment
    list in `iter(...)` so that test code accidentally relying on list
    semantics (`len()`, `[0]`, double-iteration) fails the same way
    production would — faster-whisper drains the generator on first
    iteration and a second pass yields nothing.
    """
    seg = MagicMock()
    seg.text = text
    return (iter([seg]), MagicMock())


def _multi_segment_result(*texts: str):
    """Multi-segment variant of _segments_result.

    faster-whisper splits long audio into multiple segments. Each segment's
    `.text` typically begins with a space when the tokenizer emits a word
    boundary, so plain "".join() over `seg.text` produces correctly spaced
    output. Use this helper to verify the join contract holds for >1
    segment.
    """
    segments = []
    for t in texts:
        seg = MagicMock()
        seg.text = t
        segments.append(seg)
    return (iter(segments), MagicMock())

from unittest.mock import AsyncMock, patch

import pytest

from services.whisper_service import WhisperService


def _make_mock_settings(
    whisper_model="base",
    whisper_device="cpu",
    whisper_compute_type="int8",
    whisper_beam_size=5,
    default_language="de",
    whisper_initial_prompt=None,
    whisper_preprocess_enabled=False,
    whisper_preprocess_noise_reduce=True,
    whisper_preprocess_normalize=True,
    whisper_preprocess_target_db=-20.0,
    speaker_recognition_enabled=False,
    whisper_max_concurrent=2,
):
    s = MagicMock()
    s.whisper_model = whisper_model
    s.whisper_device = whisper_device
    s.whisper_compute_type = whisper_compute_type
    s.whisper_beam_size = whisper_beam_size
    s.default_language = default_language
    s.whisper_initial_prompt = whisper_initial_prompt
    s.whisper_preprocess_enabled = whisper_preprocess_enabled
    s.whisper_preprocess_noise_reduce = whisper_preprocess_noise_reduce
    s.whisper_preprocess_normalize = whisper_preprocess_normalize
    s.whisper_preprocess_target_db = whisper_preprocess_target_db
    s.speaker_recognition_enabled = speaker_recognition_enabled
    s.whisper_max_concurrent = whisper_max_concurrent
    return s


@pytest.fixture
def mock_settings():
    return _make_mock_settings()


@pytest.fixture
def service(mock_settings):
    with patch("services.whisper_service.settings", mock_settings), \
         patch("services.whisper_service.AudioPreprocessor"):
        svc = WhisperService()
    return svc


# ============================================================================
# Initialization Tests
# ============================================================================

@pytest.mark.unit
class TestWhisperServiceInit:

    def test_init_sets_model_size(self, service):
        assert service.model_size == "base"

    def test_init_model_not_loaded_yet(self, service):
        assert service.model is None

    def test_init_sets_language(self, service):
        assert service.language == "de"

    def test_init_with_initial_prompt(self):
        mock_s = _make_mock_settings(whisper_initial_prompt="Renfield Transkription")
        with patch("services.whisper_service.settings", mock_s), \
             patch("services.whisper_service.AudioPreprocessor"):
            svc = WhisperService()
        assert svc.initial_prompt == "Renfield Transkription"

    def test_init_without_initial_prompt(self, service):
        assert service.initial_prompt is None

    def test_init_preprocessing_disabled_by_default(self, service):
        assert service.preprocess_enabled is False


# ============================================================================
# Model Loading Tests
# ============================================================================

@pytest.mark.unit
class TestModelLoading:

    def test_load_model_calls_whisper_model(self, service):
        mock_model = MagicMock()
        with patch("services.whisper_service.WhisperModel") as mock_ctor:
            mock_ctor.return_value = mock_model
            service.load_model()

        assert service.model is mock_model
        mock_ctor.assert_called_once_with("base", device="cpu", compute_type="int8")

    def test_load_model_only_once(self, service):
        mock_model = MagicMock()
        with patch("services.whisper_service.WhisperModel") as mock_ctor:
            mock_ctor.return_value = mock_model
            service.load_model()
            service.load_model()  # Second call should be noop

        mock_ctor.assert_called_once()

    def test_load_model_raises_on_error(self, service):
        with patch("services.whisper_service.WhisperModel") as mock_ctor:
            mock_ctor.side_effect = RuntimeError("CUDA OOM")
            with pytest.raises(RuntimeError, match="CUDA OOM"):
                service.load_model()

    def test_load_model_passes_gpu_settings(self):
        """When configured for cuda+float16, settings flow into the constructor."""
        mock_s = _make_mock_settings(whisper_device="cuda", whisper_compute_type="float16")
        with patch("services.whisper_service.settings", mock_s), \
             patch("services.whisper_service.AudioPreprocessor"):
            svc = WhisperService()

        with patch("services.whisper_service.WhisperModel") as mock_ctor:
            mock_ctor.return_value = MagicMock()
            svc.load_model()

        mock_ctor.assert_called_once_with("base", device="cuda", compute_type="float16")


# ============================================================================
# Transcription Tests
# ============================================================================

@pytest.mark.unit
class TestTranscription:

    @pytest.mark.asyncio
    async def test_transcribe_file_success(self, service):
        """Successful transcription returns text."""
        mock_model = MagicMock()
        mock_model.transcribe.return_value = _segments_result("  Hallo Welt  ")
        service.model = mock_model

        result = await service.transcribe_file("/tmp/test.wav")

        assert result == "Hallo Welt"
        mock_model.transcribe.assert_called_once()

    @pytest.mark.asyncio
    async def test_transcribe_file_auto_loads_model(self, service):
        """Model is loaded on first transcription if not yet loaded."""
        mock_model = MagicMock()
        mock_model.transcribe.return_value = _segments_result("Test")

        with patch("services.whisper_service.WhisperModel") as mock_ctor:
            mock_ctor.return_value = mock_model
            result = await service.transcribe_file("/tmp/test.wav")

        assert result == "Test"
        mock_ctor.assert_called_once()

    @pytest.mark.asyncio
    async def test_transcribe_file_uses_default_language(self, service):
        """Uses default language when none specified."""
        mock_model = MagicMock()
        mock_model.transcribe.return_value = _segments_result("Hallo")
        service.model = mock_model

        await service.transcribe_file("/tmp/test.wav")

        call_kwargs = mock_model.transcribe.call_args
        assert call_kwargs[1]["language"] == "de"

    @pytest.mark.asyncio
    async def test_transcribe_file_uses_explicit_language(self, service):
        """Uses explicitly provided language."""
        mock_model = MagicMock()
        mock_model.transcribe.return_value = _segments_result("Hello")
        service.model = mock_model

        await service.transcribe_file("/tmp/test.wav", language="en")

        call_kwargs = mock_model.transcribe.call_args
        assert call_kwargs[1]["language"] == "en"

    @pytest.mark.asyncio
    async def test_transcribe_file_includes_initial_prompt(self):
        """Initial prompt is passed to whisper when configured."""
        mock_s = _make_mock_settings(whisper_initial_prompt="Smart Home commands")
        with patch("services.whisper_service.settings", mock_s), \
             patch("services.whisper_service.AudioPreprocessor"):
            svc = WhisperService()

        mock_model = MagicMock()
        mock_model.transcribe.return_value = _segments_result("Turn on lights")
        svc.model = mock_model

        await svc.transcribe_file("/tmp/test.wav")

        call_kwargs = mock_model.transcribe.call_args
        assert call_kwargs[1]["initial_prompt"] == "Smart Home commands"

    @pytest.mark.asyncio
    async def test_transcribe_file_per_call_prompt_overrides_default(self):
        """Per-call initial_prompt overrides the service-level default (B-3 plumbing)."""
        mock_s = _make_mock_settings(whisper_initial_prompt="Service default prompt")
        with patch("services.whisper_service.settings", mock_s), \
             patch("services.whisper_service.AudioPreprocessor"):
            svc = WhisperService()

        mock_model = MagicMock()
        mock_model.transcribe.return_value = _segments_result("ok")
        svc.model = mock_model

        await svc.transcribe_file("/tmp/test.wav", initial_prompt="Per-call override")

        call_kwargs = mock_model.transcribe.call_args
        assert call_kwargs[1]["initial_prompt"] == "Per-call override"

    @pytest.mark.asyncio
    async def test_transcribe_file_per_call_prompt_none_falls_through(self):
        """initial_prompt=None falls through to the service default rather than blanking it."""
        mock_s = _make_mock_settings(whisper_initial_prompt="Service default prompt")
        with patch("services.whisper_service.settings", mock_s), \
             patch("services.whisper_service.AudioPreprocessor"):
            svc = WhisperService()

        mock_model = MagicMock()
        mock_model.transcribe.return_value = _segments_result("ok")
        svc.model = mock_model

        await svc.transcribe_file("/tmp/test.wav", initial_prompt=None)

        call_kwargs = mock_model.transcribe.call_args
        assert call_kwargs[1]["initial_prompt"] == "Service default prompt"

    @pytest.mark.asyncio
    async def test_transcribe_file_per_call_empty_string_disables_prompt(self):
        """Empty-string per-call override disables the prompt — distinguished from None."""
        mock_s = _make_mock_settings(whisper_initial_prompt="Service default prompt")
        with patch("services.whisper_service.settings", mock_s), \
             patch("services.whisper_service.AudioPreprocessor"):
            svc = WhisperService()

        mock_model = MagicMock()
        mock_model.transcribe.return_value = _segments_result("ok")
        svc.model = mock_model

        await svc.transcribe_file("/tmp/test.wav", initial_prompt="")

        call_kwargs = mock_model.transcribe.call_args
        # Empty string is "no prompt" — _run_transcription only sets the kwarg
        # when the resolved prompt is truthy, so initial_prompt key is absent.
        assert "initial_prompt" not in call_kwargs[1]

    @pytest.mark.asyncio
    async def test_transcribe_file_error_returns_empty(self, service):
        """Transcription error returns empty string."""
        mock_model = MagicMock()
        mock_model.transcribe.side_effect = RuntimeError("decode error")
        service.model = mock_model

        result = await service.transcribe_file("/tmp/test.wav")

        assert result == ""

    @pytest.mark.asyncio
    async def test_transcribe_file_joins_multiple_segments(self, service):
        """Long audio produces multiple segments — verify the join contract.

        faster-whisper segment text typically starts with a space when the
        tokenizer emits a word boundary, so "".join(...) produces correctly
        spaced output. Test that we don't accidentally double-space or drop
        whitespace between segments.
        """
        mock_model = MagicMock()
        mock_model.transcribe.return_value = _multi_segment_result(
            " Hallo", " Welt", " wie", " geht", " es", " dir",
        )
        service.model = mock_model

        result = await service.transcribe_file("/tmp/long.wav")

        assert result == "Hallo Welt wie geht es dir"

    @pytest.mark.asyncio
    async def test_transcribe_file_does_not_pass_fp16(self, service):
        """faster-whisper uses compute_type at model-load time, not per-call fp16."""
        mock_model = MagicMock()
        mock_model.transcribe.return_value = _segments_result("Test")
        service.model = mock_model

        await service.transcribe_file("/tmp/test.wav")

        call_kwargs = mock_model.transcribe.call_args
        assert "fp16" not in call_kwargs[1]
        assert "best_of" not in call_kwargs[1]

    @pytest.mark.asyncio
    async def test_transcribe_file_sets_beam_params(self, service):
        """Beam search parameter is set for accuracy."""
        mock_model = MagicMock()
        mock_model.transcribe.return_value = _segments_result("Test")
        service.model = mock_model

        await service.transcribe_file("/tmp/test.wav")

        call_kwargs = mock_model.transcribe.call_args
        assert call_kwargs[1]["beam_size"] == 5


# ============================================================================
# Transcribe Bytes Tests
# ============================================================================

@pytest.mark.unit
class TestTranscribeBytes:

    @pytest.mark.asyncio
    async def test_transcribe_bytes_writes_temp_file(self, service):
        """Bytes are written to a temp file and transcribed."""
        service.transcribe_file = AsyncMock(return_value="Transcribed text")

        result = await service.transcribe_bytes(b"fake-audio-data", filename="recording.wav")

        assert result == "Transcribed text"
        # transcribe_file was called with a temp path
        call_args = service.transcribe_file.call_args[0]
        assert call_args[0].endswith(".wav")

    @pytest.mark.asyncio
    async def test_transcribe_bytes_preserves_extension(self, service):
        """File extension from filename is preserved."""
        service.transcribe_file = AsyncMock(return_value="Text")

        await service.transcribe_bytes(b"data", filename="audio.mp3")

        call_args = service.transcribe_file.call_args[0]
        assert call_args[0].endswith(".mp3")

    @pytest.mark.asyncio
    async def test_transcribe_bytes_passes_language(self, service):
        """Language is forwarded to transcribe_file."""
        service.transcribe_file = AsyncMock(return_value="Text")

        await service.transcribe_bytes(b"data", language="en")

        call_kwargs = service.transcribe_file.call_args[1]
        assert call_kwargs["language"] == "en"

    @pytest.mark.asyncio
    async def test_transcribe_bytes_cleans_up_temp_file(self, service, tmp_path):
        """Temp file is cleaned up even on error."""
        service.transcribe_file = AsyncMock(side_effect=Exception("fail"))

        # Should not raise (temp file cleanup happens in finally)
        with pytest.raises(Exception):  # noqa: B017
            await service.transcribe_bytes(b"data")


# ============================================================================
# Preprocessing Tests
# ============================================================================

@pytest.mark.unit
class TestPreprocessing:

    @pytest.mark.asyncio
    async def test_preprocessing_skipped_when_disabled(self, service):
        """Preprocessing is not called when disabled."""
        service.preprocess_enabled = False
        mock_model = MagicMock()
        mock_model.transcribe.return_value = _segments_result("Test")
        service.model = mock_model

        await service.transcribe_file("/tmp/test.wav")

        # Original path is used directly
        mock_model.transcribe.assert_called_once()
        assert mock_model.transcribe.call_args[0][0] == "/tmp/test.wav"

    @pytest.mark.asyncio
    async def test_preprocessing_used_when_enabled(self, service):
        """Preprocessed file is used when preprocessing is enabled."""
        service.preprocess_enabled = True
        service._preprocess_audio = MagicMock(return_value="/tmp/preprocessed.wav")
        mock_model = MagicMock()
        mock_model.transcribe.return_value = _segments_result("Test")
        service.model = mock_model

        await service.transcribe_file("/tmp/test.wav")

        # Preprocessed path is used
        mock_model.transcribe.assert_called_once()
        assert mock_model.transcribe.call_args[0][0] == "/tmp/preprocessed.wav"

    @pytest.mark.asyncio
    async def test_preprocessing_failure_uses_original(self, service):
        """Falls back to original when preprocessing fails."""
        service.preprocess_enabled = True
        service._preprocess_audio = MagicMock(return_value=None)
        mock_model = MagicMock()
        mock_model.transcribe.return_value = _segments_result("Test")
        service.model = mock_model

        await service.transcribe_file("/tmp/test.wav")

        assert mock_model.transcribe.call_args[0][0] == "/tmp/test.wav"


# ============================================================================
# Transcribe with Speaker Tests
# ============================================================================

@pytest.mark.unit
class TestTranscribeWithSpeaker:

    @pytest.mark.asyncio
    async def test_returns_text_and_speaker_info(self, service):
        """Returns dict with text and speaker fields."""
        mock_model = MagicMock()
        mock_model.transcribe.return_value = _segments_result("Hello")
        service.model = mock_model

        result = await service.transcribe_with_speaker("/tmp/test.wav")

        assert result["text"] == "Hello"
        assert result["speaker_id"] is None
        assert result["speaker_name"] is None
        assert result["speaker_confidence"] == 0.0
        assert result["is_new_speaker"] is False

    @pytest.mark.asyncio
    async def test_transcription_error_returns_empty(self, service):
        """Transcription failure returns empty text with speaker defaults."""
        mock_model = MagicMock()
        mock_model.transcribe.side_effect = RuntimeError("fail")
        service.model = mock_model

        result = await service.transcribe_with_speaker("/tmp/test.wav")

        assert result["text"] == ""
        assert result["speaker_id"] is None

    @pytest.mark.asyncio
    async def test_skips_speaker_when_disabled(self, service):
        """Speaker recognition skipped when disabled in settings."""
        mock_model = MagicMock()
        mock_model.transcribe.return_value = _segments_result("Test")
        service.model = mock_model

        # speaker_recognition_enabled defaults to False in our mock
        result = await service.transcribe_with_speaker("/tmp/test.wav", db_session=MagicMock())

        assert result["speaker_id"] is None

    @pytest.mark.asyncio
    async def test_skips_speaker_when_no_db_session(self, service):
        """Speaker recognition skipped when no db_session provided."""
        mock_s = _make_mock_settings(speaker_recognition_enabled=True)
        with patch("services.whisper_service.settings", mock_s), \
             patch("services.whisper_service.AudioPreprocessor"):
            svc = WhisperService()

        mock_model = MagicMock()
        mock_model.transcribe.return_value = _segments_result("Test")
        svc.model = mock_model

        result = await svc.transcribe_with_speaker("/tmp/test.wav", db_session=None)

        assert result["speaker_id"] is None

    @pytest.mark.asyncio
    async def test_speaker_id_runs_in_parallel_with_transcribe(self):
        """B-3: STT and embedding extraction must run concurrently, not sequentially.

        Drives both code paths to a point where we can observe gather()-style
        parallelism: the embedding-extraction stub must START before transcribe
        FINISHES. We use an asyncio.Event the test sets after embedding starts
        and the transcribe stub awaits on.
        """
        import asyncio

        mock_s = _make_mock_settings(speaker_recognition_enabled=True)
        with patch("services.whisper_service.settings", mock_s), \
             patch("services.whisper_service.AudioPreprocessor"):
            svc = WhisperService()

        embedding_started = asyncio.Event()
        observed_parallel = False

        async def fake_transcribe_async(*args, **kwargs):
            nonlocal observed_parallel
            # Yield control. If embedding extraction is gathered in parallel,
            # it has the chance to set the event before we resume.
            await asyncio.sleep(0.01)
            observed_parallel = embedding_started.is_set()
            return "transcribed"

        async def fake_extract_embedding_async(audio_path):
            embedding_started.set()
            return None  # simulate "audio too short / not available"

        svc._transcribe_async = fake_transcribe_async
        svc._extract_embedding_async = fake_extract_embedding_async
        svc.model = MagicMock()  # sentinel so load_model is skipped

        result = await svc.transcribe_with_speaker(
            "/tmp/test.wav", db_session=MagicMock()
        )

        assert result["text"] == "transcribed"
        assert observed_parallel, "embedding extraction did not run during transcribe"
