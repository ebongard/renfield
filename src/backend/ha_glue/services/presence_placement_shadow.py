"""
Presence placement v2 — shadow mode (stage 1).

Runs ``presence_placement.decide`` beside the legacy room decision, on the same
readings, and changes NOTHING the system acts on: it reads the presence
service's sightings and room map and keeps its own state in this object. No
presence state, no events, no hooks, no kiosk, no privacy gate.

What it produces:

  * one log line when the two decisions START to disagree about a person, and
    one when they STOP — after the agreement has held for
    ``DISAGREEMENT_SETTLE_S``, so a device on the edge of a bound does not
    write a pair per report;
  * one log line at every voice/auth sighting stating what each decision
    believed at that moment — a voice sighting is free ground truth;
  * the per-device ``placement_v2`` field of the admin live-readings payload.

Only constructed when ``PRESENCE_PLACEMENT_V2_SHADOW_ENABLED`` is on; with the
flag off the presence service never imports this module. A failure in here is
logged once per entry point and never reaches presence tracking.

Several devices of one person: each device gets its own verdict (the payload
shows them one by one), and the disagreement log joins them per person — the
legacy decision keeps ONE room per person, so a second phone left elsewhere
must not count as a standing disagreement. This join is a logging rule only;
the real rule for several devices belongs to stage 2.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from loguru import logger

from ha_glue.services.presence_placement import (
    PlacementConfig,
    PlacementVerdict,
    Reading,
    Receiver,
    ReceiverEvidence,
    ReceiverParams,
    VerdictKind,
    decide,
    parse_receiver_params,
    parse_zones,
)
from ha_glue.utils.config import ha_glue_settings
from utils.config import settings

if TYPE_CHECKING:
    from ha_glue.services.presence_service import PresenceService

LOG_PREFIX = "Presence v2 shadow:"

# A disagreement is closed only after the two decisions have agreed for this
# long without interruption. A choice, not a measurement: longer than the
# default freshness window (20 s), so one receiver falling silent and coming
# back does not close a case and open the next.
DISAGREEMENT_SETTLE_S = 30.0

# satellite id → (room id, time of the last heartbeat)
Roster = Mapping[str, tuple[int | None, float | None]]


def _connected_roster() -> Roster:
    """Connected satellites with their room and last heartbeat. In-memory, no
    I/O. Best-effort: without a manager the roster is empty and every receiver
    is judged by its readings alone."""
    try:
        from ha_glue.services.satellite_manager import get_satellite_manager

        return {
            satellite_id: (sat.room_id, sat.last_heartbeat)
            for satellite_id, sat in get_satellite_manager().satellites.items()
        }
    except Exception as e:
        logger.debug(f"{LOG_PREFIX} satellite roster unavailable ({type(e).__name__})")
        return {}


@dataclass
class _Disagreement:
    """An open disagreement between the two decisions about one person."""

    since: float
    pairs: set[tuple[str, str]] = field(default_factory=set)
    # Start of the current unbroken run of agreement; None while they disagree.
    agreeing_since: float | None = None


_GONE = (VerdictKind.ABSENT, VerdictKind.UNAVAILABLE)


def _agrees(verdict: PlacementVerdict, legacy_room_id: int | None) -> bool:
    """Whether ONE device's verdict says what the legacy decision says. The
    legacy decision knows only "in room R" and "not tracked", so
    ``home_unknown`` never agrees."""
    if verdict.is_placed:
        return verdict.covers(legacy_room_id)
    if verdict.kind in _GONE:
        return legacy_room_id is None
    return False


def _person_agrees(verdicts: list[PlacementVerdict], legacy_room_id: int | None) -> bool:
    """Whether the verdicts of ALL of a person's devices, taken together, say
    what the legacy decision says about the person.

    "In room R" agrees when ANY device is placed there — the other phone on
    the desk upstairs does not contradict it. "Not tracked" agrees only when
    EVERY device is gone: one device still heard at home contradicts it.
    """
    if legacy_room_id is None:
        return all(v.kind in _GONE for v in verdicts)
    return any(v.is_placed and v.covers(legacy_room_id) for v in verdicts)


def _fmt(value: float | None, signed: bool = False) -> str:
    if value is None:
        return "-"
    return f"{value:+.1f}" if signed else f"{value:.1f}"


class PlacementShadow:
    """The shadow decision and its own state."""

    def __init__(
        self,
        receiver_params: Mapping[str, ReceiverParams],
        zones: Mapping[str, frozenset[int]],
        config: PlacementConfig,
        roster: Callable[[], Roster] = _connected_roster,
    ):
        self._params = dict(receiver_params)
        self._zones = dict(zones)
        self._config = config
        self._roster = roster
        self._verdicts: dict[str, PlacementVerdict] = {}       # device key → last verdict
        self._disagreements: dict[int, _Disagreement] = {}     # user id → open disagreement
        self._failures_logged: set[str] = set()                # entry points that failed
        # Latest clock value a report was decided at. The wall clock can step
        # back (time sync); the decision must not follow it.
        self._clock_high_water: float | None = None
        self._clock_behind_logged = False

    @classmethod
    def from_settings(cls) -> PlacementShadow | None:
        """Build the shadow from the settings, validating them. ``None`` when
        the flag is off — and when the settings are unusable: a diagnostic
        must not stop the backend, so it reports the error and stays off."""
        if not ha_glue_settings.presence_placement_v2_shadow_enabled:
            return None
        try:
            params = parse_receiver_params(ha_glue_settings.presence_placement_v2_receivers)
            zones = parse_zones(ha_glue_settings.presence_placement_v2_zones)
            config = PlacementConfig(
                time_constant_s=float(ha_glue_settings.presence_placement_v2_time_constant_seconds),
                fresh_s=float(ha_glue_settings.presence_placement_v2_fresh_seconds),
                margin_db=float(ha_glue_settings.presence_placement_v2_margin_db),
                hysteresis_db=float(ha_glue_settings.presence_placement_v2_hysteresis_db),
                absent_after_s=float(ha_glue_settings.presence_stale_timeout),
                receiver_alive_s=float(settings.device_heartbeat_timeout),
            )
        except Exception as e:
            # PlacementConfigError and ValueError name the offending setting.
            # Anything else a setting can provoke (a document nested too deep
            # to parse, a number no float holds) ends the same way: one ERROR
            # line, never a raise into the presence service's constructor.
            detail = str(e) if isinstance(e, ValueError) else f"{type(e).__name__}: {e}"
            logger.error(f"{LOG_PREFIX} settings rejected, shadow stays OFF: {detail}")
            return None
        logger.info(
            f"{LOG_PREFIX} ON — {len(params)} calibrated receiver(s), {len(zones)} zone(s), "
            f"margin {config.margin_db:g} dB, hysteresis {config.hysteresis_db:g} dB, "
            f"fresh {config.fresh_s:g} s, deadline {config.absent_after_s:g} s"
        )
        if not params:
            logger.info(
                f"{LOG_PREFIX} no receiver is calibrated — every verdict will be "
                "home_unknown, absent or unavailable"
            )
        return cls(params, zones, config)

    # ------------------------------------------------------------------
    # Entry points (called by the presence service; never raise)
    # ------------------------------------------------------------------

    def observe_report(self, presence: PresenceService, now: float) -> None:
        """A BLE report was processed by the legacy decision. Decide every
        tracked device again and log where the two decisions start or stop
        disagreeing about a person."""
        try:
            if not self._clock_moved_on(now):
                return
            receivers = self._receivers(presence)
            decided: dict[str, PlacementVerdict] = {}
            by_user: dict[int, list[PlacementVerdict]] = {}
            for key in list(presence._sightings):
                user_id = presence._user_for_key(key)
                if user_id is None:
                    # No longer anybody's device (removed, or its IRK was
                    # disabled): its verdict is dropped with ``decided``.
                    continue
                verdict = self._decide(presence, key, receivers, now)
                decided[key] = verdict
                by_user.setdefault(user_id, []).append(verdict)
            self._verdicts = decided
            for user_id, verdicts in by_user.items():
                self._track_agreement(presence, user_id, verdicts, now)
            for user_id in [u for u in self._disagreements if u not in by_user]:
                # Nothing left to compare: close the case and say why.
                self._close(user_id, now, "no tracked device left")
        except Exception as e:
            self._log_failure("observe_report", e)

    def note_voice_sighting(
        self, presence: PresenceService, user_id: int, room_id: int, now: float
    ) -> None:
        """A voice/auth sighting is about to be booked: state what each
        decision believed BEFORE it. Must be called before the legacy state is
        touched."""
        try:
            now = self._not_before_high_water(now)
            room = self._room_label(presence, room_id)
            current = presence._presence.get(user_id)
            legacy_room_id = current.room_id if current else None
            if legacy_room_id is None:
                legacy = "none"
            else:
                hit = "match" if legacy_room_id == room_id else "miss"
                legacy = f"{self._room_label(presence, legacy_room_id)}[{hit}]"

            receivers = self._receivers(presence)
            beliefs = []
            for key in presence._sightings:
                if presence._user_for_key(key) != user_id:
                    continue
                verdict = self._decide(presence, key, receivers, now)
                if verdict.is_placed:
                    hit = "match" if verdict.covers(room_id) else "miss"
                    beliefs.append(f"[{hit}] {self._describe(presence, verdict)}")
                else:
                    beliefs.append(self._describe(presence, verdict))
            v2 = " | ".join(beliefs) if beliefs else "no tracked device"
            logger.info(
                f"{LOG_PREFIX} voice sighting user {user_id} in {room}: legacy={legacy} v2={v2}"
            )
        except Exception as e:
            self._log_failure("note_voice_sighting", e)

    def payload_for(self, presence: PresenceService, key: str, now: float) -> dict[str, Any] | None:
        """The ``placement_v2`` field of the live-readings payload for one
        device: the verdict as of ``now`` and its components. A read: the
        shadow's own state is not advanced by somebody watching."""
        try:
            now = self._not_before_high_water(now)
            verdict = self._decide(presence, key, self._receivers(presence), now)
            user_id = presence._user_for_key(key)
            current = presence._presence.get(user_id) if user_id is not None else None
            c = verdict.components
            evidence = {e.receiver_id: e for e in c.receivers}

            def named(receiver_id: str | None, dbm: float | None) -> dict[str, Any] | None:
                if receiver_id is None:
                    return None
                room_id = evidence[receiver_id].room
                return {
                    "satellite_id": receiver_id,
                    "room_id": room_id,
                    "room_name": presence._room_names.get(room_id) if room_id is not None else None,
                    "dbm": round(dbm, 2) if dbm is not None else None,
                }

            runner_up = named(c.runner_up_receiver, c.runner_up_dbm)
            if runner_up is not None:
                runner_up["censored"] = c.runner_up_censored
            return {
                "kind": verdict.kind.value,
                "reason": c.reason.value,
                "room_id": verdict.room,
                "room_name": (presence._room_names.get(verdict.room)
                              if verdict.room is not None else None),
                "zone": verdict.zone,
                "zone_room_ids": sorted(verdict.rooms),
                "agrees_with_legacy": _agrees(verdict, current.room_id if current else None),
                "best": named(c.best_receiver, c.best_dbm),
                "runner_up": runner_up,
                "margin_db": round(c.margin_db, 2) if c.margin_db is not None else None,
                "margin_required_db": self._config.margin_db,
                "hysteresis_db": self._config.hysteresis_db,
                "bound_distance_db": (round(c.bound_distance_db, 2)
                                      if c.bound_distance_db is not None else None),
                "samples": c.samples,
                "age_s": round(c.age_s, 2) if c.age_s is not None else None,
                "censored": list(c.censored),
                "offline": list(c.offline),
                "receivers": [self._evidence_payload(e) for e in c.receivers],
            }
        except Exception as e:
            self._log_failure("payload_for", e)
            return None

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _clock_moved_on(self, now: float) -> bool:
        """Whether a report at ``now`` may be decided. ``decide`` ignores
        readings newer than its clock, so after a backward step of the wall
        clock every device would look unheard, lose its incumbent zone and
        open a disagreement — produced by the clock, not by the house. Until
        the clock has passed the earlier time again the shadow holds what it
        has: no verdict changes, no START, no STOP. One WARNING per episode."""
        high = self._clock_high_water
        if high is None or now >= high:
            self._clock_high_water = now
            self._clock_behind_logged = False
            return True
        if not self._clock_behind_logged:
            self._clock_behind_logged = True
            logger.warning(
                f"{LOG_PREFIX} the clock stepped back by {high - now:.1f} s; verdicts are "
                "held until it has passed the earlier time again"
            )
        return False

    def _not_before_high_water(self, now: float) -> float:
        """The clock for a read (voice line, payload): never earlier than the
        last decided report. Does not advance anything."""
        high = self._clock_high_water
        return now if high is None else max(now, high)

    def _receivers(self, presence: PresenceService) -> dict[str, Receiver]:
        """Every receiver the decision should know: the connected roster (with
        its heartbeat as the sign of life) plus any satellite that still has a
        reading in the window."""
        receivers: dict[str, Receiver] = {}
        for satellite_id, (room_id, heartbeat_at) in self._roster().items():
            receivers[satellite_id] = Receiver(
                room=room_id, params=self._params.get(satellite_id), alive_at=heartbeat_at
            )
        for sightings in presence._sightings.values():
            for s in sightings:
                if s.satellite_id not in receivers:
                    receivers[s.satellite_id] = Receiver(
                        room=s.room_id, params=self._params.get(s.satellite_id)
                    )
        return receivers

    def _decide(
        self,
        presence: PresenceService,
        key: str,
        receivers: Mapping[str, Receiver],
        now: float,
    ) -> PlacementVerdict:
        readings: dict[str, list[Reading]] = {}
        for s in presence._sightings.get(key, ()):
            readings.setdefault(s.satellite_id, []).append(Reading(at=s.timestamp, rssi=s.rssi))
        return decide(readings, receivers, self._zones, self._verdicts.get(key), now, self._config)

    def _track_agreement(
        self,
        presence: PresenceService,
        user_id: int,
        verdicts: list[PlacementVerdict],
        now: float,
    ) -> None:
        """START at the first disagreeing report; STOP once the two decisions
        have agreed for ``DISAGREEMENT_SETTLE_S`` without interruption."""
        current = presence._presence.get(user_id)
        legacy_room_id = current.room_id if current else None
        open_case = self._disagreements.get(user_id)

        if _person_agrees(verdicts, legacy_room_id):
            if open_case is None:
                return
            if open_case.agreeing_since is None:
                open_case.agreeing_since = now
            if now - open_case.agreeing_since >= DISAGREEMENT_SETTLE_S:
                self._close(
                    user_id, open_case.agreeing_since,
                    f"legacy={self._describe_legacy(presence, user_id, now)} "
                    f"v2={self._describe_all(presence, verdicts)}",
                )
            return

        pair = (
            self._room_label(presence, legacy_room_id) if legacy_room_id is not None else "none",
            " | ".join(self._short(presence, v) for v in verdicts),
        )
        if open_case is not None:
            open_case.agreeing_since = None  # the agreement did not hold
            open_case.pairs.add(pair)
            return
        self._disagreements[user_id] = _Disagreement(since=now, pairs={pair})
        logger.info(
            f"{LOG_PREFIX} disagreement START user {user_id}: "
            f"legacy={self._describe_legacy(presence, user_id, now)} "
            f"v2={self._describe_all(presence, verdicts)}"
        )

    def _close(self, user_id: int, ended_at: float, state: str) -> None:
        """Close an open disagreement with its STOP line. ``ended_at`` is when
        the disagreement itself ended, not when the line is written."""
        case = self._disagreements.pop(user_id)
        logger.info(
            f"{LOG_PREFIX} disagreement STOP user {user_id} after "
            f"{ended_at - case.since:.0f} s ({len(case.pairs)} distinct pair(s)): {state}"
        )

    def _describe_all(self, presence: PresenceService, verdicts: list[PlacementVerdict]) -> str:
        """One description per device of the person, in tracking order."""
        return " | ".join(self._describe(presence, v) for v in verdicts)

    @staticmethod
    def _room_label(presence: PresenceService, room_id: int | None) -> str:
        if room_id is None:
            return "?"
        return presence._room_names.get(room_id) or f"room {room_id}"

    def _describe_legacy(self, presence: PresenceService, user_id: int, now: float) -> str:
        current = presence._presence.get(user_id)
        if current is None or current.room_id is None:
            return "none"
        return (
            f"{self._room_label(presence, current.room_id)}"
            f"(confidence={current.confidence:.2f}, pinned={current.voice_pinned}, "
            f"uncertain={presence.is_location_uncertain(user_id, now)})"
        )

    def _short(self, presence: PresenceService, verdict: PlacementVerdict) -> str:
        if not verdict.is_placed:
            return verdict.kind.value
        if verdict.zone is not None:
            return f"placed:{verdict.zone}"
        return f"placed:{self._room_label(presence, verdict.room)}"

    def _describe(self, presence: PresenceService, verdict: PlacementVerdict) -> str:
        """Rooms and numbers only: receivers are named by their room."""
        c = verdict.components
        evidence = {e.receiver_id: e for e in c.receivers}

        def receiver_room(receiver_id: str | None) -> str:
            if receiver_id is None:
                return "-"
            return self._room_label(presence, evidence[receiver_id].room)

        head = verdict.kind.value
        if verdict.is_placed:
            head = f"placed:{self._room_label(presence, verdict.room)}"
            if verdict.zone is not None:
                zone_rooms = "+".join(
                    sorted(self._room_label(presence, r) for r in verdict.rooms)
                )
                head += f" zone={verdict.zone}({zone_rooms})"
        runner_up = f"{receiver_room(c.runner_up_receiver)}:{_fmt(c.runner_up_dbm)}"
        if c.runner_up_censored:
            runner_up += "(censored)"
        return (
            f"{head} reason={c.reason.value} "
            f"best={receiver_room(c.best_receiver)}:{_fmt(c.best_dbm)} "
            f"runner_up={runner_up} margin={_fmt(c.margin_db)} "
            f"bound_distance={_fmt(c.bound_distance_db, signed=True)} "
            f"samples={c.samples} age={_fmt(c.age_s)}s "
            f"censored=[{','.join(sorted(receiver_room(r) for r in c.censored))}] "
            f"offline=[{','.join(sorted(receiver_room(r) for r in c.offline))}]"
        )

    @staticmethod
    def _evidence_payload(e: ReceiverEvidence) -> dict[str, Any]:
        def rounded(value: float | None) -> float | None:
            return round(value, 2) if value is not None else None

        return {
            "satellite_id": e.receiver_id,
            "room_id": e.room,
            "state": e.state.value,
            "calibrated": e.calibrated,
            "smoothed_dbm": rounded(e.smoothed_dbm),
            "comparable_dbm": rounded(e.comparable_dbm),
            "bound_distance_db": rounded(e.bound_distance_db),
            "samples": e.samples,
            "age_s": rounded(e.age_s),
        }

    def _log_failure(self, where: str, error: Exception) -> None:
        """Once per entry point, at WARNING: a broken diagnostic must be
        visible, and must not write a line per report. The exception TYPE and
        the entry point only — never its message, never a traceback: a
        ``KeyError`` on a device key carries a MAC or an ``irk:<label>``, and
        the label is chosen by the user and may be a name."""
        if where in self._failures_logged:
            return
        self._failures_logged.add(where)
        logger.warning(
            f"{LOG_PREFIX} {where} failed ({type(error).__name__}); further failures "
            "there are not logged. Presence tracking is unaffected."
        )
