"""
Capture liveness counter in the heartbeat (#1211).

The backend first judged a deaf satellite by a FROZEN level. The Fitnessraum
(2026-11-09, USB microphone unplugged) defeated that: it kept reporting a level
that cycled between two exact values (1499.6 / 525.1) on every heartbeat, at
~40 % CPU -- the capture handed out its last buffers again and again. The
satellite therefore counts only chunks that differ byte for byte from every one
of the recent chunks, and reports that count as ``capture_fresh_chunks``.
"""

import struct
from collections import deque

import numpy as np
import pytest
from renfield_satellite.satellite import CHUNK_REPLAY_WINDOW, Satellite

CHUNK_SAMPLES = 1280


def _make_satellite() -> Satellite:
    """A satellite without hardware: only what the level/liveness code touches."""
    sat = object.__new__(Satellite)
    sat._current_audio_rms = 0.0
    sat._current_audio_db = -96.0
    sat._current_is_speech = False
    sat._fresh_chunk_count = 0
    sat._recent_chunk_crcs = deque(maxlen=CHUNK_REPLAY_WINDOW)
    return sat


def _noise(seed: int, amplitude: int = 800) -> bytes:
    rng = np.random.default_rng(seed)
    return rng.integers(-amplitude, amplitude, CHUNK_SAMPLES, dtype=np.int16).tobytes()


def _constant(value: int) -> bytes:
    return struct.pack(f"<{CHUNK_SAMPLES}h", *([value] * CHUNK_SAMPLES))


@pytest.mark.satellite
class TestFreshChunkCounter:
    def test_live_audio_counts_every_chunk(self):
        sat = _make_satellite()
        for seed in range(50):
            sat._count_fresh_chunk(_noise(seed))
        assert sat._fresh_chunk_count == 50

    def test_two_replayed_buffers_freeze_the_counter_but_not_the_level(self):
        """The Fitnessraum case: A, B, A, B ... moves the level, never the counter."""
        sat = _make_satellite()
        a, b = _noise(1, 2000), _noise(2, 700)
        levels = set()
        for i in range(200):
            chunk = a if i % 2 == 0 else b
            _feed_levels(sat, chunk)
            levels.add(sat._current_audio_rms)
        assert len(levels) == 2, "the level cycles -- what fooled the old watch"
        assert sat._fresh_chunk_count == 2, "only the first sight of each buffer counts"

    def test_a_ring_as_large_as_the_window_is_still_caught(self):
        sat = _make_satellite()
        ring = [_noise(seed) for seed in range(CHUNK_REPLAY_WINDOW)]
        for _ in range(10):
            for chunk in ring:
                sat._count_fresh_chunk(chunk)
        assert sat._fresh_chunk_count == CHUNK_REPLAY_WINDOW

    def test_digital_silence_counts_on_every_chunk(self):
        """A live stream in a quiet room repeats an all-zero chunk; that is not a stall."""
        sat = _make_satellite()
        for _ in range(30):
            sat._count_fresh_chunk(_constant(0))
        assert sat._fresh_chunk_count == 30

    def test_an_empty_chunk_does_not_count(self):
        sat = _make_satellite()
        sat._count_fresh_chunk(b"")
        assert sat._fresh_chunk_count == 0

    def test_the_heartbeat_carries_the_counter(self):
        sat = _make_satellite()
        sat._last_wakeword = None
        sat.enviro = None
        sat._session_counter = _Zero()
        sat._error_counter = _Zero()
        sat._count_fresh_chunk(_noise(7))
        assert sat._get_metrics()["capture_fresh_chunks"] == 1


class _Zero:
    def count(self) -> int:
        return 0


def _feed_levels(sat: Satellite, chunk: bytes) -> None:
    """Run the level + liveness part of ``_on_audio_chunk`` without the state machine.

    ``_on_audio_chunk`` goes on into wake-word processing, which needs the
    detector; the level block at its top is what the heartbeat reports.
    """
    sat._state = None  # matches no branch below the level block
    sat.wakeword = type("W", (), {"active_stop_words": []})()
    sat._wakeword_pending = False
    sat._on_audio_chunk(chunk)
