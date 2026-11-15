"""The admission of a signed federation envelope -- what a stranger and a
replay may cost.

  1. `query_brain/initiate` wrote a nonce (and created a rate bucket) BEFORE it
     knew whether the caller was paired.
  2. A replayed, validly signed envelope spent the pairing's rate budget.

Each class states the intended behaviour; each was run red against the code
before the fix.
"""
from __future__ import annotations

import secrets
import time
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from cryptography.hazmat.primitives.asymmetric import ed25519

import services.federation_query_responder as responder_module
from models.database import PeerUser
from services import federation_rate_limits
from services.federation_identity import (
    FederationIdentity,
    canonical_bytes,
    get_federation_identity,
    init_federation_identity,
    reset_federation_identity_for_tests,
)
from services.federation_pending_store import (
    NONCE_GRACE_SECONDS,
    NONCE_WINDOW_SECONDS,
    InMemoryPendingStore,
    RedisPendingStore,
    get_pending_store,
    reset_store_for_tests,
)
from services.federation_query_responder import (
    FederationQueryError,
    FederationQueryResponder,
    _clear_state_for_tests,
)
from services.federation_query_schemas import PeerPingRequest, QueryBrainInitiateRequest
from utils.config import settings

LAST_SEEN = datetime(2026, 11, 1, 12, 0, 0)


def _fresh_identity() -> FederationIdentity:
    return FederationIdentity(ed25519.Ed25519PrivateKey.generate())


@pytest.fixture
async def local_identity(tmp_path, monkeypatch):
    reset_federation_identity_for_tests()
    init_federation_identity(tmp_path / "local_key")
    reset_store_for_tests()
    await _clear_state_for_tests()
    federation_rate_limits.reset_for_tests()
    monkeypatch.setattr(settings, "federation_instance_name", "")
    monkeypatch.setattr(settings, "federation_advertised_url", "")
    yield get_federation_identity()
    reset_federation_identity_for_tests()
    await _clear_state_for_tests()
    reset_store_for_tests()
    federation_rate_limits.reset_for_tests()


@pytest.fixture
def far_identity() -> FederationIdentity:
    return _fresh_identity()


def _signed_ping(asker: FederationIdentity, *, nonce: str | None = None) -> PeerPingRequest:
    unsigned = {
        "version": 1,
        "asker_pubkey": asker.public_key_hex(),
        "nonce": nonce or secrets.token_hex(16),
        "timestamp": int(time.time()),
    }
    signature = asker.sign(canonical_bytes({"kind": "ping", **unsigned})).hex()
    return PeerPingRequest(**unsigned, signature=signature)


def _signed_initiate(
    asker: FederationIdentity, *, nonce: str | None = None,
) -> QueryBrainInitiateRequest:
    pubkey = asker.public_key_hex()
    unsigned = {
        "version": 2,
        "asker_pubkey": pubkey,
        "query": "a question",
        "nonce": nonce or secrets.token_hex(16),
        "timestamp": int(time.time()),
        "depth": 3,
        "path": [pubkey],
    }
    return QueryBrainInitiateRequest(
        **unsigned, signature=asker.sign(canonical_bytes(unsigned)).hex())


def _paired_row(asker: FederationIdentity) -> PeerUser:
    return PeerUser(
        id=77, circle_owner_id=1, remote_pubkey=asker.public_key_hex(),
        remote_display_name="far side", granted_tier=2, transport_config={},
        last_seen_at=LAST_SEEN, revoked_at=None,
    )


def _db_returning(peers: list) -> MagicMock:
    db = MagicMock()

    async def _execute(stmt):
        result = MagicMock()
        rows = peers if "peer_users" in str(stmt) else []
        result.scalars = lambda: MagicMock(all=lambda: rows)
        result.scalar_one_or_none = lambda: rows[0] if rows else None
        return result

    db.execute = AsyncMock(side_effect=_execute)
    db.commit = AsyncMock()
    db.rollback = AsyncMock()
    return db


def _responder(peers: list) -> FederationQueryResponder:
    responder = FederationQueryResponder(db=_db_returning(peers))
    # No retrieval, no model: the admission is what these tests are about.
    responder._run_query = AsyncMock()
    return responder


@pytest.fixture
def nonce_writes(monkeypatch):
    spy = AsyncMock(side_effect=responder_module._record_nonce)
    monkeypatch.setattr(responder_module, "_record_nonce", spy)
    return spy


@pytest.fixture
def nonce_peeks(monkeypatch):
    spy = AsyncMock(side_effect=responder_module._nonce_seen)
    monkeypatch.setattr(responder_module, "_nonce_seen", spy)
    return spy


# =============================================================================
# 1. initiate: signature, timestamp, pairing, rate bucket, nonce
# =============================================================================


@pytest.mark.unit
class TestAnUnpairedQueryLeavesNothingBehind:
    """🛑 A signature costs a stranger nothing -- it signs with a key of its
    own. Everything that WRITES therefore comes after the pairing lookup."""

    async def test_it_writes_no_nonce_no_bucket_and_no_row(
        self, local_identity, nonce_writes, nonce_peeks,
    ):
        responder = _responder([])
        strangers = [_fresh_identity() for _ in range(5)]
        for stranger in strangers:
            with pytest.raises(FederationQueryError, match="Unknown or revoked"):
                await responder.handle_initiate(_signed_initiate(stranger))

        nonce_writes.assert_not_awaited()
        nonce_peeks.assert_not_awaited()
        assert federation_rate_limits._responder_inbound == {}
        responder.db.commit.assert_not_awaited()
        responder._run_query.assert_not_called()
        for stranger in strangers:
            assert await get_pending_store().list_for_pubkey(stranger.public_key_hex()) == []

    async def test_a_stranger_cannot_burn_a_paired_peers_nonce(
        self, local_identity, far_identity,
    ):
        """The same statement without looking inside: a nonce a stranger sent
        is still fresh for the paired peer."""
        nonce = secrets.token_hex(16)
        with pytest.raises(FederationQueryError, match="Unknown or revoked"):
            await _responder([]).handle_initiate(_signed_initiate(_fresh_identity(), nonce=nonce))
        resp = await _responder([_paired_row(far_identity)]).handle_initiate(
            _signed_initiate(far_identity, nonce=nonce))
        assert resp.request_id

    async def test_a_stranger_cannot_push_real_nonces_out_of_the_store(
        self, local_identity, far_identity, monkeypatch,
    ):
        """The consequence that matters: the nonce store is capped, and a
        flood of unpaired queries used to evict a real peer's nonce -- after
        which that peer's recorded query could be replayed."""
        import services.federation_pending_store as store_module

        monkeypatch.setattr(store_module, "NONCE_CACHE_MAX", 4)
        paired = _responder([_paired_row(far_identity)])
        recorded = _signed_initiate(far_identity)
        await paired.handle_initiate(recorded)

        strangers = _responder([])
        for _ in range(12):
            with pytest.raises(FederationQueryError):
                await strangers.handle_initiate(_signed_initiate(_fresh_identity()))

        with pytest.raises(FederationQueryError, match="replay"):
            await paired.handle_initiate(recorded)

    async def test_an_admitted_query_still_moves_last_seen_at(
        self, local_identity, far_identity,
    ):
        row = _paired_row(far_identity)
        responder = _responder([row])
        await responder.handle_initiate(_signed_initiate(far_identity))
        assert row.last_seen_at > LAST_SEEN
        responder.db.commit.assert_awaited()

    async def test_a_refused_query_does_not_move_last_seen_at(
        self, local_identity, far_identity, monkeypatch,
    ):
        """`last_seen_at` means "last real request". The pairing is looked up
        earlier now -- a replay or a query over budget must still not count."""
        monkeypatch.setattr(settings, "federation_responder_rate_per_minute", 1)
        federation_rate_limits.reset_for_tests()
        row = _paired_row(far_identity)
        responder = _responder([row])
        admitted = _signed_initiate(far_identity)
        await responder.handle_initiate(admitted)
        row.last_seen_at = LAST_SEEN
        responder.db.commit.reset_mock()

        with pytest.raises(FederationQueryError, match="replay"):
            await responder.handle_initiate(admitted)
        with pytest.raises(FederationQueryError, match="Rate limit"):
            await responder.handle_initiate(_signed_initiate(far_identity))
        assert row.last_seen_at == LAST_SEEN
        responder.db.commit.assert_not_awaited()

    async def test_a_failing_last_seen_write_does_not_fail_the_query(
        self, local_identity, far_identity,
    ):
        responder = _responder([_paired_row(far_identity)])
        responder.db.commit = AsyncMock(side_effect=RuntimeError("database hiccup"))
        resp = await responder.handle_initiate(_signed_initiate(far_identity))
        assert resp.request_id
        responder.db.rollback.assert_awaited()


# =============================================================================
# 2. a replay does not spend the pairing's rate budget
# =============================================================================


@pytest.mark.unit
class TestAReplayDoesNotSpendTheBudget:
    """🛑 Whoever can record a pairing's traffic could replay one validly
    signed envelope and so use up that pairing's budget: the real peer's own
    checks then read `refused`, its queries failed. A replay is now refused by
    a read-only look at the nonce store, BEFORE the bucket."""

    async def test_replayed_pings_leave_the_ping_budget_whole(
        self, local_identity, far_identity, monkeypatch,
    ):
        monkeypatch.setattr(settings, "federation_ping_rate_per_minute", 2)
        federation_rate_limits.reset_for_tests()
        responder = _responder([_paired_row(far_identity)])
        recorded = _signed_ping(far_identity)
        await responder.handle_ping(recorded)            # token 1 of 2
        for _ in range(10):
            with pytest.raises(FederationQueryError, match="replay"):
                await responder.handle_ping(recorded)
        # The real peer's next check is answered: one token is still there.
        assert (await responder.handle_ping(_signed_ping(far_identity))).status == "ok"
        # ... and the budget is still a budget.
        with pytest.raises(FederationQueryError, match="rate limit"):
            await responder.handle_ping(_signed_ping(far_identity))

    async def test_replayed_queries_leave_the_query_budget_whole(
        self, local_identity, far_identity, monkeypatch,
    ):
        monkeypatch.setattr(settings, "federation_responder_rate_per_minute", 2)
        federation_rate_limits.reset_for_tests()
        responder = _responder([_paired_row(far_identity)])
        recorded = _signed_initiate(far_identity)
        await responder.handle_initiate(recorded)
        for _ in range(10):
            with pytest.raises(FederationQueryError, match="replay"):
                await responder.handle_initiate(recorded)
        assert (await responder.handle_initiate(_signed_initiate(far_identity))).request_id
        with pytest.raises(FederationQueryError, match="Rate limit"):
            await responder.handle_initiate(_signed_initiate(far_identity))

    async def test_a_replay_writes_nothing(
        self, local_identity, far_identity, nonce_writes,
    ):
        responder = _responder([_paired_row(far_identity)])
        recorded = _signed_ping(far_identity)
        await responder.handle_ping(recorded)
        for _ in range(3):
            with pytest.raises(FederationQueryError, match="replay"):
                await responder.handle_ping(recorded)
        assert nonce_writes.await_count == 1

    async def test_a_stranger_never_reaches_the_look(self, local_identity, nonce_peeks):
        """No oracle and no flood: the look happens only for an envelope that
        verified for a PAIRED key."""
        responder = _responder([])
        for _ in range(3):
            with pytest.raises(FederationQueryError, match="Unknown or revoked"):
                await responder.handle_ping(_signed_ping(_fresh_identity()))
        nonce_peeks.assert_not_awaited()

    async def test_the_record_stays_the_claim_when_two_copies_race(
        self, local_identity, far_identity, monkeypatch,
    ):
        """The look is not the claim. If two copies of one fresh envelope both
        pass it, the atomic record still lets exactly one through."""
        monkeypatch.setattr(responder_module, "_nonce_seen", AsyncMock(return_value=False))
        responder = _responder([_paired_row(far_identity)])
        recorded = _signed_ping(far_identity)
        await responder.handle_ping(recorded)
        with pytest.raises(FederationQueryError, match="replay"):
            await responder.handle_ping(recorded)


class _FakeRedis:
    def __init__(self) -> None:
        self.keys_set: dict[str, str] = {}
        self.writes = 0

    async def set(self, key, value, nx=False, ex=None):
        if nx and key in self.keys_set:
            return None
        self.keys_set[key] = value
        self.writes += 1
        return True

    async def exists(self, key):
        return 1 if key in self.keys_set else 0


@pytest.mark.unit
class TestTheLookIsReadOnly:
    async def test_memory_store_look_does_not_record(self):
        store = InMemoryPendingStore()
        now = time.time()
        assert await store.nonce_seen("n1", now) is False
        assert await store.nonce_seen("n1", now) is False
        assert await store.record_nonce("n1", now) is True, "the look claimed the nonce"
        assert await store.nonce_seen("n1", now) is True

    async def test_memory_store_look_does_not_evict(self, monkeypatch):
        import services.federation_pending_store as store_module

        monkeypatch.setattr(store_module, "NONCE_CACHE_MAX", 2)
        store = InMemoryPendingStore()
        now = time.time()
        await store.record_nonce("a", now)
        await store.record_nonce("b", now)
        for i in range(10):
            await store.nonce_seen(f"other-{i}", now)
        assert await store.nonce_seen("a", now) is True
        assert await store.nonce_seen("b", now) is True

    async def test_memory_store_forgets_like_the_record_does(self):
        store = InMemoryPendingStore()
        then = time.time()
        await store.record_nonce("old", then)
        later = then + NONCE_WINDOW_SECONDS + NONCE_GRACE_SECONDS + 1
        assert await store.nonce_seen("old", later) is False
        assert await store.record_nonce("old", later) is True

    async def test_redis_store_look_does_not_write(self):
        redis = _FakeRedis()
        store = RedisPendingStore(redis)
        now = time.time()
        assert await store.nonce_seen("n1", now) is False
        assert redis.writes == 0
        assert await store.record_nonce("n1", now) is True
        assert await store.nonce_seen("n1", now) is True
        assert await store.record_nonce("n1", now) is False
        assert redis.writes == 1
