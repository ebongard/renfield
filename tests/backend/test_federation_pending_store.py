"""
Tests for F5c — pluggable federation pending-request store.

Two backends live in services/federation_pending_store.py:

- InMemoryPendingStore (default, single-process) — tested directly.
- RedisPendingStore (multi-worker, opt-in) — tested via a small
  in-process stub that implements the aioredis interface subset
  we use (get/set/keys/delete/sadd/smembers/expire + pipeline).
  Exercises the actual serialization round-trip without needing a
  live Redis.
"""
from __future__ import annotations

import json
import time

import pytest

from services.atom_types import Provenance
from services.federation_pending_store import (
    InMemoryPendingStore,
    NONCE_WINDOW_SECONDS,
    REQUEST_TTL_SECONDS,
    RedisPendingStore,
    _PendingRequest,
    _from_jsonable,
    _to_jsonable,
)
from services.federation_query_schemas import (
    STATUS_COMPLETE,
    STATUS_EXPIRED,
    STATUS_PROCESSING,
)


# =============================================================================
# Helpers
# =============================================================================


def _make_pending(
    request_id: str = "rid-1",
    asker_pubkey: str = "a" * 64,
    with_provenance: bool = False,
) -> _PendingRequest:
    pr = _PendingRequest(
        request_id=request_id,
        asker_pubkey=asker_pubkey,
        peer_user_id=77,
        asker_local_user_id=42,
        max_visible_tier=2,
        query="what's for dinner?",
        initiated_at=time.time(),
    )
    if with_provenance:
        pr.provenance = [
            Provenance(
                atom_id="AAAA",
                atom_type="document_chunk",
                display_label="pasta",
                score=0.9,
            ).redacted_for_remote()
        ]
    return pr


# =============================================================================
# Serialization round-trip (shared by both backends)
# =============================================================================


class TestSerialization:
    @pytest.mark.unit
    def test_roundtrip_preserves_fields(self):
        pr = _make_pending(with_provenance=True)
        pr.status = STATUS_COMPLETE
        pr.answer = "pasta"
        pr.answered_at = time.time()
        pr.error_message = None

        raw = json.dumps(_to_jsonable(pr))
        back = _from_jsonable(json.loads(raw))

        assert back.request_id == pr.request_id
        assert back.asker_pubkey == pr.asker_pubkey
        assert back.status == pr.status
        assert back.answer == pr.answer
        assert back.answered_at == pr.answered_at
        assert len(back.provenance) == 1
        # Provenance dataclass rebuilt; atom_id is the redacted UUID
        # (redacted_for_remote() replaces the internal id), and score
        # + display_label survive verbatim.
        assert back.provenance[0].atom_id == pr.provenance[0].atom_id
        assert back.provenance[0].score == 0.9
        assert back.provenance[0].display_label == "pasta"


# =============================================================================
# InMemoryPendingStore
# =============================================================================


class TestInMemoryStore:
    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_put_then_get(self):
        store = InMemoryPendingStore()
        pr = _make_pending()
        await store.put(pr)
        back = await store.get(pr.request_id)
        assert back is pr  # aliased — in-memory keeps the same instance

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_get_missing_returns_none(self):
        store = InMemoryPendingStore()
        assert await store.get("no-such-rid") is None

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_list_for_pubkey(self):
        store = InMemoryPendingStore()
        await store.put(_make_pending("r1", "a" * 64))
        await store.put(_make_pending("r2", "a" * 64))
        await store.put(_make_pending("r3", "b" * 64))
        a_list = await store.list_for_pubkey("a" * 64)
        b_list = await store.list_for_pubkey("b" * 64)
        assert {p.request_id for p in a_list} == {"r1", "r2"}
        assert {p.request_id for p in b_list} == {"r3"}

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_delete_many(self):
        store = InMemoryPendingStore()
        await store.put(_make_pending("r1"))
        await store.put(_make_pending("r2"))
        await store.delete_many(["r1"])
        assert await store.get("r1") is None
        assert await store.get("r2") is not None

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_prune_expired(self):
        store = InMemoryPendingStore()
        # Past TTL — one minute ago, still PROCESSING.
        pr = _make_pending("old")
        pr.initiated_at = time.time() - REQUEST_TTL_SECONDS - 5
        await store.put(pr)
        # Fresh — still in window.
        await store.put(_make_pending("new"))

        count = await store.prune_expired()
        assert count == 1
        # Old row still present but marked EXPIRED.
        old = await store.get("old")
        assert old.status == STATUS_EXPIRED
        # New untouched.
        new = await store.get("new")
        assert new.status == STATUS_PROCESSING

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_record_nonce_replay_defense(self):
        store = InMemoryPendingStore()
        now = time.time()
        assert await store.record_nonce("n1", now) is True
        assert await store.record_nonce("n1", now) is False  # replay
        assert await store.record_nonce("n2", now) is True   # new one

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_record_nonce_evicts_past_window(self):
        store = InMemoryPendingStore()
        old_now = time.time() - (NONCE_WINDOW_SECONDS * 3)
        fresh_now = time.time()
        assert await store.record_nonce("stale", old_now) is True
        # After the window grace period, the stale nonce is forgotten
        # and can be reused. Depending on grace this test relies on
        # WINDOW + GRACE being < 3*WINDOW.
        assert await store.record_nonce("stale", fresh_now) is True


# =============================================================================
# RedisPendingStore — using an in-process stub
# =============================================================================


class FakeRedisPipeline:
    def __init__(self, parent):
        self._parent = parent
        self._ops: list[tuple[str, tuple, dict]] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def set(self, *args, **kwargs):
        self._ops.append(("set", args, kwargs))
        return self

    def sadd(self, *args, **kwargs):
        self._ops.append(("sadd", args, kwargs))
        return self

    def expire(self, *args, **kwargs):
        self._ops.append(("expire", args, kwargs))
        return self

    def delete(self, *args, **kwargs):
        self._ops.append(("delete", args, kwargs))
        return self

    async def execute(self):
        for op, args, kwargs in self._ops:
            await getattr(self._parent, op)(*args, **kwargs)
        self._ops.clear()


class FakeRedis:
    """Tiny in-memory aioredis-alike covering the subset we use:
    get/set(NX,EX)/delete/keys/sadd/smembers/expire/pipeline."""

    def __init__(self):
        self._strings: dict[str, str] = {}
        self._sets: dict[str, set[str]] = {}

    async def get(self, key):
        return self._strings.get(key)

    async def set(self, key, value, *, ex=None, nx=False):
        if nx and key in self._strings:
            return None
        self._strings[key] = value
        return True

    async def delete(self, *keys):
        n = 0
        for k in keys:
            if k in self._strings:
                del self._strings[k]
                n += 1
            if k in self._sets:
                del self._sets[k]
                n += 1
        return n

    async def keys(self, pattern):
        # Only supports trailing-* glob.
        prefix = pattern.rstrip("*")
        return [k for k in self._strings if k.startswith(prefix)] + \
               [k for k in self._sets if k.startswith(prefix)]

    async def sadd(self, key, *members):
        s = self._sets.setdefault(key, set())
        before = len(s)
        s.update(members)
        return len(s) - before

    async def smembers(self, key):
        return set(self._sets.get(key, set()))

    async def expire(self, key, seconds):
        # No-op for the stub — TTL isn't exercised.
        return True

    def pipeline(self):
        return FakeRedisPipeline(self)


@pytest.fixture
def redis_store():
    return RedisPendingStore(FakeRedis())


class TestRedisStore:
    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_put_then_get_roundtrips_through_json(self, redis_store):
        pr = _make_pending(with_provenance=True)
        pr.answer = "pasta"
        pr.status = STATUS_COMPLETE

        await redis_store.put(pr)
        back = await redis_store.get(pr.request_id)

        assert back is not None
        assert back is not pr  # different instance — round-tripped
        assert back.request_id == pr.request_id
        assert back.answer == "pasta"
        assert back.status == STATUS_COMPLETE
        assert len(back.provenance) == 1

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_save_overwrites_prior_state(self, redis_store):
        pr = _make_pending()
        await redis_store.put(pr)

        # Mutate + save
        pr.status = STATUS_COMPLETE
        pr.answer = "final"
        await redis_store.save(pr)

        back = await redis_store.get(pr.request_id)
        assert back.status == STATUS_COMPLETE
        assert back.answer == "final"

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_list_for_pubkey(self, redis_store):
        await redis_store.put(_make_pending("r1", "a" * 64))
        await redis_store.put(_make_pending("r2", "a" * 64))
        await redis_store.put(_make_pending("r3", "b" * 64))
        a_list = await redis_store.list_for_pubkey("a" * 64)
        b_list = await redis_store.list_for_pubkey("b" * 64)
        assert {p.request_id for p in a_list} == {"r1", "r2"}
        assert {p.request_id for p in b_list} == {"r3"}

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_delete_many(self, redis_store):
        await redis_store.put(_make_pending("r1"))
        await redis_store.put(_make_pending("r2"))
        await redis_store.delete_many(["r1"])
        assert await redis_store.get("r1") is None
        assert await redis_store.get("r2") is not None

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_record_nonce_nx_atomic(self, redis_store):
        now = time.time()
        assert await redis_store.record_nonce("n1", now) is True
        assert await redis_store.record_nonce("n1", now) is False
        assert await redis_store.record_nonce("n2", now) is True

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_clear_for_tests_wipes_everything(self, redis_store):
        await redis_store.put(_make_pending("r1"))
        await redis_store.record_nonce("n1", time.time())
        await redis_store.clear_for_tests()
        assert await redis_store.get("r1") is None
        # Re-recording the nonce should succeed → proves cleared.
        assert await redis_store.record_nonce("n1", time.time()) is True


# =============================================================================
# Redis-error → uniform 400 translation (responder-level)
# =============================================================================


class TestRedisErrorTranslation:
    """Reviewer S1 — when the Redis backend is enabled and Redis is
    unreachable, responder methods must translate `RedisError` into
    `FederationQueryError` so the route layer returns a uniform 400
    rather than leaking a 500 stack trace."""

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_handle_initiate_translates_redis_error(self, monkeypatch, tmp_path):
        from redis.exceptions import ConnectionError as RedisConnectionError

        from services.federation_identity import (
            init_federation_identity,
            reset_federation_identity_for_tests,
        )
        from services.federation_query_responder import (
            FederationQueryError,
            FederationQueryResponder,
        )
        from services.federation_query_schemas import QueryBrainInitiateRequest

        reset_federation_identity_for_tests()
        init_federation_identity(tmp_path / "responder_key")

        # Stub the store so any operation raises ConnectionError.
        class BrokenStore:
            async def record_nonce(self, *a, **k):
                raise RedisConnectionError("simulated outage")
            async def get(self, *a, **k): raise RedisConnectionError()
            async def put(self, *a, **k): raise RedisConnectionError()
            async def save(self, *a, **k): raise RedisConnectionError()
            async def list_for_pubkey(self, *a, **k): raise RedisConnectionError()
            async def delete_many(self, *a, **k): raise RedisConnectionError()
            async def prune_expired(self, *a, **k): raise RedisConnectionError()
            async def clear_for_tests(self): pass

        monkeypatch.setattr(
            "services.federation_query_responder.get_pending_store",
            lambda: BrokenStore(),
        )

        # Build a syntactically valid request (signature must verify so
        # we reach the nonce-record step where the store is touched).
        from cryptography.hazmat.primitives.asymmetric import ed25519
        from services.federation_identity import FederationIdentity
        from services.pairing_service import _canonical_bytes
        asker = FederationIdentity(ed25519.Ed25519PrivateKey.generate())
        unsigned = {
            "version": 2,
            "asker_pubkey": asker.public_key_hex(),
            "query": "q",
            "nonce": "deadbeefdeadbeef" * 2,
            "timestamp": int(time.time()),
            "depth": 3,
            "path": [asker.public_key_hex()],
        }
        sig = asker.sign(_canonical_bytes(unsigned)).hex()
        req = QueryBrainInitiateRequest(**unsigned, signature=sig)

        from unittest.mock import MagicMock
        responder = FederationQueryResponder(db=MagicMock())

        # Must surface as the uniform federation error, NOT a raw
        # ConnectionError that would become a 500 at the route layer.
        with pytest.raises(FederationQueryError, match="store unavailable"):
            await responder.handle_initiate(req)

        reset_federation_identity_for_tests()
