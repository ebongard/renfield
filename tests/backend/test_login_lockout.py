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
        removed = 0
        for k in keys:
            if k in self.store:
                removed += 1
            self.store.pop(k, None)
            self.ttls.pop(k, None)
        return removed

    async def scan_iter(self, match: str = "*", count: int | None = None):
        # Redis glob ≈ fnmatch for the patterns this module emits: the username
        # segment is percent-encoded (no ``*?[]\\`` can occur in it), so the only
        # metacharacter is the module's own trailing ``*``. fnmatch does NOT
        # implement Redis' backslash escapes — which is exactly why the module
        # encodes instead of escaping.
        import fnmatch
        for k in list(self.store):
            if fnmatch.fnmatchcase(k, match):
                yield k


@pytest.fixture
def lockout(monkeypatch):
    """A LoginLockout wired to a fresh fake Redis with a low trip threshold."""
    monkeypatch.setattr(ll_mod.settings, "login_lockout_enabled", True, raising=False)
    monkeypatch.setattr(ll_mod.settings, "login_lockout_max_attempts", 3, raising=False)
    monkeypatch.setattr(ll_mod.settings, "login_lockout_window_seconds", 900, raising=False)
    monkeypatch.setattr(ll_mod.settings, "login_lockout_duration_seconds", 900, raising=False)
    # Backstop = 3× the per-IP threshold (production default is 5× = 25).
    monkeypatch.setattr(ll_mod.settings, "login_lockout_username_max_attempts", 9, raising=False)
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


class TestPerIpScope:
    """BL-0125 (2026-09-20): the lock is scoped per (username, client IP) with a
    username-wide backstop, so a stranger who knows a username locks out only
    their own address — the owner at another address still gets in."""

    @pytest.mark.unit
    async def test_failures_from_one_ip_lock_only_that_ip(self, lockout):
        for _ in range(3):
            await lockout.record_failure("alice", "10.0.0.1")
        assert await lockout.is_locked("alice", "10.0.0.1") is True
        assert await lockout.is_locked("alice", "10.0.0.2") is False
        # Username-only view (no IP known) is not locked either: 3 < backstop 9.
        assert await lockout.is_locked("alice") is False

    @pytest.mark.unit
    async def test_third_failure_from_ip_reports_tripped(self, lockout):
        assert await lockout.record_failure("alice", "10.0.0.1") is False
        assert await lockout.record_failure("alice", "10.0.0.1") is False
        assert await lockout.record_failure("alice", "10.0.0.1") is True

    @pytest.mark.unit
    async def test_username_backstop_trips_across_ips(self, lockout):
        # 9 failures spread over 9 addresses: no single IP reaches 3, but the
        # username-wide counter does → locked for EVERY address.
        for i in range(8):
            assert await lockout.record_failure("alice", f"10.0.0.{i}") is False
        assert await lockout.record_failure("alice", "10.0.0.99") is True
        assert await lockout.is_locked("alice", "192.168.7.7") is True
        assert await lockout.is_locked("alice") is True

    @pytest.mark.unit
    async def test_success_clears_own_ip_and_backstop_but_not_other_ip(self, lockout):
        for _ in range(3):
            await lockout.record_failure("alice", "10.0.0.1")  # attacker
        await lockout.record_failure("alice", "10.0.0.2")      # owner typo
        await lockout.clear("alice", "10.0.0.2")               # owner logs in
        assert await lockout.is_locked("alice", "10.0.0.1") is True   # attacker stays locked
        assert await lockout.is_locked("alice", "10.0.0.2") is False
        assert "login_fail:alice" not in lockout._fake.store          # backstop reset
        assert "login_fail:alice|10.0.0.2" not in lockout._fake.store

    @pytest.mark.unit
    async def test_no_ip_keeps_strict_username_threshold(self, lockout):
        # Legacy / unknown-transport path: username scope trips at max_attempts.
        for _ in range(2):
            assert await lockout.record_failure("alice") is False
        assert await lockout.record_failure("alice") is True

    @pytest.mark.unit
    async def test_ip_whitespace_normalized(self, lockout):
        for _ in range(3):
            await lockout.record_failure("alice", " 10.0.0.1 ")
        assert await lockout.is_locked("alice", "10.0.0.1") is True

    @pytest.mark.unit
    async def test_global_ipv6_clients_share_their_64(self, lockout):
        # One ISP-assigned home prefix is a whole /64 — per-address rotation
        # inside it must not be free, so the scope is the /64, not the host.
        for _ in range(3):
            await lockout.record_failure("alice", "2a02:8071:1:2::10")
        assert await lockout.is_locked("alice", "2a02:8071:1:2:ffff::1") is True
        assert await lockout.is_locked("alice", "2a02:8071:1:3::10") is False

    @pytest.mark.unit
    async def test_ula_ipv6_keeps_host_granularity(self, lockout):
        # On a LAN (ULA fd00::/8) owner and attacker share one /64 — collapsing
        # would make the per-IP scope a per-LAN one, so the host is kept.
        for _ in range(3):
            await lockout.record_failure("alice", "fd12:3456:789a:1::10")
        assert await lockout.is_locked("alice", "fd12:3456:789a:1::10") is True
        assert await lockout.is_locked("alice", "fd12:3456:789a:1::11") is False

    @pytest.mark.unit
    async def test_has_any_lock_covers_both_scopes(self, lockout):
        assert await lockout.has_any_lock("alice") is False
        for _ in range(3):
            await lockout.record_failure("alice", "10.0.0.1")
        assert await lockout.has_any_lock("alice") is True      # per-IP lock
        assert await lockout.has_any_lock("bob") is False
        for _ in range(3):
            await lockout.record_failure("carol")
        assert await lockout.has_any_lock("Carol") is True      # backstop lock

    @pytest.mark.unit
    async def test_pipe_in_username_cannot_alias_another_users_ip_scope(self, lockout):
        # ``eve|10.0.0.5`` is a legal username; its keys must not read as eve's
        # per-IP scope for 10.0.0.5 (the separator is not reachable from a name).
        for _ in range(3):
            await lockout.record_failure("eve|10.0.0.5")
        assert await lockout.is_locked("eve|10.0.0.5") is True
        assert await lockout.is_locked("eve", "10.0.0.5") is False
        assert await lockout.locked_usernames() == {"eve|10.0.0.5"}
        assert await lockout.unlock("eve") == 0
        assert await lockout.is_locked("eve|10.0.0.5") is True

    @pytest.mark.unit
    async def test_plain_username_keys_keep_the_legacy_format(self, lockout):
        # Compat: for ordinary names the encoding is the identity, so a lock
        # written before the per-IP change (``login_lock:alice``) still applies.
        lockout._fake.store["login_lock:alice"] = "1"
        assert await lockout.is_locked("alice", "10.0.0.1") is True
        await lockout.record_failure("bob.smith-1")
        assert "login_fail:bob.smith-1" in lockout._fake.store


class TestAdminUnlock:
    @pytest.mark.unit
    async def test_unlock_removes_every_scope(self, lockout):
        for _ in range(3):
            await lockout.record_failure("alice", "10.0.0.1")
        for _ in range(3):
            await lockout.record_failure("alice", "10.0.0.2")
        assert await lockout.is_locked("alice", "10.0.0.1") is True
        removed = await lockout.unlock("Alice")
        # fail+lock for two IPs + the username fail counter = 5 keys
        assert removed == 5
        assert await lockout.is_locked("alice", "10.0.0.1") is False
        assert await lockout.is_locked("alice", "10.0.0.2") is False
        assert not [k for k in lockout._fake.store if "alice" in k]

    @pytest.mark.unit
    async def test_unlock_leaves_other_users_alone(self, lockout):
        for _ in range(3):
            await lockout.record_failure("alice", "10.0.0.1")
            await lockout.record_failure("bob", "10.0.0.1")
        await lockout.unlock("alice")
        assert await lockout.is_locked("bob", "10.0.0.1") is True

    @pytest.mark.unit
    async def test_unlock_is_idempotent(self, lockout):
        assert await lockout.unlock("alice") == 0

    @pytest.mark.unit
    async def test_unlock_username_with_glob_chars_does_not_widen_scan(self, lockout):
        for _ in range(3):
            await lockout.record_failure("bob", "10.0.0.1")
        # A username made of glob metacharacters must not match bob's keys.
        assert await lockout.unlock("*") == 0
        assert await lockout.is_locked("bob", "10.0.0.1") is True

    @pytest.mark.unit
    async def test_unlock_user_with_glob_chars_removes_own_keys_only(self, lockout):
        # The positive case: a legal username like ``a*b`` (only length is
        # validated) is percent-encoded in the key, so its own SCAN finds its
        # keys and nothing else.
        for _ in range(3):
            await lockout.record_failure("a*b", "10.0.0.1")
            await lockout.record_failure("bob", "10.0.0.1")
        assert await lockout.is_locked("a*b", "10.0.0.1") is True
        assert await lockout.unlock("a*b") == 3  # ip fail + ip lock + username fail
        assert await lockout.is_locked("a*b", "10.0.0.1") is False
        assert await lockout.is_locked("bob", "10.0.0.1") is True

    @pytest.mark.unit
    async def test_unlock_raises_when_store_unreachable(self, lockout, monkeypatch):
        # The admin must never be told "cleared" while the lock may still stand.
        from services.login_lockout import LockoutStoreUnavailable

        def _boom():
            raise ConnectionError("redis down")
        monkeypatch.setattr(lockout, "_get_redis", _boom)
        with pytest.raises(LockoutStoreUnavailable):
            await lockout.unlock("alice")

    @pytest.mark.unit
    async def test_unlock_disabled_is_noop(self, lockout, monkeypatch):
        monkeypatch.setattr(ll_mod.settings, "login_lockout_enabled", False, raising=False)
        assert await lockout.unlock("alice") == 0


class TestLockoutConfigBounds:
    """A zero threshold would lock on EVERY failure; a backstop below the per-IP
    threshold would make the per-IP scope moot. Both are boot errors."""

    @pytest.mark.unit
    def test_zero_max_attempts_is_rejected(self):
        from pydantic import ValidationError

        from utils.config import Settings

        with pytest.raises(ValidationError):
            Settings(_env_file=None, login_lockout_max_attempts=0)

    @pytest.mark.unit
    def test_backstop_below_per_ip_threshold_is_rejected(self):
        from pydantic import ValidationError

        from utils.config import Settings

        with pytest.raises(ValidationError, match="must be >= LOGIN_LOCKOUT_MAX_ATTEMPTS"):
            Settings(_env_file=None, login_lockout_max_attempts=5, login_lockout_username_max_attempts=4)

    @pytest.mark.unit
    def test_backstop_equal_or_above_is_accepted(self):
        from utils.config import Settings

        assert Settings(_env_file=None, login_lockout_max_attempts=5, login_lockout_username_max_attempts=5)
        assert Settings(_env_file=None).login_lockout_username_max_attempts >= Settings(_env_file=None).login_lockout_max_attempts


class TestLockedUsernames:
    @pytest.mark.unit
    async def test_lists_users_with_any_lock(self, lockout):
        for _ in range(3):
            await lockout.record_failure("alice", "10.0.0.1")   # per-IP lock
        for _ in range(3):
            await lockout.record_failure("carol")               # username lock
        await lockout.record_failure("bob", "10.0.0.1")         # counter only
        assert await lockout.locked_usernames() == {"alice", "carol"}

    @pytest.mark.unit
    async def test_fails_open_to_empty(self, lockout, monkeypatch):
        def _boom():
            raise ConnectionError("redis down")
        monkeypatch.setattr(lockout, "_get_redis", _boom)
        assert await lockout.locked_usernames() == set()
