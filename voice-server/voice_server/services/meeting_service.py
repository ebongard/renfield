"""Meeting diarization + ASR (§2).

Runs pyannote.audio speaker diarization + faster-whisper word-level ASR over a
whole meeting recording and aligns them into speaker-attributed segments, with a
per-cluster ECAPA embedding (same ONNX space as ``/api/voice/stt``).

The alignment is a PURE function (``align_words_to_segments``) — fixture-unit
tested with no GPU. The GPU glue (pyannote pipeline load + diarize, faster-whisper
word timestamps) lives in ``MeetingDiarizationService``.

Model loading: the pyannote pipeline is baked into the image (offline-first) and
loaded once at warmup when ``meeting_enabled``. The ASR model is the resident STT
model unless ``meeting_whisper_model`` is set (then loaded per job so a larger
model doesn't sit resident contending with live satellite STT).
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass, field

import numpy as np

from voice_server.config import settings

logger = logging.getLogger(__name__)

SAMPLE_RATE = 16000


@dataclass
class Word:
    text: str
    start_s: float
    end_s: float


@dataclass
class Turn:
    """One diarization turn: a speaker cluster label over a time span."""

    speaker: str
    start_s: float
    end_s: float


@dataclass
class MeetingSegment:
    speaker: str
    start_s: float
    end_s: float
    text: str
    embedding: list[float] | None = field(default=None)


def _overlap(a0: float, a1: float, b0: float, b1: float) -> float:
    """Overlap duration of [a0,a1] and [b0,b1] (0 if disjoint)."""
    return max(0.0, min(a1, b1) - max(a0, b0))


def _nearest_turn(mid: float, turns: list[Turn]) -> Turn:
    """The turn whose span is closest to a midpoint (for words that overlap no
    turn — e.g. in a diarization gap)."""
    def dist(t: Turn) -> float:
        if t.start_s <= mid <= t.end_s:
            return 0.0
        return min(abs(mid - t.start_s), abs(mid - t.end_s))

    return min(turns, key=dist)


def align_words_to_segments(words: list[Word], turns: list[Turn]) -> list[MeetingSegment]:
    """Assign each ASR word to the diarization turn it overlaps most (nearest
    turn on a gap), then merge consecutive same-speaker words into segments.

    PURE — no GPU, no IO. This is the correctness core of the pipeline and is
    fixture-unit-tested (overlaps, gaps, no-diarization, empty).
    """
    words = [w for w in words if (w.text or "").strip()]
    if not words:
        return []
    if not turns:
        # No diarization → a single unknown speaker over the whole transcript.
        text = " ".join(w.text.strip() for w in words)
        return [MeetingSegment("SPEAKER_00", words[0].start_s, words[-1].end_s, text)]

    labeled: list[tuple[str, Word]] = []
    for w in words:
        best: Turn | None = None
        best_ov = 0.0
        for t in turns:
            ov = _overlap(w.start_s, w.end_s, t.start_s, t.end_s)
            if ov > best_ov:
                best_ov = ov
                best = t
        if best is None:  # word falls in a diarization gap → nearest turn
            best = _nearest_turn((w.start_s + w.end_s) / 2.0, turns)
        labeled.append((best.speaker, w))

    segments: list[MeetingSegment] = []
    for speaker, w in labeled:
        if segments and segments[-1].speaker == speaker:
            segments[-1].text += " " + w.text.strip()
            segments[-1].end_s = w.end_s
        else:
            segments.append(MeetingSegment(speaker, w.start_s, w.end_s, w.text.strip()))
    return segments


class MeetingDiarizationService:
    """Loads pyannote at warmup (when meeting_enabled); runs the batch pipeline."""

    def __init__(self) -> None:
        self._pipeline = None
        self.ready: bool = False
        self._lock = asyncio.Lock()  # batch job monopolises the GPU (semaphore=1)

    async def warmup(self) -> None:
        if not settings.meeting_enabled:
            logger.info("meeting diarization disabled — skipping pyannote load")
            return
        logger.info("loading pyannote pipeline: %s", settings.meeting_diarization_model)
        loop = asyncio.get_running_loop()
        self._pipeline = await loop.run_in_executor(None, self._load_pipeline)
        self.ready = self._pipeline is not None
        logger.info("meeting diarization warm (ready=%s)", self.ready)

    def _load_pipeline(self):
        import torch
        from pyannote.audio import Pipeline

        # torch 2.6+ defaults torch.load to weights_only=True; lightning passes it
        # explicitly True and the pyannote checkpoint carries non-tensor globals.
        # Force False — the model is the trusted official/baked checkpoint.
        _orig_load = torch.load

        def _patched(*a, **k):
            k["weights_only"] = False
            return _orig_load(*a, **k)

        torch.load = _patched
        try:
            token = settings.hf_token.get_secret_value() if settings.hf_token else None
            try:
                pipeline = Pipeline.from_pretrained(settings.meeting_diarization_model, token=token)
            except TypeError:
                pipeline = Pipeline.from_pretrained(
                    settings.meeting_diarization_model, use_auth_token=token
                )
        finally:
            torch.load = _orig_load
        if pipeline is None:
            logger.error(
                "pyannote pipeline load returned None (gated model license not "
                "accepted / token missing / cache cold)"
            )
            return None
        if settings.whisper_device == "cuda":
            pipeline.to(torch.device("cuda"))
        return pipeline

    def _diarize_sync(self, pcm: np.ndarray) -> list[Turn]:
        import torch

        waveform = torch.from_numpy(np.ascontiguousarray(pcm, dtype=np.float32)).unsqueeze(0)
        annotation = self._pipeline({"waveform": waveform, "sample_rate": SAMPLE_RATE})
        turns: list[Turn] = []
        for segment, _track, speaker in annotation.itertracks(yield_label=True):
            turns.append(Turn(str(speaker), float(segment.start), float(segment.end)))
        turns.sort(key=lambda t: t.start_s)
        return turns

    def _transcribe_words_sync(self, pcm: np.ndarray, whisper_model) -> list[Word]:
        segments, _info = whisper_model.transcribe(
            pcm, word_timestamps=True, beam_size=1,
            language=settings.whisper_language_default,
        )
        words: list[Word] = []
        for seg in segments:
            for w in (seg.words or []):
                words.append(Word(text=w.word, start_s=float(w.start), end_s=float(w.end)))
        return words

    @staticmethod
    def _free_cuda_cache() -> None:
        """Return torch's caching-allocator memory to CUDA. pyannote diarization
        (torch) holds a large cache after running; faster-whisper (CTranslate2)
        allocates from a SEPARATE CUDA pool, so without this the whisper `encode`
        OOMs on a full GPU even though the diarization tensors are already dead —
        the failure mode on a ~32-min recording. Best-effort / CUDA-only."""
        try:
            import torch
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
        except Exception:  # noqa: BLE001 — never let cleanup break a transcription
            pass

    async def transcribe(self, pcm: np.ndarray, *, whisper_model, speaker_service) -> list[dict]:
        """Diarize + transcribe + align + per-cluster embed. Returns a list of
        segment dicts: {speaker(cluster id), start_s, end_s, text, embedding}."""
        if not self.ready or self._pipeline is None:
            raise RuntimeError("MeetingDiarizationService not ready")
        loop = asyncio.get_running_loop()
        async with self._lock:  # one batch job at a time (semaphore=1)
            turns = await loop.run_in_executor(None, self._diarize_sync, pcm)
            # Release pyannote's torch cache BEFORE whisper so CTranslate2's
            # (separate-pool) encode has room — this is the OOM fix.
            self._free_cuda_cache()
            words = await loop.run_in_executor(
                None, self._transcribe_words_sync, pcm, whisper_model
            )
        # Release the job's allocations so they don't accumulate across meetings
        # (and starve the other services sharing this GPU).
        self._free_cuda_cache()

        segments = align_words_to_segments(words, turns)

        # Per-cluster ECAPA embedding: concatenate each speaker's audio windows.
        embeddings: dict[str, list[float] | None] = {}
        for speaker in {s.speaker for s in segments}:
            clip = _concat_speaker_audio(pcm, [s for s in segments if s.speaker == speaker])
            if clip.size == 0:
                embeddings[speaker] = None
                continue
            try:
                emb = await speaker_service.embed(clip)
                embeddings[speaker] = emb.tolist()
            except Exception as e:  # noqa: BLE001 - embedding is best-effort
                logger.warning("cluster embed failed for %s: %s", speaker, e)
                embeddings[speaker] = None

        return [
            {
                "speaker": s.speaker,
                "start_s": round(s.start_s, 3),
                "end_s": round(s.end_s, 3),
                "text": s.text,
                "embedding": embeddings.get(s.speaker),
            }
            for s in segments
        ]


def _concat_speaker_audio(pcm: np.ndarray, segments: list[MeetingSegment]) -> np.ndarray:
    """Concatenate the PCM windows for one speaker's segments (for the embed)."""
    parts: list[np.ndarray] = []
    for s in segments:
        a = max(0, int(s.start_s * SAMPLE_RATE))
        b = min(pcm.size, int(s.end_s * SAMPLE_RATE))
        if b > a:
            parts.append(pcm[a:b])
    if not parts:
        return np.zeros(0, dtype=np.float32)
    return np.concatenate(parts).astype(np.float32)
