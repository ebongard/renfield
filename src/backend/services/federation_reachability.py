"""
Federation reachability -- what a pairing's status light may honestly say.

Until this module a peer counted as "reachable" on the kiosk when it had
QUERIED THIS INSTANCE within five minutes. That is a statement about the far
side's curiosity, not about the connection: a healthy pairing nobody used for
two weeks read as dead. Three things are now kept apart:

  paired           the row exists and is not revoked -- from the database.
  recently active  a real REQUEST went in either direction lately
                   (`peer_users.last_seen_at` inbound, the newest `asked` row
                   of `federation_query_log` outbound).
  reachable        a signed round trip (`federation_query_asker.ping_peer`)
                   came back verified. Its other outcomes stay distinguishable
                   because each needs a different repair -- see `reading_for`.

Where the result lives, and why not in the database
---------------------------------------------------
A probe result is an OBSERVATION with a shelf life, not a fact about the
pairing. It is kept in Redis under a TTL (`RESULT_TTL_SECONDS`); a result
nobody refreshed simply ceases to exist and the pairing reads `unknown` again.
A column would need the same expiry bolted on in every reader, and the first
reader to forget it would show a two-week-old "reachable" as current -- the very
defect this replaces. It also keeps `last_seen_at` untouched (it goes on
meaning "last request") and adds no write to `peer_users` per probe.

Redis is shared by every backend process, which is also what stops a double
probe: a probe is preceded by a claim (`SET NX EX`), so two processes whose
kiosk refreshers tick together -- or a refresher and somebody pressing "check
now" -- send ONE ping and both read its result. Without Redis the store falls
back to this process's memory; that is correct for one process and merely
redundant (never wrong) for several.

No free-running timer lives here. A probe happens because a wall display is
connected (`api.lifecycle._schedule_kiosk_peer_status_refresh`, the connect
hook in `kiosk_handler`) or because a person asked for one (`POST
/api/federation/peers/{id}/check`). Everything is behind
`settings.federation_reachability_enabled`.
"""
from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

from loguru import logger

from services.federation_query_asker import (
    PING_OUTCOMES,
    PING_REACHABLE,
    PING_UNSUPPORTED,
    ping_peer,
)
from utils.config import settings

# --- the three states a status light can show --------------------------------
STATE_REACHABLE = "reachable"
STATE_UNREACHABLE = "unreachable"
STATE_UNKNOWN = "unknown"

#: Reason code of `unknown` when no probe result exists (never probed, or the
#: last one expired). The other reason codes are the ping outcomes themselves.
REASON_NOT_CHECKED = "not_checked"

# --- cadence -----------------------------------------------------------------
#: A stored result younger than this is good enough for the refresher; it does
#: not probe again. One signed request per pairing per five minutes, and only
#: while a wall display is connected.
PROBE_INTERVAL_SECONDS = 300
#: A result older than this is forgotten and the pairing reads `unknown`. Three
#: missed rounds, so one lost probe does not flicker the light.
RESULT_TTL_SECONDS = 3 * PROBE_INTERVAL_SECONDS
#: After a probe of a pairing started, nobody on this instance starts another
#: one for this long -- the multi-process guard, and the floor under an
#: impatient "check now".
CLAIM_TTL_SECONDS = 30
#: Ceiling for one whole probe round. A ping is bounded by itself
#: (`PING_TOTAL_TIMEOUT_SECONDS` plus the pin pre-flight); this bounds the
#: round even if that bound is ever loosened.
PROBE_ROUND_TIMEOUT_SECONDS = 15.0
#: "Recently active" = a request in either direction within this window. The
#: same five minutes the kiosk used to call "reachable".
RECENTLY_ACTIVE_WITHIN_SECONDS = 300
#: How long a Redis call may take before the store answers from memory. The
#: kiosk snapshot reads through here, and a slow Redis must not hold a wall
#: display on its skeleton.
_REDIS_OP_TIMEOUT_SECONDS = 1.0

_RESULT_KEY = "federation:reachability:result:{peer_id}"
_CLAIM_KEY = "federation:reachability:claim:{peer_id}"


@dataclass(frozen=True)
class Reading:
    """One pairing's reachability as shown to a person."""
    state: str                 # STATE_*
    reason: str | None         # a ping outcome, REASON_NOT_CHECKED, or None when reachable
    checked_at: float | None   # unix seconds of the probe; None = never / expired

    def as_payload(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "reason": self.reason,
            "checked_at": _iso(self.checked_at),
        }


NOT_CHECKED = Reading(state=STATE_UNKNOWN, reason=REASON_NOT_CHECKED, checked_at=None)


def reading_for(outcome: str, checked_at: float) -> Reading:
    """Fold a ping outcome into a state.

    🛑 `unsupported` is UNKNOWN, never unreachable: a far side that is one
    release behind, or has reachability switched off, answers 404 to a ping
    while its pairing works perfectly. Painting that red would be a false
    alarm on every wall during every staggered rollout.
    """
    if outcome == PING_REACHABLE:
        return Reading(STATE_REACHABLE, None, checked_at)
    if outcome == PING_UNSUPPORTED:
        return Reading(STATE_UNKNOWN, outcome, checked_at)
    if outcome in PING_OUTCOMES:
        return Reading(STATE_UNREACHABLE, outcome, checked_at)
    # A value this version does not know (written by a newer process during a
    # rolling deploy): say so, rather than guess at red or green.
    return Reading(STATE_UNKNOWN, outcome, checked_at)


def _iso(ts: float | None) -> str | None:
    """Naive-UTC ISO, the shape `last_seen_at` already has on these payloads."""
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, tz=UTC).replace(tzinfo=None, microsecond=0).isoformat()


# =============================================================================
# Store
# =============================================================================


class ReachabilityStore(Protocol):
    async def claim(self, peer_id: int) -> bool: ...
    async def save(self, peer_id: int, outcome: str, checked_at: float) -> None: ...
    async def load(self, peer_ids: list[int]) -> dict[int, tuple[str, float]]: ...


class MemoryReachabilityStore:
    """Process-local. The fallback, and what the tests run on."""

    def __init__(self) -> None:
        self._results: dict[int, tuple[str, float]] = {}
        self._claims: dict[int, float] = {}

    async def claim(self, peer_id: int) -> bool:
        now = time.monotonic()
        if self._claims.get(peer_id, 0.0) > now:
            return False
        self._claims[peer_id] = now + CLAIM_TTL_SECONDS
        return True

    async def save(self, peer_id: int, outcome: str, checked_at: float) -> None:
        self._results[peer_id] = (outcome, checked_at)

    async def load(self, peer_ids: list[int]) -> dict[int, tuple[str, float]]:
        horizon = time.time() - RESULT_TTL_SECONDS
        found: dict[int, tuple[str, float]] = {}
        for peer_id in peer_ids:
            entry = self._results.get(peer_id)
            if entry is None:
                continue
            if entry[1] < horizon:
                self._results.pop(peer_id, None)
                continue
            found[peer_id] = entry
        return found


class RedisReachabilityStore:
    """Shared by every backend process; answers from memory when Redis does not.

    The fallback is per OPERATION, not sticky: a Redis that comes back is used
    again on the next call. The client is looked up per call as well
    (`services.redis_client.get_redis`) unless one was handed in: the shared
    client can be closed and rebuilt underneath a long-lived store.
    """

    def __init__(
        self, redis: Any = None, fallback: MemoryReachabilityStore | None = None,
    ):
        self._redis = redis
        self._fallback = fallback or MemoryReachabilityStore()

    @property
    def _r(self) -> Any:
        if self._redis is not None:
            return self._redis
        from services.redis_client import get_redis

        return get_redis()

    async def _call(self, awaitable: Any) -> Any:
        return await asyncio.wait_for(awaitable, timeout=_REDIS_OP_TIMEOUT_SECONDS)

    async def claim(self, peer_id: int) -> bool:
        try:
            return bool(await self._call(self._r.set(
                _CLAIM_KEY.format(peer_id=peer_id), "1", nx=True, ex=CLAIM_TTL_SECONDS,
            )))
        except Exception as e:
            logger.debug(f"Federation reachability: Redis claim failed, using memory: {e!r}")
            return await self._fallback.claim(peer_id)

    async def save(self, peer_id: int, outcome: str, checked_at: float) -> None:
        # Memory too, so a Redis outage right after still has the last reading.
        await self._fallback.save(peer_id, outcome, checked_at)
        try:
            await self._call(self._r.set(
                _RESULT_KEY.format(peer_id=peer_id),
                json.dumps({"outcome": outcome, "checked_at": checked_at}),
                ex=RESULT_TTL_SECONDS,
            ))
        except Exception as e:
            logger.debug(f"Federation reachability: Redis save failed, kept in memory: {e!r}")

    async def load(self, peer_ids: list[int]) -> dict[int, tuple[str, float]]:
        if not peer_ids:
            return {}
        try:
            raw = await self._call(self._r.mget(
                [_RESULT_KEY.format(peer_id=peer_id) for peer_id in peer_ids]
            ))
        except Exception as e:
            logger.debug(f"Federation reachability: Redis load failed, using memory: {e!r}")
            return await self._fallback.load(peer_ids)
        found: dict[int, tuple[str, float]] = {}
        for peer_id, value in zip(peer_ids, raw, strict=True):
            if not value:
                continue
            try:
                entry = json.loads(value)
                found[peer_id] = (str(entry["outcome"]), float(entry["checked_at"]))
            except (ValueError, KeyError, TypeError):
                continue
        return found


_store: ReachabilityStore | None = None


def get_store() -> ReachabilityStore:
    global _store
    if _store is None:
        _store = RedisReachabilityStore()
    return _store


def set_store_for_tests(store: ReachabilityStore | None) -> None:
    """Test-only: pin the store (`None` = pick again on next use)."""
    global _store
    _store = store


# =============================================================================
# Reading
# =============================================================================


async def read_reachability(peer_ids: list[int]) -> dict[int, Reading]:
    """The current reading for each pairing -- `NOT_CHECKED` where none exists.

    Read-only and bounded: this is what the kiosk snapshot and `GET /peers`
    call, and neither may wait on a far instance.
    """
    try:
        stored = await get_store().load(peer_ids)
    except Exception as e:
        logger.debug(f"Federation reachability: read failed: {e!r}")
        stored = {}
    return {
        peer_id: reading_for(*stored[peer_id]) if peer_id in stored else NOT_CHECKED
        for peer_id in peer_ids
    }


# =============================================================================
# Probing
# =============================================================================


async def probe_peer(peer: Any, *, force: bool = False) -> Reading:
    """Probe one pairing unless that is not due, and return its reading.

    Not due means: a stored result younger than `PROBE_INTERVAL_SECONDS`
    (skipped with `force`, the "check now" case), a claim somebody else holds
    (a probe of this pairing started less than `CLAIM_TTL_SECONDS` ago, in this
    or another process), or an empty outbound ping budget. In each case the
    stored reading is returned as it stands -- with ITS `checked_at`, so the
    caller never passes an old result off as fresh.
    """
    if not settings.federation_reachability_enabled:
        return NOT_CHECKED

    store = get_store()
    current = (await read_reachability([peer.id]))[peer.id]
    if (
        not force
        and current.checked_at is not None
        and time.time() - current.checked_at < PROBE_INTERVAL_SECONDS
    ):
        return current

    if not await store.claim(peer.id):
        return current

    from services.federation_rate_limits import acquire_ping_asker_token
    if not await acquire_ping_asker_token(peer.remote_pubkey):
        logger.debug(f"Federation reachability: ping budget spent for peer {peer.id}")
        return current

    try:
        outcome = await ping_peer(peer)
    except asyncio.CancelledError:
        raise
    except Exception as e:
        # A fault of OURS is no finding about the far side: record nothing
        # rather than paint a pairing red because of a bug on this end.
        logger.warning(f"Federation reachability: probe of peer {peer.id} failed locally: {e!r}")
        return current

    checked_at = time.time()
    await store.save(peer.id, outcome, checked_at)
    if reading_for(outcome, checked_at).state != current.state:
        logger.info(f"🔗 Federation peer {peer.id} reachability: {outcome}")
    return reading_for(outcome, checked_at)


async def probe_due_peers() -> None:
    """One probe round over every active pairing whose reading is due.

    Called by the kiosk refresher while a wall display is connected, and once
    when one connects. Pings run concurrently and the round has a ceiling, so a
    hanging peer delays neither the others nor the next tick. Never raises.
    """
    if not settings.federation_reachability_enabled:
        return
    try:
        from sqlalchemy import select

        from models.database import PeerUser
        from services.database import AsyncSessionLocal

        async with AsyncSessionLocal() as db:
            peers = (await db.execute(
                select(PeerUser).where(PeerUser.revoked_at.is_(None))
            )).scalars().all()
        if not peers:
            return
        await asyncio.wait_for(
            asyncio.gather(*(probe_peer(p) for p in peers), return_exceptions=True),
            timeout=PROBE_ROUND_TIMEOUT_SECONDS,
        )
    except asyncio.CancelledError:
        raise
    except Exception as e:
        logger.debug(f"Federation reachability: probe round incomplete: {e!r}")


# =============================================================================
# Activity
# =============================================================================


async def last_asked_at(db: Any, peer_ids: list[int]) -> dict[int, datetime]:
    """When THIS instance last sent each pairing a real query (naive UTC).

    The newest `asked` row of the audit log. Rows are pruned after 90 days and
    are not written at all with auth off -- "no entry" therefore means "not
    lately", which is all a recently-active light needs.
    """
    if not peer_ids:
        return {}
    from sqlalchemy import func, select

    from models.database import FederationQueryLog

    rows = (await db.execute(
        select(FederationQueryLog.peer_user_id, func.max(FederationQueryLog.initiated_at))
        .where(
            FederationQueryLog.direction == "asked",
            FederationQueryLog.peer_user_id.in_(peer_ids),
        )
        .group_by(FederationQueryLog.peer_user_id)
    )).all()
    return {peer_id: at for peer_id, at in rows if peer_id is not None and at is not None}


def recently_active(
    last_request_in: datetime | None,
    last_request_out: datetime | None,
    *,
    now: datetime | None = None,
) -> bool:
    """A real request in EITHER direction within the window. Pings never count."""
    now = now or datetime.now(UTC)
    for at in (last_request_in, last_request_out):
        if at is None:
            continue
        aware = at if at.tzinfo else at.replace(tzinfo=UTC)
        if (now - aware).total_seconds() < RECENTLY_ACTIVE_WITHIN_SECONDS:
            return True
    return False
