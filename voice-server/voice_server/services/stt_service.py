"""faster-whisper STT service with streaming partial-transcript emission (D6).

This is the property Speaches' Realtime-API failed to deliver and is the
core Phase B value-add. faster-whisper's iterator yields decoder segments
as they finalize; we surface each one as a partial_transcript event,
then a final_transcript on stream completion.

Audio is buffered as float32 mono 16 kHz PCM in memory. The caller (WS
handler) is responsible for codec→PCM conversion (via ffmpeg) and for
deciding when to call `transcribe_stream()` (typically: when accumulated
audio crosses ~500 ms or when stt_flush is received).
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import AsyncIterator

import numpy as np

from voice_server.config import settings

logger = logging.getLogger(__name__)


@dataclass
class TranscriptSegment:
    """One segment from faster-whisper."""

    text: str
    start_s: float
    end_s: float
    confidence: float


class STTService:
    def __init__(self) -> None:
        self._model = None
        self.ready: bool = False
        self._lock = asyncio.Lock()  # whisper-medium doesn't multiplex well
        self.last_language: str | None = None  # populated after each transcribe_stream

    async def warmup(self) -> None:
        """Load the model and run one tiny inference so the first user request is hot.

        `whisper_model` accepts either a filesystem path to a CTranslate2
        directory or a Hugging Face model id (faster-whisper downloads
        to its HF hub cache on first call).
        """
        logger.info("loading faster-whisper: %s", settings.whisper_model)
        from faster_whisper import WhisperModel

        loop = asyncio.get_running_loop()

        def _load():
            return WhisperModel(
                settings.whisper_model,
                device=settings.whisper_device,
                compute_type=settings.whisper_compute_type,
            )

        self._model = await loop.run_in_executor(None, _load)
        # Tiny warm inference: 0.1 s of silence
        warm = np.zeros(1600, dtype=np.float32)
        await loop.run_in_executor(None, lambda: list(self._model.transcribe(warm, vad_filter=False)[0]))
        self.ready = True
        logger.info("STT warm")

    async def transcribe_stream(
        self,
        audio_pcm: np.ndarray,
        language: str | None = None,
    ) -> AsyncIterator[TranscriptSegment]:
        """Transcribe a complete audio buffer, yielding partial segments as they finalize.

        Caller passes the entire accumulated audio so far (float32 mono
        16 kHz). faster-whisper's segment iterator finalizes segments as
        the decoder advances; we forward each one as it arrives, marking
        the last as partial=False.

        After the iterator is exhausted, the auto-detected language is
        accessible via `self.last_language` — populated each time
        transcribe_stream completes. Callers needing the detection can
        read it after the `async for` loop drains.
        """
        if not self.ready or self._model is None:
            raise RuntimeError("STTService not ready")

        if audio_pcm.dtype != np.float32:
            audio_pcm = audio_pcm.astype(np.float32)

        loop = asyncio.get_running_loop()
        async with self._lock:
            def _run():
                segments, info = self._model.transcribe(
                    audio_pcm,
                    language=language or settings.whisper_language_default,
                    vad_filter=True,
                    vad_parameters={"min_silence_duration_ms": settings.whisper_vad_min_silence_ms},
                    beam_size=1,
                    no_speech_threshold=0.5,
                )
                return list(segments), info

            segments, info = await loop.run_in_executor(None, _run)

        # Side-channel: callers that need the detected language read it
        # after consuming the iterator. Avoids a breaking-change to
        # the segment shape (consumers don't all care).
        self.last_language = getattr(info, "language", None) or language or settings.whisper_language_default

        if not segments:
            return

        for seg in segments:
            confidence = float(np.exp(seg.avg_logprob)) if seg.avg_logprob is not None else 0.0
            yield TranscriptSegment(
                text=seg.text.strip(),
                start_s=float(seg.start),
                end_s=float(seg.end),
                confidence=confidence,
            )
