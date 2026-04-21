"""
Federation rate-limit registries (F5b).

Two independent in-memory registries, each mapping a key to a
TokenBucketRateLimiter:

- ASKER_OUTBOUND: keyed by `peer.remote_pubkey`. Limits how fast THIS
  Renfield can initiate federation queries against any single remote
  peer. Checked in `MCPManager._execute_federation_streaming` before
  invoking the asker.

- RESPONDER_INBOUND: keyed by the incoming `asker_pubkey`. Limits how
  fast any single paired asker can hammer us. Checked in
  `FederationQueryResponder.handle_initiate` after signature
  verification (a valid sig is required first — no oracle on "which
  pubkey is rate-limited").

Scope:
  Single-process, in-memory. Sufficient for the one-backend-container
  deployment Renfield ships today. F5c (Redis-backed pending requests)
  will extend this too — rate-limit state shared across workers is the
  same story as pending-request state.

Cleanup:
  Limiters accumulate one entry per distinct key. For a household with
  2–5 paired peers total, that's 10 entries combined over a deployment's
  lifetime — no cleanup needed. If we ever face a public/open-enrollment
  deploy the registry grows unbounded; LRU-evict keyed by last-acquire
  time would be the fix. Out of scope for F5b.

Runtime config:
  `settings.federation_*_rate_per_minute` is read ONCE per key on
  first-acquire. Changing the env var at runtime does NOT rebuild
  existing buckets — the new rate only applies to keys that haven't
  been seen yet. A backend restart picks up the new value cleanly.
"""
from __future__ import annotations

import asyncio

from services.mcp_client import TokenBucketRateLimiter
from utils.config import settings


# Keyed by `peer.remote_pubkey` (hex string). One limiter per remote peer.
_asker_outbound: dict[str, TokenBucketRateLimiter] = {}
# Keyed by `asker_pubkey` (hex string from the incoming envelope).
_responder_inbound: dict[str, TokenBucketRateLimiter] = {}

# Guards registry mutation on first-acquire (two concurrent initiates
# for a new peer could race and create two limiters, losing one).
_asker_lock = asyncio.Lock()
_responder_lock = asyncio.Lock()


async def acquire_asker_token(peer_pubkey: str) -> bool:
    """Try to acquire one outbound token for `peer_pubkey`.

    Returns True if the asker may proceed, False if rate-limited.
    The limiter is created on first call and cached; subsequent
    calls reuse the same bucket.
    """
    bucket = _asker_outbound.get(peer_pubkey)
    if bucket is None:
        async with _asker_lock:
            # Double-check inside the lock (another coroutine may have
            # created it while we awaited).
            bucket = _asker_outbound.get(peer_pubkey)
            if bucket is None:
                bucket = TokenBucketRateLimiter(
                    rate_per_minute=settings.federation_asker_rate_per_minute,
                )
                _asker_outbound[peer_pubkey] = bucket
    return await bucket.acquire()


async def acquire_responder_token(asker_pubkey: str) -> bool:
    """Try to acquire one inbound token for `asker_pubkey`."""
    bucket = _responder_inbound.get(asker_pubkey)
    if bucket is None:
        async with _responder_lock:
            bucket = _responder_inbound.get(asker_pubkey)
            if bucket is None:
                bucket = TokenBucketRateLimiter(
                    rate_per_minute=settings.federation_responder_rate_per_minute,
                )
                _responder_inbound[asker_pubkey] = bucket
    return await bucket.acquire()


def reset_for_tests() -> None:
    """Test-only reset — every test starts with a clean view."""
    _asker_outbound.clear()
    _responder_inbound.clear()
