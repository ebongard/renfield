"""Connected but deaf (#1211): a frozen capture signal as the verdict.

On 2026-11-06 the Wohnzimmer XVF3800 delivered zero frames for two days (ALSA
stream RUNNING, `hw_ptr` 0) while heartbeat, state and error count looked
healthy. Without frames the satellite keeps sending its last (or initial
0.0 / -96.0) level; a live mic's noise floor never repeats at 0.1 resolution
for minutes. These tests pin the verdict in `SatelliteManager` and the
change-only report in the fleet watchdog.
"""
from __future__ import annotations

import time
from unittest.mock import MagicMock

import pytest
from httpx import AsyncClient

from ha_glue.services.satellite_manager import (
    SatelliteCapabilities,
    SatelliteInfo,
    SatelliteManager,
    UpdateStatus,
    get_satellite_manager,
)
from services import satellite_fleet_watchdog as wd
from utils.config import settings

pytestmark = [pytest.mark.unit]

WINDOW = 600


@pytest.fixture
def watch_on(monkeypatch):
    monkeypatch.setattr(settings, "satellite_deaf_watch_enabled", True)
    monkeypatch.setattr(settings, "satellite_deaf_after_seconds", WINDOW)


@pytest.fixture
def watch_off(monkeypatch):
    monkeypatch.setattr(settings, "satellite_deaf_watch_enabled", False)


@pytest.fixture
def settled(monkeypatch):
    monkeypatch.setattr(wd, "_STARTED_AT", time.monotonic() - wd.SETTLE_SECONDS - 1)


def _manager(*sat_ids: str, room: str = "Wohnzimmer") -> SatelliteManager:
    m = SatelliteManager()
    for sid in sat_ids:
        m.satellites[sid] = SatelliteInfo(
            satellite_id=sid, room=room, websocket=MagicMock(),
            capabilities=SatelliteCapabilities(),
        )
    return m


def _beat(m: SatelliteManager, sid: str, rms: float, db: float, at: float) -> None:
    """A heartbeat at a chosen time: the tracking reads the wall clock, so a
    beat that CHANGED the level gets its timestamp moved to `at`."""
    sat = m.satellites[sid]
    before = sat.capture_signal
    m.update_heartbeat(sid, {"audio_rms": rms, "audio_db": db})
    if sat.capture_signal != before:
        sat.capture_signal_changed_at = at
        if before is None:
            sat.capture_signal_first_at = at


class TestTheVerdict:
    def test_flag_off_tracks_nothing_and_never_judges(self, watch_off):
        m = _manager("sat-a")
        m.update_heartbeat("sat-a", {"audio_rms": 0.0, "audio_db": -96.0})
        assert m.satellites["sat-a"].capture_signal is None
        assert m.capture_stalled_since("sat-a", time.time() + 10 * WINDOW) is None

    def test_flag_off_roster_entry_has_no_new_key(self, watch_off):
        # Off is byte-identical: the kiosk snapshot and the API read this dict.
        assert "capture_stalled_since" not in _manager("sat-a").get_all_satellites()[0]

    def test_a_level_frozen_since_the_start_is_deaf(self, watch_on):
        # The Wohnzimmer case: never a frame, so the initial 0.0 / -96.0 stays.
        m, t0 = _manager("sat-a"), 1_000.0
        _beat(m, "sat-a", 0.0, -96.0, t0)
        for i in range(1, 25):
            _beat(m, "sat-a", 0.0, -96.0, t0 + 30 * i)
        assert m.capture_stalled_since("sat-a", t0 + WINDOW) == t0

    def test_a_level_frozen_at_a_nonzero_value_is_deaf_too(self, watch_on):
        # Capture that stalls AFTER working freezes on its last real value; a
        # check for "== -96 dB" would miss it.
        m, t0 = _manager("sat-a"), 1_000.0
        _beat(m, "sat-a", 86.1, -51.6, t0)
        assert m.capture_stalled_since("sat-a", t0 + WINDOW + 1) == t0

    def test_a_live_mic_is_never_deaf(self, watch_on):
        m, t0 = _manager("sat-a"), 1_000.0
        _beat(m, "sat-a", 85.0, -51.7, t0)
        m.satellites["sat-a"].capture_signal_changed_at = t0
        m.update_heartbeat("sat-a", {"audio_rms": 85.4, "audio_db": -51.7})
        changed = m.satellites["sat-a"].capture_signal_changed_at
        assert changed > t0
        assert m.capture_stalled_since("sat-a", changed + WINDOW - 1) is None

    def test_not_before_the_window(self, watch_on):
        m, t0 = _manager("sat-a"), 1_000.0
        _beat(m, "sat-a", 0.0, -96.0, t0)
        assert m.capture_stalled_since("sat-a", t0 + WINDOW - 1) is None

    def test_digital_silence_is_not_a_frozen_stream(self, watch_on):
        # A noise-suppressed mic in a silent room delivers frames below one LSB:
        # constant (0.0, -90.3). Only the INITIAL value before any frame is
        # -96.0, so silence must not read as deaf.
        m, t0 = _manager("sat-a"), time.time() - 2 * WINDOW
        _beat(m, "sat-a", 0.0, -90.3, t0)
        for _ in range(3):
            m.update_heartbeat("sat-a", {"audio_rms": 0.0, "audio_db": -90.3})
        assert m.capture_stalled_since("sat-a") is None
        assert m.capture_proven_live("sat-a") is True

    def test_a_satellite_without_level_metrics_is_not_judged(self, watch_on):
        m = _manager("sat-a")
        m.update_heartbeat("sat-a", {"cpu_percent": 12.0})
        m.update_heartbeat("sat-a")
        assert m.capture_stalled_since("sat-a", time.time() + 10 * WINDOW) is None

    def test_a_running_ota_is_not_judged(self, watch_on):
        # The installer blocks the satellite's event loop; no verdict then.
        m, t0 = _manager("sat-a"), 1_000.0
        _beat(m, "sat-a", 0.0, -96.0, t0)
        m.satellites["sat-a"].update_status = UpdateStatus.IN_PROGRESS
        assert m.capture_stalled_since("sat-a", t0 + 10 * WINDOW) is None

    def test_an_unknown_satellite_is_none(self, watch_on):
        assert _manager().capture_stalled_since("gone", time.time()) is None

    def test_live_needs_a_change_after_the_first_level(self, watch_on):
        # One heartbeat proves nothing: right after a (re)connect there is no
        # verdict either way.
        m, t0 = _manager("sat-a"), time.time() - 60
        _beat(m, "sat-a", 0.0, -96.0, t0)
        assert m.capture_proven_live("sat-a") is False
        _beat(m, "sat-a", 85.4, -51.7, t0 + 30)
        assert m.capture_proven_live("sat-a") is True

    def test_a_frozen_level_is_not_live(self, watch_on):
        m, t0 = _manager("sat-a"), 1_000.0
        _beat(m, "sat-a", 85.4, -51.7, t0)
        _beat(m, "sat-a", 0.0, -96.0, t0 + 30)
        assert m.capture_proven_live("sat-a", t0 + 30 + WINDOW) is False

    def test_roster_entry_carries_the_verdict_when_on(self, watch_on):
        m, t0 = _manager("sat-a"), time.time() - 2 * WINDOW
        _beat(m, "sat-a", 0.0, -96.0, t0)
        assert m.get_all_satellites()[0]["capture_stalled_since"] == t0


def _counted_beat(m: SatelliteManager, sid: str, fresh: int, rms: float, at: float) -> None:
    """A heartbeat of a satellite >= 1.4.20, which reports its fresh-chunk count."""
    sat = m.satellites[sid]
    before = sat.capture_signal
    m.update_heartbeat(sid, {"audio_rms": rms, "audio_db": -40.0, "capture_fresh_chunks": fresh})
    if sat.capture_signal != before:
        sat.capture_signal_changed_at = at
        if before is None:
            sat.capture_signal_first_at = at


class TestTheFreshChunkCounter:
    """The Hobbyraum case (2026-11-09): USB mic unplugged, the level cycled
    between 1499.6 and 525.1 on every heartbeat and the level watch saw a live
    microphone. The satellite's counter of non-repeating chunks froze."""

    def test_a_cycling_level_with_a_frozen_counter_is_deaf(self, watch_on):
        m, t0 = _manager("sat-a"), 1_000.0
        for i in range(25):
            _counted_beat(m, "sat-a", 4711, 1499.6 if i % 2 else 525.1, t0 + 30 * i)
        assert m.capture_stalled_since("sat-a", t0 + WINDOW) == t0
        assert m.capture_proven_live("sat-a", t0 + WINDOW) is False

    def test_an_advancing_counter_is_live_even_with_a_constant_level(self, watch_on):
        # The counter decides; the level path's digital-silence rule is not needed.
        m, t0 = _manager("sat-a"), 1_000.0
        for i in range(25):
            _counted_beat(m, "sat-a", 375 * i, 0.0, t0 + 30 * i)
        last = t0 + 30 * 24
        assert m.capture_stalled_since("sat-a", last + WINDOW - 1) is None
        assert m.capture_proven_live("sat-a", last + 1) is True

    def test_a_counter_of_zero_from_the_start_is_deaf(self, watch_on):
        # A satellite that never opened its microphone.
        m, t0 = _manager("sat-a"), 1_000.0
        _counted_beat(m, "sat-a", 0, 0.0, t0)
        _counted_beat(m, "sat-a", 0, 0.0, t0 + 30)
        assert m.capture_stalled_since("sat-a", t0 + WINDOW) == t0

    def test_a_wrong_typed_counter_falls_back_to_the_level(self, watch_on):
        m = _manager("sat-a")
        m.update_heartbeat("sat-a", {"audio_rms": 3.0, "audio_db": -80.0, "capture_fresh_chunks": "12"})
        assert m.satellites["sat-a"].capture_signal == ("level", 3.0, -80.0)


class _Notify:
    def __init__(self):
        self.calls: list[dict] = []

    async def __call__(self, **kw):
        self.calls.append(kw)
        return True


class _State:
    def __init__(self, initial: str = ""):
        self.value = initial

    async def read(self):
        return self.value

    async def write(self, v):
        self.value = v


def _deaf_manager(t0: float) -> SatelliteManager:
    m = _manager("sat-wohnzimmer")
    _beat(m, "sat-wohnzimmer", 0.0, -96.0, t0)
    m.satellites["sat-kueche"] = SatelliteInfo(
        satellite_id="sat-kueche", room="Küche", websocket=MagicMock(),
        capabilities=SatelliteCapabilities(),
    )
    m.update_heartbeat("sat-kueche", {"audio_rms": 120.3, "audio_db": -48.7})
    return m


async def _check(m, notify, state, now, enrolled=(("sat-wohnzimmer", "Wohnzimmer"),)):
    async def fetch():
        return [(sid, room, None) for sid, room in enrolled]

    return await wd.check_deaf_satellites(
        notify=notify, manager=m, fetch_enrolled=fetch,
        read_state=state.read, write_state=state.write, now=now,
    )


class TestTheReport:
    async def test_flag_off_is_silent(self, watch_off, settled):
        n = _Notify()
        assert await _check(_deaf_manager(1_000.0), n, _State(), 1_000.0 + 10 * WINDOW) is None
        assert n.calls == []

    async def test_no_verdict_in_the_settle_period(self, watch_on, monkeypatch):
        monkeypatch.setattr(wd, "_STARTED_AT", time.monotonic())
        n = _Notify()
        assert await _check(_deaf_manager(1_000.0), n, _State(), 1_000.0 + 10 * WINDOW) is None
        assert n.calls == []

    async def test_a_deaf_satellite_is_reported_once(self, watch_on, settled):
        t0 = time.time() - 3 * 3600
        m, n, st = _deaf_manager(t0), _Notify(), _State()
        out = await _check(m, n, st, t0 + 3 * 3600)
        assert out == "taub: Wohnzimmer (seit 3 h)"
        assert len(n.calls) == 1
        assert "Wohnzimmer" in n.calls[0]["message"] and "Küche" not in n.calls[0]["message"]
        assert n.calls[0]["event_type"] == "satellite_health"
        assert st.value == "sat-wohnzimmer"

        # The next run with the same set: logged, not notified again.
        out = await _check(m, n, st, t0 + 3 * 3600 + 60)
        assert out.endswith("(unveraendert)")
        assert len(n.calls) == 1

    async def test_recovery_is_reported_and_clears_the_marker(self, watch_on, settled):
        t0 = time.time() - 3600
        m, n, st = _deaf_manager(t0), _Notify(), _State("sat-wohnzimmer")
        m.update_heartbeat("sat-wohnzimmer", {"audio_rms": 95.2, "audio_db": -50.7})
        assert await _check(m, n, st, time.time()) is None
        assert [c["title"] for c in n.calls] == ["Satelliten hören wieder"]
        assert st.value == ""

    async def test_a_reconnect_does_not_read_as_recovery(self, watch_on, settled):
        # 🛑 The flap: after a reconnect the window starts over, so the deaf
        # satellite is not "stalled" yet -- that must not report "hören wieder"
        # and then "hört nichts" an hour later.
        m, n, st = _manager("sat-wohnzimmer"), _Notify(), _State("sat-wohnzimmer")
        m.update_heartbeat("sat-wohnzimmer", {"audio_rms": 0.0, "audio_db": -96.0})
        out = await _check(m, n, st, time.time())
        assert out == "taub: Wohnzimmer (Urteil ausstehend) (unveraendert)"
        assert n.calls == [] and st.value == "sat-wohnzimmer"

    async def test_a_reported_satellite_that_disconnected_stays_reported(self, watch_on, settled):
        n, st = _Notify(), _State("sat-wohnzimmer")
        out = await _check(_manager(), n, st, time.time())
        assert out == "taub: Wohnzimmer (Urteil ausstehend) (unveraendert)"
        assert n.calls == [] and st.value == "sat-wohnzimmer"

    async def test_a_revoked_reported_satellite_leaves_the_marker(self, watch_on, settled):
        # Never connected again and no longer enrolled: it can never be proven
        # live, so without this it would sit in the marker forever.
        n, st = _Notify(), _State("sat-wohnzimmer")
        assert await _check(_manager(), n, st, time.time(), enrolled=()) is None
        assert st.value == ""

    async def test_an_ota_does_not_read_as_recovery(self, watch_on, settled):
        # The mic died after working, so the level DID change after the first
        # observation; an OTA must still not count as "hears again".
        t0 = time.time() - 3600
        m, n, st = _manager("sat-wohnzimmer"), _Notify(), _State("sat-wohnzimmer")
        _beat(m, "sat-wohnzimmer", 85.4, -51.7, t0)
        _beat(m, "sat-wohnzimmer", 86.1, -51.6, t0 + 30)
        m.satellites["sat-wohnzimmer"].update_status = UpdateStatus.IN_PROGRESS
        out = await _check(m, n, st, time.time())
        assert out.endswith("(unveraendert)") and n.calls == []

    async def test_all_hearing_without_a_prior_report_stays_silent(self, watch_on, settled):
        m, n = _manager("sat-a"), _Notify()
        m.update_heartbeat("sat-a", {"audio_rms": 95.2, "audio_db": -50.7})
        assert await _check(m, n, _State(), time.time()) is None
        assert n.calls == []

    async def test_minutes_for_a_short_stall(self, watch_on, settled):
        t0 = time.time() - 900
        out = await _check(_deaf_manager(t0), _Notify(), _State(), t0 + 900)
        assert out == "taub: Wohnzimmer (seit 15 min)"


class TestTheTaskHandler:
    async def test_both_verdicts_share_one_run_line(self, monkeypatch):
        from services.scheduled_tasks import builtins

        async def fleet():
            return "offline: Esszimmer (seit 2 h)"

        async def deaf():
            return "taub: Wohnzimmer (seit 3 h)"

        monkeypatch.setattr(settings, "feature_satellites", True)
        monkeypatch.setattr(wd, "check_satellite_fleet", fleet)
        monkeypatch.setattr(wd, "check_deaf_satellites", deaf)
        out = await builtins._satellite_fleet_watchdog_handler(None, {})
        assert out == "offline: Esszimmer (seit 2 h); taub: Wohnzimmer (seit 3 h)"

    async def test_nothing_to_say_stays_none(self, monkeypatch):
        from services.scheduled_tasks import builtins

        async def quiet():
            return None

        monkeypatch.setattr(settings, "feature_satellites", True)
        monkeypatch.setattr(wd, "check_satellite_fleet", quiet)
        monkeypatch.setattr(wd, "check_deaf_satellites", quiet)
        assert await builtins._satellite_fleet_watchdog_handler(None, {}) is None


class TestTheApi:
    @pytest.mark.integration
    async def test_stalled_seconds_in_the_list(self, async_client: AsyncClient, watch_on):
        m = get_satellite_manager()
        m.satellites.clear()
        try:
            m.satellites["sat-deaf"] = SatelliteInfo(
                satellite_id="sat-deaf", room="Wohnzimmer", websocket=MagicMock(),
                capabilities=SatelliteCapabilities(),
            )
            m.update_heartbeat("sat-deaf", {"audio_rms": 0.0, "audio_db": -96.0})
            m.satellites["sat-deaf"].capture_signal_changed_at = time.time() - 2 * WINDOW
            r = await async_client.get("/api/satellites")
            assert r.status_code == 200
            stalled = r.json()["satellites"][0]["capture_stalled_seconds"]
            assert 2 * WINDOW - 5 < stalled < 2 * WINDOW + 60
        finally:
            m.satellites.clear()

    @pytest.mark.integration
    async def test_off_reports_null(self, async_client: AsyncClient, watch_off):
        # A level frozen long enough to be judged -- only the flag keeps it null.
        m = get_satellite_manager()
        m.satellites.clear()
        try:
            m.satellites["sat-a"] = SatelliteInfo(
                satellite_id="sat-a", room="Küche", websocket=MagicMock(),
                capabilities=SatelliteCapabilities(),
            )
            m.satellites["sat-a"].capture_signal = (0.0, -96.0)
            m.satellites["sat-a"].capture_signal_first_at = time.time() - 2 * WINDOW
            m.satellites["sat-a"].capture_signal_changed_at = time.time() - 2 * WINDOW
            r = await async_client.get("/api/satellites")
            assert r.json()["satellites"][0]["capture_stalled_seconds"] is None
        finally:
            m.satellites.clear()
