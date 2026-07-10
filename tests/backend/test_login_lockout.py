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

    async def incr(self, key):
        val = int(self.store.get(key, "0")) + 1
        self.store[key] = str(val)
        return val

    async def expire(self, key, ttl):
        if key in self.store:
            self.ttls[key] = ttl
            return True
        return False

    async def ttl(self, key):
        if key not in self.store:
            return -2  # key does not exist
        return self.ttls.get(key, -1)  # -1 = exists but no expiry set

    async def setex(self, key, _ttl, value):
        self.store[key] = value

    async def exists(self, key):
        return 1 if key in self.store else 0

    async def delete(self, *keys):
        for k in keys:
            self.store.pop(k, None)
            self.ttls.pop(k, None)


@pytest.fixture
def lockout(monkeypatch):
    """A LoginLockout wired to a fresh fake Redis with a low trip threshold."""
    monkeypatch.setattr(ll_mod.settings, "login_lockout_enabled", True, raising=False)
    monkeypatch.setattr(ll_mod.settings, "login_lockout_max_attempts", 3, raising=False)
    monkeypatch.setattr(ll_mod.settings, "login_lockout_window_seconds", 900, raising=False)
    monkeypatch.setattr(ll_mod.settings, "login_lockout_duration_seconds", 900, raising=False)
    lo = LoginLockout()
    fake = _FakeRedis()
    monkeypatch.setattr(lo, "_get_redis", lambda: fake)
    lo._fake = fake  # expose for assertions
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
