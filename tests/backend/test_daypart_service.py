"""
Unit tests for services/daypart_service.py — day/night awareness.

Pure (no DB, no async). Deterministic times are injected via the ``_now``
seam; settings windows are patched via ``patch.object`` on the module's
``settings`` instance.
"""

from datetime import datetime
from unittest.mock import patch

import pytest

import services.daypart_service as ds
from services.daypart_service import (
    build_time_context,
    get_current_daypart,
    get_daypart_info,
    is_night,
)

pytestmark = pytest.mark.unit


def _at(hour: int, minute: int = 0, *, year=2026, month=6, day=11) -> datetime:
    """A naive (treated-as-local) datetime at the given wall-clock time.

    2026-06-11 is a Thursday — used by the build_time_context weekday tests.
    """
    return datetime(year, month, day, hour, minute)


# ---------------------------------------------------------------------------
# Night window — midnight wrap (the default 22:00 → 07:00)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "hour,minute,expected",
    [
        (23, 0, "night"),    # late evening, after night_start
        (0, 1, "night"),     # just after midnight
        (3, 0, "night"),     # deep night
        (6, 59, "night"),    # last minute before night_end
        (7, 0, "day"),       # exactly night_end → no longer night
        (12, 0, "day"),      # midday
        (17, 59, "day"),     # last minute before evening
        (18, 0, "evening"),  # exactly evening_start
        (20, 0, "evening"),  # mid-evening
        (21, 59, "evening"), # last minute before night_start
        (22, 0, "night"),    # exactly night_start → night
    ],
)
def test_night_wrap_default_windows(hour, minute, expected):
    with patch.object(ds.settings, "daypart_night_start", "22:00"), \
         patch.object(ds.settings, "daypart_night_end", "07:00"), \
         patch.object(ds.settings, "daypart_evening_start", "18:00"), \
         patch.object(ds.settings, "daypart_timezone", "UTC"):
        assert get_current_daypart(_at(hour, minute)) == expected


# ---------------------------------------------------------------------------
# Night window — no wrap (night_start < night_end)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "hour,minute,expected",
    [
        # Degenerate config: an early-morning night (02:00-04:00) with evening
        # starting at 18:00. Evening then wraps midnight (evening_start >
        # night_start), i.e. 18:00 → 02:00 is evening, 02:00-04:00 night,
        # 04:00-18:00 day. (The realistic wrap config is covered above.)
        (1, 0, "evening"),   # 01:00 is still in the wrapped evening (< night_start)
        (2, 0, "night"),     # exactly night_start
        (3, 0, "night"),     # inside no-wrap night
        (3, 59, "night"),    # last minute before night_end
        (4, 0, "day"),       # exactly night_end → day
        (12, 0, "day"),      # midday — outside both evening and night
        (18, 0, "evening"),  # exactly evening_start
        (23, 0, "evening"),  # after evening_start, before the wrapped night_start
    ],
)
def test_night_no_wrap(hour, minute, expected):
    with patch.object(ds.settings, "daypart_night_start", "02:00"), \
         patch.object(ds.settings, "daypart_night_end", "04:00"), \
         patch.object(ds.settings, "daypart_evening_start", "18:00"), \
         patch.object(ds.settings, "daypart_timezone", "UTC"):
        assert get_current_daypart(_at(hour, minute)) == expected


# ---------------------------------------------------------------------------
# is_night delegates to get_current_daypart
# ---------------------------------------------------------------------------

def test_is_night_delegates():
    with patch.object(ds.settings, "daypart_night_start", "22:00"), \
         patch.object(ds.settings, "daypart_night_end", "07:00"), \
         patch.object(ds.settings, "daypart_evening_start", "18:00"), \
         patch.object(ds.settings, "daypart_timezone", "UTC"):
        assert is_night(_at(23, 0)) is True
        assert is_night(_at(6, 59)) is True
        assert is_night(_at(12, 0)) is False
        assert is_night(_at(20, 0)) is False  # evening, not night


# ---------------------------------------------------------------------------
# get_daypart_info shape
# ---------------------------------------------------------------------------

def test_get_daypart_info():
    with patch.object(ds.settings, "daypart_night_start", "22:00"), \
         patch.object(ds.settings, "daypart_night_end", "07:00"), \
         patch.object(ds.settings, "daypart_evening_start", "18:00"), \
         patch.object(ds.settings, "daypart_timezone", "UTC"):
        info = get_daypart_info(_at(22, 14))
        assert info["daypart"] == "night"
        assert info["local_time"] == "22:14"
        assert info["local_date"] == "2026-06-11"


# ---------------------------------------------------------------------------
# build_time_context — non-empty + carries the daypart label (de + en)
# ---------------------------------------------------------------------------

def test_build_time_context_de_contains_label():
    # 2026-06-11 22:14 is a Thursday night.
    with patch.object(ds.settings, "daypart_night_start", "22:00"), \
         patch.object(ds.settings, "daypart_night_end", "07:00"), \
         patch.object(ds.settings, "daypart_evening_start", "18:00"), \
         patch.object(ds.settings, "daypart_timezone", "UTC"), \
         patch.object(ds, "_now_local", return_value=_at(22, 14)):
        out = build_time_context(lang="de")
        assert out  # non-empty
        assert "ZEITKONTEXT" in out
        assert "Nacht" in out
        assert "Donnerstag" in out
        assert "22:14" in out


def test_build_time_context_en_contains_label():
    with patch.object(ds.settings, "daypart_night_start", "22:00"), \
         patch.object(ds.settings, "daypart_night_end", "07:00"), \
         patch.object(ds.settings, "daypart_evening_start", "18:00"), \
         patch.object(ds.settings, "daypart_timezone", "UTC"), \
         patch.object(ds, "_now_local", return_value=_at(22, 14)):
        out = build_time_context(lang="en")
        assert out
        assert "TIME CONTEXT" in out
        assert "Night" in out
        assert "Thursday" in out
        assert "22:14" in out


def test_build_time_context_day_label_de():
    with patch.object(ds.settings, "daypart_night_start", "22:00"), \
         patch.object(ds.settings, "daypart_night_end", "07:00"), \
         patch.object(ds.settings, "daypart_evening_start", "18:00"), \
         patch.object(ds.settings, "daypart_timezone", "UTC"), \
         patch.object(ds, "_now_local", return_value=_at(12, 0)):
        out = build_time_context(lang="de")
        assert "Tag" in out
        assert "12:00" in out


# ---------------------------------------------------------------------------
# build_time_context never raises — returns "" on error
# ---------------------------------------------------------------------------

def test_build_time_context_empty_on_error():
    # datetime.now (via _now_local) raising must degrade to "".
    with patch.object(ds, "_now_local", side_effect=RuntimeError("boom")):
        assert build_time_context(lang="de") == ""
        assert build_time_context(lang="en") == ""


# ---------------------------------------------------------------------------
# TZ fallback chain — invalid TZ degrades to UTC without crashing
# ---------------------------------------------------------------------------

def test_invalid_tz_falls_back_to_utc():
    with patch.object(ds.settings, "daypart_timezone", "Not/AReal_Zone"):
        tz = ds._resolve_tz()
        assert str(tz) == "UTC"


def test_empty_tz_falls_back_without_crashing():
    # Empty daypart_timezone → ha_glue presence_analytics_timezone (or UTC).
    # Either way we must get a usable ZoneInfo and no exception.
    with patch.object(ds.settings, "daypart_timezone", ""):
        tz = ds._resolve_tz()
        assert tz is not None
        # daypart computation still works under the resolved tz.
        with patch.object(ds.settings, "daypart_night_start", "22:00"), \
             patch.object(ds.settings, "daypart_night_end", "07:00"), \
             patch.object(ds.settings, "daypart_evening_start", "18:00"):
            assert get_current_daypart(_at(23, 0)) == "night"


# ---------------------------------------------------------------------------
# Malformed HH:MM config falls back to safe defaults (no crash)
# ---------------------------------------------------------------------------

def test_malformed_hhmm_uses_default():
    with patch.object(ds.settings, "daypart_night_start", "not-a-time"), \
         patch.object(ds.settings, "daypart_night_end", "07:00"), \
         patch.object(ds.settings, "daypart_evening_start", "18:00"), \
         patch.object(ds.settings, "daypart_timezone", "UTC"):
        # Default night_start (22:00) is used → 23:00 is still night.
        assert get_current_daypart(_at(23, 0)) == "night"
