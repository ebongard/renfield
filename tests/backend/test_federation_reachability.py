"""Reachability of a pairing: the reading, its store, the probe, who may ask.

Three things used to be one green light: "paired", "the far side queried us
lately" and "the connection works". This file pins the third and its fences:

  - how a ping outcome folds into reachable / unreachable / unknown;
  - a result is an observation with a shelf life (TTL), not a fact;
  - ONE probe per pairing, whoever asks and in however many processes;
  - nothing is probed while the feature is off, and a GET never probes;
  - `GET /peers` is byte-identical with the feature off, and gains exactly the
    documented keys with it on;
  - `POST /peers/{id}/check`: who may trigger a probe.
"""
from __future__ import annotations

import asyncio
import json
import time
from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient

import services.federation_reachability as reach
from models.database import FederationQueryLog, PeerUser, Role, User
from services import federation_rate_limits
from services.auth_service import get_user_or_default
from services.federation_query_asker import (
    PING_INVALID_RESPONSE,
    PING_NO_ENDPOINT,
    PING_PIN_MISMATCH,
    PING_REACHABLE,
    PING_REFUSED,
    PING_UNREACHABLE,
    PING_UNSUPPORTED,
)
from services.federation_reachability import (
    CLAIM_TTL_SECONDS,
    NOT_CHECKED,
    PROBE_INTERVAL_SECONDS,
    RESULT_TTL_SECONDS,
    STATE_REACHABLE,
    STATE_UNKNOWN,
    STATE_UNREACHABLE,
    MemoryReachabilityStore,
    Reading,
    RedisReachabilityStore,
    last_asked_at,
    probe_due_peers,
    probe_peer,
    read_reachability,
    reading_for,
    recently_active,
    set_store_for_tests,
)
from utils.config import settings

PEERS = "/api/federation/peers"

#: The seven keys the list has always carried.
LEGACY_KEYS = {
    "id", "remote_pubkey", "remote_display_name", "remote_user_id",
    "paired_at", "last_seen_at", "circle_tier",
}
REACHABILITY_KEYS = {"reachability", "last_asked_at", "recently_active"}


# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture(autouse=True)
def memory_store():
    """Every test on a store of its own -- never the build box's Redis."""
    store = MemoryReachabilityStore()
    set_store_for_tests(store)
    federation_rate_limits.reset_for_tests()
    yield store
    set_store_for_tests(None)
    federation_rate_limits.reset_for_tests()


@pytest.fixture
def feature_on(monkeypatch):
    monkeypatch.setattr(settings, "federation_reachability_enabled", True)


@pytest.fixture
def feature_off(monkeypatch):
    monkeypatch.setattr(settings, "federation_reachability_enabled", False)


class _Peer:
    def __init__(self, peer_id: int = 3):
        self.id = peer_id
        self.remote_pubkey = f"{peer_id:02x}" * 32


@pytest.fixture
def ping(monkeypatch):
    """Replace the real round trip. `ping.outcome` is what the far side
    "answers"; `ping.calls` counts the signed requests that would have left."""
    class _Ping:
        outcome: str | Exception = PING_REACHABLE
        delay: float = 0.0
        calls: list[int]

        def __init__(self):
            self.calls = []

        async def __call__(self, peer, client=None):
            self.calls.append(peer.id)
            if self.delay:
                await asyncio.sleep(self.delay)
            if isinstance(self.outcome, Exception):
                raise self.outcome
            return self.outcome

    spy = _Ping()
    monkeypatch.setattr(reach, "ping_peer", spy)
    return spy


def _expire_claim(store: MemoryReachabilityStore, peer_id: int) -> None:
    store._claims.pop(peer_id, None)


# =============================================================================
# Folding an outcome into a state
# =============================================================================


class TestReading:
    @pytest.mark.unit
    def test_reachable(self):
        assert reading_for(PING_REACHABLE, 1.0) == Reading(STATE_REACHABLE, None, 1.0)

    @pytest.mark.unit
    @pytest.mark.parametrize("outcome", [
        PING_UNREACHABLE, PING_PIN_MISMATCH, PING_REFUSED, PING_INVALID_RESPONSE,
        PING_NO_ENDPOINT,
    ])
    def test_a_failed_round_trip_is_unreachable_and_keeps_its_reason(self, outcome):
        """The reason survives: each one needs a different repair."""
        assert reading_for(outcome, 1.0) == Reading(STATE_UNREACHABLE, outcome, 1.0)

    @pytest.mark.unit
    def test_UNSUPPORTED_is_unknown_never_unreachable(self):
        """🛑 A far side one release behind, or with the feature off, answers
        404 to a ping while its pairing works. Red would be a false alarm on
        every wall during every staggered rollout."""
        assert reading_for(PING_UNSUPPORTED, 1.0) == Reading(
            STATE_UNKNOWN, PING_UNSUPPORTED, 1.0)

    @pytest.mark.unit
    def test_an_outcome_this_version_does_not_know_is_unknown(self):
        """Written by a newer process during a rolling deploy."""
        assert reading_for("from_the_future", 1.0).state == STATE_UNKNOWN

    @pytest.mark.unit
    def test_payload_times_are_naive_utc_iso_like_last_seen_at(self):
        at = datetime(2026, 11, 13, 9, 30, 15, 987654, tzinfo=UTC).timestamp()
        assert reading_for(PING_REFUSED, at).as_payload() == {
            "state": "unreachable", "reason": "refused", "checked_at": "2026-11-13T09:30:15",
        }
        assert NOT_CHECKED.as_payload() == {
            "state": "unknown", "reason": "not_checked", "checked_at": None,
        }


class TestRecentlyActive:
    NOW = datetime(2026, 11, 13, 12, 0, 0, tzinfo=UTC)

    @pytest.mark.unit
    @pytest.mark.parametrize(("last_in", "last_out", "expected"), [
        (None, None, False),
        (timedelta(minutes=1), None, True),       # the far side asked us
        (None, timedelta(minutes=1), True),       # we asked the far side
        (timedelta(days=2), timedelta(minutes=4), True),
        (timedelta(minutes=6), timedelta(hours=1), False),
    ])
    def test_a_request_in_either_direction_counts(self, last_in, last_out, expected):
        naive_now = self.NOW.replace(tzinfo=None)
        assert recently_active(
            naive_now - last_in if last_in else None,
            naive_now - last_out if last_out else None,
            now=self.NOW,
        ) is expected

    @pytest.mark.unit
    def test_aware_and_naive_timestamps_are_both_read_as_utc(self):
        at = self.NOW - timedelta(minutes=1)
        assert recently_active(at, None, now=self.NOW)
        assert recently_active(at.replace(tzinfo=None), None, now=self.NOW)


# =============================================================================
# The store
# =============================================================================


class _FakeRedis:
    """Enough of redis.asyncio for the store: SET (NX, EX) and MGET."""

    def __init__(self):
        self.data: dict[str, str] = {}
        self.ttl: dict[str, int] = {}
        self.fail: Exception | None = None
        self.hang: float = 0.0

    async def _gate(self):
        if self.hang:
            await asyncio.sleep(self.hang)
        if self.fail:
            raise self.fail

    async def set(self, key, value, nx=False, ex=None):
        await self._gate()
        if nx and key in self.data:
            return None
        self.data[key] = value
        self.ttl[key] = ex
        return True

    async def mget(self, keys):
        await self._gate()
        return [self.data.get(k) for k in keys]


class TestMemoryStore:
    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_a_result_older_than_the_ttl_is_forgotten(self, memory_store):
        await memory_store.save(3, PING_REACHABLE, time.time() - RESULT_TTL_SECONDS - 1)
        await memory_store.save(4, PING_REACHABLE, time.time() - RESULT_TTL_SECONDS + 30)
        assert set(await memory_store.load([3, 4])) == {4}

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_a_claim_is_exclusive_until_it_expires(self, memory_store):
        assert await memory_store.claim(3) is True
        assert await memory_store.claim(3) is False
        assert await memory_store.claim(4) is True, "per pairing, not global"
        _expire_claim(memory_store, 3)
        assert await memory_store.claim(3) is True


class TestRedisStore:
    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_results_and_claims_carry_their_ttl(self):
        redis = _FakeRedis()
        store = RedisReachabilityStore(redis)
        assert await store.claim(3) is True
        await store.save(3, PING_REFUSED, 1234.5)
        assert redis.ttl["federation:reachability:claim:3"] == CLAIM_TTL_SECONDS
        assert redis.ttl["federation:reachability:result:3"] == RESULT_TTL_SECONDS
        assert json.loads(redis.data["federation:reachability:result:3"]) == {
            "outcome": "refused", "checked_at": 1234.5}
        assert await store.load([3, 4]) == {3: ("refused", 1234.5)}

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_two_processes_share_claim_and_result(self):
        """Two stores on one Redis = two backend processes."""
        redis = _FakeRedis()
        first, second = RedisReachabilityStore(redis), RedisReachabilityStore(redis)
        assert await first.claim(3) is True
        assert await second.claim(3) is False
        await first.save(3, PING_REACHABLE, 99.0)
        assert await second.load([3]) == {3: (PING_REACHABLE, 99.0)}

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_a_failing_redis_falls_back_to_memory(self):
        redis = _FakeRedis()
        store = RedisReachabilityStore(redis)
        redis.fail = ConnectionError("redis is gone")
        assert await store.claim(3) is True
        assert await store.claim(3) is False
        await store.save(3, PING_REACHABLE, time.time())
        assert set(await store.load([3])) == {3}

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_the_last_reading_survives_a_redis_outage_right_after(self):
        redis = _FakeRedis()
        store = RedisReachabilityStore(redis)
        await store.save(3, PING_REACHABLE, time.time())
        redis.fail = ConnectionError("redis is gone")
        assert set(await store.load([3])) == {3}

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_a_hanging_redis_does_not_hold_the_caller(self, monkeypatch):
        """The kiosk snapshot reads through here."""
        monkeypatch.setattr(reach, "_REDIS_OP_TIMEOUT_SECONDS", 0.05)
        redis = _FakeRedis()
        redis.hang = 30
        store = RedisReachabilityStore(redis)
        started = time.monotonic()
        assert await store.load([3]) == {}
        assert await store.claim(3) is True
        assert time.monotonic() - started < 2

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_a_corrupt_entry_is_no_reading(self):
        redis = _FakeRedis()
        redis.data["federation:reachability:result:3"] = "not json"
        redis.data["federation:reachability:result:4"] = '{"outcome": "reachable"}'
        assert await RedisReachabilityStore(redis).load([3, 4]) == {}


# =============================================================================
# The probe
# =============================================================================


class TestProbe:
    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_feature_off_sends_nothing(self, feature_off, ping):
        assert await probe_peer(_Peer()) == NOT_CHECKED
        assert await probe_peer(_Peer(), force=True) == NOT_CHECKED
        await probe_due_peers()
        assert ping.calls == []

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_a_probe_stores_what_it_found(self, feature_on, ping):
        before = time.time()
        reading = await probe_peer(_Peer())
        assert reading.state == STATE_REACHABLE
        assert before <= reading.checked_at <= time.time()
        assert (await read_reachability([3]))[3] == reading

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_a_fresh_result_is_reused_by_the_refresher(self, feature_on, ping, memory_store):
        first = await probe_peer(_Peer())
        _expire_claim(memory_store, 3)       # so only FRESHNESS can stop the second
        again = await probe_peer(_Peer())
        assert ping.calls == [3]
        assert again == first

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_a_result_past_the_interval_is_probed_again(self, feature_on, ping, memory_store):
        await memory_store.save(3, PING_UNREACHABLE, time.time() - PROBE_INTERVAL_SECONDS - 1)
        reading = await probe_peer(_Peer())
        assert ping.calls == [3]
        assert reading.state == STATE_REACHABLE

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_check_now_probes_despite_a_fresh_result(self, feature_on, ping, memory_store):
        await memory_store.save(3, PING_UNREACHABLE, time.time() - 5)
        reading = await probe_peer(_Peer(), force=True)
        assert ping.calls == [3]
        assert reading.state == STATE_REACHABLE

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_check_now_twice_sends_ONE_ping_and_does_not_fake_freshness(
        self, feature_on, ping,
    ):
        """An impatient finger: the second press inside the claim window gets
        the STORED reading with ITS time -- not a second request, and not the
        old result relabelled as "just now"."""
        first = await probe_peer(_Peer(), force=True)
        second = await probe_peer(_Peer(), force=True)
        assert ping.calls == [3]
        assert second.checked_at == first.checked_at

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_concurrent_triggers_send_ONE_ping(self, feature_on, ping):
        """The refresher tick, a connecting wall display and "check now" at
        the same instant."""
        ping.delay = 0.05
        peer = _Peer()
        await asyncio.gather(
            probe_peer(peer), probe_peer(peer), probe_peer(peer, force=True),
            probe_peer(peer, force=True),
        )
        assert ping.calls == [3]

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_two_PROCESSES_send_one_ping_and_read_one_result(self, feature_on, ping):
        """🛑 Multi-worker: two backend processes whose refreshers tick
        together. Each has its own store object and its own memory; what they
        share is Redis, and that is where the claim lives."""
        redis = _FakeRedis()
        process_a, process_b = RedisReachabilityStore(redis), RedisReachabilityStore(redis)

        set_store_for_tests(process_a)
        reading_a = await probe_peer(_Peer())
        set_store_for_tests(process_b)
        reading_b = await probe_peer(_Peer())
        forced_b = await probe_peer(_Peer(), force=True)

        assert ping.calls == [3]
        assert reading_b == forced_b == reading_a

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_different_pairings_do_not_block_each_other(self, feature_on, ping):
        await asyncio.gather(probe_peer(_Peer(3)), probe_peer(_Peer(4)))
        assert sorted(ping.calls) == [3, 4]

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_the_ping_budget_caps_what_leaves(
        self, feature_on, ping, memory_store, monkeypatch,
    ):
        monkeypatch.setattr(settings, "federation_ping_rate_per_minute", 2)
        federation_rate_limits.reset_for_tests()
        for _ in range(5):
            _expire_claim(memory_store, 3)
            await probe_peer(_Peer(), force=True)
        assert ping.calls == [3, 3]
        # The query budget towards the same peer is untouched.
        assert federation_rate_limits._asker_outbound == {}

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_a_fault_of_OURS_is_no_finding_about_the_far_side(self, feature_on, ping):
        """A bug on this end must not paint a pairing red."""
        ping.outcome = RuntimeError("a bug in our own code")
        assert await probe_peer(_Peer()) == NOT_CHECKED
        assert (await read_reachability([3]))[3] == NOT_CHECKED

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_the_state_follows_the_far_side(self, feature_on, ping, memory_store):
        ping.outcome = PING_PIN_MISMATCH
        down = await probe_peer(_Peer(), force=True)
        assert (down.state, down.reason) == (STATE_UNREACHABLE, PING_PIN_MISMATCH)
        _expire_claim(memory_store, 3)
        ping.outcome = PING_REACHABLE
        up = await probe_peer(_Peer(), force=True)
        assert (up.state, up.reason) == (STATE_REACHABLE, None)

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_a_store_that_cannot_be_read_reads_as_not_checked(self, feature_on):
        class _Broken(MemoryReachabilityStore):
            async def load(self, peer_ids):
                raise RuntimeError("store down")

        set_store_for_tests(_Broken())
        assert await read_reachability([3, 4]) == {3: NOT_CHECKED, 4: NOT_CHECKED}


# =============================================================================
# Real Postgres: the probe round, the activity lookup, the routes
# =============================================================================


async def _user(db, name: str, permissions: list[str]) -> User:
    role = Role(name=f"role-{name}", permissions=permissions, is_system=False)
    db.add(role)
    await db.flush()
    user = User(username=name, password_hash="x", role_id=role.id)
    db.add(user)
    await db.commit()
    await db.refresh(user, attribute_names=["role"])
    return user


NOW = datetime.now(UTC).replace(tzinfo=None)


@pytest.fixture
async def world(db_session):
    """owner: three pairings -- `plain` (no label at all), `named` (verified
    instance name + endpoint), `hosted` (endpoint only) -- and a revoked one.
    member: owns nothing. admin: holds `admin`, owns nothing."""
    owner = await _user(db_session, "reach-owner", ["chat.own"])
    member = await _user(db_session, "reach-member", ["chat.own"])
    admin = await _user(db_session, "reach-admin", ["admin"])

    def _peer(key: str, **kw) -> PeerUser:
        return PeerUser(
            circle_owner_id=owner.id, remote_pubkey=key * 32,
            remote_display_name="evdb", remote_user_id=555, granted_tier=2,
            paired_at=NOW - timedelta(days=3), **kw,
        )

    plain = _peer("ab", transport_config={}, last_seen_at=NOW - timedelta(days=1))
    named = _peer(
        "bc", transport_config={"endpoints": ["https://far.example.org:8443"]},
        remote_instance_name="Instance B", last_seen_at=None,
    )
    hosted = _peer(
        "cd", transport_config={"endpoints": [{"url": "https://other.example.org"}]},
        last_seen_at=None,
    )
    revoked = _peer(
        "de", transport_config={}, last_seen_at=None, revoked_at=NOW - timedelta(days=1),
    )
    db_session.add_all([plain, named, hosted, revoked])
    await db_session.commit()
    return {
        "owner": owner, "member": member, "admin": admin,
        "plain": plain, "named": named, "hosted": hosted, "revoked": revoked,
    }


@pytest.fixture
def as_user(app_with_test_db):
    def _as(user: User) -> None:
        app_with_test_db.dependency_overrides[get_user_or_default] = lambda: user
    return _as


@pytest.fixture
def visible_to_all(monkeypatch):
    monkeypatch.setattr(settings, "federation_peers_visible_to_all", True)


@pytest.fixture
def owner_only(monkeypatch):
    monkeypatch.setattr(settings, "federation_peers_visible_to_all", False)


def _by_id(resp, peer: PeerUser) -> dict:
    return next(row for row in resp.json()["peers"] if row["id"] == peer.id)


@pytest.mark.database
class TestProbeRound:
    async def test_every_active_pairing_is_probed_and_no_revoked_one(
        self, world, own_session_to_test_db, feature_on, ping,
    ):
        await probe_due_peers()
        assert sorted(ping.calls) == sorted(
            [world["plain"].id, world["named"].id, world["hosted"].id])

    async def test_a_second_round_inside_the_interval_sends_nothing(
        self, world, own_session_to_test_db, feature_on, ping,
    ):
        await probe_due_peers()
        sent = len(ping.calls)
        await probe_due_peers()
        assert len(ping.calls) == sent

    async def test_a_hanging_peer_does_not_hold_the_round(
        self, world, own_session_to_test_db, feature_on, monkeypatch,
    ):
        """One far instance never answers. The round ends at its ceiling, the
        others have their result, and nothing is raised into the refresher."""
        monkeypatch.setattr(reach, "PROBE_ROUND_TIMEOUT_SECONDS", 0.3)
        hanging = world["named"].id

        async def _ping(peer, client=None):
            if peer.id == hanging:
                await asyncio.sleep(30)
            return PING_REACHABLE

        monkeypatch.setattr(reach, "ping_peer", _ping)
        started = time.monotonic()
        await probe_due_peers()
        assert time.monotonic() - started < 3
        readings = await read_reachability([world["plain"].id, hanging])
        assert readings[world["plain"].id].state == STATE_REACHABLE
        assert readings[hanging] == NOT_CHECKED

    async def test_a_database_failure_is_swallowed(self, feature_on, ping, monkeypatch):
        import services.database as database

        def _boom():
            raise RuntimeError("database down")

        monkeypatch.setattr(database, "AsyncSessionLocal", _boom)
        await probe_due_peers()
        assert ping.calls == []


@pytest.mark.database
class TestLastAsked:
    async def _log(self, db, world, peer, direction, at):
        db.add(FederationQueryLog(
            user_id=world["owner"].id, peer_user_id=peer.id,
            peer_pubkey_snapshot=peer.remote_pubkey, peer_display_name_snapshot="evdb",
            query_text="frage", initiated_at=at, final_status="success",
            verified_signature=True, direction=direction,
        ))
        await db.commit()

    async def test_the_newest_ASKED_row_per_pairing(self, db_session, world):
        plain, named = world["plain"], world["named"]
        await self._log(db_session, world, plain, "asked", NOW - timedelta(hours=3))
        await self._log(db_session, world, plain, "asked", NOW - timedelta(minutes=2))
        # An ANSWERED row is the far side asking us -- `last_seen_at` covers it.
        await self._log(db_session, world, named, "answered", NOW - timedelta(minutes=1))
        asked = await last_asked_at(db_session, [plain.id, named.id, world["hosted"].id])
        assert set(asked) == {plain.id}
        assert abs((asked[plain.id] - (NOW - timedelta(minutes=2))).total_seconds()) < 1

    async def test_no_ids_no_query(self, db_session):
        assert await last_asked_at(db_session, []) == {}


@pytest.mark.database
class TestPeersListFeatureOff:
    """Byte-identical: nothing about reachability, and no ping."""

    async def test_a_pairing_without_any_label_keeps_the_exact_legacy_row(
        self, async_client: AsyncClient, world, as_user, feature_off, owner_only, ping,
    ):
        as_user(world["owner"])
        plain = world["plain"]
        assert _by_id(await async_client.get(PEERS), plain) == {
            "id": plain.id,
            "remote_pubkey": "ab" * 32,
            "remote_display_name": "evdb",
            "remote_user_id": 555,
            "paired_at": plain.paired_at.isoformat(),
            "last_seen_at": plain.last_seen_at.isoformat(),
            "circle_tier": 2,
        }
        assert ping.calls == []

    async def test_a_label_adds_exactly_one_key(
        self, async_client: AsyncClient, world, as_user, feature_off, owner_only,
    ):
        as_user(world["owner"])
        resp = await async_client.get(PEERS)
        named, hosted = _by_id(resp, world["named"]), _by_id(resp, world["hosted"])
        assert set(named) == set(hosted) == LEGACY_KEYS | {"instance_name"}
        assert named["instance_name"] == "Instance B", "the verified name beats the host"
        assert hosted["instance_name"] == "other.example.org"
        assert named["remote_display_name"] == "evdb", "the person's name stays where it was"

    async def test_check_now_does_not_exist(
        self, async_client: AsyncClient, world, as_user, feature_off, ping,
    ):
        as_user(world["owner"])
        resp = await async_client.post(f"{PEERS}/{world['plain'].id}/check")
        assert resp.status_code == 404
        assert resp.json() == {"detail": "Not Found"}
        assert ping.calls == []


@pytest.mark.database
class TestPeersListFeatureOn:
    async def test_each_row_gains_exactly_the_reachability_keys(
        self, async_client: AsyncClient, world, as_user, feature_on, owner_only, ping,
    ):
        as_user(world["owner"])
        resp = await async_client.get(PEERS)
        assert set(_by_id(resp, world["plain"])) == LEGACY_KEYS | REACHABILITY_KEYS
        assert set(_by_id(resp, world["named"])) == (
            LEGACY_KEYS | REACHABILITY_KEYS | {"instance_name"})
        row = _by_id(resp, world["plain"])
        assert row["reachability"] == {
            "state": "unknown", "reason": "not_checked", "checked_at": None}
        assert row["last_asked_at"] is None
        assert row["recently_active"] is False
        # "Letzte Anfrage ..." keeps its source.
        assert row["last_seen_at"] == world["plain"].last_seen_at.isoformat()

    async def test_listing_NEVER_sends_a_ping(
        self, async_client: AsyncClient, world, as_user, feature_on, owner_only, ping,
    ):
        """A GET that made the backend talk to another instance would be a
        poll with extra steps."""
        as_user(world["owner"])
        for _ in range(3):
            await async_client.get(PEERS)
        assert ping.calls == []

    async def test_a_stored_result_is_shown(
        self, async_client: AsyncClient, world, as_user, feature_on, owner_only, memory_store,
    ):
        await memory_store.save(world["named"].id, PING_PIN_MISMATCH, time.time() - 60)
        as_user(world["owner"])
        row = _by_id(await async_client.get(PEERS), world["named"])
        assert row["reachability"]["state"] == "unreachable"
        assert row["reachability"]["reason"] == "pin_mismatch"
        assert row["reachability"]["checked_at"] is not None

    async def test_recently_active_counts_our_own_question(
        self, async_client: AsyncClient, db_session, world, as_user, feature_on, owner_only,
    ):
        """`named` never queried us (`last_seen_at` is null) -- but we asked it
        two minutes ago. The old rule called that pairing dead."""
        named = world["named"]
        db_session.add(FederationQueryLog(
            user_id=world["owner"].id, peer_user_id=named.id,
            peer_pubkey_snapshot=named.remote_pubkey, peer_display_name_snapshot="evdb",
            query_text="frage", final_status="success", verified_signature=True,
            initiated_at=datetime.now(UTC).replace(tzinfo=None) - timedelta(minutes=2),
        ))
        await db_session.commit()
        as_user(world["owner"])
        row = _by_id(await async_client.get(PEERS), named)
        assert row["recently_active"] is True
        assert row["last_asked_at"] is not None
        assert row["last_seen_at"] is None

    async def test_the_all_pairings_view_carries_them_too(
        self, async_client: AsyncClient, world, as_user, feature_on, visible_to_all,
    ):
        as_user(world["member"])
        row = _by_id(await async_client.get(PEERS), world["plain"])
        assert REACHABILITY_KEYS <= set(row)


@pytest.mark.database
class TestCheckNow:
    def _url(self, peer: PeerUser) -> str:
        return f"{PEERS}/{peer.id}/check"

    async def test_the_owner_gets_a_fresh_reading(
        self, async_client: AsyncClient, world, as_user, feature_on, owner_only, ping,
    ):
        as_user(world["owner"])
        resp = await async_client.post(self._url(world["named"]))
        assert resp.status_code == 200
        body = resp.json()
        assert set(body) == {"state", "reason", "checked_at"}
        assert (body["state"], body["reason"]) == ("reachable", None)
        assert ping.calls == [world["named"].id]
        # ... and the list now shows it.
        row = _by_id(await async_client.get(PEERS), world["named"])
        assert row["reachability"] == body

    async def test_the_reason_reaches_the_caller(
        self, async_client: AsyncClient, world, as_user, feature_on, owner_only, ping,
    ):
        ping.outcome = PING_UNSUPPORTED
        as_user(world["owner"])
        body = (await async_client.post(self._url(world["named"]))).json()
        assert (body["state"], body["reason"]) == ("unknown", "unsupported")

    @pytest.mark.parametrize("who", ["member", "admin"])
    async def test_who_may_not_and_who_may_with_owner_only_visibility(
        self, async_client: AsyncClient, world, as_user, feature_on, owner_only, ping, who,
    ):
        """Owner-only list: a plain account can neither see nor probe the
        pairing; an `admin` holder may probe it (as it may revoke it under the
        all-pairings view)."""
        as_user(world[who])
        resp = await async_client.post(self._url(world["named"]))
        if who == "admin":
            assert resp.status_code == 200
            assert ping.calls == [world["named"].id]
        else:
            assert resp.status_code == 404
            assert resp.json() == {"detail": "Peer not found"}
            assert ping.calls == []

    async def test_everyone_who_sees_the_pairing_may_check_it(
        self, async_client: AsyncClient, world, as_user, feature_on, visible_to_all, ping,
    ):
        as_user(world["member"])
        resp = await async_client.post(self._url(world["named"]))
        assert resp.status_code == 200
        assert ping.calls == [world["named"].id]

    @pytest.mark.parametrize("who", ["owner", "admin", "member"])
    async def test_a_revoked_pairing_is_never_probed(
        self, async_client: AsyncClient, world, as_user, feature_on, visible_to_all, ping, who,
    ):
        as_user(world[who])
        resp = await async_client.post(self._url(world["revoked"]))
        assert resp.status_code == 404
        assert resp.json() == {"detail": "Peer not found"}
        assert ping.calls == []

    async def test_a_missing_id_looks_like_a_forbidden_one(
        self, async_client: AsyncClient, world, as_user, feature_on, owner_only, ping,
    ):
        as_user(world["member"])
        missing = await async_client.post(f"{PEERS}/987654/check")
        forbidden = await async_client.post(self._url(world["named"]))
        assert missing.status_code == forbidden.status_code == 404
        assert missing.json() == forbidden.json()

    async def test_hammering_the_button_sends_one_ping(
        self, async_client: AsyncClient, world, as_user, feature_on, owner_only, ping,
    ):
        as_user(world["owner"])
        answers = [
            (await async_client.post(self._url(world["named"]))).json() for _ in range(5)
        ]
        assert ping.calls == [world["named"].id]
        assert len({a["checked_at"] for a in answers}) == 1
