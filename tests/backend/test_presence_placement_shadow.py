"""Placement v2 shadow mode — the wiring around the pure decision.

  * flag OFF (the default): the presence service never calls into the new
    modules, writes no new log line, keeps the same state and payload;
  * flag ON: the legacy decision, its state and its events are identical to
    the flag-off run — the shadow only logs and adds one payload field;
  * it logs when the two decisions START and STOP disagreeing about a person,
    never per report — STOP only once the agreement has settled — and one
    line per voice/auth sighting;
  * a clock that steps back holds the shadow (reads are marked ``held``), and
    one that is behind by more than the deadline resets it;
  * unusable settings, and anything else that keeps the shadow from being
    built, switch it off with an ERROR; a failing shadow never reaches
    presence tracking and never prints a device key.
"""
from __future__ import annotations

import copy
import json
import math
import os
import subprocess
import sys
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from loguru import logger

import ha_glue.services.presence_debug as presence_debug
import ha_glue.services.presence_placement as placement
import ha_glue.services.presence_placement_shadow as shadow_module
from ha_glue.services import presence_service as presence_service_module
from ha_glue.services.presence_placement import Reason, VerdictKind
from ha_glue.services.presence_placement_shadow import (
    CAUSE_DECISIONS_DIFFER,
    CAUSE_VOICE_WITHOUT_DEVICE,
    DISAGREEMENT_SETTLE_S,
    LOG_PREFIX,
    PlacementShadow,
    _person_agrees,
)
from ha_glue.services.presence_service import PresenceService
from ha_glue.utils.config import HaGlueSettings, ha_glue_settings

pytestmark = pytest.mark.unit

MAC = "AA:BB:CC:DD:EE:01"
OTHER_MAC = "AA:BB:CC:DD:EE:02"
SECOND_MAC = "AA:BB:CC:DD:EE:03"   # a second device of user 1
IRK_LABEL = "Alices iPhone"        # user-chosen, may be a name
KITCHEN, BATH = 10, 20
T0 = 1_800_000_000.0

RECEIVERS_JSON = json.dumps({
    "sat-kitchen": {"offset_db": 0, "in_room_dbm": -60, "cutoff_dbm": -80},
    "sat-bath": {"offset_db": 0, "in_room_dbm": -60, "cutoff_dbm": -80},
})


@pytest.fixture
def clock(monkeypatch):
    """The presence service's clock, set by the test."""
    state = SimpleNamespace(now=T0)
    monkeypatch.setattr(
        presence_service_module, "time", SimpleNamespace(time=lambda: state.now)
    )
    monkeypatch.setattr(presence_debug, "time", SimpleNamespace(
        time=lambda: state.now, monotonic=lambda: state.now,
    ))
    return state


@pytest.fixture
def log_lines():
    lines: list[str] = []
    sink = logger.add(
        lambda m: lines.append(str(m).rstrip()), level="DEBUG", format="{level}|{message}"
    )
    yield lines
    logger.remove(sink)


def build_service(monkeypatch, clock, *, enabled: bool, receivers: str = RECEIVERS_JSON,
                  zones: str = "") -> tuple[PresenceService, list[tuple[str, dict]]]:
    """A presence service as production builds it, with the flag set. Returns
    the service and the list its fired events are collected in."""
    monkeypatch.setattr(ha_glue_settings, "presence_placement_v2_shadow_enabled", enabled)
    monkeypatch.setattr(ha_glue_settings, "presence_placement_v2_receivers", receivers)
    monkeypatch.setattr(ha_glue_settings, "presence_placement_v2_zones", zones)
    svc = PresenceService()
    svc._mac_to_user = {MAC: 1, OTHER_MAC: 2}
    svc._user_names = {1: "alice", 2: "bob"}
    svc._room_names = {KITCHEN: "Kitchen", BATH: "Bath"}  # production caches them at startup
    if svc._placement_shadow is not None:
        # Both receivers connected, heartbeat current.
        svc._placement_shadow._roster = lambda: {
            "sat-kitchen": (KITCHEN, clock.now), "sat-bath": (BATH, clock.now),
        }
    fired: list[tuple[str, dict]] = []

    async def collect_events():
        fired.extend(copy.deepcopy(svc._pending_events))
        svc._pending_events.clear()

    monkeypatch.setattr(svc, "_fire_pending_events", collect_events)
    return svc, fired


async def report(svc, clock, satellite: str, rssi: float, mac: str = MAC, step: float = 3.0,
                 devices: list[dict] | None = None):
    clock.now += step
    room_id, room_name = {"sat-kitchen": (KITCHEN, "Kitchen"), "sat-bath": (BATH, "Bath")}[satellite]
    await svc.process_ble_report(
        satellite_id=satellite, room_id=room_id,
        devices=devices if devices is not None else [{"mac": mac, "rssi": rssi}],
        room_name=room_name,
    )


async def walk(svc, clock):
    """A short scripted evening: into the kitchen, a voice turn in the bath,
    back to the kitchen, a second person, then both fall silent."""
    for _ in range(4):
        await report(svc, clock, "sat-kitchen", -72)
    for _ in range(6):
        await report(svc, clock, "sat-kitchen", -50)
    await svc.register_voice_presence(user_id=1, room_id=BATH, room_name="Bath")
    for _ in range(4):
        await report(svc, clock, "sat-bath", -55)
        await report(svc, clock, "sat-kitchen", -78)
    clock.now += 70.0
    for _ in range(8):
        await report(svc, clock, "sat-kitchen", -48)
    await report(svc, clock, "sat-bath", -66, mac=OTHER_MAC)
    clock.now += 200.0
    await report(svc, clock, "sat-bath", -66, mac=OTHER_MAC)


def legacy_state(svc) -> dict:
    return {
        "presence": {uid: asdict(p) for uid, p in svc._presence.items()},
        "sightings": {k: [asdict(s) for s in v] for k, v in svc._sightings.items()},
        "room_names": dict(svc._room_names),
    }


def shadow_lines(lines: list[str]) -> list[str]:
    return [line for line in lines if LOG_PREFIX in line]


# ---------------------------------------------------------------------------
# Flag off
# ---------------------------------------------------------------------------


class TestFlagOff:
    def test_the_defaults_are_off_and_empty(self):
        """Dark by default, and no invented receiver numbers."""
        fields = HaGlueSettings.model_fields
        assert fields["presence_placement_v2_shadow_enabled"].default is False
        assert fields["presence_placement_v2_receivers"].default == ""
        assert fields["presence_placement_v2_zones"].default == ""

    async def test_no_call_into_the_new_modules(self, monkeypatch, clock, log_lines):
        """Every entry point of the new modules is replaced by a mock; a whole
        walk with reports and voice sightings touches none of them."""
        mocks = {
            "from_settings": MagicMock(),
            "observe_report": MagicMock(),
            "note_voice_sighting": MagicMock(),
            "payload_for": MagicMock(),
        }
        for name, mock in mocks.items():
            monkeypatch.setattr(PlacementShadow, name, mock)
        decide = MagicMock()
        monkeypatch.setattr(shadow_module, "decide", decide)
        monkeypatch.setattr(placement, "decide", decide)

        svc, _fired = build_service(monkeypatch, clock, enabled=False)
        assert svc._placement_shadow is None
        await walk(svc, clock)
        payload = presence_debug.build_sightings_payload(svc)

        for name, mock in mocks.items():
            assert not mock.called, name
        assert not decide.called
        assert shadow_lines(log_lines) == []
        assert payload["devices"]
        assert all("placement_v2" not in device for device in payload["devices"])

    @pytest.mark.parametrize(("flag", "built", "loaded"), [
        (None, False, []),
        ("false", False, []),
        # The control: the same probe DOES see the modules when the flag is on.
        ("true", True, ["ha_glue.services.presence_placement",
                        "ha_glue.services.presence_placement_shadow"]),
    ])
    def test_the_new_modules_are_not_imported(self, flag, built, loaded):
        """In a fresh interpreter — this file imports both modules at its top,
        so its own ``sys.modules`` proves nothing. The probe loads BOTH
        modules that reach for the shadow (the presence service and the
        live-readings builder) and runs the builder once."""
        backend = Path(presence_service_module.__file__).resolve().parents[2]
        env = {k: v for k, v in os.environ.items()
               if not k.upper().startswith("PRESENCE_PLACEMENT_V2_")}
        env["PYTHONPATH"] = str(backend)
        if flag is not None:
            env["PRESENCE_PLACEMENT_V2_SHADOW_ENABLED"] = flag
        code = (
            "import json, sys\n"
            "import ha_glue.services.presence_debug as presence_debug\n"
            "from ha_glue.services.presence_service import PresenceService\n"
            "svc = PresenceService()\n"
            "payload = presence_debug.build_sightings_payload(svc)\n"
            "print('PROBE ' + json.dumps({\n"
            "    'enabled': payload['enabled'],\n"
            "    'built': svc._placement_shadow is not None,\n"
            "    'loaded': sorted(m for m in sys.modules if 'presence_placement' in m),\n"
            "}))\n"
        )
        proc = subprocess.run(
            [sys.executable, "-c", code], env=env, capture_output=True, text=True, timeout=180,
        )
        assert proc.returncode == 0, proc.stderr[-2000:]
        probe = [line for line in proc.stdout.splitlines() if line.startswith("PROBE ")]
        assert len(probe) == 1, proc.stdout[-2000:]
        assert json.loads(probe[0][len("PROBE "):]) == {
            "enabled": True, "built": built, "loaded": loaded,
        }

    async def test_a_service_built_without_the_attribute_still_works(self, clock):
        """Older test doubles build the service with ``__new__`` and never set
        the shadow attribute; the service must treat that as "off"."""
        svc = PresenceService.__new__(PresenceService)
        svc.__dict__.update(PresenceService().__dict__)
        del svc.__dict__["_placement_shadow"]
        svc._mac_to_user = {MAC: 1}
        await svc.process_ble_report("sat-kitchen", KITCHEN, [{"mac": MAC, "rssi": -50}], "Kitchen")
        await svc.register_voice_presence(user_id=1, room_id=BATH, room_name="Bath")
        assert svc.get_user_presence(1).room_id == BATH
        assert "placement_v2" not in presence_debug.build_sightings_payload(svc)["devices"][0]


# ---------------------------------------------------------------------------
# Flag on: nothing the system acts on changes
# ---------------------------------------------------------------------------


class TestShadowChangesNothing:
    async def test_legacy_state_events_and_logs_are_identical(self, monkeypatch, clock, log_lines):
        """The same walk with the flag off and on, compared after every step
        of it: presence state, sightings, fired events, legacy log lines."""
        async def run(enabled: bool):
            clock.now = T0
            log_lines.clear()
            svc, fired = build_service(monkeypatch, clock, enabled=enabled)
            await walk(svc, clock)
            return legacy_state(svc), fired, list(log_lines), svc

        state_off, fired_off, lines_off, _svc = await run(enabled=False)
        state_on, fired_on, lines_on, svc_on = await run(enabled=True)

        assert svc_on._placement_shadow is not None
        assert state_on == state_off
        assert fired_on == fired_off
        assert fired_off, "the walk must fire events for this comparison to mean anything"
        assert shadow_lines(lines_off) == []
        assert shadow_lines(lines_on), "the walk must make the shadow speak"
        assert [line for line in lines_on if LOG_PREFIX not in line] == lines_off

    async def test_the_shadow_never_writes_to_the_presence_service(self, monkeypatch, clock):
        """Stronger than comparing outcomes: during the shadow's own calls the
        service's state objects are frozen copies-compared before and after."""
        svc, _fired = build_service(monkeypatch, clock, enabled=True)
        for _ in range(5):
            await report(svc, clock, "sat-kitchen", -50)
        before = legacy_state(svc)
        events_before = list(svc._pending_events)
        shadow = svc._placement_shadow
        shadow.observe_report(svc, clock.now + 1)
        shadow.note_voice_sighting(svc, 1, BATH, clock.now + 2)
        shadow.payload_for(svc, MAC, clock.now + 3)
        shadow.observe_report(svc, clock.now + 500)  # past every deadline
        assert legacy_state(svc) == before
        assert svc._pending_events == events_before


# ---------------------------------------------------------------------------
# Flag on: what the shadow says
# ---------------------------------------------------------------------------


class TestDisagreementLog:
    async def test_logs_when_a_disagreement_starts_and_when_it_stops(
        self, monkeypatch, clock, log_lines
    ):
        svc, _fired = build_service(monkeypatch, clock, enabled=True)
        log_lines.clear()

        # -72 dBm: the legacy decision books the kitchen, v2 says "outside the
        # kitchen receiver's bound".
        await report(svc, clock, "sat-kitchen", -72)
        assert svc.get_user_presence(1).room_id == KITCHEN
        assert len(shadow_lines(log_lines)) == 1
        start = shadow_lines(log_lines)[0]
        assert start.startswith("INFO|")
        assert "disagreement START user 1 cause=decisions_differ: legacy=Kitchen(" in start
        assert "v2=home_unknown reason=outside_bound best=Kitchen:-72.0" in start
        assert "bound_distance=-12.0" in start
        assert "runner_up=Bath:-80.0(censored)" in start
        assert "censored=[Bath]" in start

        # The disagreement goes on: not one more line.
        for _ in range(5):
            await report(svc, clock, "sat-kitchen", -72)
        assert len(shadow_lines(log_lines)) == 1

        # The phone comes into the room: both say kitchen — from the second
        # of these reports on (21 s after the START). No STOP yet: the
        # agreement has to hold for the settle period first.
        for _ in range(6):
            await report(svc, clock, "sat-kitchen", -45)
        assert svc._placement_shadow._verdicts[MAC].kind is VerdictKind.PLACED
        assert len(shadow_lines(log_lines)) == 1

        for _ in range(6):
            await report(svc, clock, "sat-kitchen", -45)
        lines = shadow_lines(log_lines)
        assert len(lines) == 2
        stop = lines[1]
        # The duration is that of the disagreement, not "until this line".
        assert "disagreement STOP user 1 cause=decisions_differ after 21 s" in stop
        assert "(1 distinct pair(s))" in stop
        assert "v2=placed:Kitchen reason=in_bound_and_margin" in stop

        # Agreement goes on: silence.
        for _ in range(5):
            await report(svc, clock, "sat-kitchen", -45)
        assert len(shadow_lines(log_lines)) == 2

    async def test_lines_carry_rooms_and_numbers_only(self, monkeypatch, clock, log_lines):
        svc, _fired = build_service(monkeypatch, clock, enabled=True)
        await walk(svc, clock)
        lines = shadow_lines(log_lines)
        assert lines
        for line in lines:
            assert MAC not in line and OTHER_MAC not in line
            assert "alice" not in line and "bob" not in line
            assert "sat-" not in line
            assert presence_debug.redact_device_key(MAC) not in line

    async def test_absent_agrees_with_a_user_the_legacy_decision_dropped(
        self, monkeypatch, clock, log_lines
    ):
        """Past the deadline the legacy sweep drops the user and v2 says
        ``absent`` (receivers alive): agreement, so an open disagreement ends."""
        svc, _fired = build_service(monkeypatch, clock, enabled=True)
        await report(svc, clock, "sat-kitchen", -72)             # disagreement opens
        clock.now += 300.0
        await report(svc, clock, "sat-bath", -66, mac=OTHER_MAC)  # the sweep runs
        assert svc.get_user_presence(1) is None
        assert svc._placement_shadow._verdicts[MAC].kind is VerdictKind.ABSENT
        assert not [line for line in shadow_lines(log_lines) if "STOP user 1" in line]
        await report(svc, clock, "sat-bath", -66, mac=OTHER_MAC, step=DISAGREEMENT_SETTLE_S)
        stops = [line for line in shadow_lines(log_lines) if "STOP user 1" in line]
        assert len(stops) == 1
        assert "STOP user 1 cause=decisions_differ after 303 s" in stops[0]
        assert "legacy=none v2=absent reason=not_heard" in stops[0]

    async def test_with_no_receiver_alive_the_shadow_says_unavailable(
        self, monkeypatch, clock
    ):
        svc, _fired = build_service(monkeypatch, clock, enabled=True)
        await report(svc, clock, "sat-kitchen", -50)
        svc._placement_shadow._roster = dict  # nobody connected any more
        clock.now += 300.0
        svc._placement_shadow.observe_report(svc, clock.now)
        assert svc._placement_shadow._verdicts[MAC].kind is VerdictKind.UNAVAILABLE

    async def test_a_removed_device_is_forgotten(self, monkeypatch, clock, log_lines):
        svc, _fired = build_service(monkeypatch, clock, enabled=True)
        await report(svc, clock, "sat-kitchen", -72)
        assert 1 in svc._placement_shadow._disagreements
        svc._sightings.pop(MAC)
        await report(svc, clock, "sat-bath", -66, mac=OTHER_MAC)
        assert MAC not in svc._placement_shadow._verdicts
        assert 1 not in svc._placement_shadow._disagreements
        stops = [line for line in shadow_lines(log_lines) if "STOP user 1" in line]
        assert len(stops) == 1 and stops[0].endswith("no tracked device left")

    async def test_a_device_that_is_nobodys_any_more_is_forgotten(
        self, monkeypatch, clock, log_lines
    ):
        """The device was removed from the registry while its sightings are
        still in the window: its verdict and the open disagreement must not
        stay behind for ever — and a START must get its STOP."""
        svc, _fired = build_service(monkeypatch, clock, enabled=True)
        await report(svc, clock, "sat-kitchen", -72)
        assert 1 in svc._placement_shadow._disagreements
        svc._mac_to_user.pop(MAC)
        assert MAC in svc._sightings
        await report(svc, clock, "sat-bath", -66, mac=OTHER_MAC)
        assert MAC not in svc._placement_shadow._verdicts
        assert 1 not in svc._placement_shadow._disagreements
        stops = [line for line in shadow_lines(log_lines) if "STOP user 1" in line]
        assert len(stops) == 1
        assert "after 3 s (1 distinct pair(s)): no tracked device left" in stops[0]

    async def test_a_device_on_the_edge_of_its_bound_writes_one_pair(
        self, monkeypatch, clock, log_lines
    ):
        """Reports 10 s apart, alternately inside and well outside the bound:
        the two decisions agree on every second report. One START — and no
        STOP until the agreement has held for the settle period."""
        svc, _fired = build_service(monkeypatch, clock, enabled=True)
        log_lines.clear()
        kinds = []
        for i in range(12):
            await report(svc, clock, "sat-kitchen", -72 if i % 2 == 0 else -45, step=10.0)
            kinds.append(svc._placement_shadow._verdicts[MAC].kind)
            assert svc.get_user_presence(1).room_id == KITCHEN
        # The scenario really toggles: without that the test shows nothing.
        assert kinds == [VerdictKind.HOME_UNKNOWN, VerdictKind.PLACED] * 6
        lines = shadow_lines(log_lines)
        assert len(lines) == 1 and "disagreement START user 1" in lines[0]

        # It settles in the room. The last report above already agreed.
        for _ in range(2):
            await report(svc, clock, "sat-kitchen", -45, step=10.0)
        assert len(shadow_lines(log_lines)) == 1   # 20 s of agreement: not yet
        await report(svc, clock, "sat-kitchen", -45, step=10.0)
        lines = shadow_lines(log_lines)
        assert len(lines) == 2
        assert ("disagreement STOP user 1 cause=decisions_differ after 110 s "
                "(1 distinct pair(s))") in lines[1]
        assert DISAGREEMENT_SETTLE_S == 30.0

    async def test_a_backward_step_of_the_clock_is_not_a_disagreement(
        self, monkeypatch, clock, log_lines
    ):
        """The wall clock is set back by a minute (time sync). Every stored
        reading is then "in the future" and the decision would see nothing:
        absent, a START, the incumbent gone. The shadow holds instead."""
        monkeypatch.setattr(presence_debug, "_connected_satellites", lambda: [])
        svc, _fired = build_service(monkeypatch, clock, enabled=True)
        for _ in range(6):
            await report(svc, clock, "sat-kitchen", -50)
        for _ in range(8):
            await report(svc, clock, "sat-kitchen", -62)
        shadow = svc._placement_shadow
        held = shadow._verdicts[MAC]
        # 2 dB below the bound: only the incumbent holds this.
        assert held.kind is VerdictKind.PLACED
        assert held.components.reason is Reason.INCUMBENT_HELD
        assert shadow.payload_for(svc, MAC, clock.now)["held"] is False
        before_step = clock.now
        log_lines.clear()

        clock.now -= 60.0
        for _ in range(3):
            await report(svc, clock, "sat-bath", -79, mac=OTHER_MAC)
        assert svc.get_user_presence(1).room_id == KITCHEN
        assert shadow._verdicts[MAC] is held
        lines = shadow_lines(log_lines)
        assert [line for line in lines if "START" in line or "STOP" in line] == []
        assert len(lines) == 1   # once per hold, not once per report
        assert lines[0].startswith("WARNING|")
        assert "the clock stepped back by 57.0 s" in lines[0]
        assert "held" in lines[0] and "reset" not in lines[0]

        # A read during the hold is not decided: it shows the verdict of the
        # last decided report and says so.
        decide_calls = []
        real_decide = shadow_module.decide
        monkeypatch.setattr(shadow_module, "decide", lambda *a, **k: (
            decide_calls.append(1), real_decide(*a, **k))[1])
        payload = shadow.payload_for(svc, MAC, clock.now)
        assert payload["held"] is True
        assert (payload["kind"], payload["reason"]) == ("placed", "incumbent_held")
        assert payload["age_s"] == round(held.components.age_s, 2)
        # A device that has no decided verdict yet has nothing to show.
        assert shadow.payload_for(svc, OTHER_MAC, clock.now) == {"held": True, "kind": None}
        by_key = {
            device["user_id"]: device["placement_v2"]
            for device in presence_debug.build_sightings_payload(svc)["devices"]
        }
        assert by_key[1]["held"] is True and by_key[2] == {"held": True, "kind": None}

        # The ground-truth line carries no verdict it could not have.
        log_lines.clear()
        await svc.register_voice_presence(user_id=1, room_id=KITCHEN, room_name="Kitchen")
        voice = shadow_lines(log_lines)
        assert len(voice) == 1
        assert voice[0].endswith("voice sighting user 1 in Kitchen: legacy=Kitchen[match] v2=held(clock)")
        assert decide_calls == []
        monkeypatch.setattr(shadow_module, "decide", real_decide)

        # The clock has passed the earlier time: decisions resume, and the
        # incumbent is still the incumbent.
        clock.now = before_step
        await report(svc, clock, "sat-kitchen", -62)
        resumed = shadow._verdicts[MAC]
        assert resumed is not held
        assert resumed.kind is VerdictKind.PLACED
        assert resumed.components.reason is Reason.INCUMBENT_HELD
        assert shadow.payload_for(svc, MAC, clock.now)["held"] is False
        assert not [line for line in shadow_lines(log_lines)
                    if "START user 1" in line or "STOP user 1" in line]

    async def test_a_hold_is_not_counted_into_the_duration_of_a_case(
        self, monkeypatch, clock, log_lines
    ):
        """A case is open when the clock steps back by a minute. The reports
        of that minute are held; the duration in the STOP line is the time
        the two decisions were SEEN to disagree, one minute less than what
        passed in the house."""
        svc, _fired = build_service(monkeypatch, clock, enabled=True)
        shadow = svc._placement_shadow
        log_lines.clear()
        await report(svc, clock, "sat-kitchen", -72)       # START
        since = clock.now
        await report(svc, clock, "sat-kitchen", -72)
        high = clock.now
        passed = 3.0                                       # seconds in the house since START

        clock.now -= 60.0
        while clock.now + 3.0 < high:
            await report(svc, clock, "sat-kitchen", -72)
            passed += 3.0
        lines = shadow_lines(log_lines)
        assert len(lines) == 2 and "START user 1" in lines[0]
        assert lines[1].startswith("WARNING|") and "stepped back by 57.0 s" in lines[1]
        assert shadow._disagreements[1].since == since

        passed_at_agreement = None
        for _ in range(30):
            await report(svc, clock, "sat-kitchen", -45)
            passed += 3.0
            case = shadow._disagreements.get(1)
            if case is None:
                break
            if passed_at_agreement is None and case.agreeing_since is not None:
                passed_at_agreement = passed
                assert case.agreeing_since == clock.now
                assert clock.now - since == passed - 60.0
        stops = [line for line in shadow_lines(log_lines) if "STOP user 1" in line]
        assert len(stops) == 1 and passed_at_agreement is not None
        assert f"after {passed_at_agreement - 60.0:.0f} s (1 distinct pair(s))" in stops[0]

    async def test_a_clock_far_behind_is_a_reset_not_a_hold(
        self, monkeypatch, clock, log_lines
    ):
        """The clock jumps three hours ahead, a report is decided there, and
        the clock is corrected. Holding until it has caught up would freeze
        the shadow for three hours. It starts over instead: one WARNING, open
        cases closed with that reason and no invented duration, the verdicts
        (and with them every incumbent) dropped, decisions at the new clock."""
        monkeypatch.setattr(presence_debug, "_connected_satellites", lambda: [])
        svc, _fired = build_service(monkeypatch, clock, enabled=True)
        shadow = svc._placement_shadow
        for _ in range(6):
            await report(svc, clock, "sat-kitchen", -50)
        for _ in range(8):
            await report(svc, clock, "sat-kitchen", -62)
        await report(svc, clock, "sat-bath", -72, mac=OTHER_MAC)   # a case for user 2 opens
        assert list(shadow._disagreements) == [2]
        before_jump = clock.now

        clock.now += 3 * 3600.0
        await report(svc, clock, "sat-kitchen", -62)
        in_the_future = shadow._verdicts[MAC]
        assert in_the_future.components.reason is Reason.INCUMBENT_HELD
        assert list(shadow._disagreements) == [2]
        log_lines.clear()

        clock.now = before_jump + 30.0
        await report(svc, clock, "sat-kitchen", -62)
        lines = shadow_lines(log_lines)
        warnings = [line for line in lines if line.startswith("WARNING|")]
        assert len(warnings) == 1
        assert "the clock is 10770.0 s behind the last decided report" in warnings[0]
        assert "taken as a clock reset — 1 open case(s) closed, verdicts cleared" in warnings[0]
        stops = [line for line in lines if "STOP user 2" in line]
        assert len(stops) == 1
        assert stops[0].endswith(
            "disagreement STOP user 2 cause=decisions_differ after ? s (1 distinct pair(s)): clock reset"
        )
        assert lines.index(warnings[0]) < lines.index(stops[0])

        # Decided at the new clock, in the same report, without the incumbent:
        # -62 dBm is 2 dB outside the bound.
        assert shadow._clock_high_water == clock.now
        fresh = shadow._verdicts[MAC]
        assert fresh is not in_the_future
        assert fresh.kind is VerdictKind.HOME_UNKNOWN
        assert fresh.components.reason is Reason.OUTSIDE_BOUND
        assert fresh.components.age_s == 0.0
        payload = shadow.payload_for(svc, MAC, clock.now)
        assert (payload["held"], payload["kind"]) == (False, "home_unknown")

        # And it goes on deciding, without another warning.
        log_lines.clear()
        for _ in range(6):
            await report(svc, clock, "sat-kitchen", -50)
        assert shadow._verdicts[MAC].kind is VerdictKind.PLACED
        assert not [line for line in shadow_lines(log_lines) if line.startswith("WARNING|")]

    @pytest.mark.parametrize(("behind", "reset"), [(120.0, False), (120.5, True)])
    async def test_the_deadline_separates_a_hold_from_a_reset(
        self, monkeypatch, clock, log_lines, behind, reset
    ):
        """The limit is a choice: the absence deadline. Behind by exactly the
        deadline is still a hold."""
        monkeypatch.setattr(ha_glue_settings, "presence_stale_timeout", 120)
        svc, _fired = build_service(monkeypatch, clock, enabled=True)
        shadow = svc._placement_shadow
        assert shadow._config.absent_after_s == 120.0
        for _ in range(6):
            await report(svc, clock, "sat-kitchen", -50)
        decided = shadow._verdicts[MAC]
        log_lines.clear()

        clock.now -= behind + 3.0          # the report below steps 3 s forward
        await report(svc, clock, "sat-kitchen", -50)
        warnings = [line for line in shadow_lines(log_lines) if line.startswith("WARNING|")]
        assert len(warnings) == 1
        assert ("taken as a clock reset" in warnings[0]) is reset
        assert ("verdicts are held" in warnings[0]) is (not reset)
        assert (shadow._verdicts[MAC] is decided) is (not reset)
        assert shadow._clock_is_behind(clock.now) is (not reset)


class TestCause:
    """``cause=`` in the START/STOP lines tells the expected kind of
    disagreement from the kind that says something about the decisions."""

    async def _voice_room_with_the_phone_away(self, svc, clock):
        """User 1's phone was heard, then left the house; the person is then
        placed by a voice turn in the bath."""
        for _ in range(6):
            await report(svc, clock, "sat-kitchen", -50)
        clock.now += 300.0
        await report(svc, clock, "sat-bath", -66, mac=OTHER_MAC)   # the legacy sweep runs
        assert svc.get_user_presence(1) is None
        assert svc._placement_shadow._verdicts[MAC].kind is VerdictKind.ABSENT
        assert MAC in svc._sightings   # the stale key stays, as absent

    async def test_a_voice_room_with_no_device_heard_has_its_own_cause(
        self, monkeypatch, clock, log_lines
    ):
        """Every voice sighting of a person whose phone is away opens a case:
        the legacy decision books the voice room, v2 says "absent" about the
        phone. Both are right. The case is kept — and marked."""
        svc, _fired = build_service(monkeypatch, clock, enabled=True)
        await self._voice_room_with_the_phone_away(svc, clock)
        # The control: the other person's case (heard outside the bound, and
        # open through the whole run) is the ordinary kind.
        others = [line for line in shadow_lines(log_lines) if "START user 2" in line]
        assert len(others) == 1 and "START user 2 cause=decisions_differ:" in others[0]
        log_lines.clear()

        for _ in range(2):
            await svc.register_voice_presence(user_id=1, room_id=BATH, room_name="Bath")
            await report(svc, clock, "sat-bath", -66, mac=OTHER_MAC)
            assert svc._placement_shadow._disagreements[1].cause == CAUSE_VOICE_WITHOUT_DEVICE
            clock.now += 300.0
            await report(svc, clock, "sat-bath", -66, mac=OTHER_MAC)   # the sweep drops user 1
            assert svc.get_user_presence(1) is None
            await report(svc, clock, "sat-bath", -66, mac=OTHER_MAC, step=DISAGREEMENT_SETTLE_S)
            assert 1 not in svc._placement_shadow._disagreements

        lines = [line for line in shadow_lines(log_lines) if "user 1" in line]
        assert [line.split(f"{LOG_PREFIX} ")[1].split(" user 1")[0] for line in lines] == [
            "voice sighting", "disagreement START", "disagreement STOP",
        ] * 2
        for start in lines[1::3]:
            assert "START user 1 cause=voice_without_device: legacy=Bath(" in start
            assert "pinned=True" in start and "v2=absent reason=not_heard" in start
        for stop in lines[2::3]:
            assert "STOP user 1 cause=voice_without_device after 303 s (1 distinct pair(s))" in stop
        assert not [line for line in shadow_lines(log_lines) if "START user 2" in line]
        assert CAUSE_VOICE_WITHOUT_DEVICE == "voice_without_device"
        assert CAUSE_DECISIONS_DIFFER == "decisions_differ"

    async def test_a_voice_room_while_the_phone_is_heard_elsewhere_is_the_ordinary_cause(
        self, monkeypatch, clock, log_lines
    ):
        svc, _fired = build_service(monkeypatch, clock, enabled=True)
        svc._voice_hold_seconds = 90.0
        for _ in range(6):
            await report(svc, clock, "sat-kitchen", -50)
        log_lines.clear()
        await svc.register_voice_presence(user_id=1, room_id=BATH, room_name="Bath")
        await report(svc, clock, "sat-kitchen", -50)
        assert svc.get_user_presence(1).room_id == BATH
        starts = [line for line in shadow_lines(log_lines) if "START user 1" in line]
        assert len(starts) == 1
        assert "START user 1 cause=decisions_differ: legacy=Bath(" in starts[0]
        assert "pinned=True" in starts[0] and "v2=placed:Kitchen" in starts[0]

    async def test_a_room_booked_on_a_reading_v2_refuses_is_the_ordinary_cause(
        self, monkeypatch, clock, log_lines
    ):
        """"No device heard" alone does not make the marked kind: the legacy
        room must rest on a voice sighting. Here it rests on a level v2 does
        not accept as a reading (+35 dBm) — the two decisions differ over a
        reading, which is exactly what must not be filtered away."""
        svc, _fired = build_service(monkeypatch, clock, enabled=True)
        log_lines.clear()
        await report(svc, clock, "sat-kitchen", 35)
        current = svc.get_user_presence(1)
        assert current.room_id == KITCHEN and current.voice_pinned is False
        assert svc._placement_shadow._verdicts[MAC].kind is VerdictKind.ABSENT
        starts = [line for line in shadow_lines(log_lines) if "START user 1" in line]
        assert len(starts) == 1
        assert "START user 1 cause=decisions_differ: legacy=Kitchen(" in starts[0]

    async def test_a_case_that_turns_into_the_ordinary_kind_is_closed_as_that(
        self, monkeypatch, clock, log_lines
    ):
        """The marked case is open when the phone comes back and is heard
        outside every bound: from then on the two decisions do differ over
        readings, and a filter on the cause must not hide the case."""
        svc, _fired = build_service(monkeypatch, clock, enabled=True)
        svc._voice_hold_seconds = 90.0
        await self._voice_room_with_the_phone_away(svc, clock)
        log_lines.clear()
        await svc.register_voice_presence(user_id=1, room_id=BATH, room_name="Bath")
        await report(svc, clock, "sat-bath", -66, mac=OTHER_MAC)
        shadow = svc._placement_shadow
        assert shadow._disagreements[1].cause == CAUSE_VOICE_WITHOUT_DEVICE

        await report(svc, clock, "sat-kitchen", -72)
        assert shadow._verdicts[MAC].kind is VerdictKind.HOME_UNKNOWN
        assert shadow._disagreements[1].cause == CAUSE_DECISIONS_DIFFER

        # It ends in the kitchen: a voice turn there, the phone inside the bound.
        await svc.register_voice_presence(user_id=1, room_id=KITCHEN, room_name="Kitchen")
        for _ in range(16):
            await report(svc, clock, "sat-kitchen", -45)
        lines = [line for line in shadow_lines(log_lines) if "disagreement" in line and "user 1" in line]
        assert len(lines) == 2
        assert "START user 1 cause=voice_without_device" in lines[0]
        assert "STOP user 1 cause=decisions_differ after" in lines[1]
        assert "(2 distinct pair(s))" in lines[1]


class TestSeveralDevicesOfOnePerson:
    """The legacy decision keeps ONE room per person. Scored device by
    device, a second phone left somewhere else is a disagreement that never
    ends."""

    async def test_a_second_device_elsewhere_is_not_a_standing_disagreement(
        self, monkeypatch, clock, log_lines
    ):
        """The second device is heard at -85 dBm: below the legacy floor, so
        the legacy decision ignores it and keeps the person in the kitchen.
        (Above the floor the legacy decision moves the person with every
        report of either device — its own defect, not this test's subject.)"""
        svc, _fired = build_service(monkeypatch, clock, enabled=True)
        svc._mac_to_user[SECOND_MAC] = 1
        log_lines.clear()
        for _ in range(8):
            await report(svc, clock, "sat-kitchen", -45, step=2.0)                # carried along
            await report(svc, clock, "sat-bath", -85, mac=SECOND_MAC, step=1.0)   # left behind
            assert svc.get_user_presence(1).room_id == KITCHEN
        shadow = svc._placement_shadow
        assert shadow._verdicts[MAC].kind is VerdictKind.PLACED
        assert shadow._verdicts[MAC].covers(KITCHEN)
        # The device left behind, on its own, does NOT say "kitchen".
        assert shadow._verdicts[SECOND_MAC].kind is VerdictKind.HOME_UNKNOWN
        assert shadow_lines(log_lines) == []
        assert shadow._disagreements == {}

    async def test_a_disagreement_is_one_case_per_person_naming_every_device(
        self, monkeypatch, clock, log_lines
    ):
        svc, _fired = build_service(monkeypatch, clock, enabled=True)
        svc._mac_to_user[SECOND_MAC] = 1
        log_lines.clear()
        for _ in range(4):
            await report(svc, clock, "sat-kitchen", -72, step=1.0)
            await report(svc, clock, "sat-kitchen", -74, mac=SECOND_MAC, step=2.0)
        lines = shadow_lines(log_lines)
        # One START at the first report (one device known then), none for the second device.
        assert len(lines) == 1 and "disagreement START user 1" in lines[0]
        assert list(svc._placement_shadow._disagreements) == [1]
        await svc.register_voice_presence(user_id=1, room_id=KITCHEN, room_name="Kitchen")
        voice = shadow_lines(log_lines)[-1]
        assert voice.count("home_unknown reason=outside_bound") == 2 and " | " in voice

    def test_the_join_rule(self):
        """"In room R" agrees when ANY device is placed there; "not tracked"
        agrees only when EVERY device is gone."""
        def verdict(rssi=None, room=KITCHEN, alive=True, now=100.0):
            receivers = {"sat": placement.Receiver(
                room, placement.ReceiverParams(0.0, -60.0, -80.0), alive_at=now if alive else None
            )}
            readings = {"sat": [placement.Reading(now, rssi)]} if rssi is not None else {}
            return placement.decide(readings, receivers, {}, None, now)

        in_kitchen, in_bath = verdict(-50.0), verdict(-50.0, room=BATH)
        home, absent, unavailable = verdict(-70.0), verdict(), verdict(alive=False)
        assert [v.kind.value for v in (in_kitchen, home, absent, unavailable)] == [
            "placed", "home_unknown", "absent", "unavailable",
        ]
        assert _person_agrees([in_bath, in_kitchen], KITCHEN)
        assert _person_agrees([home, in_kitchen, absent], KITCHEN)
        assert not _person_agrees([home, in_bath], KITCHEN)
        assert not _person_agrees([absent, unavailable], KITCHEN)
        assert _person_agrees([absent, unavailable], None)
        assert not _person_agrees([absent, home], None)      # one device is still at home
        assert not _person_agrees([absent, in_kitchen], None)


class TestDeviceKeys:
    async def test_an_irk_identity_runs_through_the_shadow(self, monkeypatch, clock, log_lines):
        """A phone resolved by its IRK is keyed ``irk:<label>``, and the label
        is chosen by the user. It gets a verdict like any device, and the
        label reaches neither a log line nor the payload."""
        monkeypatch.setattr(presence_debug, "_connected_satellites", lambda: [])
        svc, _fired = build_service(monkeypatch, clock, enabled=True)
        svc._irk_label_to_user = {IRK_LABEL: 1}
        key = "irk:" + IRK_LABEL
        entry = {"mac": "4F:11:22:33:44:55", "identity": IRK_LABEL}
        log_lines.clear()

        await report(svc, clock, "sat-kitchen", 0, devices=[{**entry, "rssi": -72}])
        assert list(svc._sightings) == [key]
        assert svc._placement_shadow._verdicts[key].kind is VerdictKind.HOME_UNKNOWN
        for _ in range(6):
            await report(svc, clock, "sat-kitchen", 0, devices=[{**entry, "rssi": -45}])
        assert svc._placement_shadow._verdicts[key].kind is VerdictKind.PLACED
        await svc.register_voice_presence(user_id=1, room_id=KITCHEN, room_name="Kitchen")

        lines = shadow_lines(log_lines)
        assert any("disagreement START user 1" in line for line in lines)
        assert "v2=[match] placed:Kitchen" in lines[-1]
        payload = presence_debug.build_sightings_payload(svc)
        assert payload["devices"][0]["placement_v2"]["kind"] == "placed"
        for text in (*lines, json.dumps(payload)):
            assert IRK_LABEL not in text and "4F:11" not in text

    async def test_two_entries_for_one_device_in_one_report(self, monkeypatch, clock):
        """Both are stored with the report's timestamp. For the smoothing two
        readings of one receiver at one instant are ONE reading, and the later
        entry counts — the documented rule, not an average."""
        svc, _fired = build_service(monkeypatch, clock, enabled=True)
        for _ in range(6):
            await report(svc, clock, "sat-kitchen", 0, devices=[
                {"mac": MAC, "rssi": -45}, {"mac": MAC, "rssi": -70},
            ])
        assert len(svc._sightings[MAC]) == 12
        verdict = svc._placement_shadow._verdicts[MAC]
        kitchen = next(e for e in verdict.components.receivers if e.receiver_id == "sat-kitchen")
        assert kitchen.samples == 6
        assert kitchen.smoothed_dbm == pytest.approx(-70.0)
        assert verdict.kind is VerdictKind.HOME_UNKNOWN

    @pytest.mark.parametrize("bad", [math.nan, math.inf])
    async def test_a_reading_that_is_not_a_number_places_nobody(self, monkeypatch, clock, bad):
        """``json.loads`` accepts NaN and Infinity, and the satellite handler
        passes device entries through as they come."""
        svc, _fired = build_service(monkeypatch, clock, enabled=True)
        for _ in range(6):
            await report(svc, clock, "sat-kitchen", bad)
            await report(svc, clock, "sat-bath", -70, step=0.0)
        verdict = svc._placement_shadow._verdicts[MAC]
        assert verdict.kind is VerdictKind.HOME_UNKNOWN
        assert verdict.components.best_receiver == "sat-bath"
        payload = svc._placement_shadow.payload_for(svc, MAC, clock.now)
        json.dumps(payload, allow_nan=False)   # nothing that is not a number left in it


class TestVoiceSightingLog:
    async def test_one_line_per_sighting_with_both_beliefs_before_the_booking(
        self, monkeypatch, clock, log_lines
    ):
        svc, _fired = build_service(monkeypatch, clock, enabled=True)
        for _ in range(6):
            await report(svc, clock, "sat-kitchen", -50)
        log_lines.clear()

        # Both decisions believe "kitchen"; the voice turn happens in the bath.
        await svc.register_voice_presence(user_id=1, room_id=BATH, room_name="Bath")
        lines = shadow_lines(log_lines)
        assert len(lines) == 1
        assert lines[0].startswith("INFO|")
        assert "voice sighting user 1 in Bath: legacy=Kitchen[miss]" in lines[0]
        assert "v2=[miss] placed:Kitchen reason=in_bound_and_margin best=Kitchen:-50.0" in lines[0]
        assert svc.get_user_presence(1).room_id == BATH  # booked as before

        # A second turn in the same room: the legacy belief is now the bath
        # (it was just told), v2 still hears the phone in the kitchen.
        clock.now += 5.0
        log_lines.clear()
        await svc.register_voice_presence(user_id=1, room_id=BATH, room_name="Bath")
        lines = shadow_lines(log_lines)
        assert len(lines) == 1
        assert "legacy=Bath[match]" in lines[0]
        assert "v2=[miss] placed:Kitchen" in lines[0]

    async def test_a_matching_placement_and_a_home_unknown_read_differently(
        self, monkeypatch, clock, log_lines
    ):
        svc, _fired = build_service(monkeypatch, clock, enabled=True)
        for _ in range(6):
            await report(svc, clock, "sat-kitchen", -50)
        log_lines.clear()
        await svc.register_voice_presence(user_id=1, room_id=KITCHEN, room_name="Kitchen")
        assert "legacy=Kitchen[match] v2=[match] placed:Kitchen" in shadow_lines(log_lines)[0]

        for _ in range(8):
            await report(svc, clock, "sat-kitchen", -72)
        log_lines.clear()
        await svc.register_voice_presence(user_id=1, room_id=KITCHEN, room_name="Kitchen")
        assert "v2=home_unknown reason=outside_bound" in shadow_lines(log_lines)[0]

    async def test_a_user_without_a_tracked_device(self, monkeypatch, clock, log_lines):
        svc, _fired = build_service(monkeypatch, clock, enabled=True)
        log_lines.clear()
        await svc.register_voice_presence(user_id=2, room_id=BATH, room_name="Bath")
        lines = shadow_lines(log_lines)
        assert len(lines) == 1
        assert "voice sighting user 2 in Bath: legacy=none v2=no tracked device" in lines[0]


class TestReportCountIndependence:
    async def test_more_reporting_receivers_change_the_legacy_value_but_not_the_new_one(
        self, monkeypatch, clock
    ):
        """The same six kitchen readings, 3 s apart — once alone, once with a
        second satellite reporting twice in between. The legacy filter steps
        once per report from ANY satellite, so the kitchen's filtered value
        ends somewhere else; the new smoothing is a function of the kitchen's
        own readings and their times."""
        kitchen_levels = [-78, -74, -70, -66, -62, -58]

        async def run(crowded: bool) -> tuple[float, float]:
            clock.now = T0
            svc, _fired = build_service(monkeypatch, clock, enabled=True)
            for level in kitchen_levels:
                if crowded:
                    await report(svc, clock, "sat-bath", -79, step=1.0)
                    await report(svc, clock, "sat-bath", -79, step=1.0)
                    await report(svc, clock, "sat-kitchen", level, step=1.0)
                else:
                    await report(svc, clock, "sat-kitchen", level, step=3.0)
            verdict = svc._placement_shadow._verdicts[MAC]
            kitchen = next(e for e in verdict.components.receivers if e.receiver_id == "sat-kitchen")
            return svc._presence[1].room_rssi_filtered[KITCHEN], kitchen.smoothed_dbm

        legacy_alone, v2_alone = await run(crowded=False)
        legacy_crowded, v2_crowded = await run(crowded=True)

        assert abs(legacy_crowded - legacy_alone) > 1.0   # the legacy value moved
        assert v2_crowded == pytest.approx(v2_alone)      # the new one did not
        assert -70.0 < v2_alone < -58.0


# ---------------------------------------------------------------------------
# Flag on: the live-readings payload
# ---------------------------------------------------------------------------


class TestPayload:
    async def test_adds_the_verdict_and_its_components_per_device(self, monkeypatch, clock):
        monkeypatch.setattr(presence_debug, "_connected_satellites", lambda: [])
        svc, _fired = build_service(monkeypatch, clock, enabled=True)
        for _ in range(6):
            await report(svc, clock, "sat-kitchen", -50)

        payload = presence_debug.build_sightings_payload(svc)
        device = payload["devices"][0]
        v2 = device["placement_v2"]
        assert v2["held"] is False
        assert v2["kind"] == "placed"
        assert v2["reason"] == "in_bound_and_margin"
        assert (v2["room_id"], v2["room_name"]) == (KITCHEN, "Kitchen")
        assert v2["zone"] is None
        assert v2["zone_room_ids"] == [KITCHEN]
        assert v2["agrees_with_legacy"] is True
        assert v2["best"] == {
            "satellite_id": "sat-kitchen", "room_id": KITCHEN, "room_name": "Kitchen", "dbm": -50.0,
        }
        assert v2["runner_up"] == {
            "satellite_id": "sat-bath", "room_id": BATH, "room_name": "Bath",
            "dbm": -80.0, "censored": True,
        }
        assert v2["margin_db"] == 30.0
        assert v2["margin_required_db"] == 10.0
        assert v2["hysteresis_db"] == 3.0
        assert v2["bound_distance_db"] == 10.0
        assert v2["samples"] == 6
        assert v2["age_s"] == 0.0
        assert v2["censored"] == ["sat-bath"]
        assert v2["offline"] == []
        assert [r["satellite_id"] for r in v2["receivers"]] == ["sat-bath", "sat-kitchen"]
        assert v2["receivers"][1] == {
            "satellite_id": "sat-kitchen", "room_id": KITCHEN, "state": "heard",
            "calibrated": True, "smoothed_dbm": -50.0, "comparable_dbm": -50.0,
            "bound_distance_db": 10.0, "samples": 6, "age_s": 0.0,
        }
        # Additive: every field the view reads today is still there.
        for field in ("key", "user_id", "current_room_id", "current_room_name",
                      "current_confidence", "voice_hold_remaining_s", "voice_pinned",
                      "location_uncertain", "filtered_by_room", "filtered_rooms", "sightings"):
            assert field in device
        json.dumps(payload)  # serialisable as it stands
        assert MAC not in json.dumps(payload)

    async def test_follows_the_clock_without_a_report(self, monkeypatch, clock):
        """The legacy room freezes when no report arrives; the payload's v2
        verdict is evaluated at the moment of the snapshot."""
        monkeypatch.setattr(presence_debug, "_connected_satellites", lambda: [])
        svc, _fired = build_service(monkeypatch, clock, enabled=True)
        for _ in range(6):
            await report(svc, clock, "sat-kitchen", -50)

        clock.now += 60.0
        device = presence_debug.build_sightings_payload(svc)["devices"][0]
        assert device["current_room_id"] == KITCHEN
        assert device["placement_v2"]["kind"] == "home_unknown"
        assert device["placement_v2"]["reason"] == "no_fresh_reading"
        assert device["placement_v2"]["agrees_with_legacy"] is False

        clock.now += 120.0
        device = presence_debug.build_sightings_payload(svc)["devices"][0]
        assert device["current_room_id"] == KITCHEN  # still booked: no report, no sweep
        assert device["placement_v2"]["kind"] == "absent"

    async def test_watching_does_not_advance_the_shadow(self, monkeypatch, clock, log_lines):
        monkeypatch.setattr(presence_debug, "_connected_satellites", lambda: [])
        svc, _fired = build_service(monkeypatch, clock, enabled=True)
        for _ in range(6):
            await report(svc, clock, "sat-kitchen", -50)
        shadow = svc._placement_shadow
        verdict_before = shadow._verdicts[MAC]
        disagreements_before = dict(shadow._disagreements)
        log_lines.clear()

        clock.now += 60.0
        presence_debug.build_sightings_payload(svc)
        presence_debug.build_sightings_payload(svc)

        assert shadow._verdicts[MAC] is verdict_before
        assert shadow._disagreements == disagreements_before
        assert shadow_lines(log_lines) == []

    async def test_a_zone_is_reported_with_all_its_rooms(self, monkeypatch, clock):
        monkeypatch.setattr(presence_debug, "_connected_satellites", lambda: [])
        svc, _fired = build_service(
            monkeypatch, clock, enabled=True, zones=json.dumps({"open-plan": [KITCHEN, BATH]})
        )
        for _ in range(6):
            await report(svc, clock, "sat-kitchen", -50)
            await report(svc, clock, "sat-bath", -52, step=0.0)
        v2 = presence_debug.build_sightings_payload(svc)["devices"][0]["placement_v2"]
        assert v2["kind"] == "placed"
        assert v2["zone"] == "open-plan"
        assert v2["zone_room_ids"] == [KITCHEN, BATH]
        assert v2["room_id"] == KITCHEN
        assert v2["runner_up"] is None


# ---------------------------------------------------------------------------
# Settings and failures
# ---------------------------------------------------------------------------


class TestSettings:
    def test_valid_settings_build_the_shadow(self, monkeypatch, clock, log_lines):
        monkeypatch.setattr(ha_glue_settings, "presence_stale_timeout", 90)
        monkeypatch.setattr(ha_glue_settings, "presence_placement_v2_margin_db", 12.0)
        monkeypatch.setattr(ha_glue_settings, "presence_placement_v2_hysteresis_db", 4.0)
        monkeypatch.setattr(ha_glue_settings, "presence_placement_v2_fresh_seconds", 25.0)
        monkeypatch.setattr(ha_glue_settings, "presence_placement_v2_time_constant_seconds", 9.0)
        monkeypatch.setattr(shadow_module.settings, "device_heartbeat_timeout", 45.0)
        svc, _fired = build_service(
            monkeypatch, clock, enabled=True, zones='{"open-plan": [10, 20]}'
        )
        shadow = svc._placement_shadow
        assert isinstance(shadow, PlacementShadow)
        assert sorted(shadow._params) == ["sat-bath", "sat-kitchen"]
        assert shadow._zones == {"open-plan": frozenset({10, 20})}
        config = shadow._config
        assert (config.margin_db, config.hysteresis_db) == (12.0, 4.0)
        assert (config.fresh_s, config.time_constant_s) == (25.0, 9.0)
        # The deadline is the existing presence timeout, the sign-of-life
        # window the satellite heartbeat timeout: no second copy of either.
        assert config.absent_after_s == 90.0
        assert config.receiver_alive_s == 45.0
        on = [line for line in shadow_lines(log_lines) if " ON " in line]
        assert len(on) == 1
        assert "2 calibrated receiver(s), 1 zone(s)" in on[0]

    def test_empty_parameters_are_valid_and_announced(self, monkeypatch, clock, log_lines):
        svc, _fired = build_service(monkeypatch, clock, enabled=True, receivers="")
        assert svc._placement_shadow is not None
        assert svc._placement_shadow._params == {}
        assert any("no receiver is calibrated" in line for line in shadow_lines(log_lines))

    @pytest.mark.parametrize(("receivers", "zones"), [
        ("{not json", ""),
        ('{"sat-kitchen": {"offset_db": 0, "in_room_dbm": -60}}', ""),
        (RECEIVERS_JSON, '{"open-plan": [10]}'),
        # Not a PlacementConfigError by nature: an integer no float holds
        # (OverflowError inside math.isfinite) ...
        ('{"sat-kitchen": {"offset_db": 1' + "0" * 400 + ', "in_room_dbm": -60, "cutoff_dbm": -80}}', ""),
        # ... and a document nested too deep to parse (RecursionError).
        (RECEIVERS_JSON, "[" * 200_000),
    ], ids=["broken-json", "field-missing", "zone-of-one", "huge-integer", "nested-too-deep"])
    def test_unusable_json_switches_the_shadow_off_with_an_error(
        self, monkeypatch, clock, log_lines, receivers, zones
    ):
        """A diagnostic must not stop the backend: the error is logged and the
        presence service runs as with the flag off."""
        svc, _fired = build_service(
            monkeypatch, clock, enabled=True, receivers=receivers, zones=zones
        )
        assert svc._placement_shadow is None
        errors = [line for line in shadow_lines(log_lines) if line.startswith("ERROR|")]
        assert len(errors) == 1
        assert "shadow stays OFF" in errors[0]

    @pytest.mark.parametrize("name", [
        "presence_placement_v2_margin_db",
        "presence_placement_v2_hysteresis_db",
        "presence_placement_v2_fresh_seconds",
        "presence_placement_v2_time_constant_seconds",
    ])
    @pytest.mark.parametrize("bad", [math.nan, math.inf])
    def test_a_number_that_is_not_finite_switches_the_shadow_off(
        self, monkeypatch, clock, log_lines, name, bad
    ):
        """pydantic reads "nan" and "inf" as floats. ``nan < 0`` is False, so
        a NaN margin would pass a range check and never reject a placement."""
        monkeypatch.setattr(ha_glue_settings, name, bad)
        svc, _fired = build_service(monkeypatch, clock, enabled=True)
        assert svc._placement_shadow is None
        errors = [line for line in shadow_lines(log_lines) if line.startswith("ERROR|")]
        assert len(errors) == 1
        assert "shadow stays OFF" in errors[0] and "must be a finite number" in errors[0]

    def test_an_unexpected_error_while_building_switches_the_shadow_off(
        self, monkeypatch, clock, log_lines
    ):
        def broken(_raw):
            raise RuntimeError("boom")

        monkeypatch.setattr(shadow_module, "parse_zones", broken)
        svc, _fired = build_service(monkeypatch, clock, enabled=True)
        assert svc._placement_shadow is None
        errors = [line for line in shadow_lines(log_lines) if line.startswith("ERROR|")]
        assert len(errors) == 1 and "RuntimeError" in errors[0]

    async def test_a_shadow_module_that_cannot_be_imported_does_not_stop_presence(
        self, monkeypatch, clock, log_lines
    ):
        """``PresenceService()`` is built unguarded at startup: with the flag
        on, an import error in the diagnostic must not take presence down."""
        monkeypatch.setitem(sys.modules, "ha_glue.services.presence_placement_shadow", None)
        svc, _fired = build_service(monkeypatch, clock, enabled=True)
        assert svc._placement_shadow is None
        errors = [line for line in shadow_lines(log_lines) if line.startswith("ERROR|")]
        assert len(errors) == 1
        assert "could not be built, stays OFF (ModuleNotFoundError" in errors[0]
        await report(svc, clock, "sat-kitchen", -50)
        assert svc.get_user_presence(1).room_id == KITCHEN

    def test_numbers_that_break_the_rule_switch_the_shadow_off(
        self, monkeypatch, clock, log_lines
    ):
        monkeypatch.setattr(ha_glue_settings, "presence_placement_v2_margin_db", 2.0)
        monkeypatch.setattr(ha_glue_settings, "presence_placement_v2_hysteresis_db", 5.0)
        svc, _fired = build_service(monkeypatch, clock, enabled=True)
        assert svc._placement_shadow is None
        assert any("hysteresis_db must not exceed margin_db" in line
                   for line in shadow_lines(log_lines))


class TestFailureIsolation:
    async def test_a_failing_shadow_never_reaches_presence_tracking(
        self, monkeypatch, clock, log_lines
    ):
        monkeypatch.setattr(presence_debug, "_connected_satellites", lambda: [])
        svc, fired = build_service(monkeypatch, clock, enabled=True)

        def broken(*_args, **_kwargs):
            raise RuntimeError("boom")

        monkeypatch.setattr(shadow_module, "decide", broken)
        log_lines.clear()
        for _ in range(5):
            await report(svc, clock, "sat-kitchen", -50)
        await svc.register_voice_presence(user_id=1, room_id=BATH, room_name="Bath")

        assert svc.get_user_presence(1).room_id == BATH
        assert [name for name, _kwargs in fired].count("presence_enter_room") == 2
        warnings = [line for line in shadow_lines(log_lines) if line.startswith("WARNING|")]
        # Once per entry point, not once per report: five reports, one sighting.
        assert len(warnings) == 2
        assert "observe_report failed (RuntimeError)" in warnings[0]
        assert "note_voice_sighting failed (RuntimeError)" in warnings[1]
        device = presence_debug.build_sightings_payload(svc)["devices"][0]
        assert device["placement_v2"] is None
        assert device["current_room_id"] == BATH

    async def test_a_failure_is_logged_once_per_entry_point_without_its_message(
        self, monkeypatch, clock, log_lines
    ):
        """A ``KeyError`` on a device key carries the key: a MAC, or an
        ``irk:<label>`` whose label the user chose. The line names the entry
        point and the exception type, nothing the exception says. And a
        failure in one entry point must not silence the next one."""
        monkeypatch.setattr(presence_debug, "_connected_satellites", lambda: [])
        svc, _fired = build_service(monkeypatch, clock, enabled=True)
        svc._irk_label_to_user = {IRK_LABEL: 1}

        def broken(readings, *_args, **_kwargs):
            raise KeyError(f"{MAC} irk:{IRK_LABEL}")

        monkeypatch.setattr(shadow_module, "decide", broken)
        log_lines.clear()
        for _ in range(4):
            await report(svc, clock, "sat-kitchen", -50)
            await svc.register_voice_presence(user_id=1, room_id=KITCHEN, room_name="Kitchen")
            presence_debug.build_sightings_payload(svc)

        lines = shadow_lines(log_lines)
        assert [line.split(" failed ")[0].split()[-1] for line in lines] == [
            "observe_report", "note_voice_sighting", "payload_for",
        ]
        for line in lines:
            assert line.startswith("WARNING|") and "(KeyError)" in line
            assert MAC not in line and IRK_LABEL not in line and "irk:" not in line

    async def test_the_roster_is_best_effort(self, monkeypatch, clock):
        """Without a satellite manager the receivers are known from their
        readings alone — uncalibrated ones included."""
        svc, _fired = build_service(monkeypatch, clock, enabled=True)
        svc._placement_shadow._roster = dict
        for _ in range(6):
            await report(svc, clock, "sat-kitchen", -50)
        verdict = svc._placement_shadow._verdicts[MAC]
        assert verdict.kind is VerdictKind.PLACED
        assert [e.receiver_id for e in verdict.components.receivers] == ["sat-kitchen"]

    def test_the_default_roster_reads_the_satellite_manager(self, monkeypatch):
        import ha_glue.services.satellite_manager as satellite_manager

        manager = SimpleNamespace(satellites={
            "sat-kitchen": SimpleNamespace(room_id=KITCHEN, last_heartbeat=123.0),
        })
        monkeypatch.setattr(satellite_manager, "get_satellite_manager", lambda: manager)
        assert shadow_module._connected_roster() == {"sat-kitchen": (KITCHEN, 123.0)}

    async def test_a_failing_roster_is_reported_once_and_the_readings_still_decide(
        self, monkeypatch, clock, log_lines
    ):
        """The roster is asked on every report, every voice sighting and every
        payload. When the satellite manager fails, that is one WARNING — not a
        line per report, and not a silent DEBUG — and the receivers are judged
        by their readings alone."""
        import ha_glue.services.satellite_manager as satellite_manager

        monkeypatch.setattr(presence_debug, "_connected_satellites", lambda: [])
        svc, _fired = build_service(monkeypatch, clock, enabled=True)
        shadow = svc._placement_shadow

        def unavailable():
            raise RuntimeError(f"no manager for {MAC}")

        monkeypatch.setattr(satellite_manager, "get_satellite_manager", unavailable)
        shadow._roster = shadow_module._connected_roster   # the production roster
        with pytest.raises(RuntimeError):
            shadow_module._connected_roster()
        log_lines.clear()

        for _ in range(6):
            await report(svc, clock, "sat-kitchen", -50)
        await svc.register_voice_presence(user_id=1, room_id=KITCHEN, room_name="Kitchen")
        payload = presence_debug.build_sightings_payload(svc)["devices"][0]["placement_v2"]

        about_the_roster = [line for line in log_lines if "roster" in line]
        assert len(about_the_roster) == 1
        assert about_the_roster[0].startswith("WARNING|")
        assert "satellite roster failed (RuntimeError)" in about_the_roster[0]
        assert MAC not in about_the_roster[0] and "no manager" not in about_the_roster[0]
        # No entry point failed: the decision ran on what the readings say.
        assert shadow._failures_logged == {"satellite roster"}
        verdict = shadow._verdicts[MAC]
        assert verdict.kind is VerdictKind.PLACED
        assert [e.receiver_id for e in verdict.components.receivers] == ["sat-kitchen"]
        assert payload["kind"] == "placed"
