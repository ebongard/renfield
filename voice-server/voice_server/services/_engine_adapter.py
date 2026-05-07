"""B.5 spike — uniform TTS-engine adapter for the benchmark.

Defines the `TTSEngine` Protocol the benchmark uses across all three
spike-time engines (`piper`, `xtts-default`, `xtts-clone`). Production
code keeps using `TTSService.stream_sentences()` directly — the
adapter exists only so `voice-server/scripts/b5_benchmark.py` has a
single contract to drive against, not so production gains an
abstraction layer.

If the spike concludes with a Piper-stays decision, this module is
deleted along with `xtts_service.py`. If XTTS wins, the swap-in PR
folds whichever survives into a clean production-shape service and
deletes the spike module.

Returns 22.05 kHz PCM-16 mono WAV bytes (header + audio) — the rate
Piper produces natively and the rate the listening pass uses for both
engines (XTTS native 24 kHz is resampled in `XTTSService`).
"""

from __future__ import annotations

import asyncio
import io
import wave
from pathlib import Path
from typing import Protocol, runtime_checkable

from voice_server.services.tts_service import TTSService, _split_sentences


@runtime_checkable
class TTSEngine(Protocol):
    """One-shot synth surface used by the B.5 benchmark only."""

    async def synth_one(
        self,
        text: str,
        voice_ref: Path | None = None,
        language: str = "de",
    ) -> bytes:
        """Synthesise the full input text and return one 22.05 kHz mono WAV.

        Implementations chunk long inputs internally (XTTS drifts past
        ~240 chars; Piper splits per sentence anyway). Returns a single
        complete WAV with the header + concatenated PCM.
        """
        ...


class PiperEngine:
    """Adapter wrapping `TTSService` to expose the uniform `synth_one` shape.

    Reuses the production sentence-split + per-sentence Piper synthesis
    from `TTSService._synth_one_sentence`, then concatenates the WAV
    payloads into one buffer. Sample rate is whatever Piper's voice
    natively produces (22.05 kHz for `de_DE-thorsten-medium`).

    `voice_ref` is unused for Piper — kept in the signature so the
    adapter contract is uniform across engines.
    """

    def __init__(self, tts: TTSService) -> None:
        self._tts = tts

    async def synth_one(
        self,
        text: str,
        voice_ref: Path | None = None,
        language: str = "de",
    ) -> bytes:
        sentences = _split_sentences(text)
        if not sentences:
            return b""

        voice_name = self._tts._voice_for_language(language)

        # Match TTSService.stream_sentences locking: load the voice under
        # the lock, run synthesis itself off the lock and on a thread.
        async with self._tts._lock:
            voice = self._tts._load_voice(voice_name)

        wavs: list[bytes] = []
        for sentence in sentences:
            wav = await asyncio.to_thread(self._tts._synth_one_sentence, voice, sentence)
            wavs.append(wav)

        return _concat_wavs(wavs)


def _concat_wavs(wav_chunks: list[bytes]) -> bytes:
    """Concatenate WAV byte payloads sharing the same format into one WAV.

    Duplicates the helper in `api/rest_voice.py` deliberately — the spike
    adapter shouldn't reach into the API module. Both copies will be
    consolidated whenever the spike-or-stay decision is made.
    """
    if not wav_chunks:
        return b""
    if len(wav_chunks) == 1:
        return wav_chunks[0]

    first = wave.open(io.BytesIO(wav_chunks[0]), "rb")
    nchannels = first.getnchannels()
    sampwidth = first.getsampwidth()
    framerate = first.getframerate()
    frames = [first.readframes(first.getnframes())]
    first.close()

    for chunk in wav_chunks[1:]:
        w = wave.open(io.BytesIO(chunk), "rb")
        if (w.getnchannels(), w.getsampwidth(), w.getframerate()) != (nchannels, sampwidth, framerate):
            raise RuntimeError(
                f"WAV format mismatch across chunks: "
                f"first=({nchannels},{sampwidth},{framerate}) "
                f"vs ({w.getnchannels()},{w.getsampwidth()},{w.getframerate()})"
            )
        frames.append(w.readframes(w.getnframes()))
        w.close()

    out = io.BytesIO()
    with wave.open(out, "wb") as w:
        w.setnchannels(nchannels)
        w.setsampwidth(sampwidth)
        w.setframerate(framerate)
        w.writeframes(b"".join(frames))
    return out.getvalue()
