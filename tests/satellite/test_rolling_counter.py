"""Tests for the trailing-window metrics counters (renfield_satellite.metrics).

Before this, ``session_count_1h`` / ``error_count_1h`` were plain ints that
were incremented and never reset — "since boot" reported under a name that
promised "last hour", which made the value incomparable between satellites
with different uptimes.
"""

import pytest

from renfield_satellite.metrics import METRICS_WINDOW_SECONDS, RollingCounter


@pytest.mark.satellite
@pytest.mark.unit
class TestRollingCounter:
    def test_starts_empty(self):
        assert RollingCounter().count(now=1000.0) == 0

    def test_counts_events_inside_the_window(self):
        c = RollingCounter()
        for t in (1000.0, 1100.0, 1200.0):
            c.record(now=t)
        assert c.count(now=1200.0) == 3

    def test_events_older_than_the_window_are_dropped(self):
        c = RollingCounter()
        c.record(now=1000.0)
        c.record(now=1010.0)
        # One second past the window boundary of the first event.
        assert c.count(now=1000.0 + METRICS_WINDOW_SECONDS + 1) == 1

    def test_decays_to_zero_when_quiet(self):
        """A satellite that stops erroring must report 0 again, not a total."""
        c = RollingCounter()
        for t in (1000.0, 1001.0, 1002.0):
            c.record(now=t)
        assert c.count(now=1002.0) == 3
        assert c.count(now=1002.0 + METRICS_WINDOW_SECONDS + 1) == 0

    def test_boundary_event_exactly_at_cutoff_is_dropped(self):
        c = RollingCounter()
        c.record(now=1000.0)
        assert c.count(now=1000.0 + METRICS_WINDOW_SECONDS) == 0

    def test_recording_prunes_so_memory_stays_bounded(self):
        """A long-running satellite must not accumulate timestamps forever."""
        c = RollingCounter(window_seconds=10)
        for t in range(0, 1000):
            c.record(now=float(t))
        # Only the last 10 seconds may remain.
        assert c.count(now=999.0) <= 11

    def test_custom_window(self):
        c = RollingCounter(window_seconds=60)
        c.record(now=100.0)
        assert c.count(now=159.0) == 1
        assert c.count(now=161.0) == 0

    def test_record_and_count_default_to_wall_clock(self):
        """Called without an explicit ``now`` the counter uses time.time()."""
        c = RollingCounter()
        c.record()
        assert c.count() == 1


@pytest.mark.satellite
@pytest.mark.unit
def test_window_is_one_hour():
    assert METRICS_WINDOW_SECONDS == 3600
