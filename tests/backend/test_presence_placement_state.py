"""Placement v2, stage 2 — the presence STATE follows v2.

  * one rule for several sources of a person: the source that changed zone
    LAST wins (``combine_person``) — a second device lying in another room no
    longer flips the person on every report (#1551; the legacy path is the
    negative control and still flips);
  * contested sources and "home, placed by nothing" are UNCERTAIN, so the
    privacy gates treat the person as a possible listener; the last room stays
    booked for the display, without a leave event;
  * expiry is a deadline with a timer, not a sweep inside a report;
    ``unavailable`` (no receiver alive) changes nothing;
  * a voice/auth sighting is a claim with its own time: a phone lying
    elsewhere cannot pull the person back once the hold ends;
  * the voice-check counter records what each decision believed;
  * flag off: the state module is not even imported.
"""
from __future__ import annotations

import asyncio
import copy
import json
import os
import subprocess
import sys
from itertools import pairwise
from pathlib import Path
from types import SimpleNamespace

import pytest
from loguru import logger

import ha_glue.services.presence_placement_shadow as shadow_module
from ha_glue.services import presence_service as presence_service_module
from ha_glue.services.presence_placement import (
    Claim,
    PersonKind,
    VerdictKind,
    combine_person,
)
from ha_glue.services.presence_placement_state import PlacementState
from ha_glue.services.presence_service import PresenceService
from ha_glue.utils.config import HaGlueSettings, ha_glue_settings

pytestmark = pytest.mark.unit

PHONE = "AA:BB:CC:DD:EE:01"
WATCH = "AA:BB:CC:DD:EE:03"   # a second device of user 1
KITCHEN, BATH, LIVING = 10, 20, 30
T0 = 1_800_000_000.0
SATS = {
    "sat-kitchen": (KITCHEN, "Kitchen"),
    "sat-bath": (BATH, "Bath"),
    "sat-living": (LIVING, "Living"),
}
RECEIVERS_JSON = json.dumps({
    sat: {"offset_db": 0, "in_room_dbm": -60, "cutoff_dbm": -80} for sat in SATS
})


@pytest.fixture
def clock(monkeypatch):
    state = SimpleNamespace(now=T0)
    monkeypatch.setattr(presence_service_module, "time", SimpleNamespace(time=lambda: state.now))
    return state


@pytest.fixture
def log_lines():
    lines: list[str] = []
    sink = logger.add(lambda m: lines.append(str(m).rstrip()), level="DEBUG",
                      format="{level}|{message}")
    yield lines
    logger.remove(sink)


def build_service(monkeypatch, clock, *, state: bool = True, shadow: bool = False,
                  receivers: str = RECEIVERS_JSON):
    monkeypatch.setattr(ha_glue_settings, "presence_placement_v2_state_enabled", state)
    monkeypatch.setattr(ha_glue_settings, "presence_placement_v2_shadow_enabled", shadow)
    monkeypatch.setattr(ha_glue_settings, "presence_placement_v2_receivers", receivers)
    monkeypatch.setattr(ha_glue_settings, "presence_placement_v2_zones", "")
    svc = PresenceService()
    svc._mac_to_user = {PHONE: 1, WATCH: 1}
    svc._user_names = {1: "alice"}
    svc._room_names = {room: name for room, name in SATS.values()}
    roster = SimpleNamespace(alive=True)
    if svc._placement_shadow is not None:
        svc._placement_shadow._roster = lambda: (
            {sat: (room, clock.now) for sat, (room, _n) in SATS.items()} if roster.alive else {}
        )
    fired: list[tuple[str, dict]] = []

    async def collect_events():
        fired.extend(copy.deepcopy(svc._pending_events))
        svc._pending_events.clear()

    monkeypatch.setattr(svc, "_fire_pending_events", collect_events)
    return svc, fired, roster


async def report(svc, clock, satellite: str, rssi: float, mac: str = PHONE, step: float = 2.0):
    clock.now += step
    room_id, room_name = SATS[satellite]
    await svc.process_ble_report(
        satellite_id=satellite, room_id=room_id,
        devices=[{"mac": mac, "rssi": rssi}], room_name=room_name,
    )


def room(svc) -> int | None:
    p = svc.get_user_presence(1)
    return p.room_id if p else None


def enters(fired) -> list[int]:
    return [kw["room_id"] for name, kw in fired if name == "presence_enter_room"]


@pytest.fixture(autouse=True)
def _cancel_timers():
    """A test must not leave an armed deadline timer behind."""
    built: list[PlacementState] = []
    original = PlacementState.__init__

    def tracking_init(self, *a, **kw):
        original(self, *a, **kw)
        built.append(self)

    PlacementState.__init__ = tracking_init
    yield
    PlacementState.__init__ = original
    for s in built:
        if s._timer is not None:
            s._timer.cancel()


# ---------------------------------------------------------------------------
# The pure rule
# ---------------------------------------------------------------------------


def claim(source: str, room_id: int, since: float) -> Claim:
    return Claim(source=source, rooms=frozenset({room_id}), room=room_id, since=since)


class TestCombinePerson:
    def test_the_source_that_changed_zone_last_wins(self):
        person = combine_person(
            [VerdictKind.PLACED, VerdictKind.PLACED],
            [claim("watch", BATH, 100.0), claim("phone", KITCHEN, 200.0)],
            incumbent_rooms=frozenset({BATH}),
        )
        assert person.kind is PersonKind.PLACED
        assert person.winner.source == "phone"
        assert person.contested

    def test_agreeing_sources_are_not_contested(self):
        person = combine_person(
            [VerdictKind.PLACED, VerdictKind.PLACED],
            [claim("watch", KITCHEN, 100.0), claim("phone", KITCHEN, 200.0)], None,
        )
        assert person.kind is PersonKind.PLACED and not person.contested

    def test_a_tie_keeps_the_incumbent(self):
        claims = [claim("a", KITCHEN, 100.0), claim("b", BATH, 100.0)]
        assert combine_person([], claims, frozenset({KITCHEN})).winner.source == "a"
        assert combine_person([], claims, frozenset({BATH})).winner.source == "b"

    @pytest.mark.parametrize(("kinds", "expected"), [
        ([VerdictKind.HOME_UNKNOWN, VerdictKind.ABSENT], PersonKind.HOME_UNKNOWN),
        ([VerdictKind.ABSENT, VerdictKind.ABSENT], PersonKind.ABSENT),
        ([VerdictKind.ABSENT, VerdictKind.UNAVAILABLE], PersonKind.UNAVAILABLE),
        ([VerdictKind.UNAVAILABLE], PersonKind.UNAVAILABLE),
        ([], PersonKind.UNAVAILABLE),
    ])
    def test_without_a_claim(self, kinds, expected):
        assert combine_person(kinds, [], None).kind is expected


# ---------------------------------------------------------------------------
# #1551: two devices in two rooms
# ---------------------------------------------------------------------------


async def two_devices_script(svc, clock, rounds: int = 12) -> list[int | None]:
    """The watch lies in the bath from the start; the phone is carried into
    the kitchen. Both are reported in turn, as the satellites do."""
    await report(svc, clock, "sat-bath", -45, mac=WATCH)
    rooms = []
    for _ in range(rounds):
        await report(svc, clock, "sat-kitchen", -45, mac=PHONE)
        rooms.append(room(svc))
        await report(svc, clock, "sat-bath", -45, mac=WATCH)
        rooms.append(room(svc))
    return rooms


class TestTwoDevices:
    async def test_the_person_follows_the_device_that_moved_and_stays(self, monkeypatch, clock):
        svc, fired, _ = build_service(monkeypatch, clock)
        rooms = await two_devices_script(svc, clock)
        assert set(rooms) == {KITCHEN}
        # bath first (only the watch was known), then the kitchen — and nothing more.
        assert enters(fired) == [BATH, KITCHEN]
        # The two devices disagree: the room is booked, but not trusted.
        assert svc.is_location_uncertain(1, clock.now)
        assert 1 in svc.uncertain_user_ids()

    async def test_negative_control_the_legacy_decision_flips(self, monkeypatch, clock):
        """The same script with the flag off: the defect #1551 describes."""
        svc, fired, _ = build_service(monkeypatch, clock, state=False)
        assert svc._placement_state is None
        rooms = await two_devices_script(svc, clock)
        changes = sum(1 for a, b in pairwise(rooms) if a != b)
        assert changes >= 10
        assert len(enters(fired)) >= 10

    async def test_when_the_devices_agree_again_the_room_is_certain(self, monkeypatch, clock):
        svc, _fired, _ = build_service(monkeypatch, clock)
        await two_devices_script(svc, clock, rounds=3)
        # The watch is picked up and carried into the kitchen too.
        for _ in range(6):
            await report(svc, clock, "sat-kitchen", -45, mac=WATCH)
            await report(svc, clock, "sat-kitchen", -45, mac=PHONE)
        assert room(svc) == KITCHEN
        assert not svc.get_user_presence(1).contested
        assert not svc.is_location_uncertain(1, clock.now)


# ---------------------------------------------------------------------------
# Home, placed by nothing
# ---------------------------------------------------------------------------


class TestHomeUnknown:
    async def test_the_last_room_stays_booked_and_turns_uncertain(self, monkeypatch, clock):
        svc, fired, _ = build_service(monkeypatch, clock)
        for _ in range(4):
            await report(svc, clock, "sat-kitchen", -45)
        assert room(svc) == KITCHEN and not svc.is_location_uncertain(1, clock.now)
        evidence_at = svc.get_user_presence(1).room_evidence_at
        fired.clear()

        # Weak readings: outside every bound. Once the smoothing has followed
        # them down, v2 places the phone nowhere.
        for _ in range(20):
            await report(svc, clock, "sat-kitchen", -75, step=1.0)
            if svc._placement_state._verdicts[PHONE].kind is VerdictKind.HOME_UNKNOWN:
                break
        else:
            pytest.fail("the phone never dropped out of the kitchen bound")
        evidence_at = svc.get_user_presence(1).room_evidence_at
        while clock.now - evidence_at <= 60.0:
            assert not svc.is_location_uncertain(1, clock.now)
            await report(svc, clock, "sat-kitchen", -75, step=5.0)
            assert svc._placement_state._verdicts[PHONE].kind is VerdictKind.HOME_UNKNOWN
        assert room(svc) == KITCHEN          # the display keeps it
        assert fired == []                   # no leave event
        assert svc.is_location_uncertain(1, clock.now)

    async def test_a_person_first_heard_without_a_room_is_uncertain(self, monkeypatch, clock):
        svc, fired, _ = build_service(monkeypatch, clock)
        await report(svc, clock, "sat-kitchen", -75)
        p = svc.get_user_presence(1)
        assert p is not None and p.room_id is None
        assert svc.is_location_uncertain(1, clock.now)
        assert 1 in svc.uncertain_user_ids()
        assert fired == []


# ---------------------------------------------------------------------------
# Deadlines
# ---------------------------------------------------------------------------


class TestDeadline:
    async def test_the_timer_is_armed_for_the_next_deadline(self, monkeypatch, clock):
        svc, _fired, _ = build_service(monkeypatch, clock)
        await report(svc, clock, "sat-kitchen", -45)
        timer = svc._placement_state._timer
        assert timer is not None
        delay = timer.when() - asyncio.get_running_loop().time()
        # The reading turns stale after the freshness window (20 s).
        assert delay == pytest.approx(20.0, abs=0.5)

    async def test_absent_after_the_deadline_without_any_report(self, monkeypatch, clock):
        svc, fired, _ = build_service(monkeypatch, clock)
        for _ in range(4):
            await report(svc, clock, "sat-kitchen", -45)
        fired.clear()
        clock.now += 121.0
        await svc._placement_state_tick()      # what the timer runs
        assert svc.get_user_presence(1) is None
        assert [name for name, _ in fired] == ["presence_leave_room", "presence_last_left"]

    async def test_nobody_listening_changes_nothing(self, monkeypatch, clock):
        """No receiver alive: silence proves nothing — no absence, no event."""
        svc, fired, roster = build_service(monkeypatch, clock)
        for _ in range(4):
            await report(svc, clock, "sat-kitchen", -45)
        fired.clear()
        roster.alive = False
        clock.now += 300.0
        await svc._placement_state_tick()
        assert room(svc) == KITCHEN
        assert fired == []
        assert svc._placement_state._verdicts[PHONE].kind is VerdictKind.UNAVAILABLE


# ---------------------------------------------------------------------------
# Voice / auth sightings
# ---------------------------------------------------------------------------


class TestVoice:
    async def test_a_phone_lying_elsewhere_does_not_pull_the_person_back(self, monkeypatch, clock):
        svc, _fired, _ = build_service(monkeypatch, clock)
        for _ in range(4):
            await report(svc, clock, "sat-kitchen", -45)
        await svc.register_voice_presence(user_id=1, room_id=BATH, room_name="Bath")
        assert room(svc) == BATH
        # Far past the hold; the phone is still placed in the kitchen.
        for _ in range(40):
            await report(svc, clock, "sat-kitchen", -45, step=3.0)
        assert room(svc) == BATH
        assert svc.is_location_uncertain(1, clock.now)    # contested: phone says kitchen

    async def test_a_device_that_moves_after_the_sighting_takes_over(self, monkeypatch, clock):
        svc, _fired, _ = build_service(monkeypatch, clock)
        for _ in range(4):
            await report(svc, clock, "sat-kitchen", -45)
        await svc.register_voice_presence(user_id=1, room_id=BATH, room_name="Bath")
        clock.now += 61.0                                  # the hold is over
        for _ in range(6):
            await report(svc, clock, "sat-living", -45)    # the phone is carried on
        assert room(svc) == LIVING
        assert not svc.get_user_presence(1).voice_pinned

    async def test_no_move_while_the_hold_runs(self, monkeypatch, clock):
        svc, _fired, _ = build_service(monkeypatch, clock)
        await svc.register_voice_presence(user_id=1, room_id=BATH, room_name="Bath")
        for _ in range(6):
            await report(svc, clock, "sat-living", -45)
        assert room(svc) == BATH

    async def test_a_voice_only_person_is_absent_after_the_deadline(self, monkeypatch, clock):
        svc, fired, _ = build_service(monkeypatch, clock)
        svc._mac_to_user = {}                              # no tracked device at all
        await svc.register_voice_presence(user_id=1, room_id=BATH, room_name="Bath")
        clock.now += 121.0
        await svc._placement_state_tick()
        assert svc.get_user_presence(1) is None
        assert ("presence_leave_room", BATH) in [(n, kw.get("room_id")) for n, kw in fired]


# ---------------------------------------------------------------------------
# The voice-check counter
# ---------------------------------------------------------------------------


class TestVoiceCheckCounter:
    @pytest.fixture
    def recorded(self, monkeypatch):
        calls: list[tuple[str, str]] = []
        monkeypatch.setattr(shadow_module, "record_presence_voice_check",
                            lambda d, o: calls.append((d, o)))
        return calls

    async def test_shadow_mode_records_both_decisions(self, monkeypatch, clock, recorded):
        svc, _fired, _ = build_service(monkeypatch, clock, state=False, shadow=True)
        for _ in range(4):
            await report(svc, clock, "sat-kitchen", -45)
        await svc.register_voice_presence(user_id=1, room_id=BATH, room_name="Bath")
        await svc.register_voice_presence(user_id=1, room_id=BATH, room_name="Bath")
        assert recorded == [("legacy", "miss"), ("v2", "miss"), ("legacy", "match"), ("v2", "miss")]

    async def test_state_mode_records_v2_only(self, monkeypatch, clock, recorded):
        svc, _fired, _ = build_service(monkeypatch, clock)
        await svc.register_voice_presence(user_id=1, room_id=BATH, room_name="Bath")
        for _ in range(4):
            await report(svc, clock, "sat-kitchen", -45)
        assert recorded == [("v2", "unplaced")]

    def test_the_counter_is_primed_and_bounded(self):
        """Every decision × outcome exists from the start (a rate over a
        series that appears late is wrong), and a value outside the
        vocabulary cannot mint a series."""
        import utils.metrics as metrics_module
        from tests.backend.metrics_registry import label_values, live_metrics, sample_count

        family = "renfield_presence_voice_check"
        with live_metrics() as registry:
            metrics_module._prime_presence_voice_check()
            assert label_values(registry, family, "decision") == {"legacy", "v2"}
            assert label_values(registry, family, "outcome") == {"match", "miss", "unplaced"}
            metrics_module.record_presence_voice_check("v2", "match")
            metrics_module.record_presence_voice_check("kitchen", "user 1")
            total = f"{family}_total"
            assert sample_count(registry, total, decision="v2", outcome="match") == 1.0
            assert sample_count(registry, total, decision="v2", outcome="unplaced") == 1.0
            assert label_values(registry, family, "decision") == {"legacy", "v2"}


# ---------------------------------------------------------------------------
# Flag and settings
# ---------------------------------------------------------------------------


class TestFlag:
    def test_dark_by_default(self):
        assert HaGlueSettings.model_fields["presence_placement_v2_state_enabled"].default is False

    async def test_unusable_settings_keep_the_legacy_decision(self, monkeypatch, clock, log_lines):
        svc, _fired, _ = build_service(monkeypatch, clock, receivers="{not json")
        assert svc._placement_state is None and svc._placement_shadow is None
        assert any(
            line.startswith("ERROR|") and "the LEGACY decision acts" in line for line in log_lines
        )
        await report(svc, clock, "sat-kitchen", -45)
        assert room(svc) == KITCHEN          # the legacy decision placed the user

    @pytest.mark.parametrize(("flag", "loaded"), [
        (None, False),
        ("false", False),
        ("true", True),   # the control: the probe does see it when on
    ])
    def test_the_state_module_is_not_imported_when_off(self, flag, loaded):
        backend = Path(presence_service_module.__file__).resolve().parents[2]
        env = {k: v for k, v in os.environ.items()
               if not k.upper().startswith("PRESENCE_PLACEMENT_V2_")}
        env["PYTHONPATH"] = str(backend)
        if flag is not None:
            env["PRESENCE_PLACEMENT_V2_STATE_ENABLED"] = flag
        code = (
            "import sys\n"
            "from ha_glue.services.presence_service import PresenceService\n"
            "svc = PresenceService()\n"
            "print('PROBE', 'ha_glue.services.presence_placement_state' in sys.modules)\n"
        )
        proc = subprocess.run([sys.executable, "-c", code], env=env,
                              capture_output=True, text=True, timeout=180)
        assert proc.returncode == 0, proc.stderr[-2000:]
        assert f"PROBE {loaded}" in proc.stdout
