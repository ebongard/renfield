"""Piper TTS with sentence-streaming + binary frame headers.

Sentence boundary detection runs ahead of synthesis: as soon as the first
sentence is delimited, it goes through Piper and emits one binary frame.
This is what makes "first word audible within 1-2 s regardless of total
answer length" work.

Each binary frame carries a self-describing 24-byte header (RFWA magic +
UUID request_id + uint32 sequence) per protocol GAP-3 fix. The frontend
routes by reading the header — no JSON-meta-frame ordering trap.
"""

from __future__ import annotations

import asyncio
import io
import logging
import re
import struct
import uuid
import wave
from typing import AsyncIterator

from voice_server.config import settings

logger = logging.getLogger(__name__)

MAGIC = b"RFWA"
HEADER_LEN = 4 + 16 + 4  # magic + uuid + sequence

# Split on sentence-terminator (.!?) followed by whitespace, but stay
# liberal about what comes after — German output may continue with a
# numeral, a quote ("„"), or a lowercase word after an abbreviation.
# We use a positive lookbehind only; the next sentence boundary is
# whatever follows the whitespace.
_SENTENCE_RE = re.compile(r"(?<=[.!?])\s+")
_COMMA_FALLBACK_RE = re.compile(r"(?<=,)\s+")


def _split_sentences(text: str, max_chars: int = 240) -> list[str]:
    """Split on .!? boundaries, fallback to comma if a "sentence" exceeds max_chars.

    Pure-text routine; trivially testable, no Piper dependency.
    """
    text = text.strip()
    if not text:
        return []

    parts = [p.strip() for p in _SENTENCE_RE.split(text) if p.strip()]

    out: list[str] = []
    for p in parts:
        if len(p) <= max_chars:
            out.append(p)
            continue
        sub = [s.strip() for s in _COMMA_FALLBACK_RE.split(p) if s.strip()]
        out.extend(sub or [p])
    return out


def encode_binary_frame(request_id: uuid.UUID, sequence: int, wav_bytes: bytes) -> bytes:
    """Prepend the 24-byte protocol header to a WAV payload."""
    return MAGIC + request_id.bytes + struct.pack(">I", sequence) + wav_bytes


class TTSService:
    def __init__(self) -> None:
        self._voice_cache: dict[str, object] = {}
        self._lock = asyncio.Lock()

    def _voice_path(self, voice_name: str):
        return settings.piper_voices_dir / f"{voice_name}.onnx"

    def _load_voice(self, voice_name: str):
        cached = self._voice_cache.get(voice_name)
        if cached is not None:
            return cached
        path = self._voice_path(voice_name)
        if not path.exists():
            raise FileNotFoundError(f"Piper voice not found: {path}")
        from piper.voice import PiperVoice

        # piper-tts CUDA EP: the OnnxRuntime providers list is set inside
        # PiperVoice.load via env vars; nothing to plumb here.
        voice = PiperVoice.load(str(path), use_cuda=settings.piper_use_cuda)
        self._voice_cache[voice_name] = voice
        logger.info("piper voice loaded: %s (cuda=%s)", voice_name, settings.piper_use_cuda)
        return voice

    def _voice_for_language(self, language: str | None) -> str:
        lang = (language or "de").lower()
        if lang.startswith("de"):
            return settings.piper_default_voice_de
        return settings.piper_default_voice_en

    def _synth_one_sentence(self, voice, sentence: str) -> bytes:
        """Run Piper synthesis to a complete WAV byte buffer (sync — call via to_thread)."""
        buffer = io.BytesIO()
        with wave.open(buffer, "wb") as wav_file:
            voice.synthesize_wav(sentence, wav_file)
        return buffer.getvalue()

    async def stream_sentences(
        self,
        text: str,
        request_id: uuid.UUID,
        language: str | None = None,
    ) -> AsyncIterator[bytes]:
        """Yield binary frames (WAV with header), one per sentence, in order.

        The lock guards `_voice_cache` mutation in `_load_voice` only. We
        deliberately do NOT hold it during synthesis: the iterator may be
        cancelled or hit a slow consumer, which under `async with` would
        leak the lock until generator GC. Synthesis itself runs in a
        thread (`asyncio.to_thread`) so concurrent TTS requests proceed
        in parallel as long as Piper voice objects are reentrant — which
        they are for the same loaded voice instance.
        """
        sentences = _split_sentences(text)
        if not sentences:
            return

        voice_name = self._voice_for_language(language)

        async with self._lock:
            try:
                voice = self._load_voice(voice_name)
            except FileNotFoundError as e:
                logger.error("piper voice missing: %s", e)
                raise

        for seq, sentence in enumerate(sentences):
            wav_bytes = await asyncio.to_thread(self._synth_one_sentence, voice, sentence)
            yield encode_binary_frame(request_id, seq, wav_bytes)
