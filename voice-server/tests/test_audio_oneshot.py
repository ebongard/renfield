"""One-shot decoder: MP4/m4a (moov-at-end) must decode.

Regression for the phone/Mac m4a bug. A normal (non-faststart) m4a stores its
`moov` index atom at the END of the file; ffmpeg must SEEK back to it to demux.
The old decoder fed the upload over a stdin pipe (`pipe:0`), which is not
seekable, so any m4a whose `moov` sits beyond ffmpeg's ~5 MB pipe-probe window
decoded to zero PCM (`offset 0x2c: partial file`). Decoding from a seekable
on-disk file fixes it.

The fixture is generated at test time (the voice-server image always ships
ffmpeg); the test skips where ffmpeg / the AAC encoder is unavailable. It is
sized past the pipe-probe window on purpose — a tiny m4a would decode over a
pipe too (ffmpeg buffers the whole small input) and would NOT catch the bug.
"""

from __future__ import annotations

import asyncio
import shutil
import struct
import subprocess

import numpy as np
import pytest

from voice_server.services.audio_oneshot import (
    OneshotDecodeError,
    decode_audio_to_pcm,
    decode_upload_to_pcm,
)

# Big enough that `moov` lands well past ffmpeg's default ~5 MB pipe probe window,
# so this genuinely exercises the seek path (a small m4a would not).
_MIN_TRIGGER_BYTES = 6 * 1024 * 1024


def _have_ffmpeg_aac() -> bool:
    if not shutil.which("ffmpeg"):
        return False
    try:
        enc = subprocess.run(
            ["ffmpeg", "-hide_banner", "-encoders"],
            capture_output=True, text=True, timeout=10,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    return " aac " in enc


def _moov_after_mdat(data: bytes) -> bool:
    pos, moov, mdat = 0, None, None
    while pos + 8 <= len(data):
        size = struct.unpack(">I", data[pos:pos + 4])[0]
        typ = data[pos + 4:pos + 8]
        if typ == b"moov":
            moov = pos
        elif typ == b"mdat":
            mdat = pos
        if size == 0:
            break
        pos += size
    return moov is not None and mdat is not None and moov > mdat


def _make_m4a_moov_at_end(tmp_path) -> bytes:
    """A >6 MB single-channel 16 kHz AAC/m4a with moov at the end (not faststart).

    White noise, not a tone: AAC compresses a pure sine to a fraction of the
    nominal bitrate, so a tone can't reliably clear the ~5 MB pipe-probe window.
    Incompressible noise actually spends the bits; 16 kHz mono AAC still caps
    the effective rate well under the requested 128 k (~9 KB/s), so 1200 s lands
    ≈ 11 MB — `moov` ends up well past where a stdin pipe could reach it.
    """
    out = tmp_path / "sample.m4a"
    subprocess.run(
        [
            "ffmpeg", "-y", "-loglevel", "error",
            "-f", "lavfi", "-i", "anoisesrc=color=white:duration=1200:sample_rate=16000",
            "-ac", "1", "-c:a", "aac", "-b:a", "128k",
            str(out),
        ],
        check=True, timeout=90,
    )
    return out.read_bytes()


class _FakeUpload:
    """Minimal Starlette-UploadFile stand-in: async chunked read."""

    def __init__(self, data: bytes) -> None:
        self._data = data
        self._pos = 0

    async def read(self, size: int = -1) -> bytes:
        if size is None or size < 0:
            chunk, self._pos = self._data[self._pos:], len(self._data)
            return chunk
        chunk = self._data[self._pos:self._pos + size]
        self._pos += len(chunk)
        return chunk


ffmpeg_aac = pytest.mark.skipif(not _have_ffmpeg_aac(), reason="ffmpeg+aac unavailable")


@ffmpeg_aac
async def test_decode_audio_to_pcm_handles_moov_at_end_m4a(tmp_path):
    data = _make_m4a_moov_at_end(tmp_path)
    assert len(data) >= _MIN_TRIGGER_BYTES, "fixture too small to exercise the seek path"
    assert _moov_after_mdat(data), "fixture must have moov AFTER mdat to reproduce the bug"

    pcm = await decode_audio_to_pcm(data)
    assert pcm.dtype == np.float32
    assert pcm.size > 16000  # >1 s of 16 kHz PCM — a piped decode would give 0


@ffmpeg_aac
async def test_decode_upload_to_pcm_streams_and_decodes_m4a(tmp_path):
    data = _make_m4a_moov_at_end(tmp_path)
    pcm = await decode_upload_to_pcm(_FakeUpload(data))
    assert pcm.dtype == np.float32
    assert pcm.size > 16000


async def test_empty_upload_raises():
    with pytest.raises(OneshotDecodeError):
        await decode_upload_to_pcm(_FakeUpload(b""))


async def test_empty_bytes_raise():
    with pytest.raises(OneshotDecodeError):
        await decode_audio_to_pcm(b"")


@ffmpeg_aac
async def test_garbage_is_terminal_not_hang(tmp_path):
    # Non-audio bytes → a clean OneshotDecodeError (4xx), never a hang.
    with pytest.raises(OneshotDecodeError):
        await decode_audio_to_pcm(b"\x00\x01\x02not audio at all" * 100, timeout_s=15.0)
