"""ffmpeg-driven codec→PCM conversion for streaming chunks.

Browsers ship MediaRecorder output as webm/opus (Chrome/Safari) or
ogg/opus (Firefox); satellites send raw WAV. ffmpeg eats all three.

The decoder is per-session — we spawn one ffmpeg subprocess at session
start with `-f` locked to the announced codec (RISK-2 fix: no probing
near-silence chunks), feed audio chunks into stdin, and read 16 kHz
mono float32 PCM from stdout.

Subprocess args are a fixed argv list (no shell) with codec validated
against an allowlist; no string interpolation reaches a shell.
"""

from __future__ import annotations

import asyncio
import logging
from typing import AsyncIterator

import numpy as np

logger = logging.getLogger(__name__)

CODEC_FFMPEG_FORMAT = {
    "audio/webm;codecs=opus": "webm",
    "audio/ogg;codecs=opus": "ogg",
    "audio/wav": "wav",
}


class AudioDecoder:
    """One ffmpeg subprocess per voice session. Push chunks, pull PCM."""

    def __init__(self, codec: str) -> None:
        if codec not in CODEC_FFMPEG_FORMAT:
            raise ValueError(f"unsupported codec: {codec}")
        self.codec = codec
        self._proc: asyncio.subprocess.Process | None = None
        self._reader_task: asyncio.Task | None = None
        self._pcm_buffer = bytearray()
        self._buffer_lock = asyncio.Lock()
        self._closed = False

    async def start(self) -> None:
        """Spawn ffmpeg with stdin/stdout pipes (argv list, no shell)."""
        ffmpeg_format = CODEC_FFMPEG_FORMAT[self.codec]
        argv = [
            "ffmpeg",
            "-loglevel", "error",
            "-f", ffmpeg_format,
            "-i", "pipe:0",
            "-ac", "1",
            "-ar", "16000",
            "-f", "f32le",
            "pipe:1",
        ]
        self._proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        self._reader_task = asyncio.create_task(self._drain_stdout())
        logger.debug("ffmpeg started: codec=%s", self.codec)

    async def _drain_stdout(self) -> None:
        if self._proc is None or self._proc.stdout is None:
            return
        try:
            while True:
                chunk = await self._proc.stdout.read(8192)
                if not chunk:
                    break
                async with self._buffer_lock:
                    self._pcm_buffer.extend(chunk)
        except Exception as e:
            logger.warning("ffmpeg drain error: %s", e)

    async def push(self, encoded_chunk: bytes) -> None:
        if self._closed or self._proc is None or self._proc.stdin is None:
            raise RuntimeError("decoder closed")
        try:
            self._proc.stdin.write(encoded_chunk)
            await self._proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError) as e:
            logger.error("ffmpeg pipe broken: %s", e)
            raise

    async def take_pcm(self) -> np.ndarray:
        """Atomically drain the PCM accumulator and return float32 samples."""
        async with self._buffer_lock:
            data = bytes(self._pcm_buffer)
            self._pcm_buffer.clear()
        if not data:
            return np.empty(0, dtype=np.float32)
        return np.frombuffer(data, dtype=np.float32).copy()

    async def flush(self) -> np.ndarray:
        """Close stdin, wait for ffmpeg to finalize, return remaining PCM."""
        if self._closed or self._proc is None:
            return np.empty(0, dtype=np.float32)
        self._closed = True
        if self._proc.stdin:
            try:
                self._proc.stdin.close()
            except Exception:
                pass
        try:
            await asyncio.wait_for(self._proc.wait(), timeout=5.0)
        except asyncio.TimeoutError:
            logger.warning("ffmpeg flush timeout, killing")
            self._proc.kill()
            await self._proc.wait()
        if self._reader_task is not None:
            try:
                await asyncio.wait_for(self._reader_task, timeout=2.0)
            except asyncio.TimeoutError:
                self._reader_task.cancel()
        return await self.take_pcm()

    async def close(self) -> None:
        if self._proc is None:
            return
        if not self._closed:
            await self.flush()
        self._proc = None
