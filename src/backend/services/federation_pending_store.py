"""
Pluggable storage for federation `_PendingRequest` + nonce cache (F5c).

Two backends:

- **InMemoryPendingStore** — single-process dict + OrderedDict, identical
  to the pre-F5c behavior. Default. All existing tests use this.
- **RedisPendingStore** — opt-in via `settings.federation_pending_use_redis`.
  Multi-worker deploys need this so a poll landing on a different
  worker than the initiate can still read state, AND so nonce dedup
  works across workers (replay defense).

Background-task ownership:
  Even with the Redis backend, the bg `_run_query` task that produces
  the answer runs on the worker that handled `/initiate`. Other
  workers can READ progress + terminal state via Redis but never
  execute the synthesis. If the originating worker crashes mid-query,
  the request is stranded until TTL expiry — F5 graceful-drain is a
  separate future concern.

Wire format (Redis):
  - `fed:pending:{request_id}` → JSON-serialized `_PendingRequest`
    with EXPIRE = REQUEST_TTL_SECONDS + 5s grace.
  - `fed:nonce:{nonce}` → "1" with EXPIRE = NONCE_WINDOW_SECONDS + 60s
    grace (matches the local OrderedDict eviction window).
  - `fed:asker:{pubkey}` → SET of request_ids, used by
    `purge_requests_for_pubkey`. Each membership add re-EXPIREs the
    set so it lives at least as long as its members.
"""
from __future__ import annotations

import json
import time
from collections import OrderedDict
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING, Protocol

from loguru import logger

from services.atom_types import Provenance
from services.federation_query_schemas import (
    STATUS_EXPIRED,
    STATUS_PROCESSING,
)
from services.mcp_streaming import PROGRESS_LABEL_RETRIEVING
from utils.config import settings

if TYPE_CHECKING:
    from redis.asyncio import Redis


REQUEST_TTL_SECONDS = 60
NONCE_WINDOW_SECONDS = 60
NONCE_GRACE_SECONDS = 60          # how long past the window we still remember
# In-memory only — the Redis backend bounds growth via TTL rather than
# a size cap. Tuning this here has no effect on multi-worker deploys.
NONCE_CACHE_MAX = 4096

_REDIS_PENDING_PREFIX = "fed:pending:"
_REDIS_NONCE_PREFIX = "fed:nonce:"
_REDIS_ASKER_SET_PREFIX = "fed:asker:"


@dataclass
class _PendingRequest:
    """One in-flight federated query, kept until terminal or TTL expiry."""
    request_id: str
    asker_pubkey: str
    peer_user_id: int
    asker_local_user_id: int | None
    max_visible_tier: int
    query: str
    initiated_at: float
    status: str = STATUS_PROCESSING
    progress_label: str = PROGRESS_LABEL_RETRIEVING
    progress_count: int = 0
    answer: str | None = None
    provenance: list[Provenance] = field(default_factory=list)
    answered_at: float | None = None
    error_message: str | None = None


def _to_jsonable(pr: _PendingRequest) -> dict:
    """Convert dataclass → JSON-safe dict. `provenance` is a list of
    Provenance dataclasses; serialize each via `asdict`."""
    d = asdict(pr)
    d["provenance"] = [asdict(p) for p in pr.provenance]
    return d


def _from_jsonable(d: dict) -> _PendingRequest:
    """Reverse of `_to_jsonable`. Accepts either dataclass-asdict or
    raw dict provenance entries."""
    prov_dicts = d.pop("provenance", [])
    pr = _PendingRequest(**d)
    pr.provenance = [Provenance(**p) for p in prov_dicts]
    return pr


# =============================================================================
# Store protocol
# =============================================================================


class PendingStore(Protocol):
    """All ops are async to keep the Redis impl natural; the in-memory
    impl awaits trivially."""

    async def get(self, request_id: str) -> _PendingRequest | None: ...
    async def put(self, pending: _PendingRequest) -> None: ...
    async def save(self, pending: _PendingRequest) -> None: ...
    async def list_for_pubkey(self, asker_pubkey: str) -> list[_PendingRequest]: ...
    async def delete_many(self, request_ids: list[str]) -> None: ...
    async def prune_expired(self, now: float | None = None) -> int: ...
    async def record_nonce(self, nonce: str, now: float) -> bool: ...
    async def clear_for_tests(self) -> None: ...


# =============================================================================
# In-memory implementation (default)
# =============================================================================


class InMemoryPendingStore:
    """Module-local dicts. Single-process semantics — identical to the
    pre-F5c responder behavior."""

    def __init__(self) -> None:
        self._pending: dict[str, _PendingRequest] = {}
        self._nonces: OrderedDict[str, float] = OrderedDict()

    async def get(self, request_id: str) -> _PendingRequest | None:
        return self._pending.get(request_id)

    async def put(self, pending: _PendingRequest) -> None:
        self._pending[pending.request_id] = pending

    async def save(self, pending: _PendingRequest) -> None:
        # In-memory mutations are aliased — nothing to do; the caller
        # has been mutating the same instance we already store.
        self._pending[pending.request_id] = pending

    async def list_for_pubkey(self, asker_pubkey: str) -> list[_PendingRequest]:
        return [
            pr for pr in list(self._pending.values())
            if pr.asker_pubkey == asker_pubkey
        ]

    async def delete_many(self, request_ids: list[str]) -> None:
        for rid in request_ids:
            self._pending.pop(rid, None)

    async def prune_expired(self, now: float | None = None) -> int:
        t = now if now is not None else time.time()
        # Snapshot via list() so an interleaving put() can't raise.
        expired = [
            rid for rid, pr in list(self._pending.items())
            if t - pr.initiated_at > REQUEST_TTL_SECONDS
            and pr.status == STATUS_PROCESSING
        ]
        for rid in expired:
            pr = self._pending.get(rid)
            if pr is None:
                continue
            pr.status = STATUS_EXPIRED
            logger.debug(
                f"Federation query_brain: expired {rid} (peer={pr.peer_user_id})"
            )
        return len(expired)

    async def record_nonce(self, nonce: str, now: float) -> bool:
        # Drop entries past the window first.
        cutoff = now - (NONCE_WINDOW_SECONDS + NONCE_GRACE_SECONDS)
        while self._nonces:
            oldest_nonce, oldest_at = next(iter(self._nonces.items()))
            if oldest_at < cutoff:
                self._nonces.pop(oldest_nonce, None)
            else:
                break
        if nonce in self._nonces:
            return False
        if len(self._nonces) >= NONCE_CACHE_MAX:
            self._nonces.popitem(last=False)
        self._nonces[nonce] = now
        return True

    async def clear_for_tests(self) -> None:
        self._pending.clear()
        self._nonces.clear()


# =============================================================================
# Redis implementation (opt-in)
# =============================================================================


class RedisPendingStore:
    """Multi-worker store. All state lives in Redis; the worker process
    holds nothing but a connection.

    Failure mode: if Redis is unreachable, every operation raises and
    the responder returns a 400 to the asker (same as a route-level
    error). We don't fall back to in-memory because that would break
    cross-worker correctness silently — better to fail loud and let
    the operator fix Redis."""

    def __init__(self, redis: "Redis"):
        self._r = redis

    @staticmethod
    def _pending_key(request_id: str) -> str:
        return f"{_REDIS_PENDING_PREFIX}{request_id}"

    @staticmethod
    def _nonce_key(nonce: str) -> str:
        return f"{_REDIS_NONCE_PREFIX}{nonce}"

    @staticmethod
    def _asker_set_key(asker_pubkey: str) -> str:
        return f"{_REDIS_ASKER_SET_PREFIX}{asker_pubkey}"

    async def get(self, request_id: str) -> _PendingRequest | None:
        raw = await self._r.get(self._pending_key(request_id))
        if raw is None:
            return None
        return _from_jsonable(json.loads(raw))

    async def put(self, pending: _PendingRequest) -> None:
        # Set both the body (with TTL) and add to the asker's index set.
        body = json.dumps(_to_jsonable(pending))
        async with self._r.pipeline() as pipe:
            pipe.set(self._pending_key(pending.request_id), body,
                     ex=REQUEST_TTL_SECONDS + 5)
            pipe.sadd(self._asker_set_key(pending.asker_pubkey),
                      pending.request_id)
            pipe.expire(self._asker_set_key(pending.asker_pubkey),
                        REQUEST_TTL_SECONDS + 5)
            await pipe.execute()

    async def save(self, pending: _PendingRequest) -> None:
        # Mutations from the bg task — re-serialize and write through.
        # The asker-set entry was already created by put(); we don't
        # touch it here.
        #
        # TTL refresh: every save() resets the key's expiry to
        # REQUEST_TTL_SECONDS + 5s. Net effect: a synthesis that
        # emits 4 progress updates plus a terminal save can keep
        # the key alive past the in-memory `initiated_at + TTL`
        # window. This is a deliberate UX improvement (the asker
        # gets a fair poll window AFTER terminal status, not just
        # from initiation) but is a documented divergence from the
        # InMemory backend's strict initiated_at-based expiry.
        # Bounded in practice by MAX_PROGRESS_UPDATES (4) +
        # synthesis timeout — a stuck request can't keep claiming
        # storage indefinitely.
        body = json.dumps(_to_jsonable(pending))
        await self._r.set(
            self._pending_key(pending.request_id), body,
            ex=REQUEST_TTL_SECONDS + 5,
        )

    async def list_for_pubkey(self, asker_pubkey: str) -> list[_PendingRequest]:
        # `services.redis_client` constructs the client with
        # `decode_responses=True`, so `smembers` returns `set[str]` —
        # no bytes-decode dance needed.
        rids = await self._r.smembers(self._asker_set_key(asker_pubkey))
        if not rids:
            return []
        result: list[_PendingRequest] = []
        for rid in rids:
            pr = await self.get(rid)
            if pr is not None:
                result.append(pr)
        return result

    async def delete_many(self, request_ids: list[str]) -> None:
        if not request_ids:
            return
        async with self._r.pipeline() as pipe:
            for rid in request_ids:
                pipe.delete(self._pending_key(rid))
            await pipe.execute()

    async def prune_expired(self, now: float | None = None) -> int:
        # Redis EXPIRE handles eviction for us. This is a no-op kept
        # for interface symmetry — the InMemory impl needs explicit
        # pruning because it has no built-in TTL.
        return 0

    async def record_nonce(self, nonce: str, now: float) -> bool:
        # SET NX with TTL = atomic "claim if absent". Returns True if
        # the key was set (new nonce); False if it already existed.
        ok = await self._r.set(
            self._nonce_key(nonce), "1",
            nx=True,
            ex=NONCE_WINDOW_SECONDS + NONCE_GRACE_SECONDS,
        )
        return bool(ok)

    async def clear_for_tests(self) -> None:
        # Best-effort scan-and-delete. We don't rely on this in
        # production (Redis TTL handles cleanup). Tests against a
        # real Redis should use a unique key prefix per test run.
        keys = await self._r.keys(f"{_REDIS_PENDING_PREFIX}*")
        keys += await self._r.keys(f"{_REDIS_NONCE_PREFIX}*")
        keys += await self._r.keys(f"{_REDIS_ASKER_SET_PREFIX}*")
        if keys:
            await self._r.delete(*keys)


# =============================================================================
# Factory
# =============================================================================


_store: PendingStore | None = None


def get_pending_store() -> PendingStore:
    """Return the process-wide store. First call selects the backend
    based on `settings.federation_pending_use_redis`. Subsequent calls
    reuse the same instance.

    Backend choice is sticky for the process lifetime. Flipping
    `federation_pending_use_redis` at runtime requires a backend
    restart to take effect. Tests can call `reset_store_for_tests()`
    to force re-selection (e.g., to swap backends within one process).
    """
    global _store
    if _store is None:
        if settings.federation_pending_use_redis:
            from services.redis_client import get_redis
            _store = RedisPendingStore(get_redis())
            logger.info("Federation pending store: Redis (multi-worker safe)")
        else:
            _store = InMemoryPendingStore()
            logger.info("Federation pending store: in-memory (single-worker)")
    return _store


def reset_store_for_tests() -> None:
    """Force re-selection of the backend on next get_pending_store().
    Mostly for tests that need to swap backends within one process."""
    global _store
    _store = None
