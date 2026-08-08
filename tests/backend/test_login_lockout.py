"""Tests for the per-username login lockout service (#693).

Covers the trip threshold, is_locked, clear-on-success, username normalization,
the disabled short-circuit, and the fail-OPEN guarantee on a Redis outage (a
lockout store must never lock out the whole household when Redis blips).
"""
import pytest

import services.login_lockout as ll_mod
from services.login_lockout import LoginLockout


class _FakeRedis:
    """Minimal in-memory async Redis supporting the ops LoginLockout uses."""

    def __init__(self):
        self.store: dict[str, str] = {}
        self.ttls: dict[str, int] = {}
        self.sets: dict[str, set] = {}

    async def incr(self, key):
        val = int(self.store.get(key, "0")) + 1
        self.store[key] = str(val)
        return val

    async def expire(self, key, ttl):
        if key in self.store or key in self.sets:
            self.ttls[key] = ttl
            return True
        return False

    async def ttl(self, key):
        if key not in self.store and key not in self.sets:
            return -2  # key does not exist
        return self.ttls.get(key, -1)  # -1 = exists but no expiry set

    async def setex(self, key, _ttl, value):
        self.store[key] = value

    async def exists(self, key):
        return 1 if key in self.store else 0

    async def sadd(self, key, *members):
        s = self.sets.setdefault(key, set())
        before = len(s)
        s.update(members)
        return len(s) - before

    async def scard(self, key):
        return len(self.sets.get(key, set()))

    async def delete(self, *keys):
        for k in keys:
            self.store.pop(k, None)
            self.ttls.pop(k, None)
            self.sets.pop(k, None)


@pytest.fixture
def lockout(monkeypatch):
    """A LoginLockout wired to a fresh fake Redis with a low trip threshold."""
    monkeypatch.setattr(ll_mod.settings, "login_lockout_enabled", True, raising=False)
    monkeypatch.setattr(ll_mod.settings, "login_lockout_max_attempts", 3, raising=False)
    monkeypatch.setattr(ll_mod.settings, "login_lockout_window_seconds", 900, raising=False)
    monkeypatch.setattr(ll_mod.settings, "login_lockout_duration_seconds", 900, raising=False)
    # These tests exercise the counter mechanics with the distinct-IP anti-DoS
    # gate DISABLED (min_distinct_ips=1 = legacy: any single source can trip).
    # The gate itself is covered by TestLockoutDistinctIpGate below.
    monkeypatch.setattr(ll_mod.settings, "login_lockout_min_distinct_ips", 1, raising=False)
    lo = LoginLockout()
    fake = _FakeRedis()
    monkeypatch.setattr(lo, "_get_redis", lambda: fake)
    lo._fake = fake  # expose for assertions
    return lo


@pytest.fixture
def lockout_multi_ip(monkeypatch):
    """A LoginLockout requiring failures from >=2 distinct source IPs to lock."""
    monkeypatch.setattr(ll_mod.settings, "login_lockout_enabled", True, raising=False)
    monkeypatch.setattr(ll_mod.settings, "login_lockout_max_attempts", 3, raising=False)
    monkeypatch.setattr(ll_mod.settings, "login_lockout_window_seconds", 900, raising=False)
    monkeypatch.setattr(ll_mod.settings, "login_lockout_duration_seconds", 900, raising=False)
    monkeypatch.setattr(ll_mod.settings, "login_lockout_min_distinct_ips", 2, raising=False)
    lo = LoginLockout()
    fake = _FakeRedis()
    monkeypatch.setattr(lo, "_get_redis", lambda: fake)
    lo._fake = fake
    return lo


class TestLoginLockout:
    @pytest.mark.unit
    async def test_not_locked_initially(self, lockout):
        assert await lockout.is_locked("alice") is False

    @pytest.mark.unit
    async def test_trips_after_max_attempts(self, lockout):
        # max_attempts = 3 → first two return False, the third trips.
        assert await lockout.record_failure("alice") is False
        assert await lockout.record_failure("alice") is False
        assert await lockout.record_failure("alice") is True
        assert await lockout.is_locked("alice") is True

    @pytest.mark.unit
    async def test_clear_resets_state(self, lockout):
        for _ in range(3):
            await lockout.record_failure("alice")
        assert await lockout.is_locked("alice") is True
        await lockout.clear("alice")
        assert await lockout.is_locked("alice") is False

    @pytest.mark.unit
    async def test_username_normalized(self, lockout):
        """Lockout is case/whitespace-insensitive on the username."""
        for _ in range(3):
            await lockout.record_failure("  Alice ")
        assert await lockout.is_locked("alice") is True
        assert await lockout.is_locked("ALICE") is True

    @pytest.mark.unit
    async def test_distinct_usernames_independent(self, lockout):
        for _ in range(3):
            await lockout.record_failure("alice")
        assert await lockout.is_locked("alice") is True
        assert await lockout.is_locked("bob") is False

    @pytest.mark.unit
    async def test_disabled_short_circuits(self, lockout, monkeypatch):
        monkeypatch.setattr(ll_mod.settings, "login_lockout_enabled", False, raising=False)
        for _ in range(5):
            assert await lockout.record_failure("alice") is False
        assert await lockout.is_locked("alice") is False

    @pytest.mark.unit
    async def test_empty_username_ignored(self, lockout):
        assert await lockout.record_failure("") is False
        assert await lockout.is_locked("") is False

    @pytest.mark.unit
    async def test_is_locked_fails_open_on_redis_error(self, lockout, monkeypatch):
        """A Redis outage must NOT lock everyone out — is_locked returns False."""
        def _boom():
            raise ConnectionError("redis down")
        monkeypatch.setattr(lockout, "_get_redis", _boom)
        assert await lockout.is_locked("alice") is False

    @pytest.mark.unit
    async def test_record_failure_fails_open_on_redis_error(self, lockout, monkeypatch):
        """record_failure swallows a Redis error (best-effort, never raises)."""
        def _boom():
            raise ConnectionError("redis down")
        monkeypatch.setattr(lockout, "_get_redis", _boom)
        assert await lockout.record_failure("alice") is False

    @pytest.mark.unit
    async def test_ttl_rearmed_if_lost(self, lockout):
        """A counter left without a TTL (dropped EXPIRE) is re-armed on the next
        failure, so it can never become a permanent lock (review #5)."""
        fake = lockout._fake
        # Simulate a first failure whose EXPIRE was lost: counter exists, no TTL.
        fail_key = "login_fail:alice"
        fake.store[fail_key] = "1"  # count present
        # no entry in fake.ttls → ttl() returns -1 (no expiry)
        assert fake.ttls.get(fail_key) is None
        # Next failure (count becomes 2) must detect the missing TTL and re-arm.
        await lockout.record_failure("alice")
        assert fake.ttls.get(fail_key) == 900


class TestLockoutDistinctIpGate:
    """Anti-DoS gate (security audit): with login_lockout_min_distinct_ips=2 a
    single attacker can no longer lock out a known username at will — the lock
    only trips once failures come from multiple distinct source IPs."""

    @pytest.mark.unit
    async def test_single_ip_never_locks(self, lockout_multi_ip):
        """5 failures all from ONE IP must NOT lock (the DoS the fix closes)."""
        for _ in range(5):
            assert await lockout_multi_ip.record_failure("alice", "1.2.3.4") is False
        assert await lockout_multi_ip.is_locked("alice") is False

    @pytest.mark.unit
    async def test_two_ips_lock_at_threshold(self, lockout_multi_ip):
        """Reaching max_attempts across >=2 distinct IPs DOES lock (real attack)."""
        assert await lockout_multi_ip.record_failure("alice", "1.1.1.1") is False
        assert await lockout_multi_ip.record_failure("alice", "1.1.1.1") is False
        # 3rd failure hits max_attempts=3 AND a 2nd distinct IP → trips.
        assert await lockout_multi_ip.record_failure("alice", "2.2.2.2") is True
        assert await lockout_multi_ip.is_locked("alice") is True

    @pytest.mark.unit
    async def test_count_met_but_one_ip_holds_open(self, lockout_multi_ip):
        """Counter past threshold but a single IP → still not locked."""
        for _ in range(4):
            await lockout_multi_ip.record_failure("alice", "9.9.9.9")
        assert await lockout_multi_ip.is_locked("alice") is False
        # A second IP then completes the distinct-IP requirement on the next fail.
        assert await lockout_multi_ip.record_failure("alice", "8.8.8.8") is True

    @pytest.mark.unit
    async def test_clear_removes_ip_set(self, lockout_multi_ip):
        await lockout_multi_ip.record_failure("alice", "1.1.1.1")
        await lockout_multi_ip.record_failure("alice", "2.2.2.2")
        assert lockout_multi_ip._fake.sets.get("login_fail_ips:alice")
        await lockout_multi_ip.clear("alice")
        assert lockout_multi_ip._fake.sets.get("login_fail_ips:alice") is None
