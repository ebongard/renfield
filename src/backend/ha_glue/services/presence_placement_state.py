"""
Presence placement v2 — stage 2: the presence STATE follows v2.

With ``PRESENCE_PLACEMENT_V2_STATE_ENABLED`` on, this replaces the legacy room
decision (``_assign_room`` / ``_cleanup_stale`` in ``presence_service.py``):

  * every tracked device gets its v2 verdict (``presence_placement.decide``);
  * the verdicts of one person and their voice/auth sighting are joined by ONE
    rule — the source that changed zone LAST wins (``combine_person``), so a
    second phone lying in another room cannot pull the person to it (#1551);
  * expiry is a deadline per device, kept by a timer, not a sweep that only
    runs when some satellite happens to report; ``unavailable`` (no receiver
    alive) changes nothing and fires no leave event;
  * a person heard at home but placed by nothing keeps the last room booked
    and is UNCERTAIN, as is a person whose sources disagree: the privacy gates
    treat them as possible listeners anywhere (``is_location_uncertain``).

It is a ``PlacementShadow``: the same receivers, clock guard and per-device
verdicts, the same voice line, metric and live-readings payload. What it does
not do is compare against the legacy decision — that decision no longer runs.
Design: ``docs/design/ble-presence-improvement.md`` → "Stage 2".
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from loguru import logger

from ha_glue.services.presence_placement import (
    Claim,
    PersonKind,
    PersonVerdict,
    PlacementVerdict,
    ReceiverState,
    VerdictKind,
    combine_person,
)
from ha_glue.services.presence_placement_shadow import PlacementShadow

if TYPE_CHECKING:
    from ha_glue.services.presence_service import PresenceService

LOG_PREFIX = "Presence v2 state:"

# A wake-up lands this long after the deadline it is for, so the decision at
# that moment sees the reading as just past the bound, not exactly on it.
_WAKE_SLACK_S = 0.05

VOICE_SOURCE = "voice"


def _confidence(verdict: PlacementVerdict) -> float | None:
    """The legacy confidence formula on the vouching receiver's smoothed level
    and the number of heard calibrated receivers in the zone — carried over
    unchanged until stage 3 computes confidence from the components."""
    c = verdict.components
    members = [
        e for e in c.receivers
        if e.state is ReceiverState.HEARD and e.calibrated and e.room in verdict.rooms
    ]
    best = next((e for e in members if e.receiver_id == c.best_receiver), None)
    if best is None or best.smoothed_dbm is None:
        return None
    rssi_conf = max(0.0, min(1.0, (best.smoothed_dbm + 90) / 60.0))
    return rssi_conf * 0.7 + min(1.0, len(members) / 3.0) * 0.3


class PlacementState(PlacementShadow):
    """Placement v2 driving the presence state."""

    _CHECKS_LEGACY = False
    _LOG_PREFIX = LOG_PREFIX
    _FLAG = "presence_placement_v2_state_enabled"
    _OFF_MESSAGE = "settings rejected, the LEGACY decision acts"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._timer: asyncio.TimerHandle | None = None
        self._tick_task: asyncio.Task | None = None
        self._booking_failed_logged = False

    # ------------------------------------------------------------------
    # Entry points
    # ------------------------------------------------------------------

    def observe_report(self, presence: PresenceService, now: float) -> None:
        """Decide every device, join them per person, book the result, and
        re-arm the deadline timer. Called after every BLE report and by the
        timer itself."""
        try:
            if not self._clock_allows_a_decision(now):
                return
            receivers = self._receivers(presence)
            decided: dict[str, PlacementVerdict] = {}
            by_user: dict[int, list[tuple[str, PlacementVerdict]]] = {}
            for key in list(presence._sightings):
                user_id = presence._user_for_key(key)
                if user_id is None:
                    continue
                verdict = self._decide(presence, key, receivers, now)
                decided[key] = verdict
                self._note_zone(key, verdict, now)
                by_user.setdefault(user_id, []).append((key, verdict))
            self._verdicts = decided
            self._zone_since = {k: v for k, v in self._zone_since.items() if k in decided}

            for user_id in sorted(set(by_user) | set(presence._presence)):
                self._book(presence, user_id, by_user.get(user_id, []), now)
        except Exception as e:
            # Unlike the shadow, a failure here leaves presence where it was.
            # Loud once, then quiet: the type only (a message may carry a key).
            if not self._booking_failed_logged:
                self._booking_failed_logged = True
                logger.error(
                    f"{LOG_PREFIX} deciding failed ({type(e).__name__}); presence "
                    "stays as last booked. Further failures are not logged."
                )
        finally:
            self.schedule(presence, now)

    def schedule(self, presence: PresenceService, now: float) -> None:
        """Arm ONE timer for the next moment a verdict can change without a
        new report: a reading ageing past the freshness window or the deadline,
        a receiver's sign of life running out, a voice hold or a voice-only
        person's deadline. No polling: no deadline ahead, no timer."""
        try:
            wake = self._next_deadline(presence, now)
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None
            if wake is None:
                return
            loop = asyncio.get_running_loop()
            delay = max(0.0, wake - now) + _WAKE_SLACK_S
            self._timer = loop.call_later(delay, self._fire, loop, presence)
        except RuntimeError:
            # No running loop (a synchronous caller, e.g. a test): nothing to arm.
            self._timer = None
        except Exception as e:
            self._log_failure("schedule", e)

    def _fire(self, loop: asyncio.AbstractEventLoop, presence: PresenceService) -> None:
        # Keep a reference: the loop holds tasks only weakly.
        self._timer = None
        self._tick_task = loop.create_task(presence._placement_state_tick())

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _book(
        self,
        presence: PresenceService,
        user_id: int,
        devices: list[tuple[str, PlacementVerdict]],
        now: float,
    ) -> None:
        current = presence._presence.get(user_id)
        kinds = [v.kind for _, v in devices]
        claims: list[Claim] = []
        device_zones: set[frozenset] = set()
        by_source: dict[str, PlacementVerdict] = {}
        for key, verdict in devices:
            claim = self._claim(key, verdict, now)
            if claim is not None:
                claims.append(claim)
                device_zones.add(claim.rooms)
                by_source[key] = verdict

        all_gone = all(k is VerdictKind.ABSENT for k in kinds)
        voice_live = (
            current is not None
            and current.voice_pinned
            and current.room_id is not None
            # A voice claim outlives its devices only up to the deadline: a
            # person who spoke and whose phone is gone is absent after it.
            and not (all_gone and now - current.voice_at > self._config.absent_after_s)
        )
        if voice_live:
            claims.append(Claim(
                source=VOICE_SOURCE,
                rooms=self._zone_of(current.room_id),
                room=current.room_id,
                since=current.voice_at,
            ))

        incumbent = self._zone_of(current.room_id) if current and current.room_id is not None else None
        if not devices and not voice_live:
            person = PersonVerdict(PersonKind.ABSENT)
        else:
            person = combine_person(kinds, claims, incumbent)

        winner = person.winner
        winning = by_source.get(winner.source) if winner is not None else None
        presence._apply_placement(
            user_id,
            person,
            now=now,
            last_heard=self._last_heard(presence, [k for k, _ in devices], now),
            device_zones=device_zones,
            satellite_id=winning.components.best_receiver if winning else None,
            confidence=_confidence(winning) if winning else None,
        )

    def _last_heard(self, presence: PresenceService, keys: list[str], now: float) -> float | None:
        """Newest reading of these devices that lies inside the deadline."""
        newest = None
        for key in keys:
            for s in presence._sightings.get(key, ()):
                if not getattr(s, "comparable", True):
                    continue  # Classic BT: v2 reads no such sighting (DeviceSighting)
                if 0.0 <= now - s.timestamp <= self._config.absent_after_s and (
                    newest is None or s.timestamp > newest
                ):
                    newest = s.timestamp
        return newest

    def _next_deadline(self, presence: PresenceService, now: float) -> float | None:
        cfg = self._config
        candidates: list[float] = []
        for key, sightings in presence._sightings.items():
            if presence._user_for_key(key) is None:
                continue
            for s in sightings:
                if not getattr(s, "comparable", True):
                    continue
                candidates.append(s.timestamp + cfg.fresh_s)
                candidates.append(s.timestamp + cfg.absent_after_s)
        for p in presence._presence.values():
            candidates.append(p.last_seen + cfg.absent_after_s)
            if p.voice_pinned:
                candidates.append(p.voice_at + cfg.absent_after_s)
            if p.voice_hold_until:
                candidates.append(p.voice_hold_until)
        # A receiver's sign of life running out — the same receivers, with the
        # same window, the decision sees (a scanner's last advert or a heartbeat).
        for receiver in self._receivers(presence).values():
            if receiver.alive_at is not None:
                window = receiver.alive_window_s
                candidates.append(
                    receiver.alive_at + (window if window is not None else cfg.receiver_alive_s)
                )
        ahead = [t for t in candidates if t > now]
        return min(ahead) if ahead else None
