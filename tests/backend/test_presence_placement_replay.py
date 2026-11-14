"""Replay of a labelled walk through the legacy room decision and placement v2.

The recording (``fixtures/presence_walk_2026-11-13.json``): one phone, one
evening, five receivers, 7020 readings over 124 minutes, with twelve labelled
segments of about 50 s each — eight ``room`` stations (five in a room with a
receiver, three in a room WITHOUT one), three ``one_metre`` segments and one
``unclear`` segment that is replayed and printed but never scored. Rooms and
receivers carry neutral names: room-a … room-e have a receiver (sat-a … sat-e),
room-f … room-h have none, and room-a and room-e are one open-plan space.

HOW THIN THIS EVIDENCE IS. One phone, one evening, one position and one
orientation per station, about 17 readings per receiver and station — and on
the three receivers that cut at -80 dBm most of those are the same value sent
again (see ``test_the_recording_itself``). A pass here shows that the rule is
not contradicted by this walk. It does not show that any bound holds on another
day, with another phone, or two metres further into the same room.

PROVISIONAL PARAMETERS — derived from the recording by a fixed rule, in this
file, never shipped as defaults (``derive_params``):

  * ``cutoff_dbm``  = the weakest reading that receiver sent anywhere in the
    recording (it demonstrably reports down to there);
  * ``in_room_dbm`` = the 10th percentile (linear interpolation) of that
    receiver's readings during the labelled station(s) in its OWN room;
  * ``offset_db``   = 0 — the recording holds nothing that would calibrate one
    receiver against another (the 1 m readings differ by 16 dB and move by
    10-20 dB with the phone's orientation);
  * a receiver without such a station stays uncalibrated.

Held out: the stations in rooms without a receiver never enter any bound, the
``one_metre`` segments never enter the main derivation, and two further checks
derive from one kind of segment and score on the other.
"""
from __future__ import annotations

import bisect
import json
from collections import Counter, defaultdict
from dataclasses import dataclass
from itertools import pairwise
from pathlib import Path
from types import SimpleNamespace

import pytest
from loguru import logger

from ha_glue.services import presence_service as presence_service_module
from ha_glue.services.presence_placement import (
    PlacementConfig,
    PlacementVerdict,
    Reading,
    Reason,
    Receiver,
    ReceiverParams,
    VerdictKind,
    decide,
)
from ha_glue.services.presence_placement_shadow import LOG_PREFIX, PlacementShadow
from ha_glue.services.presence_service import PresenceService

pytestmark = pytest.mark.unit

FIXTURE = Path(__file__).parent / "fixtures" / "presence_walk_2026-11-13.json"

# The decision's defaults, written out so a changed default shows up here.
CONFIG = PlacementConfig(
    time_constant_s=6.0,
    fresh_s=20.0,
    margin_db=10.0,
    hysteresis_db=3.0,
    absent_after_s=120.0,
    receiver_alive_s=60.0,
)
IN_ROOM_PERCENTILE = 10.0
SCORED_KINDS = ("room", "one_metre")
TRACKED_MAC = "AA:BB:CC:DD:EE:01"
TRACKED_USER = 1
# The replay runs on an epoch-like clock so the legacy code sees ordinary times.
EPOCH = 1_800_000_000.0


# ---------------------------------------------------------------------------
# The recording
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Station:
    start: float
    end: float
    kind: str
    true_room: str
    has_live_receiver: bool
    note: str

    @property
    def name(self) -> str:
        return f"{self.true_room} ({self.kind})"


@dataclass(frozen=True)
class Recording:
    receivers: dict[str, tuple[int, str]]          # satellite id → (room id, room name)
    zones: dict[str, frozenset[int]]
    stations: tuple[Station, ...]
    sightings: tuple[tuple[float, str, int], ...]  # (t_s, satellite id, rssi)
    legacy_changes: tuple[tuple[float, str], ...]  # (t_s, room name) as decided live
    settings: dict

    @property
    def room_id(self) -> dict[str, int]:
        return {name: rid for rid, name in self.receivers.values()}

    @property
    def room_name(self) -> dict[int, str]:
        return {rid: name for rid, name in self.receivers.values()}

    def true_zone(self, station: Station) -> frozenset[int]:
        """Room ids a placement may name at this station: the true room, or any
        room of its open-plan zone. Empty when the true room has no receiver."""
        true_id = self.room_id.get(station.true_room)
        if true_id is None:
            return frozenset()
        for rooms in self.zones.values():
            if true_id in rooms:
                return rooms
        return frozenset({true_id})

    def stations_of(self, *kinds: str) -> list[Station]:
        return [s for s in self.stations if s.kind in kinds]


@pytest.fixture(scope="module")
def recording() -> Recording:
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    receivers = {sid: (r["room_id"], r["room_name"]) for sid, r in data["receivers"].items()}
    ids = {name: rid for rid, name in receivers.values()}
    return Recording(
        receivers=receivers,
        zones={
            "+".join(group): frozenset(ids[name] for name in group)
            for group in data["open_plan"]
        },
        stations=tuple(
            Station(label["start_s"], label["end_s"], label["kind"], label["true_room"],
                    label["has_live_receiver"], label.get("note", ""))
            for label in data["labels"]
        ),
        sightings=tuple((float(t), sat, int(rssi)) for t, sat, rssi in data["sightings"]),
        legacy_changes=tuple((float(t), room) for t, room, _uncertain in data["legacy_verdict_changes"]),
        settings=data["settings_at_recording"],
    )


# ---------------------------------------------------------------------------
# Provisional parameters
# ---------------------------------------------------------------------------


def percentile(values: list[float], pct: float) -> float:
    """Linear interpolation between closest ranks."""
    ordered = sorted(values)
    position = (len(ordered) - 1) * pct / 100.0
    low = int(position)
    high = min(low + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


def derive_params(
    rec: Recording,
    kinds: tuple[str, ...] = ("room",),
    portion: tuple[float, float] = (0.0, 1.0),
) -> dict[str, ReceiverParams]:
    """The mechanical rule from the module docstring. ``portion`` restricts
    each station to a fraction of its duration (for the split-half check)."""
    params: dict[str, ReceiverParams] = {}
    for satellite_id, (_room_id, room_name) in rec.receivers.items():
        own = [r for _t, sat, r in rec.sightings if sat == satellite_id]
        in_room: list[float] = []
        for station in rec.stations_of(*kinds):
            if station.true_room != room_name:
                continue
            length = station.end - station.start
            first = station.start + portion[0] * length
            last = station.start + portion[1] * length
            in_room += [
                r for t, sat, r in rec.sightings if sat == satellite_id and first <= t <= last
            ]
        if not in_room:
            continue  # no station in its own room: uncalibrated
        params[satellite_id] = ReceiverParams(
            offset_db=0.0,
            in_room_dbm=percentile(in_room, IN_ROOM_PERCENTILE),
            cutoff_dbm=float(min(own)),
        )
    return params


# ---------------------------------------------------------------------------
# Replays
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Timeline:
    """Verdicts in time order, one per evaluation instant."""

    points: tuple[tuple[float, PlacementVerdict], ...]

    def at(self, t: float) -> PlacementVerdict:
        """The verdict in force at ``t`` (the last evaluation at or before it)."""
        times = [p[0] for p in self.points]
        return self.points[bisect.bisect_right(times, t) - 1][1]

    def during(self, station: Station, portion: tuple[float, float] = (0.0, 1.0)) -> list[PlacementVerdict]:
        length = station.end - station.start
        first = station.start + portion[0] * length
        last = station.start + portion[1] * length
        return [v for t, v in self.points if first <= t <= last]

    def changes(self) -> int:
        signatures = [(v.kind, v.rooms) for _t, v in self.points]
        return sum(1 for a, b in pairwise(signatures) if a != b)


def replay_v2(
    rec: Recording,
    params: dict[str, ReceiverParams],
    *,
    silent: str | None = None,
    silent_is_offline: bool = False,
    until: float | None = None,
    extra_instants: tuple[float, ...] = (),
    receivers_alive: bool = True,
    config: PlacementConfig = CONFIG,
) -> Timeline:
    """Run the walk through ``decide``: one evaluation per reading (as the
    shadow does per report), plus one at every station boundary.

    ``silent`` removes one receiver's stream entirely; it stays connected (a
    heartbeat, i.e. deaf) unless ``silent_is_offline``. ``until`` cuts the
    recording off: no reading after it. The recording holds no heartbeats —
    its note says all five receivers were live — so with ``receivers_alive``
    every receiver is given a heartbeat at each instant.
    """
    by_receiver: dict[str, list[Reading]] = defaultdict(list)
    for t, sat, rssi in rec.sightings:
        if sat == silent or (until is not None and t > until):
            continue
        by_receiver[sat].append(Reading(at=t, rssi=float(rssi)))
    times = {sat: [r.at for r in readings] for sat, readings in by_receiver.items()}

    instants = {r.at for readings in by_receiver.values() for r in readings}
    for station in rec.stations:
        instants.update((station.start, station.end))
    instants.update(extra_instants)
    if until is not None:
        instants = {t for t in instants if t <= until} | set(extra_instants)

    points: list[tuple[float, PlacementVerdict]] = []
    previous: PlacementVerdict | None = None
    for now in sorted(instants):
        receivers = {}
        for satellite_id, (room_id, _name) in rec.receivers.items():
            alive = receivers_alive and not (satellite_id == silent and silent_is_offline)
            receivers[satellite_id] = Receiver(
                room=room_id, params=params.get(satellite_id), alive_at=now if alive else None
            )
        window = {
            sat: readings[
                bisect.bisect_left(times[sat], now - config.absent_after_s):
                bisect.bisect_right(times[sat], now)
            ]
            for sat, readings in by_receiver.items()
        }
        previous = decide(window, receivers, rec.zones, previous, now, config)
        points.append((now, previous))
    return Timeline(tuple(points))


async def replay_legacy(
    rec: Recording,
    monkeypatch,
    shadow_params: dict[str, ReceiverParams] | None = None,
    shadow_verdicts: list[tuple[float, PlacementVerdict]] | None = None,
) -> list[tuple[float, str | None]]:
    """Run the walk through the REAL legacy decision (``process_ble_report``)
    with the decision settings in force when it was recorded. Returns the room
    after every report.

    With ``shadow_params`` the placement shadow runs alongside, exactly as it
    is wired in production, and its verdict after every report is appended to
    ``shadow_verdicts``."""
    clock = SimpleNamespace(now=EPOCH)
    monkeypatch.setattr(
        presence_service_module, "time", SimpleNamespace(time=lambda: clock.now)
    )
    svc = PresenceService()
    assert getattr(svc, "_placement_shadow", None) is None  # the flag is off by default
    if shadow_params is not None:
        svc._placement_shadow = PlacementShadow(
            shadow_params,
            rec.zones,
            CONFIG,
            # The recording holds no heartbeats; its note says all five were live.
            roster=lambda: {sat: (room_id, clock.now) for sat, (room_id, _n) in rec.receivers.items()},
        )
    svc._mac_to_user = {TRACKED_MAC: TRACKED_USER}
    svc._user_names = {TRACKED_USER: "walker"}
    svc._filter_enabled = bool(rec.settings["rssi_filter_enabled"])
    svc._rssi_threshold = int(rec.settings["rssi_floor_dbm"])
    svc._switch_enter_margin_db = float(rec.settings["switch_enter_margin_db"])
    svc._stale_timeout = float(rec.settings["stale_timeout_s"])
    svc._voice_hold_seconds = float(rec.settings["voice_hold_s"])
    svc._voice_release_margin_db = float(rec.settings["voice_release_margin_db"])

    async def drop_events():
        # The replay records decisions; hooks and the kiosk are not its subject.
        svc._pending_events.clear()

    monkeypatch.setattr(svc, "_fire_pending_events", drop_events)

    rooms: list[tuple[float, str | None]] = []
    for t, satellite_id, rssi in rec.sightings:
        clock.now = EPOCH + t
        room_id, room_name = rec.receivers[satellite_id]
        await svc.process_ble_report(
            satellite_id=satellite_id,
            room_id=room_id,
            devices=[{"mac": TRACKED_MAC, "rssi": rssi}],
            room_name=room_name,
        )
        current = svc.get_user_presence(TRACKED_USER)
        rooms.append((t, current.room_name if current else None))
        if shadow_verdicts is not None:
            shadow_verdicts.append((t, svc._placement_shadow._verdicts[TRACKED_MAC]))
    return rooms


def room_at(rooms: list[tuple[float, str | None]], t: float) -> str | None:
    times = [r[0] for r in rooms]
    index = bisect.bisect_right(times, t) - 1
    return rooms[index][1] if index >= 0 else None


def describe(rec: Recording, verdict: PlacementVerdict) -> str:
    if not verdict.is_placed:
        return verdict.kind.value
    text = f"placed:{rec.room_name[verdict.room]}"
    if verdict.zone is not None:
        text += f" [zone {verdict.zone}]"
    return text


def wrong_placements(rec: Recording, station: Station, verdicts: list[PlacementVerdict]) -> int:
    """Instants at which a room OUTSIDE the true zone is claimed."""
    allowed = rec.true_zone(station)
    return sum(1 for v in verdicts if v.is_placed and not (v.rooms <= allowed))


def print_table(title: str, header: tuple[str, ...], rows: list[tuple]) -> None:
    widths = [max(len(str(cell)) for cell in column) for column in zip(header, *rows, strict=True)]
    print(f"\n{title}")
    for row in (header, *rows):
        print("  " + "  ".join(str(cell).ljust(width) for cell, width in zip(row, widths, strict=True)))


# ---------------------------------------------------------------------------
# The recording itself
# ---------------------------------------------------------------------------


def test_the_recording_itself(recording):
    """What the file holds — and two properties of it that limit what any
    replay can show."""
    rec = recording
    assert len(rec.sightings) == 7020
    assert len(rec.receivers) == 5
    assert Counter(s.kind for s in rec.stations) == {"room": 8, "one_metre": 3, "unclear": 1}
    assert [s.true_room for s in rec.stations_of("room") if not s.has_live_receiver] == [
        "room-f", "room-g", "room-h",
    ]
    assert sorted(len(z) for z in rec.zones.values()) == [2]

    # Two kinds of receiver: two report everything, three cut at -80 dBm.
    weakest = {sat: min(r for _t, s, r in rec.sightings if s == sat) for sat in rec.receivers}
    assert weakest == {
        "sat-d": -99, "sat-a": -100,
        "sat-b": -80, "sat-e": -80, "sat-c": -80,
    }

    # (1) The receivers that cut at -80 mostly REPEAT a value: more than 40 %
    # of their readings sit in runs of five or more identical values, against
    # under 20 % on the two that report everything. "17 readings in a station"
    # are far fewer independent measurements there.
    def share_in_long_runs(satellite_id: str) -> float:
        series = [(t, r) for t, s, r in rec.sightings if s == satellite_id]
        runs, run = [], 1
        for (t0, r0), (t1, r1) in pairwise(series):
            if r1 == r0 and t1 - t0 < 4.0:
                run += 1
            else:
                runs.append(run)
                run = 1
        runs.append(run)
        return sum(n for n in runs if n >= 5) / len(series)

    for satellite_id in ("sat-b", "sat-e", "sat-c"):
        assert share_in_long_runs(satellite_id) > 0.40
    for satellite_id in ("sat-d", "sat-a"):
        assert share_in_long_runs(satellite_id) < 0.20

    # (2) Those receivers also fall silent with the phone in the room: gaps of
    # up to a minute between two STRONG readings (>= -65 dBm). Silence from
    # such a receiver is "at or below the cutoff, or deaf" — and the recording
    # shows the second case is common.
    def longest_gap_between_strong(satellite_id: str) -> float:
        series = [(t, r) for t, s, r in rec.sightings if s == satellite_id]
        return max(
            (t1 - t0 for (t0, r0), (t1, r1) in pairwise(series)
             if r0 >= -65 and r1 >= -65),
            default=0.0,
        )

    assert longest_gap_between_strong("sat-e") == pytest.approx(63.0, abs=0.5)
    assert longest_gap_between_strong("sat-c") == pytest.approx(57.0, abs=0.5)
    assert longest_gap_between_strong("sat-d") <= 3.5


def test_the_provisional_parameters(recording):
    """The numbers the rule yields from this recording. Pinned so that a
    changed rule is a visible change — not because they are right."""
    params = derive_params(recording)
    rounded = {
        sat: (p.offset_db, round(p.in_room_dbm, 1), p.cutoff_dbm) for sat, p in params.items()
    }
    assert rounded == {
        "sat-b": (0.0, -53.0, -80.0),
        "sat-e": (0.0, -55.0, -80.0),
        "sat-c": (0.0, -61.0, -80.0),
        "sat-d": (0.0, -47.4, -99.0),
        "sat-a": (0.0, -76.4, -100.0),
    }


# ---------------------------------------------------------------------------
# (a) The legacy decision — a regression record
# ---------------------------------------------------------------------------


# What the legacy decision says at the END of each room station. The three
# rooms without a receiver are each booked to a neighbour; the middle of the
# open-plan space goes to the other room of the zone.
LEGACY_AT_STATION_END = {
    "room-f": "room-e",
    "room-a": "room-e",
    "room-e": "room-e",
    "room-g": "room-b",
    "room-d": "room-d",
    "room-h": "room-b",
    "room-c": "room-c",
    "room-b": "room-b",
}


async def test_legacy_replay_is_the_regression_record(recording, monkeypatch):
    rec = recording
    replayed = await replay_legacy(rec, monkeypatch)
    live = [(t, room) for t, room in rec.legacy_changes]

    rows = []
    for station in rec.stations_of("room"):
        rows.append((
            station.true_room,
            "yes" if station.has_live_receiver else "NO",
            room_at(live, station.end),
            room_at(replayed, station.end),
        ))
    print_table(
        "Legacy decision at the end of each room station",
        ("true room", "receiver", "legacy (as decided live)", "legacy (replayed)"),
        rows,
    )

    at_end = {row[0]: row[3] for row in rows}
    assert at_end == LEGACY_AT_STATION_END
    # The replay reproduces what the live system decided at every station.
    assert {row[0]: row[2] for row in rows} == LEGACY_AT_STATION_END

    without_receiver = [s for s in rec.stations_of("room") if not s.has_live_receiver]
    assert all(at_end[s.true_room] != s.true_room for s in without_receiver)
    assert len(without_receiver) == 3
    with_receiver = [s for s in rec.stations_of("room") if s.has_live_receiver]
    right = [s.true_room for s in with_receiver if at_end[s.true_room] == s.true_room]
    assert right == ["room-e", "room-d", "room-c", "room-b"]
    # The legacy decision has no "none of these": it never ends a station
    # without a room.
    assert all(room is not None for room in at_end.values())


# ---------------------------------------------------------------------------
# (b) Placement v2 — the acceptance for stage 1
# ---------------------------------------------------------------------------


# The verdict at the END of every labelled segment, with parameters derived
# from the room stations.
V2_AT_SEGMENT_END = {
    "room-f (room)": "home_unknown",
    "room-a (room)": "placed:room-e [zone room-a+room-e]",
    "room-e (room)": "placed:room-e [zone room-a+room-e]",
    "room-g (room)": "home_unknown",
    "room-d (room)": "placed:room-d",
    "room-h (room)": "home_unknown",
    "room-c (room)": "placed:room-c",
    "room-b (room)": "placed:room-b",
    "room-a (one_metre)": "placed:room-a [zone room-a+room-e]",
    "room-e (one_metre)": "placed:room-e [zone room-a+room-e]",
    "room-d (unclear)": "home_unknown",
    "room-d (one_metre)": "home_unknown",
}


async def test_replay_per_station_table(recording, monkeypatch):
    """The table: true room, legacy verdict, new verdict — and the acceptance."""
    rec = recording
    legacy = await replay_legacy(rec, monkeypatch)
    timeline = replay_v2(rec, derive_params(rec))

    rows, at_end, wrong, lost = [], {}, {}, []
    for station in rec.stations:
        verdicts = timeline.during(station)
        final = timeline.at(station.end)
        c = final.components
        at_end[station.name] = describe(rec, final)
        wrong[station.name] = wrong_placements(rec, station, verdicts)
        scored = station.kind in SCORED_KINDS
        if scored and station.has_live_receiver and not final.is_placed:
            lost.append(station.name)
        kinds = Counter(v.kind.value for v in verdicts)
        rows.append((
            station.true_room,
            station.kind,
            "yes" if station.has_live_receiver else "NO",
            room_at(legacy, station.end),
            describe(rec, final),
            c.reason.value,
            "-" if c.bound_distance_db is None else f"{c.bound_distance_db:+.1f}",
            "-" if c.margin_db is None else f"{c.margin_db:.1f}",
            " ".join(f"{kind}={n}" for kind, n in sorted(kinds.items())),
            wrong[station.name] if scored else "(not scored)",
        ))
    print_table(
        "Per-station replay (parameters derived from the room stations)",
        ("true room", "kind", "receiver", "legacy", "v2 at end", "reason", "bound", "margin",
         "v2 verdicts during the segment", "wrong"),
        rows,
    )
    print(f"  price: {len(lost)} of "
          f"{sum(1 for s in rec.stations if s.kind in SCORED_KINDS and s.has_live_receiver)} "
          f"scored segments in a room with a receiver end without a placement: {lost}")
    print(f"  verdict changes over the walk: v2 {timeline.changes()}, "
          f"legacy (as decided live) {len(rec.legacy_changes)}")

    assert at_end == V2_AT_SEGMENT_END

    scored = [s for s in rec.stations if s.kind in SCORED_KINDS]
    # Acceptance 1: NO wrong room — at no evaluated instant of any scored
    # segment is a room outside the true zone claimed.
    assert {s.name: wrong[s.name] for s in scored} == {s.name: 0 for s in scored}
    # Acceptance 2: every station in a room without a receiver ends home_unknown
    # (the legacy decision booked a neighbour each time)...
    without_receiver = [s for s in scored if not s.has_live_receiver]
    assert len(without_receiver) == 3
    for station in without_receiver:
        assert timeline.at(station.end).kind is VerdictKind.HOME_UNKNOWN
        # ...and not only at the end: never placed anywhere during it.
        assert {v.kind for v in timeline.during(station)} == {VerdictKind.HOME_UNKNOWN}
        assert room_at(legacy, station.end) is not None
    # The price: correct placements given up for home_unknown.
    assert lost == ["room-d (one_metre)"]
    # The unscored segment stays in the file and in the table.
    assert "room-d (unclear)" in at_end


def test_one_metre_segments_are_held_out_of_the_main_derivation(recording):
    """The bounds come from the ``room`` stations only, so the three 1 m
    segments are data the parameters have never seen. Two are placed in the
    right zone, none in a wrong room — and room-d's is LOST: its receiver
    reads -66 dBm at 1 m but -46 dBm at the room station, so a bound taken at
    one position rejects another position in the same room."""
    rec = recording
    timeline = replay_v2(rec, derive_params(rec, kinds=("room",)))
    result = {}
    for station in rec.stations_of("one_metre"):
        assert wrong_placements(rec, station, timeline.during(station)) == 0
        result[station.true_room] = timeline.at(station.end).kind
    assert result == {
        "room-a": VerdictKind.PLACED,
        "room-e": VerdictKind.PLACED,
        "room-d": VerdictKind.HOME_UNKNOWN,
    }
    room_d = next(s for s in rec.stations_of("one_metre") if s.true_room == "room-d")
    final = timeline.at(room_d.end)
    assert final.components.reason is Reason.OUTSIDE_BOUND
    assert final.components.bound_distance_db < -15.0


def test_held_out_derive_from_one_metre_score_on_room_stations(recording):
    """The other direction: bounds from the three 1 m segments only, scored on
    the eight room stations. Two receivers have no 1 m segment and stay
    uncalibrated — their rooms can then never be placed."""
    rec = recording
    params = derive_params(rec, kinds=("one_metre",))
    assert sorted(params) == ["sat-a", "sat-d", "sat-e"]
    timeline = replay_v2(rec, params)

    rows, result = [], {}
    for station in rec.stations_of("room"):
        verdicts = timeline.during(station)
        assert wrong_placements(rec, station, verdicts) == 0, station.name
        final = timeline.at(station.end)
        result[station.true_room] = describe(rec, final)
        rows.append((station.true_room, "yes" if station.has_live_receiver else "NO",
                     describe(rec, final), final.components.reason.value))
    print_table(
        "Held out: bounds from the 1 m segments, scored on the room stations",
        ("true room", "receiver", "v2 at end", "reason"),
        rows,
    )
    assert result == {
        "room-f": "home_unknown",
        "room-a": "placed:room-e [zone room-a+room-e]",
        "room-e": "placed:room-e [zone room-a+room-e]",
        "room-g": "home_unknown",
        "room-d": "placed:room-d",
        "room-h": "home_unknown",
        "room-c": "home_unknown",      # receiver uncalibrated: the price
        "room-b": "home_unknown",    # receiver uncalibrated: the price
    }
    room_b = next(s for s in rec.stations_of("room") if s.true_room == "room-b")
    assert timeline.at(room_b.end).components.reason is Reason.NO_CALIBRATED_RECEIVER


def test_held_out_first_half_of_each_station_scores_the_second_half(recording):
    """Bounds from the first 25 s of each room station, scored on the last
    25 s. The weakest of the three checks: the two halves are adjacent in time
    with the phone lying in the same place."""
    rec = recording
    timeline = replay_v2(rec, derive_params(rec, kinds=("room",), portion=(0.0, 0.5)))
    lost = []
    for station in rec.stations_of("room"):
        second_half = timeline.during(station, portion=(0.5, 1.0))
        assert wrong_placements(rec, station, second_half) == 0, station.name
        final = timeline.at(station.end)
        if not station.has_live_receiver:
            assert final.kind is VerdictKind.HOME_UNKNOWN, station.name
        elif not final.is_placed:
            lost.append(station.true_room)
    assert lost == []


# ---------------------------------------------------------------------------
# Negative controls
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("silent_is_offline", [False, True], ids=["deaf", "offline"])
def test_negative_control_a_removed_receiver_never_hands_its_room_to_a_neighbour(
    recording, silent_is_offline
):
    """Take one receiver's stream away entirely — as a deaf receiver (still
    connected) and as an offline one. Its room has no receiver any more, so the
    station there must end ``home_unknown`` and must never be booked to a
    neighbour."""
    rec = recording
    params = derive_params(rec)
    rows = []
    for satellite_id, (_room_id, room_name) in rec.receivers.items():
        timeline = replay_v2(
            rec, params, silent=satellite_id, silent_is_offline=silent_is_offline
        )
        for station in rec.stations_of(*SCORED_KINDS):
            if station.true_room != room_name:
                continue
            verdicts = timeline.during(station)
            final = timeline.at(station.end)
            rows.append((satellite_id, station.name, describe(rec, final),
                         final.components.reason.value))
            assert wrong_placements(rec, station, verdicts) == 0, station.name
            if len(rec.true_zone(station)) == 1:
                # A room of its own: nothing may place the device at all.
                assert {v.kind for v in verdicts} == {VerdictKind.HOME_UNKNOWN}, station.name
            else:
                # Open-plan: the other receiver of the zone may still vouch for
                # the ZONE, never for a room outside it (checked above).
                assert final.kind in (VerdictKind.HOME_UNKNOWN, VerdictKind.PLACED)
    print_table(
        f"Negative control: one receiver removed ({'offline' if silent_is_offline else 'deaf'})",
        ("removed receiver", "segment in its room", "v2 at end", "reason"),
        rows,
    )
    # The controls ran for every receiver that has a station in its room.
    assert {row[0] for row in rows} == set(rec.receivers)


def test_negative_control_removed_room_b_receiver_is_a_coverage_hole(recording):
    """Stated plainly: in room-b NO other receiver hears the phone at
    all. Without its own receiver the station ends ``home_unknown`` only
    because it is shorter than the deadline; past the deadline the verdict is
    ``absent`` for a person who is at home. No decision rule can fix a room
    nobody hears."""
    rec = recording
    station = next(s for s in rec.stations_of("room") if s.true_room == "room-b")
    past_deadline = station.end + CONFIG.absent_after_s
    timeline = replay_v2(
        rec, derive_params(rec), silent="sat-b",
        until=station.end, extra_instants=(past_deadline,),
    )
    final = timeline.at(station.end)
    assert final.kind is VerdictKind.HOME_UNKNOWN
    assert final.components.reason is Reason.NO_FRESH_READING
    assert timeline.at(past_deadline).kind is VerdictKind.ABSENT


def test_negative_control_away_is_absent_only_while_a_receiver_is_alive(recording):
    """An "away" segment: the recording is cut off after the room-d station —
    from then on nobody hears the phone. (The walk itself never leaves the
    house; its longest silence is 37 s.)

    Receivers alive → ``home_unknown`` until the deadline, then ``absent``.
    No receiver alive → ``unavailable``, never ``absent``: silence from
    receivers that are not listening is not evidence."""
    rec = recording
    params = derive_params(rec)
    station = next(s for s in rec.stations_of("room") if s.true_room == "room-d")
    last = max(t for t, _sat, _rssi in rec.sightings if t <= station.end)
    before = last + CONFIG.absent_after_s - 1.0
    after = last + CONFIG.absent_after_s + 1.0
    much_later = last + 3600.0

    alive = replay_v2(rec, params, until=station.end,
                      extra_instants=(before, after, much_later))
    assert alive.at(station.end).kind is VerdictKind.PLACED
    assert alive.at(before).kind is VerdictKind.HOME_UNKNOWN
    assert alive.at(before).components.reason is Reason.NO_FRESH_READING
    assert alive.at(after).kind is VerdictKind.ABSENT
    assert alive.at(much_later).kind is VerdictKind.ABSENT
    assert set(alive.at(after).components.censored) == set(rec.receivers)

    dead = replay_v2(rec, params, until=station.end, receivers_alive=False,
                     extra_instants=(before, after, much_later))
    assert dead.at(after).kind is VerdictKind.UNAVAILABLE
    assert dead.at(much_later).kind is VerdictKind.UNAVAILABLE
    assert dead.at(after).components.reason is Reason.NO_RECEIVER_ALIVE
    assert VerdictKind.ABSENT not in {v.kind for _t, v in dead.points}


def test_without_parameters_nobody_is_ever_placed(recording):
    """The shipped default: no receiver calibrated. Over the whole walk the
    phone is home — and never in a room."""
    timeline = replay_v2(recording, {})
    assert {v.kind for _t, v in timeline.points} == {VerdictKind.HOME_UNKNOWN}


# ---------------------------------------------------------------------------
# The shadow on the real service
# ---------------------------------------------------------------------------


async def test_shadow_beside_the_real_service_changes_nothing_and_decides_the_same(
    recording, monkeypatch
):
    """The whole walk through ``PresenceService.process_ble_report`` twice:
    without the shadow and with it.

      * the legacy decision is IDENTICAL after every one of the 7020 reports;
      * the shadow, reading the service's own sightings, reaches the same
        verdict as the pure replay after every report;
      * it logs when the two decisions start and stop disagreeing — a few
        dozen lines for two hours, not one per report.
    """
    rec = recording
    params = derive_params(rec)

    lines: list[str] = []
    sink = logger.add(lambda m: lines.append(str(m).rstrip()), level="DEBUG", format="{message}")
    try:
        legacy_alone = await replay_legacy(rec, monkeypatch)
        lines_without = list(lines)
        lines.clear()
        shadow_verdicts: list[tuple[float, PlacementVerdict]] = []
        legacy_beside = await replay_legacy(
            rec, monkeypatch, shadow_params=params, shadow_verdicts=shadow_verdicts
        )
        lines_with = list(lines)
    finally:
        logger.remove(sink)

    assert legacy_beside == legacy_alone
    assert len(legacy_alone) == len(rec.sightings)

    # Without the shadow not one line of it; with it, the legacy lines are
    # exactly the same and the shadow's lines come on top.
    assert not [line for line in lines_without if LOG_PREFIX in line]
    shadow_lines = [line for line in lines_with if LOG_PREFIX in line]
    assert [line for line in lines_with if LOG_PREFIX not in line] == lines_without

    # The pure decision, fed report by report in the same order.
    window: dict[str, list[Reading]] = defaultdict(list)
    previous: PlacementVerdict | None = None
    mismatches = []
    for (t, satellite_id, rssi), (_t, shadow_verdict) in zip(
        rec.sightings, shadow_verdicts, strict=True
    ):
        window[satellite_id].append(Reading(at=t, rssi=float(rssi)))
        for sat in window:
            window[sat] = [r for r in window[sat] if t - r.at < CONFIG.absent_after_s]
        receivers = {
            sat: Receiver(room=room_id, params=params.get(sat), alive_at=t)
            for sat, (room_id, _name) in rec.receivers.items()
        }
        previous = decide(window, receivers, rec.zones, previous, t, CONFIG)
        if (shadow_verdict.kind, shadow_verdict.rooms) != (previous.kind, previous.rooms):
            mismatches.append((t, shadow_verdict.kind.value, previous.kind.value))
    assert mismatches == []

    starts = [line for line in shadow_lines if "disagreement START" in line]
    stops = [line for line in shadow_lines if "disagreement STOP" in line]
    print(f"\nShadow log over the walk: {len(starts)} START, {len(stops)} STOP "
          f"({len(rec.sightings)} reports)")
    for line in shadow_lines[:6]:
        print("  " + line)
    assert len(shadow_lines) == len(starts) + len(stops)
    assert len(starts) - len(stops) in (0, 1)
    assert 0 < len(starts) < len(rec.sightings) / 50
    # Rooms and numbers only.
    for line in shadow_lines:
        assert TRACKED_MAC not in line
        assert "walker" not in line
        assert "sat-" not in line
