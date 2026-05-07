"""B.5 Step 3 — generate the Piper-thorsten reference clip for XTTS cloning.

Synthesises a 14-second canonical German passage via the production
Piper-thorsten voice and writes it to NFS as the reference XTTS will
clone against in `engine=xtts-clone` mode.

Why Piper-synthesised, not a real Thorsten Müller dataset clip: per
docs/B5_PLAN.md Step 3, the spike's brand-consistency question is "can
XTTS reproduce the exact voice the household has been hearing for
months?" A real dataset clip would answer "can XTTS clone the original
speaker," which is a different (less-relevant) question. We clone what
production currently sounds like, not what the underlying TTS dataset
sounds like.

Run from inside any voice-server pod that has the Piper voices mounted:

    kubectl exec -n renfield deploy/voice-server -- \\
        python /app/scripts/generate_thorsten_ref.py

Or pre-window from the production pod (before scaling down for the
spike), so the reference exists on NFS before the spike pod boots.

Output: /mnt/llm/voice/xtts_refs/thorsten_ref.wav (~14s, 22.05 kHz,
PCM-16 mono, ~600 KB).
"""

from __future__ import annotations

import io
import logging
import os
import sys
import wave
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("generate_thorsten_ref")

# Canonical reference text — natural German, varied sentence lengths,
# phonetically representative of typical assistant-style replies. ~40
# words; thorsten-medium speaks German at ~3 wps, so synth duration
# lands around 13-14s — comfortably inside the XTTS-v2 reference window
# (which accepts 5-30 s; longer than 30 s gets truncated internally).
CANONICAL_TEXT = (
    "Heute scheint die Sonne über dem Garten. "
    "Die Vögel singen, und der Wind bewegt die Blätter sanft. "
    "Es ist ein warmer Frühlingstag. "
    "Der Junge spielt im Schatten der alten Eiche. "
    "Seine Mutter ruft ihn zum Mittagessen ins Haus."
)

DEFAULT_OUTPUT = Path("/mnt/llm/voice/xtts_refs/thorsten_ref.wav")


def main() -> int:
    voices_dir = Path(os.environ.get("PIPER_VOICES_DIR", "/mnt/llm/voice/piper"))
    voice_name = os.environ.get("PIPER_DEFAULT_VOICE_DE", "de_DE-thorsten-medium")
    output = Path(os.environ.get("XTTS_REF_OUTPUT", str(DEFAULT_OUTPUT)))

    voice_path = voices_dir / f"{voice_name}.onnx"
    if not voice_path.is_file():
        log.error("piper voice not found: %s", voice_path)
        return 2

    log.info("voice: %s", voice_path)
    log.info("output: %s", output)
    log.info("text (%d chars): %s", len(CANONICAL_TEXT), CANONICAL_TEXT)

    # Ensure output directory exists (NFS share is writable from voice-server pods).
    output.parent.mkdir(parents=True, exist_ok=True)

    try:
        from piper.voice import PiperVoice
    except ImportError as e:
        log.error("piper-tts not installed: %s", e)
        return 3

    use_cuda = os.environ.get("PIPER_USE_CUDA", "true").lower() in ("1", "true", "yes")
    voice = PiperVoice.load(str(voice_path), use_cuda=use_cuda)
    log.info("piper voice loaded (cuda=%s)", use_cuda)

    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav_file:
        voice.synthesize_wav(CANONICAL_TEXT, wav_file)
    wav_bytes = buffer.getvalue()

    # Sanity-check the produced WAV before writing it.
    with wave.open(io.BytesIO(wav_bytes), "rb") as r:
        nch = r.getnchannels()
        sw = r.getsampwidth()
        sr = r.getframerate()
        nframes = r.getnframes()
        duration_s = nframes / sr if sr else 0.0
    log.info(
        "synth: %d bytes, %d ch, %d-bit, %d Hz, %.2f s",
        len(wav_bytes),
        nch,
        sw * 8,
        sr,
        duration_s,
    )

    if duration_s < 5.0 or duration_s > 30.0:
        log.error(
            "duration %.2f s outside XTTS-v2 reference window (5-30 s) — adjust CANONICAL_TEXT",
            duration_s,
        )
        return 4

    output.write_bytes(wav_bytes)
    log.info("written: %s (%d bytes)", output, len(wav_bytes))
    return 0


if __name__ == "__main__":
    sys.exit(main())
