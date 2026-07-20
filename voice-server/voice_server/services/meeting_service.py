"""Meeting diarization + ASR (§2).

Runs pyannote.audio speaker diarization + faster-whisper word-level ASR over a
meeting recording and aligns them into speaker-attributed segments, with a
per-speaker ECAPA embedding (same ONNX space as ``/api/voice/stt``).

**Chunked for long recordings.** A recording longer than ``meeting_chunk_seconds``
is diarized + transcribed in bounded time-windows so peak VRAM is ∝ the window,
not the whole recording — a multi-hour meeting fits a shared GPU, and CTranslate2
never retains a huge workspace that starves the next meeting or live STT. Each
window is diarized independently (chunk-local speaker labels), so the local
speakers are stitched into GLOBAL speakers by ECAPA cosine similarity
(``SpeakerRegistry``). A short recording is a single pass (pyannote labels used
directly, byte-identical to the pre-chunking behaviour).

The correctness cores are PURE and fixture-unit-tested with no GPU:
``align_words_to_segments`` (word→turn), ``SpeakerRegistry`` (cross-chunk speaker
stitching), ``_chunk_bounds``, ``_merge_adjacent_same_speaker``. The GPU glue
(pyannote load + diarize, faster-whisper words, per-window orchestration) lives in
``MeetingDiarizationService``.

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


def _l2norm(v: np.ndarray) -> np.ndarray:
    """Unit-normalize (so dot product == cosine); safe on a zero vector."""
    n = float(np.linalg.norm(v))
    return v / n if n > 0 else v


class SpeakerRegistry:
    """Online cross-chunk speaker stitching (PURE — no GPU/IO, fixture-tested).

    Chunked transcription diarizes each window independently, so the same person
    is a different chunk-local label per window. This maps each chunk-local
    speaker (via its ECAPA embedding) to a GLOBAL speaker: greedy nearest-centroid
    match if cosine ≥ ``threshold``, else a new global speaker. Centroids are a
    running mean over the matched embeddings (re-normalized), so a speaker's
    identity firms up across the meeting. A local speaker with no embedding
    (silent/too-short clip) always gets its own global label (never mis-merged)."""

    def __init__(self, threshold: float) -> None:
        self.threshold = threshold
        self._centroids: list[np.ndarray] = []
        self._counts: list[int] = []
        self._labels: list[str] = []

    def _new_label(self) -> str:
        label = f"SPEAKER_{len(self._labels):02d}"
        return label

    def assign(self, embedding: np.ndarray | None) -> str:
        if embedding is None:
            label = self._new_label()
            self._centroids.append(np.zeros(0, dtype=np.float32))
            self._counts.append(0)
            self._labels.append(label)
            return label
        v = _l2norm(np.asarray(embedding, dtype=np.float32))
        best_i, best_sim = -1, -1.0
        for i, c in enumerate(self._centroids):
            if c.size == 0:
                continue
            sim = float(np.dot(v, c))
            if sim > best_sim:
                best_sim, best_i = sim, i
        if best_i >= 0 and best_sim >= self.threshold:
            n = self._counts[best_i]
            self._centroids[best_i] = _l2norm((self._centroids[best_i] * n + v) / (n + 1))
            self._counts[best_i] = n + 1
            return self._labels[best_i]
        label = self._new_label()
        self._centroids.append(v)
        self._counts.append(1)
        self._labels.append(label)
        return label

    def centroid(self, label: str) -> list[float] | None:
        try:
            i = self._labels.index(label)
        except ValueError:
            return None
        c = self._centroids[i]
        return c.tolist() if c.size else None


def _chunk_bounds(total_samples: int, chunk_samples: int) -> list[tuple[int, int]]:
    """Non-overlapping [start, end) sample windows covering the whole recording
    (last window is short). PURE. ``chunk_samples <= 0`` → one window (no chunking)."""
    if chunk_samples <= 0 or total_samples <= chunk_samples:
        return [(0, total_samples)]
    return [(s, min(s + chunk_samples, total_samples))
            for s in range(0, total_samples, chunk_samples)]


def _merge_adjacent_same_speaker(segments: list[MeetingSegment]) -> list[MeetingSegment]:
    """Merge consecutive segments with the same (global) speaker — e.g. a turn
    split across a chunk boundary. PURE. Assumes segments are time-ordered."""
    merged: list[MeetingSegment] = []
    for s in segments:
        if merged and merged[-1].speaker == s.speaker:
            prev = merged[-1]
            prev.text = (prev.text + " " + s.text).strip()
            prev.end_s = s.end_s
        else:
            merged.append(s)
    return merged


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

    async def _process_window(
        self, pcm: np.ndarray, *, whisper_model, speaker_service
    ) -> tuple[list[MeetingSegment], dict[str, np.ndarray | None]]:
        """Diarize + ASR + align ONE audio window → window-local segments (labels
        + timestamps relative to the window) + a per-local-speaker ECAPA embedding.
        Peak VRAM is bounded by the window length. Caller offsets timestamps and
        stitches local→global speakers (chunked path) or uses labels as-is."""
        loop = asyncio.get_running_loop()
        turns = await loop.run_in_executor(None, self._diarize_sync, pcm)
        # Release pyannote's torch cache BEFORE whisper so CTranslate2's
        # (separate-pool) encode has room — the OOM guard, per window.
        self._free_cuda_cache()
        words = await loop.run_in_executor(None, self._transcribe_words_sync, pcm, whisper_model)
        self._free_cuda_cache()

        segments = align_words_to_segments(words, turns)
        embeddings: dict[str, np.ndarray | None] = {}
        for speaker in {s.speaker for s in segments}:
            clip = _concat_speaker_audio(pcm, [s for s in segments if s.speaker == speaker])
            if clip.size == 0:
                embeddings[speaker] = None
                continue
            try:
                embeddings[speaker] = await speaker_service.embed(clip)
            except Exception as e:  # noqa: BLE001 — embedding is best-effort
                logger.warning("cluster embed failed for %s: %s", speaker, e)
                embeddings[speaker] = None
        return segments, embeddings

    @staticmethod
    def _seg_dict(s: MeetingSegment, embedding) -> dict:
        emb = embedding.tolist() if isinstance(embedding, np.ndarray) else embedding
        return {
            "speaker": s.speaker,
            "start_s": round(s.start_s, 3),
            "end_s": round(s.end_s, 3),
            "text": s.text,
            "embedding": emb,
        }

    async def transcribe(self, pcm: np.ndarray, *, whisper_model, speaker_service) -> list[dict]:
        """Diarize + transcribe + align + per-speaker embed → segment dicts
        {speaker, start_s, end_s, text, embedding}.

        Recordings longer than ``meeting_chunk_seconds`` are processed in bounded
        windows (peak VRAM ∝ window, not the whole recording) and the chunk-local
        speakers are stitched into GLOBAL speakers by ECAPA cosine — so a
        multi-hour meeting fits a shared GPU and repeated meetings don't starve
        it. A short recording is a single pass (pyannote's labels used directly)."""
        if not self.ready or self._pipeline is None:
            raise RuntimeError("MeetingDiarizationService not ready")

        chunk_samples = max(0, int(settings.meeting_chunk_seconds)) * SAMPLE_RATE
        bounds = _chunk_bounds(int(pcm.size), chunk_samples)

        async with self._lock:  # one meeting job monopolises the GPU
            if len(bounds) == 1:
                segments, embeddings = await self._process_window(
                    pcm, whisper_model=whisper_model, speaker_service=speaker_service
                )
                return [self._seg_dict(s, embeddings.get(s.speaker)) for s in segments]

            # Chunked: process each window, stitch local speakers → global.
            registry = SpeakerRegistry(float(settings.meeting_speaker_match_threshold))
            all_segments: list[MeetingSegment] = []
            for idx, (a, b) in enumerate(bounds):
                offset_s = a / SAMPLE_RATE
                segments, embeddings = await self._process_window(
                    pcm[a:b], whisper_model=whisper_model, speaker_service=speaker_service
                )
                # Assign in a deterministic order so stitching is reproducible.
                local_to_global = {
                    loc: registry.assign(embeddings.get(loc)) for loc in sorted(embeddings)
                }
                for s in segments:
                    s.speaker = local_to_global.get(s.speaker, s.speaker)
                    s.start_s += offset_s
                    s.end_s += offset_s
                all_segments.extend(segments)
                logger.info(
                    "meeting chunk %d/%d done (%.0f–%.0fs, %d segments)",
                    idx + 1, len(bounds), offset_s, b / SAMPLE_RATE, len(segments),
                )

            all_segments = _merge_adjacent_same_speaker(all_segments)
            return [self._seg_dict(s, registry.centroid(s.speaker)) for s in all_segments]


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
