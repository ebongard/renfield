"""Presence placement v2 — the pure decision (``ha_glue/services/presence_placement.py``).

No I/O anywhere: every test builds readings, receivers and a clock by hand.

  * smoothing — time-weighted, independent of how the readings were delivered
    and of what other receivers sent;
  * reject option — outside the in-room bound is ``home_unknown``;
  * censoring — a silent receiver stands at its cutoff, it is not "far";
  * calibration — an uncalibrated receiver keeps a device home, never places it;
  * values that are not finite numbers never place — dropped as readings,
    rejected by the gates, refused as parameters;
  * margin and hysteresis; zones; deadlines; the injected clock;
  * the settings parsers.
"""
from __future__ import annotations

import math

import pytest

from ha_glue.services.presence_placement import (
    PlacementComponents,
    PlacementConfig,
    PlacementConfigError,
    PlacementVerdict,
    Reading,
    Reason,
    Receiver,
    ReceiverEvidence,
    ReceiverParams,
    ReceiverState,
    VerdictKind,
    _ZoneCandidate,
    decide,
    parse_receiver_params,
    parse_zones,
    smooth,
)

pytestmark = pytest.mark.unit

NOW = 10_000.0
KITCHEN, BATH, LIVING, DINING, HALL = 1, 2, 3, 4, 5

CONFIG = PlacementConfig(
    time_constant_s=6.0,
    fresh_s=20.0,
    margin_db=10.0,
    hysteresis_db=3.0,
    absent_after_s=120.0,
    receiver_alive_s=60.0,
)


def steady(rssi: float, until: float = NOW, count: int = 5, every: float = 3.0) -> list[Reading]:
    """``count`` readings of the same level, the last one at ``until``."""
    return [Reading(at=until - i * every, rssi=rssi) for i in range(count)]


def calibrated(room: int, in_room: float = -60.0, cutoff: float = -80.0,
               offset: float = 0.0, alive_at: float | None = NOW) -> Receiver:
    return Receiver(room=room, params=ReceiverParams(offset, in_room, cutoff), alive_at=alive_at)


def uncalibrated(room: int, alive_at: float | None = NOW) -> Receiver:
    return Receiver(room=room, params=None, alive_at=alive_at)


def run(readings, receivers, zones=None, previous=None, now=NOW, config=CONFIG) -> PlacementVerdict:
    return decide(readings, receivers, zones or {}, previous, now, config)


def evidence_of(verdict: PlacementVerdict, receiver_id: str):
    return next(e for e in verdict.components.receivers if e.receiver_id == receiver_id)


# ---------------------------------------------------------------------------
# Smoothing
# ---------------------------------------------------------------------------


class TestSmoothing:
    def test_is_the_time_weighted_mean(self):
        readings = [Reading(0.0, -60.0), Reading(6.0, -80.0)]
        w_old = math.exp(-1.0)  # 6 s old at tau = 6 s
        expected = (-60.0 * w_old + -80.0) / (w_old + 1.0)
        assert smooth(readings, now=6.0, time_constant_s=6.0) == pytest.approx(expected)

    def test_a_constant_level_stays_that_level(self):
        assert smooth(steady(-57.0), NOW, 6.0) == pytest.approx(-57.0)

    def test_does_not_depend_on_delivery(self):
        """Same (timestamp, value) pairs → same value, however they arrived:
        reversed, or with every reading delivered twice."""
        readings = [Reading(NOW - 12, -70.0), Reading(NOW - 6, -64.0), Reading(NOW, -58.0)]
        base = smooth(readings, NOW, 6.0)
        assert smooth(list(reversed(readings)), NOW, 6.0) == pytest.approx(base)
        assert smooth(readings + readings, NOW, 6.0) == pytest.approx(base)

    def test_readings_after_now_are_ignored(self):
        readings = [Reading(NOW, -60.0), Reading(NOW + 5, -30.0)]
        assert smooth(readings, NOW, 6.0) == pytest.approx(-60.0)

    def test_nothing_to_smooth(self):
        assert smooth([], NOW, 6.0) is None
        assert smooth([Reading(NOW + 1, -50.0)], NOW, 6.0) is None

    def test_weights_that_underflow_fall_back_to_the_newest_reading(self):
        readings = [Reading(0.0, -70.0), Reading(10.0, -60.0)]
        assert smooth(readings, now=1e6, time_constant_s=1.0) == -60.0

    def test_other_receivers_reporting_do_not_move_a_receivers_value(self):
        """The legacy filter advances one step per REPORT from any satellite,
        so more reporting receivers mean faster attack and decay. Here a
        receiver's value depends on its own readings alone."""
        kitchen = [Reading(NOW - 9, -70.0), Reading(NOW - 6, -62.0), Reading(NOW, -55.0)]
        receivers = {
            "kitchen": calibrated(KITCHEN),
            "bath": calibrated(BATH),
            "hall": uncalibrated(HALL),
        }
        alone = run({"kitchen": kitchen}, receivers)
        crowded = run(
            {
                "kitchen": kitchen,
                # Five and ten times the kitchen's report rate.
                "bath": [Reading(NOW - i * 0.6, -85.0) for i in range(15)],
                "hall": [Reading(NOW - i * 0.3, -88.0) for i in range(30)],
            },
            receivers,
        )
        assert (evidence_of(crowded, "kitchen").smoothed_dbm
                == pytest.approx(evidence_of(alone, "kitchen").smoothed_dbm))

    def test_the_same_readings_in_more_reports_give_the_same_verdict(self):
        """A rising series delivered once, or every reading of it delivered
        again in three reports: the value and the verdict are identical. (A
        filter that steps once per report lands somewhere else — the level
        must change over the series for this to tell the two apart.)"""
        once = [Reading(NOW - 12 + 3 * i, -70.0 + 4 * i) for i in range(5)]  # -70 … -54
        thrice = [r for r in once for _ in range(3)]
        receivers = {"kitchen": calibrated(KITCHEN), "bath": calibrated(BATH)}
        a = run({"kitchen": once, "bath": steady(-75.0)}, receivers)
        b = run({"kitchen": thrice, "bath": steady(-75.0)}, receivers)
        assert a.kind is b.kind is VerdictKind.PLACED
        assert -60.0 < a.components.best_dbm < -54.0  # smoothed, not the last reading
        assert a.components.best_dbm == pytest.approx(b.components.best_dbm)
        assert a.components.samples == b.components.samples == 5


# ---------------------------------------------------------------------------
# Reject option
# ---------------------------------------------------------------------------


class TestRejectOption:
    def test_inside_the_bound_and_ahead_is_placed(self):
        verdict = run(
            {"kitchen": steady(-55.0), "bath": steady(-75.0)},
            {"kitchen": calibrated(KITCHEN, in_room=-60.0), "bath": calibrated(BATH)},
        )
        assert verdict.kind is VerdictKind.PLACED
        assert verdict.room == KITCHEN
        assert verdict.rooms == frozenset({KITCHEN})
        assert verdict.zone is None
        c = verdict.components
        assert c.reason is Reason.IN_BOUND_AND_MARGIN
        assert (c.best_receiver, c.runner_up_receiver) == ("kitchen", "bath")
        assert c.best_dbm == pytest.approx(-55.0)
        assert c.runner_up_dbm == pytest.approx(-75.0)
        assert c.margin_db == pytest.approx(20.0)
        assert c.bound_distance_db == pytest.approx(5.0)
        assert c.samples == 5
        assert c.age_s == pytest.approx(0.0)

    def test_the_best_receiver_outside_its_bound_places_nobody(self):
        """The case the legacy decision gets wrong: a room without a receiver
        is booked to whichever neighbour hears the phone. The neighbour is the
        clear best here — and still does not place."""
        verdict = run(
            {"kitchen": steady(-72.0), "bath": steady(-90.0)},
            {"kitchen": calibrated(KITCHEN, in_room=-60.0),
             "bath": calibrated(BATH, cutoff=-100.0)},
        )
        assert verdict.kind is VerdictKind.HOME_UNKNOWN
        assert verdict.room is None and not verdict.rooms
        assert verdict.components.reason is Reason.OUTSIDE_BOUND
        assert verdict.components.best_receiver == "kitchen"
        assert verdict.components.bound_distance_db == pytest.approx(-12.0)
        assert verdict.components.margin_db == pytest.approx(18.0)

    def test_a_single_receiver_outside_its_bound_places_nobody(self):
        verdict = run({"kitchen": steady(-61.0)}, {"kitchen": calibrated(KITCHEN, in_room=-60.0)})
        assert verdict.kind is VerdictKind.HOME_UNKNOWN
        assert verdict.components.reason is Reason.OUTSIDE_BOUND

    def test_the_bound_is_on_the_receivers_own_scale(self):
        """The offset shifts the comparison between receivers, never the bound."""
        receivers = {"kitchen": calibrated(KITCHEN, in_room=-60.0, offset=8.0)}
        assert run({"kitchen": steady(-62.0)}, receivers).kind is VerdictKind.HOME_UNKNOWN
        assert run({"kitchen": steady(-59.0)}, receivers).kind is VerdictKind.PLACED


# ---------------------------------------------------------------------------
# Censoring
# ---------------------------------------------------------------------------


class TestCensoring:
    def test_a_silent_receiver_stands_at_its_cutoff_not_far_away(self):
        """A receiver that cuts at -80 and says nothing hears the device at or
        below -80 — or not at all. Against a claimant at -72 that is 8 dB at
        best: no clear lead, no placement."""
        verdict = run(
            {"kitchen": steady(-72.0)},
            {"kitchen": calibrated(KITCHEN, in_room=-75.0),
             "bath": calibrated(BATH, cutoff=-80.0)},
        )
        assert verdict.kind is VerdictKind.HOME_UNKNOWN
        c = verdict.components
        assert c.reason is Reason.MARGIN_TOO_SMALL
        assert c.runner_up_receiver == "bath"
        assert c.runner_up_censored is True
        assert c.runner_up_dbm == pytest.approx(-80.0)
        assert c.margin_db == pytest.approx(8.0)
        assert c.censored == ("bath",)
        silent = evidence_of(verdict, "bath")
        assert silent.state is ReceiverState.CENSORED
        assert silent.smoothed_dbm is None  # silence is never turned into a reading

    def test_a_receiver_that_reports_everything_hides_much_less(self):
        """Same scene, but the silent receiver reports down to -100: its
        silence now does say "far"."""
        verdict = run(
            {"kitchen": steady(-72.0)},
            {"kitchen": calibrated(KITCHEN, in_room=-75.0),
             "bath": calibrated(BATH, cutoff=-100.0)},
        )
        assert verdict.kind is VerdictKind.PLACED
        assert verdict.components.margin_db == pytest.approx(28.0)
        assert verdict.components.runner_up_censored is True

    def test_the_cutoff_is_compared_on_the_calibrated_scale(self):
        verdict = run(
            {"kitchen": steady(-66.0)},
            {"kitchen": calibrated(KITCHEN, in_room=-75.0),
             "bath": calibrated(BATH, cutoff=-80.0, offset=6.0)},
        )
        assert verdict.components.runner_up_dbm == pytest.approx(-74.0)
        assert verdict.kind is VerdictKind.HOME_UNKNOWN  # 8 dB lead

    def test_an_offline_receiver_says_nothing_either_way(self):
        verdict = run(
            {"kitchen": steady(-72.0)},
            {"kitchen": calibrated(KITCHEN, in_room=-75.0),
             "bath": calibrated(BATH, cutoff=-80.0, alive_at=NOW - 600)},
        )
        assert verdict.kind is VerdictKind.PLACED
        assert verdict.components.runner_up_receiver is None
        assert verdict.components.margin_db is None
        assert verdict.components.offline == ("bath",)
        assert verdict.components.censored == ()

    def test_a_silent_receiver_without_a_known_cutoff_cannot_be_compared(self):
        verdict = run(
            {"kitchen": steady(-72.0)},
            {"kitchen": calibrated(KITCHEN, in_room=-75.0), "bath": uncalibrated(BATH)},
        )
        assert verdict.kind is VerdictKind.PLACED
        assert verdict.components.censored == ("bath",)
        assert evidence_of(verdict, "bath").comparable_dbm is None

    def test_a_reading_older_than_fresh_no_longer_counts_as_heard(self):
        """The incumbent does not keep its room on a value with no fresh
        reading behind it: once the reading is stale the receiver is censored."""
        receivers = {"kitchen": calibrated(KITCHEN, in_room=-60.0)}
        readings = {"kitchen": steady(-50.0, until=NOW - 21.0)}
        placed_before = run({"kitchen": steady(-50.0)}, receivers)
        verdict = run(readings, receivers, previous=placed_before)
        assert verdict.kind is VerdictKind.HOME_UNKNOWN
        assert verdict.components.reason is Reason.NO_FRESH_READING
        assert verdict.components.age_s == pytest.approx(21.0)
        assert evidence_of(verdict, "kitchen").state is ReceiverState.CENSORED


# ---------------------------------------------------------------------------
# Calibration
# ---------------------------------------------------------------------------


class TestUncalibrated:
    @pytest.mark.parametrize("rssi", [-30.0, -50.0, -70.0, -95.0])
    def test_an_uncalibrated_receiver_never_places(self, rssi):
        verdict = run({"hall": steady(rssi)}, {"hall": uncalibrated(HALL)})
        assert verdict.kind is VerdictKind.HOME_UNKNOWN
        assert verdict.components.reason is Reason.NO_CALIBRATED_RECEIVER
        assert verdict.components.best_receiver == "hall"
        assert verdict.components.bound_distance_db is None

    def test_no_parameters_at_all_means_nobody_is_ever_placed(self):
        """The default configuration: empty. Heard is home, never a room."""
        verdict = run(
            {"kitchen": steady(-40.0), "bath": steady(-90.0)},
            {"kitchen": uncalibrated(KITCHEN), "bath": uncalibrated(BATH)},
        )
        assert verdict.kind is VerdictKind.HOME_UNKNOWN
        assert verdict.components.runner_up_receiver == "bath"

    def test_an_uncalibrated_receiver_can_stand_in_the_way(self):
        """It is compared at face value: a strong uncalibrated neighbour takes
        the clear lead away from a calibrated claimant."""
        verdict = run(
            {"kitchen": steady(-55.0), "hall": steady(-50.0)},
            {"kitchen": calibrated(KITCHEN, in_room=-60.0), "hall": uncalibrated(HALL)},
        )
        assert verdict.kind is VerdictKind.HOME_UNKNOWN
        assert verdict.components.reason is Reason.MARGIN_TOO_SMALL
        assert verdict.components.runner_up_receiver == "hall"

    def test_a_weak_uncalibrated_receiver_does_not_stop_a_placement(self):
        verdict = run(
            {"kitchen": steady(-55.0), "hall": steady(-80.0)},
            {"kitchen": calibrated(KITCHEN, in_room=-60.0), "hall": uncalibrated(HALL)},
        )
        assert verdict.kind is VerdictKind.PLACED and verdict.room == KITCHEN

    def test_a_receiver_known_only_from_its_readings_is_uncalibrated_and_roomless(self):
        verdict = run({"stranger": steady(-40.0)}, {})
        assert verdict.kind is VerdictKind.HOME_UNKNOWN
        assert evidence_of(verdict, "stranger").calibrated is False
        assert evidence_of(verdict, "stranger").room is None

    def test_a_calibrated_receiver_without_a_room_cannot_place(self):
        verdict = run(
            {"orphan": steady(-40.0)},
            {"orphan": Receiver(room=None, params=ReceiverParams(0.0, -60.0, -80.0), alive_at=NOW)},
        )
        assert verdict.kind is VerdictKind.HOME_UNKNOWN


# ---------------------------------------------------------------------------
# Values that are not finite numbers
# ---------------------------------------------------------------------------

NOT_FINITE = [math.nan, math.inf, -math.inf]


def _heard(receiver_id: str, room: int, comparable: float, bound_distance: float) -> ReceiverEvidence:
    return ReceiverEvidence(
        receiver_id=receiver_id, room=room, state=ReceiverState.HEARD, calibrated=True,
        smoothed_dbm=comparable, comparable_dbm=comparable, bound_distance_db=bound_distance,
        samples=5, age_s=0.0,
    )


class TestNotFinite:
    """The satellite's device entries reach the presence service unvalidated,
    and ``json.loads`` accepts ``NaN`` and ``Infinity``. Every comparison
    against NaN is False, so a gate written ``if x < bound: reject`` lets it
    through."""

    RECEIVERS = {
        "kitchen": calibrated(KITCHEN, in_room=-60.0),
        "bath": calibrated(BATH, in_room=-30.0),
    }

    @pytest.mark.parametrize("bad", NOT_FINITE)
    def test_a_receiver_with_only_such_readings_places_nobody(self, bad):
        """The reviewer's probe: the kitchen sends NaN, the bath hears the
        device at -40 dBm. The kitchen must not be placed — it has not heard
        anything."""
        verdict = run(
            {"kitchen": steady(bad), "bath": steady(-40.0)},
            self.RECEIVERS,
        )
        assert verdict.kind is VerdictKind.HOME_UNKNOWN
        assert verdict.components.best_receiver == "bath"
        assert verdict.components.reason is Reason.OUTSIDE_BOUND
        kitchen = evidence_of(verdict, "kitchen")
        assert kitchen.state is ReceiverState.CENSORED  # alive, and no reading
        assert kitchen.samples == 0 and kitchen.smoothed_dbm is None
        for value in (verdict.components.best_dbm, verdict.components.margin_db):
            assert math.isfinite(value)

    @pytest.mark.parametrize("bad", NOT_FINITE)
    def test_such_a_reading_is_no_sign_of_life_and_does_not_keep_a_device_home(self, bad):
        receivers = {"kitchen": calibrated(KITCHEN, alive_at=None)}
        assert run({"kitchen": steady(bad)}, receivers).kind is VerdictKind.UNAVAILABLE

    @pytest.mark.parametrize("bad", NOT_FINITE)
    def test_among_real_readings_it_changes_nothing(self, bad):
        clean = {"kitchen": steady(-55.0), "bath": steady(-75.0)}
        dirty = {
            "kitchen": [*steady(-55.0), Reading(NOW - 1.0, bad)],
            "bath": [Reading(NOW - 2.0, bad), *steady(-75.0)],
        }
        expected, verdict = run(clean, self.RECEIVERS), run(dirty, self.RECEIVERS)
        assert expected.kind is VerdictKind.PLACED
        assert (verdict.kind, verdict.room) == (expected.kind, expected.room)
        assert verdict.components.best_dbm == pytest.approx(expected.components.best_dbm)
        assert verdict.components.margin_db == pytest.approx(expected.components.margin_db)
        assert evidence_of(verdict, "kitchen").samples == 5

    @pytest.mark.parametrize("bad", NOT_FINITE)
    def test_smoothing_ignores_it(self, bad):
        assert smooth([Reading(NOW, bad), Reading(NOW - 3.0, -50.0)], NOW, 6.0) == pytest.approx(-50.0)
        assert smooth([Reading(NOW, bad)], NOW, 6.0) is None
        assert smooth([Reading(bad, -50.0)], NOW, 6.0) is None  # a timestamp that is none

    @pytest.mark.parametrize("bound_distance", [math.nan, -0.1])
    def test_the_bound_gate_rejects_what_it_cannot_compare(self, bound_distance):
        """The gate itself, with the value handed to it directly: whatever
        reaches it in the future, "not proven inside" must reject."""
        member = _heard("kitchen", KITCHEN, -50.0, bound_distance)
        candidate = _ZoneCandidate(
            rooms=frozenset({KITCHEN}), name=None, best=member, most_inside=member, runner_up=None
        )
        assert candidate.claims(CONFIG, 0.0) is Reason.OUTSIDE_BOUND
        assert candidate.claims(CONFIG, CONFIG.hysteresis_db) is (
            Reason.OUTSIDE_BOUND if math.isnan(bound_distance) else None
        )

    @pytest.mark.parametrize("runner_up_dbm", [math.nan, -45.0])
    def test_the_margin_gate_rejects_what_it_cannot_compare(self, runner_up_dbm):
        member = _heard("kitchen", KITCHEN, -50.0, 10.0)
        candidate = _ZoneCandidate(
            rooms=frozenset({KITCHEN}), name=None, best=member, most_inside=member,
            runner_up=_heard("bath", BATH, runner_up_dbm, -20.0),
        )
        assert candidate.claims(CONFIG, 0.0) is Reason.MARGIN_TOO_SMALL
        assert candidate.claims(CONFIG, CONFIG.hysteresis_db) is Reason.MARGIN_TOO_SMALL

    @pytest.mark.parametrize("field", ["offset_db", "in_room_dbm", "cutoff_dbm"])
    @pytest.mark.parametrize("bad", [*NOT_FINITE, True, "-60", None, 10 ** 400])
    def test_receiver_parameters_must_be_finite_numbers(self, field, bad):
        values = {"offset_db": 0.0, "in_room_dbm": -60.0, "cutoff_dbm": -80.0, field: bad}
        with pytest.raises(ValueError, match=field):
            ReceiverParams(**values)


# ---------------------------------------------------------------------------
# Margin and hysteresis
# ---------------------------------------------------------------------------


class TestMarginAndHysteresis:
    RECEIVERS = {
        "kitchen": calibrated(KITCHEN, in_room=-60.0),
        "bath": calibrated(BATH, in_room=-60.0),
    }

    @pytest.mark.parametrize(("runner_up", "kind"), [
        (-64.5, VerdictKind.HOME_UNKNOWN),  # 9.5 dB behind
        (-65.5, VerdictKind.PLACED),        # 10.5 dB behind
    ])
    def test_the_margin_decides_between_two_receivers(self, runner_up, kind):
        verdict = run({"kitchen": steady(-55.0), "bath": steady(runner_up)}, self.RECEIVERS)
        assert verdict.kind is kind

    def test_two_receivers_inside_their_bound_and_close_place_nobody(self):
        verdict = run({"kitchen": steady(-55.0), "bath": steady(-57.0)}, self.RECEIVERS)
        assert verdict.kind is VerdictKind.HOME_UNKNOWN
        assert verdict.components.reason is Reason.MARGIN_TOO_SMALL
        assert verdict.components.margin_db == pytest.approx(2.0)

    def _placed_in_kitchen(self) -> PlacementVerdict:
        verdict = run({"kitchen": steady(-55.0), "bath": steady(-75.0)}, self.RECEIVERS)
        assert verdict.kind is VerdictKind.PLACED
        return verdict

    def test_the_incumbent_holds_slightly_below_the_bound(self):
        readings = {"kitchen": steady(-62.0), "bath": steady(-85.0)}
        assert run(readings, self.RECEIVERS).kind is VerdictKind.HOME_UNKNOWN
        held = run(readings, self.RECEIVERS, previous=self._placed_in_kitchen())
        assert held.kind is VerdictKind.PLACED and held.room == KITCHEN
        assert held.components.reason is Reason.INCUMBENT_HELD
        assert held.components.bound_distance_db == pytest.approx(-2.0)

    def test_the_incumbent_is_released_beyond_the_hysteresis(self):
        readings = {"kitchen": steady(-63.5), "bath": steady(-85.0)}
        verdict = run(readings, self.RECEIVERS, previous=self._placed_in_kitchen())
        assert verdict.kind is VerdictKind.HOME_UNKNOWN
        assert verdict.components.reason is Reason.OUTSIDE_BOUND

    def test_the_incumbent_holds_on_a_slightly_smaller_margin(self):
        readings = {"kitchen": steady(-55.0), "bath": steady(-63.0)}  # 8 dB
        assert run(readings, self.RECEIVERS).kind is VerdictKind.HOME_UNKNOWN
        held = run(readings, self.RECEIVERS, previous=self._placed_in_kitchen())
        assert held.kind is VerdictKind.PLACED
        assert held.components.reason is Reason.INCUMBENT_HELD

    def test_the_incumbent_is_released_when_the_margin_shrinks_further(self):
        readings = {"kitchen": steady(-55.0), "bath": steady(-61.5)}  # 6.5 dB
        verdict = run(readings, self.RECEIVERS, previous=self._placed_in_kitchen())
        assert verdict.kind is VerdictKind.HOME_UNKNOWN

    def test_a_clear_challenger_takes_over_from_the_incumbent(self):
        readings = {"kitchen": steady(-70.0), "bath": steady(-52.0)}
        verdict = run(readings, self.RECEIVERS, previous=self._placed_in_kitchen())
        assert verdict.kind is VerdictKind.PLACED and verdict.room == BATH
        assert verdict.components.reason is Reason.IN_BOUND_AND_MARGIN

    def test_a_challenger_gets_no_hysteresis(self):
        """The relaxation belongs to the zone that already holds the device."""
        readings = {"kitchen": steady(-85.0), "bath": steady(-62.0)}
        verdict = run(readings, self.RECEIVERS, previous=self._placed_in_kitchen())
        assert verdict.kind is VerdictKind.HOME_UNKNOWN

    def test_a_previous_home_unknown_gives_no_hysteresis(self):
        readings = {"kitchen": steady(-62.0), "bath": steady(-85.0)}
        unknown = run(readings, self.RECEIVERS)
        assert run(readings, self.RECEIVERS, previous=unknown).kind is VerdictKind.HOME_UNKNOWN

    def test_offsets_make_receivers_comparable(self):
        """A receiver that reads 8 dB weak gets +8 before the comparison."""
        receivers = {
            "kitchen": calibrated(KITCHEN, in_room=-70.0, offset=8.0),
            "bath": calibrated(BATH, in_room=-60.0),
        }
        verdict = run({"kitchen": steady(-66.0), "bath": steady(-69.0)}, receivers)
        assert verdict.kind is VerdictKind.PLACED and verdict.room == KITCHEN
        assert verdict.components.best_dbm == pytest.approx(-58.0)
        assert verdict.components.margin_db == pytest.approx(11.0)


# ---------------------------------------------------------------------------
# Zones
# ---------------------------------------------------------------------------


class TestZones:
    ZONES = {"open-plan": frozenset({LIVING, DINING})}
    RECEIVERS = {
        "living": calibrated(LIVING, in_room=-60.0),
        "dining": calibrated(DINING, in_room=-60.0),
        "kitchen": calibrated(KITCHEN, in_room=-60.0, cutoff=-100.0),
    }

    def test_an_open_plan_zone_is_decided_as_one_unit(self):
        """Two receivers of one space hear the phone almost equally. As two
        rooms that is no clear lead; as one zone it is a placement."""
        readings = {"living": steady(-55.0), "dining": steady(-57.0), "kitchen": steady(-85.0)}
        apart = run(readings, self.RECEIVERS)
        assert apart.kind is VerdictKind.HOME_UNKNOWN

        together = run(readings, self.RECEIVERS, zones=self.ZONES)
        assert together.kind is VerdictKind.PLACED
        assert together.zone == "open-plan"
        assert together.rooms == frozenset({LIVING, DINING})
        assert together.room == LIVING  # the nearer room, a hint only
        assert together.covers(DINING) and together.covers(LIVING)
        assert not together.covers(KITCHEN) and not together.covers(None)
        # The runner-up is the strongest receiver OUTSIDE the zone.
        assert together.components.runner_up_receiver == "kitchen"
        assert together.components.margin_db == pytest.approx(30.0)

    def test_one_member_inside_its_bound_is_enough_for_the_zone(self):
        """The stronger member is outside its own bound, the weaker one inside
        its (looser) bound: the zone is claimed, the hint is the stronger."""
        receivers = dict(self.RECEIVERS, dining=calibrated(DINING, in_room=-78.0, cutoff=-100.0))
        readings = {"living": steady(-63.0), "dining": steady(-75.0), "kitchen": steady(-92.0)}
        verdict = run(readings, receivers, zones=self.ZONES)
        assert verdict.kind is VerdictKind.PLACED
        assert verdict.room == LIVING
        assert verdict.components.best_receiver == "living"
        assert verdict.components.bound_distance_db == pytest.approx(3.0)  # the dining receiver's

    def test_a_zone_still_needs_its_lead_over_the_rest_of_the_house(self):
        readings = {"living": steady(-55.0), "dining": steady(-57.0), "kitchen": steady(-50.0)}
        receivers = dict(self.RECEIVERS, kitchen=calibrated(KITCHEN, in_room=-45.0))
        verdict = run(readings, receivers, zones=self.ZONES)
        assert verdict.kind is VerdictKind.HOME_UNKNOWN

    def test_hysteresis_follows_the_zone_not_the_hinted_room(self):
        readings = {"living": steady(-55.0), "dining": steady(-70.0), "kitchen": steady(-85.0)}
        first = run(readings, self.RECEIVERS, zones=self.ZONES)
        assert first.room == LIVING
        # The person crosses the space: the dining receiver is now the only
        # one heard and sits 2 dB below its bound.
        later = run({"dining": steady(-62.0), "kitchen": steady(-95.0)},
                    self.RECEIVERS, zones=self.ZONES, previous=first)
        assert later.kind is VerdictKind.PLACED
        assert later.room == DINING
        assert later.components.reason is Reason.INCUMBENT_HELD

    def test_a_room_in_two_zones_is_rejected(self):
        with pytest.raises(ValueError, match="two zones"):
            run({}, {}, zones={"a": {LIVING, DINING}, "b": {DINING, KITCHEN}})

    def test_an_uncalibrated_member_louder_than_the_calibrated_one(self):
        """The dining receiver has no parameters and hears the device 15 dB
        louder than the calibrated living receiver. It cannot vouch, and it
        does not compete against its own zone: the zone is decided by the
        living receiver alone, and the hint names ITS room — the louder,
        uncalibrated room is not named."""
        receivers = dict(self.RECEIVERS, dining=uncalibrated(DINING))
        readings = {"living": steady(-55.0), "dining": steady(-40.0), "kitchen": steady(-85.0)}
        verdict = run(readings, receivers, zones=self.ZONES)
        assert verdict.kind is VerdictKind.PLACED
        assert verdict.rooms == frozenset({LIVING, DINING})
        assert verdict.room == LIVING
        assert verdict.components.best_receiver == "living"
        assert verdict.components.best_dbm == pytest.approx(-55.0)
        assert verdict.components.runner_up_receiver == "kitchen"  # not the dining receiver
        assert verdict.components.margin_db == pytest.approx(30.0)

        # It does not help either: with the calibrated member outside its
        # bound, the loud uncalibrated one places nobody.
        readings = {"living": steady(-65.0), "dining": steady(-40.0), "kitchen": steady(-85.0)}
        verdict = run(readings, receivers, zones=self.ZONES)
        assert verdict.kind is VerdictKind.HOME_UNKNOWN
        assert verdict.components.reason is Reason.OUTSIDE_BOUND

        # Outside the zone the same receiver DOES stand in the way.
        verdict = run(
            {"living": steady(-55.0), "dining": steady(-40.0), "kitchen": steady(-85.0)}, receivers
        )
        assert verdict.kind is VerdictKind.HOME_UNKNOWN
        assert verdict.components.reason is Reason.MARGIN_TOO_SMALL


# ---------------------------------------------------------------------------
# Deadlines and the clock
# ---------------------------------------------------------------------------


class TestDeadlines:
    RECEIVERS = {
        "kitchen": calibrated(KITCHEN, in_room=-60.0),
        "bath": calibrated(BATH, in_room=-60.0),
    }

    def at(self, now: float, alive: bool = True) -> PlacementVerdict:
        """The same readings (last heard at t=1000), looked at later and later."""
        receivers = {
            rid: Receiver(r.room, r.params, alive_at=now if alive else None)
            for rid, r in self.RECEIVERS.items()
        }
        readings = {"kitchen": steady(-50.0, until=1000.0), "bath": steady(-78.0, until=1000.0)}
        return run(readings, receivers, now=now)

    def test_the_verdict_is_a_function_of_the_clock(self):
        """No report arrives in between: only ``now`` moves. The legacy sweep
        runs inside a BLE report and would stay frozen here."""
        assert self.at(1000.0).kind is VerdictKind.PLACED
        assert self.at(1020.0).kind is VerdictKind.PLACED          # still fresh
        assert self.at(1020.5).kind is VerdictKind.HOME_UNKNOWN    # stale, inside the deadline
        assert self.at(1120.0).kind is VerdictKind.HOME_UNKNOWN
        assert self.at(1120.5).kind is VerdictKind.ABSENT          # past the deadline

    def test_between_fresh_and_deadline_the_device_is_home(self):
        verdict = self.at(1060.0)
        assert verdict.kind is VerdictKind.HOME_UNKNOWN
        assert verdict.components.reason is Reason.NO_FRESH_READING
        assert verdict.components.age_s == pytest.approx(60.0)
        assert verdict.components.censored == ("bath", "kitchen")

    def test_absent_needs_a_receiver_that_is_alive(self):
        verdict = self.at(1500.0, alive=True)
        assert verdict.kind is VerdictKind.ABSENT
        assert verdict.components.reason is Reason.NOT_HEARD
        assert verdict.components.censored == ("bath", "kitchen")
        assert verdict.components.age_s is None

    def test_with_no_receiver_alive_the_answer_is_unavailable_never_absent(self):
        verdict = self.at(1500.0, alive=False)
        assert verdict.kind is VerdictKind.UNAVAILABLE
        assert verdict.components.reason is Reason.NO_RECEIVER_ALIVE
        assert verdict.components.offline == ("bath", "kitchen")

    def test_a_stale_heartbeat_is_not_a_sign_of_life(self):
        receivers = {"kitchen": calibrated(KITCHEN, alive_at=NOW - 61.0)}
        assert run({}, receivers).kind is VerdictKind.UNAVAILABLE
        receivers = {"kitchen": calibrated(KITCHEN, alive_at=NOW - 59.0)}
        assert run({}, receivers).kind is VerdictKind.ABSENT

    def test_a_heartbeat_from_the_future_is_not_a_sign_of_life(self):
        receivers = {"kitchen": calibrated(KITCHEN, alive_at=NOW + 30.0)}
        assert run({}, receivers).kind is VerdictKind.UNAVAILABLE

    def test_one_live_receiver_is_enough_for_absent(self):
        receivers = {
            "kitchen": calibrated(KITCHEN, alive_at=None),
            "bath": uncalibrated(BATH, alive_at=NOW),
        }
        verdict = run({}, receivers)
        assert verdict.kind is VerdictKind.ABSENT
        assert verdict.components.offline == ("kitchen",)

    def test_no_receivers_at_all_is_unavailable(self):
        assert run({}, {}).kind is VerdictKind.UNAVAILABLE

    def test_a_recent_reading_is_a_sign_of_life_of_its_own(self):
        """No heartbeat known, but the receiver reported 30 s ago: it is alive
        and silent now, i.e. censored — not offline."""
        receivers = {"kitchen": calibrated(KITCHEN, alive_at=None)}
        verdict = run({"kitchen": steady(-50.0, until=NOW - 30.0)}, receivers)
        assert evidence_of(verdict, "kitchen").state is ReceiverState.CENSORED

    def test_readings_from_the_future_do_not_count(self):
        receivers = {"kitchen": calibrated(KITCHEN)}
        verdict = run({"kitchen": steady(-50.0, until=NOW + 60.0, count=3)}, receivers)
        assert verdict.kind is VerdictKind.ABSENT

    def test_the_default_config_is_used_when_none_is_given(self):
        verdict = decide({"kitchen": steady(-50.0)}, {"kitchen": calibrated(KITCHEN)}, {}, None, NOW)
        assert verdict.kind is VerdictKind.PLACED


# ---------------------------------------------------------------------------
# The verdict type and the config
# ---------------------------------------------------------------------------


def _components() -> PlacementComponents:
    return run({}, {}).components


class TestTypes:
    def test_a_placed_verdict_needs_a_room_inside_its_zone(self):
        with pytest.raises(ValueError, match="needs a room"):
            PlacementVerdict(VerdictKind.PLACED, _components())
        with pytest.raises(ValueError, match="needs a room"):
            PlacementVerdict(VerdictKind.PLACED, _components(), room=1, rooms=frozenset({2}))

    @pytest.mark.parametrize("kind", [
        VerdictKind.HOME_UNKNOWN, VerdictKind.ABSENT, VerdictKind.UNAVAILABLE,
    ])
    def test_every_other_verdict_carries_no_room(self, kind):
        with pytest.raises(ValueError, match="carries no room"):
            PlacementVerdict(kind, _components(), room=1, rooms=frozenset({1}))
        assert not PlacementVerdict(kind, _components()).covers(1)

    @pytest.mark.parametrize("kwargs", [
        {"time_constant_s": 0.0},
        {"fresh_s": -1.0},
        {"margin_db": -0.1},
        {"hysteresis_db": 11.0, "margin_db": 10.0},
        {"fresh_s": 200.0, "absent_after_s": 120.0},
        {"receiver_alive_s": 0.0},
    ])
    def test_config_rejects_values_that_break_the_rule(self, kwargs):
        with pytest.raises(ValueError):
            PlacementConfig(**kwargs)

    @pytest.mark.parametrize("name", [
        "time_constant_s", "fresh_s", "margin_db", "hysteresis_db",
        "absent_after_s", "receiver_alive_s",
    ])
    @pytest.mark.parametrize("bad", [math.nan, math.inf, -math.inf, True, "10", None])
    def test_config_numbers_must_be_finite(self, name, bad):
        """``nan < 0`` is False: a NaN margin passes a range check and then
        never rejects a placement."""
        with pytest.raises(ValueError, match=f"{name} must be a finite number"):
            PlacementConfig(**{name: bad})


# ---------------------------------------------------------------------------
# Settings parsers
# ---------------------------------------------------------------------------


class TestParsers:
    @pytest.mark.parametrize("raw", ["", "   ", "{}"])
    def test_empty_means_no_receiver_is_calibrated(self, raw):
        assert parse_receiver_params(raw) == {}
        assert parse_zones(raw) == {}

    def test_receiver_parameters(self):
        raw = '{"sat-a": {"offset_db": 2, "in_room_dbm": -60.5, "cutoff_dbm": -80}}'
        assert parse_receiver_params(raw) == {"sat-a": ReceiverParams(2.0, -60.5, -80.0)}

    @pytest.mark.parametrize("raw", [
        "not json",
        "[]",
        '{"sat-a": []}',
        '{"": {"offset_db": 0, "in_room_dbm": -60, "cutoff_dbm": -80}}',
        '{"sat-a": {"offset_db": 0, "in_room_dbm": -60}}',                       # a field missing
        '{"sat-a": {"offset_db": 0, "in_room_dbm": -60, "cutoff_dbm": -80, "x": 1}}',
        '{"sat-a": {"offset_db": "0", "in_room_dbm": -60, "cutoff_dbm": -80}}',  # a string
        '{"sat-a": {"offset_db": true, "in_room_dbm": -60, "cutoff_dbm": -80}}',
        '{"sat-a": {"offset_db": 0, "in_room_dbm": NaN, "cutoff_dbm": -80}}',
        '{"sat-a": {"offset_db": Infinity, "in_room_dbm": -60, "cutoff_dbm": -80}}',
        '{"sat-a": {"offset_db": 0, "in_room_dbm": -60, "cutoff_dbm": -1e999}}',  # parses as -inf
        # An integer no float holds: ``math.isfinite`` raises OverflowError on it.
        '{"sat-a": {"offset_db": 1' + "0" * 400 + ', "in_room_dbm": -60, "cutoff_dbm": -80}}',
        '{"sat-a": {"offset_db": 0, "in_room_dbm": -80, "cutoff_dbm": -80}}',    # bound at the cutoff
        '{"sat-a": {"offset_db": 0, "in_room_dbm": -90, "cutoff_dbm": -80}}',    # bound below it
    ])
    def test_unusable_receiver_parameters_are_rejected(self, raw):
        with pytest.raises(PlacementConfigError):
            parse_receiver_params(raw)

    def test_zones(self):
        assert parse_zones('{"open-plan": [1, 5]}') == {"open-plan": frozenset({1, 5})}

    @pytest.mark.parametrize("raw", [
        "not json",
        "[[1, 5]]",
        '{"open-plan": 1}',
        '{"open-plan": [1]}',                    # one room is not a zone
        '{"open-plan": ["room-a", "room-e"]}',
        '{"open-plan": [1, true]}',
        '{"": [1, 5]}',
        '{"a": [1, 5], "b": [5, 4]}',         # a room in two zones
    ])
    def test_unusable_zones_are_rejected(self, raw):
        with pytest.raises(PlacementConfigError):
            parse_zones(raw)
