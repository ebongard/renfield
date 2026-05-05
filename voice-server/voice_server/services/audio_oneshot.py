"""One-shot audio decoder for REST file uploads.

Distinct from AudioDecoder (streaming path) — REST gives us a complete
file, so we let ffmpeg auto-detect the format instead of pinning `-f`
to a codec hint. Browsers' MediaRecorder webm output sometimes fails
the strict `-f webm` demuxer when the explicit codec hint doesn't
match the Matroska track-codec inside; auto-detect handles webm/opus,
ogg/opus, wav, mp3, flac, m4a transparently.

Stderr captured (not DEVNULL) so failures surface in logs instead of
the silent "0 PCM bytes" mystery the streaming path produces.
"""

from __future__ import annotations

import asyncio
import logging

import numpy as np

logger = logging.getLogger(__name__)


class OneshotDecodeError(Exception):
    pass


async def decode_audio_to_pcm(audio_bytes: bytes, *, timeout_s: float = 30.0) -> np.ndarray:
    """Decode any common audio container to mono 16 kHz float32 PCM.

    Fixed argv (no shell). ffmpeg auto-detects the input format from
    the byte stream — works for the full set browsers / satellites
    actually send (webm/opus, ogg/opus, wav, mp3, flac, m4a).
    """
    if not audio_bytes:
        raise OneshotDecodeError("empty input")

    argv = [
        "ffmpeg",
        "-loglevel", "error",
        "-i", "pipe:0",
        "-ac", "1",
        "-ar", "16000",
        "-f", "f32le",
        "pipe:1",
    ]
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(audio_bytes), timeout=timeout_s
        )
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise OneshotDecodeError(f"ffmpeg timeout after {timeout_s}s") from None

    if proc.returncode != 0:
        msg = stderr.decode("utf-8", errors="replace").strip()[:400]
        raise OneshotDecodeError(f"ffmpeg exit {proc.returncode}: {msg}")

    if not stdout:
        msg = stderr.decode("utf-8", errors="replace").strip()[:400]
        raise OneshotDecodeError(f"ffmpeg produced no PCM (stderr: {msg})")

    return np.frombuffer(stdout, dtype=np.float32).copy()
