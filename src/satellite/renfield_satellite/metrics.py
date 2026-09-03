"""Satellite-side metrics helpers.

Dependency-free by design: the heartbeat counters must be unit-testable
without importing the hardware stack (pyaudio, gpiozero, yaml, …) that
``satellite.py`` pulls in.
"""

import time
from collections import deque
from typing import Optional

# Rolling window for the "_1h" metrics reported in the heartbeat.
METRICS_WINDOW_SECONDS = 3600


class RollingCounter:
    """Count events inside a trailing time window.

    The heartbeat advertises ``session_count_1h`` and ``error_count_1h``. Both
    used to be plain ints that were incremented and NEVER reset, so the value
    was "since boot" while the name promised "last hour". That made the number
    incomparable between satellites: a box up for 50 minutes showing 70 looked
    ten times worse than one up for six hours showing 7, when in fact the rates
    differ by a factor of sixty in the other direction.

    Timestamps are stored rather than a bucketed count because the window has
    to slide continuously — the backend reads the heartbeat at an arbitrary
    phase, and a fixed hourly reset would let a satellite look healthy purely
    because its bucket had just rolled over. Memory is bounded by the event
    rate, which for wake-word sessions and server errors sits orders of
    magnitude below any concern.
    """

    __slots__ = ("_events", "_window")

    def __init__(self, window_seconds: int = METRICS_WINDOW_SECONDS) -> None:
        self._events: deque = deque()
        self._window = window_seconds

    def record(self, now: Optional[float] = None) -> None:
        """Record one event at ``now`` (defaults to the current time)."""
        ts = time.time() if now is None else now
        self._events.append(ts)
        self._prune(ts)

    def count(self, now: Optional[float] = None) -> int:
        """Events inside the trailing window, pruning expired ones first."""
        ts = time.time() if now is None else now
        self._prune(ts)
        return len(self._events)

    def _prune(self, now: float) -> None:
        cutoff = now - self._window
        while self._events and self._events[0] <= cutoff:
            self._events.popleft()
