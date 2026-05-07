"""B.5 spike — Coqui XTTS-v2 inference, conforming to the TTSEngine Protocol.

This service is constructed only when `settings.xtts_enabled=True`, which
the spike Dockerfile sets. Production v0.1.5 doesn't ship `coqui-tts` and
keeps `xtts_enabled=False`; the import never fires.

License: XTTS-v2 weights are CPML 1.0.0 (non-commercial). The Dockerfile
sets `COQUI_TOS_AGREED=1` so non-interactive `tts.tts(...)` calls don't
hang on the y/n confirm. See `docs/B5_LICENSE_NOTE.md`.

Output contract: returns 22.05 kHz PCM-16 mono WAV bytes. XTTS native is
24 kHz; we resample with librosa (transitive dep of coqui-tts) so the
listening pass and benchmark see one rate across all engines.

Long-prompt handling: XTTS-v2 OOMs and drifts on inputs >~240 chars
(autoregressive decode). We pre-split on sentence boundaries (reusing
`tts_service._split_sentences`) and synth each chunk separately, then
concatenate raw PCM. The benchmark records one TTFB (first chunk) and
total synth time across all chunks.
"""

from __future__ import annotations

import asyncio
import io
import logging
import time
import wave
from pathlib import Path

import numpy as np

from voice_server.config import settings
from voice_server.services.tts_service import _split_sentences

logger = logging.getLogger(__name__)


class XTTSError(Exception):
    """Raised when XTTS load or synth fails."""


class XTTSService:
    """Lazy-loaded XTTS-v2 inference, exposing the uniform `synth_one` shape.

    Model load is deferred to first `synth_one()` call (~10-30 s for the
    initial CUDA setup + checkpoint load). The benchmark warmup loop in
    Step 5 ensures the cold-start cost doesn't contaminate measurements.
    """

    def __init__(self) -> None:
        self._tts = None  # lazily set on first synth_one
        self._lock = asyncio.Lock()
        # Per-call timing record exposed to the REST handler so the
        # benchmark can read first-chunk TTFB and total time without
        # having to reverse-engineer them from response timing.
        self.last_chunk_times_ms: list[float] = []

    async def _ensure_loaded(self) -> None:
        if self._tts is not None:
            return
        async with self._lock:
            if self._tts is not None:
                return
            self._tts = await asyncio.to_thread(self._load_blocking)

    def _load_blocking(self):
        """Synchronous model load — run via to_thread."""
        try:
            from TTS.api import TTS
        except ImportError as e:
            raise XTTSError(
                "coqui-tts is not installed; this service requires the spike image"
            ) from e

        started = time.monotonic()
        # The high-level TTS() facade resolves the HF model and downloads
        # if needed; with the predownload script run, this hits the cache.
        # `gpu=True` puts inference on CUDA.
        # NB: TTS() looks at COQUI_TOS_AGREED env var to bypass the y/n
        # confirm — set in the Dockerfile.
        tts = TTS(model_name="tts_models/multilingual/multi-dataset/xtts_v2", gpu=settings.xtts_use_cuda)
        elapsed = time.monotonic() - started
        logger.info("xtts loaded (gpu=%s) in %.2fs", settings.xtts_use_cuda, elapsed)
        return tts

    async def synth_one(
        self,
        text: str,
        voice_ref: Path | None = None,
        language: str = "de",
    ) -> bytes:
        """Synth full text → one 22.05 kHz PCM-16 mono WAV. See module docstring.

        `voice_ref` semantics:
          - None     → XTTS default speaker for the given language
          - Path     → speaker_wav cloning reference (5-30 s clip)
        """
        if not text.strip():
            return b""

        await self._ensure_loaded()
        assert self._tts is not None  # for type-checker; _ensure_loaded raises otherwise

        chunks = _split_sentences(text, max_chars=settings.xtts_max_chunk_chars)
        if not chunks:
            return b""

        # Per-chunk synth + timing. Each call returns float samples at 24 kHz
        # native; we resample after.
        self.last_chunk_times_ms = []
        all_samples_24khz: list[np.ndarray] = []

        for chunk in chunks:
            t0 = time.monotonic()
            samples = await asyncio.to_thread(self._synth_chunk_blocking, chunk, voice_ref, language)
            self.last_chunk_times_ms.append((time.monotonic() - t0) * 1000.0)
            all_samples_24khz.append(np.asarray(samples, dtype=np.float32))

        # Concatenate raw float samples at 24 kHz before resampling so the
        # resampler sees one continuous signal (avoids per-chunk edge
        # artifacts at chunk boundaries).
        concat = np.concatenate(all_samples_24khz)
        target_sr = settings.xtts_target_sample_rate

        # Resample 24000 → target_sr. librosa is a coqui-tts transitive,
        # always available when this code is reached.
        import librosa  # noqa: PLC0415 — defer until xtts is actually called
        resampled = librosa.resample(concat, orig_sr=24000, target_sr=target_sr)

        # Float → PCM-16 mono WAV. Clip to [-1, 1] before scaling to int16
        # so any rare overshoot from resampling doesn't wrap around.
        clipped = np.clip(resampled, -1.0, 1.0)
        pcm16 = (clipped * 32767.0).astype(np.int16)

        out = io.BytesIO()
        with wave.open(out, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)  # 16-bit
            w.setframerate(target_sr)
            w.writeframes(pcm16.tobytes())
        return out.getvalue()

    def _synth_chunk_blocking(
        self,
        chunk: str,
        voice_ref: Path | None,
        language: str,
    ) -> list[float]:
        """Single-chunk synth — run via to_thread. Returns 24 kHz float samples."""
        if voice_ref is None:
            # Default-speaker mode. XTTS-v2 ships built-in speakers; pick
            # one that matches the requested language. The model card lists
            # ~50 speakers; "Damien Black" is a serviceable German default
            # in the documented examples. Hardcoded here because for the
            # spike we measure ONE default-speaker number, not a sweep.
            samples = self._tts.tts(
                text=chunk,
                language=self._lang_code(language),
                speaker="Damien Black",
            )
        else:
            if not voice_ref.is_file():
                raise XTTSError(f"voice_ref not found: {voice_ref}")
            samples = self._tts.tts(
                text=chunk,
                language=self._lang_code(language),
                speaker_wav=str(voice_ref),
            )
        return samples

    @staticmethod
    def _lang_code(language: str) -> str:
        """XTTS expects ISO-639-1 ('de', 'en'); incoming may be 'de_DE' etc."""
        return (language or "de").split("_")[0].split("-")[0].lower()
