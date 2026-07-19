"""One-shot audio decoder for REST file uploads.

Distinct from AudioDecoder (streaming path) — REST gives us a complete
file, so we let ffmpeg auto-detect the format instead of pinning `-f`
to a codec hint. Browsers' MediaRecorder webm output sometimes fails
the strict `-f webm` demuxer when the explicit codec hint doesn't
match the Matroska track-codec inside; auto-detect handles webm/opus,
ogg/opus, wav, mp3, flac, m4a transparently.

**ffmpeg decodes from a real file on disk, NOT a stdin pipe.** MP4/m4a
put the `moov` index atom at the END of the file (only faststart-remuxed
files carry it up front), and ffmpeg must SEEK back to it to demux. A pipe
(`pipe:0`) is not seekable, so a normal phone/Mac m4a decodes to zero PCM
with `offset 0x2c: partial file / Invalid data`. Reading from a seekable
file fixes it (and every other container is unaffected — WAV/webm/opus/mp3
decode from a file just as well as from a pipe).

The upload is streamed to that file in fixed-size chunks — never the whole
recording in RAM — so a multi-hour meeting stays bounded on the media layer
too (mirrors the backend's chunked upload).

Stderr captured (not DEVNULL) so failures surface in logs instead of
the silent "0 PCM bytes" mystery the streaming path produces.
"""

from __future__ import annotations

import asyncio
import logging
import os
import tempfile
from typing import Protocol

import numpy as np

logger = logging.getLogger(__name__)

# Streamed to the on-disk temp file in 1 MiB chunks (never the whole file in RAM).
_SPOOL_CHUNK = 1 << 20

# Where the seekable temp file lands. tempfile default honours $TMPDIR; a
# dedicated env lets the deployment steer it onto a disk-backed volume (a
# multi-hour recording must NOT land on a tmpfs /tmp, which would be RAM again).
_SPOOL_DIR = os.environ.get("VOICE_ONESHOT_SPOOL_DIR") or None


class OneshotDecodeError(Exception):
    pass


class _UploadLike(Protocol):
    """The slice of Starlette's UploadFile we depend on (async chunked read)."""

    async def read(self, size: int = -1) -> bytes: ...


async def _spool_upload_to_tempfile(upload: _UploadLike) -> tuple[str, int]:
    """Stream an upload to a fresh seekable temp file in chunks; return (path, bytes).

    Never buffers the whole upload in RAM. Caller owns the file and MUST unlink it.
    """
    fd, path = tempfile.mkstemp(suffix=".oneshot", dir=_SPOOL_DIR)
    total = 0
    try:
        # Blocking file writes are fine here: the media host is I/O-light and the
        # chunk loop yields to the event loop on every awaited upload.read().
        with os.fdopen(fd, "wb") as fh:
            while True:
                chunk = await upload.read(_SPOOL_CHUNK)
                if not chunk:
                    break
                total += len(chunk)
                fh.write(chunk)
    except BaseException:
        _safe_unlink(path)
        raise
    return path, total


def _safe_unlink(path: str) -> None:
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
    except OSError as e:  # noqa: BLE001 — cleanup best-effort
        logger.warning("oneshot temp cleanup failed for %s: %s", path, e)


async def _ffmpeg_file_to_pcm(path: str, *, timeout_s: float) -> np.ndarray:
    """Decode a seekable on-disk audio file to mono 16 kHz float32 PCM.

    Fixed argv (no shell). ffmpeg auto-detects the container from the file and
    seeks freely (so MP4/m4a moov-at-end works). PCM is pulled over stdout.
    """
    argv = [
        "ffmpeg",
        "-loglevel", "error",
        "-i", path,          # seekable file — NOT pipe:0
        "-ac", "1",
        "-ar", "16000",
        "-f", "f32le",
        "pipe:1",
    ]
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
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


async def decode_upload_to_pcm(upload: _UploadLike, *, timeout_s: float = 30.0) -> np.ndarray:
    """Decode a complete-file upload to mono 16 kHz float32 PCM.

    Streams the upload to a seekable temp file (bounded RAM) and decodes from
    there so MP4/m4a (moov-at-end, needs seek) works. Preferred entry for REST
    endpoints — they hold an UploadFile and should never `.read()` a whole
    multi-hour recording into memory.
    """
    path, total = await _spool_upload_to_tempfile(upload)
    try:
        if total == 0:
            raise OneshotDecodeError("empty input")
        return await _ffmpeg_file_to_pcm(path, timeout_s=timeout_s)
    finally:
        _safe_unlink(path)


async def decode_audio_to_pcm(audio_bytes: bytes, *, timeout_s: float = 30.0) -> np.ndarray:
    """Decode an in-memory audio blob to mono 16 kHz float32 PCM.

    Kept for callers that already hold the bytes (raw-opus sibling, tests). The
    bytes are written to a seekable temp file first — same reason as
    ``decode_upload_to_pcm``: MP4/m4a need a seekable input, a pipe does not
    work. Prefer ``decode_upload_to_pcm`` when you have an UploadFile so the
    recording never sits in RAM.
    """
    if not audio_bytes:
        raise OneshotDecodeError("empty input")
    fd, path = tempfile.mkstemp(suffix=".oneshot", dir=_SPOOL_DIR)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(audio_bytes)
        return await _ffmpeg_file_to_pcm(path, timeout_s=timeout_s)
    finally:
        _safe_unlink(path)
