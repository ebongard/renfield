"""
Presence placement v2 — a pure room decision with a reject option.

``decide()`` answers, for ONE tracked device at ONE moment: is the device in a
room a receiver can vouch for, at home but in no room any receiver vouches for,
or gone? It has no I/O and no module state, and the clock is an argument, so
the same inputs always give the same verdict and a recording can be replayed
through it.

What it does differently from the legacy decision in ``presence_service.py``:

  * **Reject option.** The legacy logic books the best of whatever was heard.
    Here a room is claimed only when its own receiver hears the device inside
    that receiver's in-room bound AND clearly ahead of every other receiver.
    Otherwise the answer is ``home_unknown``.
  * **Censoring.** A receiver that reports nothing is not "far": it hears the
    device below its reporting cutoff, or it does not hear at all. It counts as
    a runner-up at its cutoff — the strongest value it could be hiding.
  * **Time-based smoothing per receiver.** The smoothed value depends on the
    readings and their timestamps only, never on how many reports arrived or
    how many receivers sent them.
  * **Deadlines, not sweeps.** "Not heard for longer than the deadline" is
    evaluated at ``now``, whenever the caller asks.
  * **Zones.** Rooms that form one open-plan space are decided as one unit; the
    nearer room is a hint only.

Stage 1 runs this in shadow mode beside the legacy decision
(``presence_placement_shadow.py``). Nothing acts on its verdicts yet. Design:
``docs/design/ble-presence-improvement.md`` → "Placement v2".
"""

from __future__ import annotations

import json
import math
from collections.abc import Collection, Hashable, Mapping, Sequence
from dataclasses import dataclass
from enum import Enum

RoomKey = Hashable


def _is_finite(value: object) -> bool:
    """A real, finite number. NaN, infinities, booleans, non-numbers and
    integers too large for a float are not: every comparison against NaN is
    False, so an unchecked NaN would pass any ``if x < bound: reject`` gate."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def _at_least(value: object, bound: float) -> bool:
    """``value >= bound``, proven: False for anything that is not a finite
    number. The gates reject on False, so the unknown never places."""
    return _is_finite(value) and value >= bound


class VerdictKind(str, Enum):
    """The four answers of the placement decision."""

    # A receiver vouches for the device: inside its in-room bound and ahead of
    # every receiver outside the zone by the margin.
    PLACED = "placed"
    # Heard within the deadline, but no receiver places it.
    HOME_UNKNOWN = "home_unknown"
    # Not heard for longer than the deadline while at least one receiver is alive.
    ABSENT = "absent"
    # Not heard and no receiver is alive: nothing can be said, least of all "absent".
    UNAVAILABLE = "unavailable"


class ReceiverState(str, Enum):
    HEARD = "heard"        # a reading younger than ``fresh_s``
    CENSORED = "censored"  # alive but silent: at or below its cutoff, or deaf
    OFFLINE = "offline"    # no sign of life: says nothing either way


class Reason(str, Enum):
    """Why the verdict is what it is (machine-readable, stable)."""

    IN_BOUND_AND_MARGIN = "in_bound_and_margin"
    INCUMBENT_HELD = "incumbent_held"
    NO_CALIBRATED_RECEIVER = "no_calibrated_receiver"
    OUTSIDE_BOUND = "outside_bound"
    MARGIN_TOO_SMALL = "margin_too_small"
    NO_FRESH_READING = "no_fresh_reading"
    NOT_HEARD = "not_heard"
    NO_RECEIVER_ALIVE = "no_receiver_alive"


@dataclass(frozen=True)
class Reading:
    """One RSSI reading of the device at one receiver."""

    at: float    # epoch seconds
    rssi: float  # dBm


@dataclass(frozen=True)
class ReceiverParams:
    """What is known about one receiver. Measured per receiver, never shared:
    the receivers differ by more than the margin at the same distance.

    ``offset_db`` is ADDED to this receiver's smoothed reading before it is
    compared with other receivers (a receiver that reads 6 dB weak gets +6).
    ``in_room_dbm`` is the weakest smoothed RAW reading of this receiver that
    still means "in this receiver's room" — on the receiver's own scale, so
    changing the offset never moves the bound. ``cutoff_dbm`` is the level
    below which the satellite sends nothing.
    """

    offset_db: float
    in_room_dbm: float
    cutoff_dbm: float

    def __post_init__(self) -> None:
        for name in ("offset_db", "in_room_dbm", "cutoff_dbm"):
            if not _is_finite(getattr(self, name)):
                raise ValueError(f"{name} must be a finite number")


@dataclass(frozen=True)
class Receiver:
    """A receiver as the decision sees it."""

    room: RoomKey | None
    # None = uncalibrated: may keep a device "home", can never place it.
    params: ReceiverParams | None = None
    # Latest evidence that the receiver is running (a heartbeat). A reading is
    # evidence of its own; None = nothing known beyond the readings.
    alive_at: float | None = None


@dataclass(frozen=True)
class PlacementConfig:
    """Numbers of the decision itself. The defaults are choices, not
    measurements; the per-receiver numbers have no defaults at all."""

    # Weight of a reading falls to 1/e after this many seconds.
    time_constant_s: float = 6.0
    # A receiver counts as hearing the device only with a reading this young.
    fresh_s: float = 20.0
    # dB the claiming zone must lead every receiver outside it.
    margin_db: float = 10.0
    # dB by which the bound and the margin are relaxed for the incumbent zone.
    hysteresis_db: float = 3.0
    # The deadline: not heard anywhere for longer than this = absent/unavailable.
    absent_after_s: float = 120.0
    # A receiver without a sign of life for longer than this is offline.
    receiver_alive_s: float = 60.0

    def __post_init__(self) -> None:
        for name in (
            "time_constant_s", "fresh_s", "margin_db", "hysteresis_db",
            "absent_after_s", "receiver_alive_s",
        ):
            # NaN first: ``nan < 0`` is False, so a NaN margin would pass the
            # range checks below and then never reject anything.
            if not _is_finite(getattr(self, name)):
                raise ValueError(f"{name} must be a finite number")
        for name in ("time_constant_s", "fresh_s", "absent_after_s", "receiver_alive_s"):
            if not getattr(self, name) > 0:
                raise ValueError(f"{name} must be positive")
        if self.margin_db < 0 or self.hysteresis_db < 0:
            raise ValueError("margin_db and hysteresis_db must not be negative")
        if self.hysteresis_db > self.margin_db:
            # Otherwise the incumbent could hold a zone while trailing another.
            raise ValueError("hysteresis_db must not exceed margin_db")
        if self.fresh_s > self.absent_after_s:
            raise ValueError("fresh_s must not exceed absent_after_s")


@dataclass(frozen=True)
class ReceiverEvidence:
    """One receiver's contribution to a verdict."""

    receiver_id: str
    room: RoomKey | None
    state: ReceiverState
    calibrated: bool
    # Time-weighted mean of the raw readings; None unless HEARD.
    smoothed_dbm: float | None
    # What the margin compares: smoothed + offset when HEARD; cutoff + offset
    # (an UPPER bound) when CENSORED and calibrated; else None.
    comparable_dbm: float | None
    # smoothed - in_room_dbm; >= 0 means inside the bound. None unless HEARD
    # and calibrated.
    bound_distance_db: float | None
    samples: int          # readings no older than ``fresh_s``
    age_s: float | None   # age of the newest reading inside the deadline


@dataclass(frozen=True)
class PlacementComponents:
    """The numbers that justify a verdict."""

    reason: Reason
    # The strongest claimant: for PLACED the receiver that vouches (strongest
    # comparable value in the zone); otherwise the strongest heard receiver.
    best_receiver: str | None
    best_dbm: float | None
    # The strongest receiver outside the best one's zone. ``runner_up_censored``
    # = that value is a cutoff, i.e. the most a silent receiver could hide.
    runner_up_receiver: str | None
    runner_up_dbm: float | None
    runner_up_censored: bool
    margin_db: float | None          # best - runner-up; None without a runner-up
    bound_distance_db: float | None  # of the receiver inside its bound, else of the best
    samples: int                     # readings behind ``best_receiver``
    age_s: float | None              # age of the newest reading anywhere
    censored: tuple[str, ...]        # alive but silent receivers
    offline: tuple[str, ...]         # receivers without a sign of life
    receivers: tuple[ReceiverEvidence, ...]


@dataclass(frozen=True)
class PlacementVerdict:
    """The typed answer.

    ``rooms`` is what a gate must use: every room of the zone the device is
    placed in (one room, or all rooms of an open-plan zone). ``room`` is only
    the nearer room inside that zone — a hint for a display. Both are empty
    unless ``kind`` is PLACED.
    """

    kind: VerdictKind
    components: PlacementComponents
    room: RoomKey | None = None
    zone: str | None = None  # name of the open-plan zone; None for a single room
    rooms: frozenset = frozenset()

    def __post_init__(self) -> None:
        if self.kind is VerdictKind.PLACED:
            if self.room is None or self.room not in self.rooms:
                raise ValueError("a placed verdict needs a room inside its zone")
        elif self.room is not None or self.zone is not None or self.rooms:
            raise ValueError(f"a {self.kind.value} verdict carries no room")

    @property
    def is_placed(self) -> bool:
        return self.kind is VerdictKind.PLACED

    def covers(self, room: RoomKey | None) -> bool:
        """Whether ``room`` lies in the zone this verdict places the device in."""
        return room is not None and room in self.rooms


# ---------------------------------------------------------------------------
# Smoothing
# ---------------------------------------------------------------------------


# The levels a reading may carry at all. A choice, and a wide one: the range
# the Bluetooth HCI defines for an RSSI value (-127..+20 dBm). It is not a
# statement about any room — only about what cannot be a measurement.
RSSI_MIN_DBM = -127.0
RSSI_MAX_DBM = 20.0


def _usable(reading: Reading) -> bool:
    """A reading the decision may use at all. The satellite's device entries
    reach the presence service unvalidated: JSON carries NaN and Infinity, and
    a finite absurdity does the same damage (a single +35 dBm would place a
    device; two readings of 1e308 sum to infinity). A reading that is not a
    finite level inside the plausible range is no reading — it neither places
    nor counts as heard."""
    return (
        _is_finite(reading.at)
        and _is_finite(reading.rssi)
        and RSSI_MIN_DBM <= reading.rssi <= RSSI_MAX_DBM
    )


def smooth(readings: Sequence[Reading], now: float, time_constant_s: float) -> float | None:
    """Time-weighted mean of ``readings`` as of ``now``:

        value = sum(rssi_i * w_i) / sum(w_i),   w_i = exp(-(now - at_i) / tau)

    A function of the (timestamp, value) pairs alone: it does not matter how
    many reports delivered them, in which order, or what other receivers sent
    meanwhile. Two readings with the same timestamp are one reading (the later
    one in the sequence wins), so a repeated delivery changes nothing.
    Readings after ``now`` are ignored, and so is a reading whose timestamp is
    not a finite number or whose level is not a finite number inside
    ``RSSI_MIN_DBM..RSSI_MAX_DBM``: one NaN would turn the whole mean into
    NaN. None when nothing is left.
    """
    by_time: dict[float, float] = {}
    for reading in readings:
        if _usable(reading) and reading.at <= now:
            by_time[reading.at] = reading.rssi
    if not by_time:
        return None
    total = 0.0
    weight_sum = 0.0
    for at, rssi in by_time.items():
        weight = math.exp(-(now - at) / time_constant_s)
        total += rssi * weight
        weight_sum += weight
    if weight_sum == 0.0:
        # Every weight underflowed: only readings far older than the time
        # constant are left. The newest one is the best remaining estimate.
        return by_time[max(by_time)]
    return total / weight_sum


# ---------------------------------------------------------------------------
# Decision
# ---------------------------------------------------------------------------


def _zone_index(zones: Mapping[str, Collection[RoomKey]]) -> dict[RoomKey, str]:
    index: dict[RoomKey, str] = {}
    for name, rooms in zones.items():
        for room in rooms:
            if room in index and index[room] != name:
                raise ValueError(f"room {room!r} is in two zones: {index[room]!r} and {name!r}")
            index[room] = name
    return index


def _evidence(
    receiver_id: str,
    receiver: Receiver,
    readings: Sequence[Reading],
    now: float,
    config: PlacementConfig,
) -> ReceiverEvidence:
    window = [
        r for r in readings
        if _usable(r) and 0.0 <= now - r.at <= config.absent_after_s
    ]
    newest = max((r.at for r in window), default=None)
    age = None if newest is None else now - newest
    samples = len({r.at for r in window if now - r.at <= config.fresh_s})
    params = receiver.params

    if age is not None and age <= config.fresh_s:
        smoothed = smooth(window, now, config.time_constant_s)
        assert smoothed is not None
        return ReceiverEvidence(
            receiver_id=receiver_id,
            room=receiver.room,
            state=ReceiverState.HEARD,
            calibrated=params is not None,
            smoothed_dbm=smoothed,
            # An uncalibrated receiver is compared at face value: it can stand
            # in the way of a placement elsewhere, it cannot make one.
            comparable_dbm=smoothed + (params.offset_db if params else 0.0),
            bound_distance_db=(smoothed - params.in_room_dbm) if params else None,
            samples=samples,
            age_s=age,
        )

    signs_of_life = [t for t in (receiver.alive_at, newest) if t is not None and t <= now]
    alive = bool(signs_of_life) and now - max(signs_of_life) <= config.receiver_alive_s
    if alive:
        # Silent, not far: all that is known is "at or below the cutoff, or
        # deaf". For the margin it stands at the cutoff — the strongest value
        # it could be hiding. Without a known cutoff nothing can be said.
        return ReceiverEvidence(
            receiver_id=receiver_id,
            room=receiver.room,
            state=ReceiverState.CENSORED,
            calibrated=params is not None,
            smoothed_dbm=None,
            comparable_dbm=(params.cutoff_dbm + params.offset_db) if params else None,
            bound_distance_db=None,
            samples=samples,
            age_s=age,
        )
    return ReceiverEvidence(
        receiver_id=receiver_id,
        room=receiver.room,
        state=ReceiverState.OFFLINE,
        calibrated=params is not None,
        smoothed_dbm=None,
        comparable_dbm=None,
        bound_distance_db=None,
        samples=samples,
        age_s=age,
    )


@dataclass(frozen=True)
class _ZoneCandidate:
    rooms: frozenset
    name: str | None
    best: ReceiverEvidence            # strongest calibrated, heard member
    most_inside: ReceiverEvidence     # member deepest inside its own bound
    runner_up: ReceiverEvidence | None

    @property
    def margin(self) -> float | None:
        if self.runner_up is None:
            return None
        return self.best.comparable_dbm - self.runner_up.comparable_dbm

    def claims(self, config: PlacementConfig, relax_db: float) -> Reason | None:
        """None when the zone may be claimed, else the reason it may not.

        Both gates are written as "unless proven inside": a value that is not
        a finite number rejects, it never places. NaN compares False either
        way; an infinite distance or margin is no measurement either (it can
        only come from a level that is none), however large it looks.
        """
        if not _at_least(self.most_inside.bound_distance_db, -relax_db):
            return Reason.OUTSIDE_BOUND
        margin = self.margin
        if margin is not None and not _at_least(margin, config.margin_db - relax_db):
            return Reason.MARGIN_TOO_SMALL
        return None


def decide(
    readings: Mapping[str, Sequence[Reading]],
    receivers: Mapping[str, Receiver],
    zones: Mapping[str, Collection[RoomKey]],
    previous: PlacementVerdict | None,
    now: float,
    config: PlacementConfig | None = None,
) -> PlacementVerdict:
    """Decide where ONE device is at ``now``.

    ``readings``: receiver id → that receiver's readings of the device (any
    order; older than the deadline or newer than ``now`` are ignored).
    ``receivers``: every receiver that exists, heard or not — a receiver that
    only appears in ``readings`` is treated as uncalibrated and roomless.
    ``zones``: zone name → rooms forming one open-plan space; a room in no zone
    is its own. ``previous``: the last verdict for this device (hysteresis).

    ``now`` must not run backwards between calls for one device: readings
    newer than ``now`` are ignored, so an earlier ``now`` sees an emptier
    window and drops the incumbent. The caller owns the clock.

    An uncalibrated receiver INSIDE a zone neither helps nor hinders that
    zone: it cannot vouch, and it does not compete against its own zone. The
    zone is then decided by its calibrated members alone, and the hint names
    the strongest calibrated member's room — even if the uncalibrated one
    hears the device louder.
    """
    config = config or PlacementConfig()
    zone_of = _zone_index(zones)

    known = dict(receivers)
    for receiver_id in readings:
        known.setdefault(receiver_id, Receiver(room=None))
    evidence = tuple(
        _evidence(receiver_id, known[receiver_id], readings.get(receiver_id, ()), now, config)
        for receiver_id in sorted(known)
    )
    heard = [e for e in evidence if e.state is ReceiverState.HEARD]
    censored = tuple(e.receiver_id for e in evidence if e.state is ReceiverState.CENSORED)
    offline = tuple(e.receiver_id for e in evidence if e.state is ReceiverState.OFFLINE)
    ages = [e.age_s for e in evidence if e.age_s is not None]
    newest_age = min(ages) if ages else None

    def components(
        reason: Reason,
        best: ReceiverEvidence | None = None,
        runner_up: ReceiverEvidence | None = None,
        bound_of: ReceiverEvidence | None = None,
    ) -> PlacementComponents:
        margin = None
        if best is not None and runner_up is not None:
            margin = best.comparable_dbm - runner_up.comparable_dbm
        bound_source = bound_of or best
        return PlacementComponents(
            reason=reason,
            best_receiver=best.receiver_id if best else None,
            best_dbm=best.comparable_dbm if best else None,
            runner_up_receiver=runner_up.receiver_id if runner_up else None,
            runner_up_dbm=runner_up.comparable_dbm if runner_up else None,
            runner_up_censored=bool(runner_up and runner_up.state is ReceiverState.CENSORED),
            margin_db=margin,
            bound_distance_db=bound_source.bound_distance_db if bound_source else None,
            samples=best.samples if best else 0,
            age_s=newest_age,
            censored=censored,
            offline=offline,
            receivers=evidence,
        )

    # --- Deadline: not heard anywhere ------------------------------------
    if newest_age is None:
        if censored:
            return PlacementVerdict(VerdictKind.ABSENT, components(Reason.NOT_HEARD))
        # Nobody is listening. Silence from no receiver is not evidence of absence.
        return PlacementVerdict(VerdictKind.UNAVAILABLE, components(Reason.NO_RECEIVER_ALIVE))

    # --- Heard within the deadline, but by nobody right now ---------------
    if not heard:
        return PlacementVerdict(VerdictKind.HOME_UNKNOWN, components(Reason.NO_FRESH_READING))

    def rooms_of(room: RoomKey) -> tuple[frozenset, str | None]:
        name = zone_of.get(room)
        if name is None:
            return frozenset({room}), None
        return frozenset(zones[name]), name

    def runner_up_for(rooms: frozenset, best: ReceiverEvidence) -> ReceiverEvidence | None:
        """The strongest receiver outside ``rooms`` (and other than ``best``):
        heard ones at their value, censored ones at their cutoff. Offline or
        unknown-cutoff receivers have no value to compare."""
        outside = [
            e for e in evidence
            if e is not best
            and e.comparable_dbm is not None
            and (e.room is None or e.room not in rooms)
        ]
        return max(outside, key=lambda e: e.comparable_dbm, default=None)

    # --- Zones a calibrated receiver could claim ---------------------------
    candidates: dict[frozenset, _ZoneCandidate] = {}
    for e in heard:
        if not e.calibrated or e.room is None:
            continue
        rooms, name = rooms_of(e.room)
        if rooms in candidates:
            continue
        members = [
            m for m in heard
            if m.calibrated and m.room is not None and m.room in rooms
        ]
        best = max(members, key=lambda m: m.comparable_dbm)
        candidates[rooms] = _ZoneCandidate(
            rooms=rooms,
            name=name,
            best=best,
            most_inside=max(members, key=lambda m: m.bound_distance_db),
            runner_up=runner_up_for(rooms, best),
        )

    strongest = max(heard, key=lambda e: e.comparable_dbm)
    if not candidates:
        rooms = rooms_of(strongest.room)[0] if strongest.room is not None else frozenset()
        return PlacementVerdict(
            VerdictKind.HOME_UNKNOWN,
            components(Reason.NO_CALIBRATED_RECEIVER, strongest, runner_up_for(rooms, strongest)),
        )

    def placed(candidate: _ZoneCandidate, reason: Reason) -> PlacementVerdict:
        return PlacementVerdict(
            kind=VerdictKind.PLACED,
            components=components(reason, candidate.best, candidate.runner_up, candidate.most_inside),
            room=candidate.best.room,
            zone=candidate.name,
            rooms=candidate.rooms,
        )

    # --- Incumbent first: relaxed bound and margin -------------------------
    if previous is not None and previous.is_placed:
        incumbent = candidates.get(previous.rooms)
        if incumbent is not None and incumbent.claims(config, config.hysteresis_db) is None:
            entered = incumbent.claims(config, 0.0) is None
            return placed(
                incumbent,
                Reason.IN_BOUND_AND_MARGIN if entered else Reason.INCUMBENT_HELD,
            )

    # --- Otherwise the strongest zone that meets the full bound and margin ---
    claimable = [c for c in candidates.values() if c.claims(config, 0.0) is None]
    if claimable:
        return placed(
            max(claimable, key=lambda c: c.best.comparable_dbm),
            Reason.IN_BOUND_AND_MARGIN,
        )

    # Nobody places it. Report the zone that came closest: one inside its
    # bound whose margin failed, else the strongest.
    inside = [
        c for c in candidates.values()
        if c.claims(config, 0.0) is Reason.MARGIN_TOO_SMALL
    ]
    closest = max(inside or candidates.values(), key=lambda c: c.best.comparable_dbm)
    return PlacementVerdict(
        VerdictKind.HOME_UNKNOWN,
        components(
            closest.claims(config, 0.0), closest.best, closest.runner_up, closest.most_inside
        ),
    )


# ---------------------------------------------------------------------------
# Configuration (JSON settings; a later stage moves this into a table)
# ---------------------------------------------------------------------------


class PlacementConfigError(ValueError):
    """The receiver or zone settings are not usable."""


_RECEIVER_FIELDS = ("offset_db", "in_room_dbm", "cutoff_dbm")


def _number(value: object, where: str) -> float:
    # bool is an int in Python; "true" is never a level. An integer too large
    # for a float (JSON has no upper limit) is not a level either — and
    # ``math.isfinite`` raises OverflowError on it instead of answering.
    if not _is_finite(value):
        raise PlacementConfigError(f"{where} must be a finite number")
    return float(value)


def parse_receiver_params(raw: str) -> dict[str, ReceiverParams]:
    """``{"<satellite_id>": {"offset_db": 0, "in_room_dbm": -60, "cutoff_dbm": -80}}``.

    Empty = no receiver is calibrated. All three fields are required for a
    listed receiver: a half-described receiver must not place anyone.
    """
    if not raw or not raw.strip():
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise PlacementConfigError(f"receiver parameters are not valid JSON: {e}") from e
    if not isinstance(data, dict):
        raise PlacementConfigError("receiver parameters must be a JSON object keyed by satellite id")
    out: dict[str, ReceiverParams] = {}
    for satellite_id, entry in data.items():
        if not satellite_id.strip():
            raise PlacementConfigError("a receiver entry has an empty satellite id")
        if not isinstance(entry, dict):
            raise PlacementConfigError(f"receiver {satellite_id!r} must be a JSON object")
        unknown = sorted(set(entry) - set(_RECEIVER_FIELDS))
        missing = [f for f in _RECEIVER_FIELDS if f not in entry]
        if unknown or missing:
            raise PlacementConfigError(
                f"receiver {satellite_id!r}: missing {missing}, unknown {unknown} "
                f"(expected exactly {list(_RECEIVER_FIELDS)})"
            )
        values = {f: _number(entry[f], f"receiver {satellite_id!r}.{f}") for f in _RECEIVER_FIELDS}
        if values["in_room_dbm"] <= values["cutoff_dbm"]:
            # Below the cutoff nothing is reported, so such a bound is never
            # reached by a reading — and would be met by silence if misread.
            raise PlacementConfigError(
                f"receiver {satellite_id!r}: in_room_dbm must be above cutoff_dbm"
            )
        out[satellite_id] = ReceiverParams(**values)
    return out


def parse_zones(raw: str) -> dict[str, frozenset[int]]:
    """``{"<zone name>": [<room_id>, <room_id>, ...]}`` — rooms that form one
    open-plan space. Empty = every room is its own zone."""
    if not raw or not raw.strip():
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise PlacementConfigError(f"zones are not valid JSON: {e}") from e
    if not isinstance(data, dict):
        raise PlacementConfigError("zones must be a JSON object keyed by zone name")
    out: dict[str, frozenset[int]] = {}
    seen: dict[int, str] = {}
    for name, rooms in data.items():
        if not name.strip():
            raise PlacementConfigError("a zone has an empty name")
        if not isinstance(rooms, list) or len(rooms) < 2:
            raise PlacementConfigError(f"zone {name!r} must list at least two room ids")
        for room in rooms:
            if isinstance(room, bool) or not isinstance(room, int):
                raise PlacementConfigError(f"zone {name!r}: room ids must be integers")
            if room in seen:
                raise PlacementConfigError(
                    f"room {room} is in two zones: {seen[room]!r} and {name!r}"
                )
            seen[room] = name
        out[name] = frozenset(rooms)
    return out
