"""
Login Account-Lockout Service

Redis-backed failed-login throttle (#693, per-IP scoping + admin unlock 2026-09-20,
BL-0125). Complements the per-IP REST rate limit (``API_RATE_LIMIT_AUTH``): the
rate limit caps request VOLUME from one IP, this locks a login target after
repeated failures.

Two lock scopes, both keyed on the normalized username (lower-cased, stripped),
never on a secret:

- **(username, client IP)** — the primary lock. ``login_lockout_max_attempts``
  failures from ONE address lock that address out of that account for
  ``login_lockout_duration_seconds``. Someone who merely knows a username can
  therefore lock out only themselves — the legitimate owner logging in from
  another address is unaffected (the username-keyed DoS the first version had).
- **username only** — the backstop. ``login_lockout_username_max_attempts``
  (default 5× the per-IP threshold) failures from ANY addresses lock the
  account regardless of source, so an attacker rotating IPs is still stopped;
  the higher threshold keeps a single hostile address from tripping it.

Redis keys::

    login_fail:<user>|<ip>   counter, rolling TTL   login_lock:<user>|<ip>   lock marker
    login_fail:<user>        counter, rolling TTL   login_lock:<user>        lock marker

The username-only keys are the pre-2026-09-20 key format, so an existing lock
survives the upgrade as a backstop lock.

Fail-OPEN by design: if Redis is unreachable, ``is_locked`` returns False and
``record_failure`` is a no-op. A revocation store (token blacklist) fails CLOSED
because honoring a revoked token is the dangerous direction; a lockout store
must fail OPEN because a Redis blip locking out the entire household is the
dangerous direction here. The per-IP rate limit remains as the backstop.

The lockout response stays OPAQUE (the caller returns the same 401 as bad
credentials) so it never becomes a username-enumeration oracle; the event is
surfaced via logging + the ``login_failure_total{reason="locked_out"}`` metric.

Admin recovery: ``unlock(username)`` removes every counter and lock of a user
(``POST /api/users/{id}/unlock``, ``users.manage``); before it existed the only
way out of a lock was waiting or deleting Redis keys by hand.
"""
from loguru import logger

from services.redis_client import get_redis
from utils.config import settings

FAIL_PREFIX = "login_fail:"
LOCK_PREFIX = "login_lock:"
_IP_SEP = "|"


def _normalize(username: str) -> str:
    return (username or "").strip().lower()


def _normalize_ip(ip: str | None) -> str | None:
    ip = (ip or "").strip()
    return ip or None


def _glob_escape(value: str) -> str:
    """Escape Redis glob metacharacters so a username can't widen a SCAN."""
    out = []
    for ch in value:
        if ch in "*?[]\\":
            out.append("\\" + ch)
        else:
            out.append(ch)
    return "".join(out)


def _username_of_key(key: str, prefix: str) -> str:
    """``login_lock:alice|1.2.3.4`` → ``alice`` (also handles the bare form)."""
    rest = key[len(prefix):]
    return rest.split(_IP_SEP, 1)[0]


class LoginLockout:
    """Redis-backed per-(username, IP) login lockout with a per-username backstop."""

    def _get_redis(self):
        # Reuse the process-wide pooled client so the connection is set up once
        # and closed by lifecycle.close_redis() on shutdown (no leaked private
        # connection). decode_responses=True matches this module's string ops.
        return get_redis()

    # ------------------------------------------------------------------ keys
    @staticmethod
    def _keys(user: str, ip: str | None) -> tuple[str, str]:
        """(fail_key, lock_key) for the scope: username-only when ``ip`` is None."""
        suffix = f"{user}{_IP_SEP}{ip}" if ip else user
        return f"{FAIL_PREFIX}{suffix}", f"{LOCK_PREFIX}{suffix}"

    # --------------------------------------------------------------- queries
    async def is_locked(self, username: str, ip: str | None = None) -> bool:
        """True if this username is locked for this IP OR locked outright.

        Fails OPEN (returns False) when disabled or on a Redis error.
        """
        if not settings.login_lockout_enabled:
            return False
        user = _normalize(username)
        if not user:
            return False
        ip = _normalize_ip(ip)
        try:
            redis = self._get_redis()
            _, user_lock = self._keys(user, None)
            if await redis.exists(user_lock) > 0:
                return True
            if ip:
                _, ip_lock = self._keys(user, ip)
                return await redis.exists(ip_lock) > 0
            return False
        except Exception as e:
            logger.error(f"Login lockout check failed — failing OPEN: {e}")
            return False

    async def locked_usernames(self) -> set[str]:
        """Normalized usernames that currently hold ANY lock (per-IP or backstop).

        One SCAN for the admin user list. Fails OPEN to an empty set.
        """
        if not settings.login_lockout_enabled:
            return set()
        try:
            redis = self._get_redis()
            found: set[str] = set()
            async for key in redis.scan_iter(match=f"{LOCK_PREFIX}*"):
                found.add(_username_of_key(str(key), LOCK_PREFIX))
            return found
        except Exception as e:
            logger.error(f"Login lockout scan failed — failing OPEN: {e}")
            return set()

    # --------------------------------------------------------------- writes
    async def _count_failure(self, redis, fail_key: str, lock_key: str, threshold: int) -> bool:
        count = await redis.incr(fail_key)
        # Arm the window TTL on the first failure. We deliberately do NOT
        # refresh it on later failures (a rolling reset would let a slow drip
        # hold the window open forever) — the counter expires N seconds after
        # the FIRST failure. BUT re-arm if the key somehow has no TTL (e.g. a
        # transient Redis error dropped the earlier EXPIRE): a persistent,
        # never-expiring counter would eventually lock a legitimate user out
        # permanently. `ttl < 0` means no expiry set (-1) or missing (-2).
        if count == 1 or await redis.ttl(fail_key) < 0:
            await redis.expire(fail_key, settings.login_lockout_window_seconds)
        if count >= threshold:
            await redis.setex(lock_key, settings.login_lockout_duration_seconds, "1")
            return True
        return False

    async def record_failure(self, username: str, ip: str | None = None) -> bool:
        """Record a failed login for this username (and, if known, this IP).

        Counts against BOTH scopes: the per-IP counter trips at
        ``login_lockout_max_attempts``, the username backstop at
        ``login_lockout_username_max_attempts``. Returns True if THIS failure
        tripped either lock (so the caller can log the transition). Best-effort —
        a Redis error is swallowed (fail-open).
        """
        if not settings.login_lockout_enabled:
            return False
        user = _normalize(username)
        if not user:
            return False
        ip = _normalize_ip(ip)
        try:
            redis = self._get_redis()
            tripped = False
            if ip:
                fail_key, lock_key = self._keys(user, ip)
                tripped = await self._count_failure(
                    redis, fail_key, lock_key, settings.login_lockout_max_attempts
                )
            fail_key, lock_key = self._keys(user, None)
            threshold = (
                settings.login_lockout_username_max_attempts
                if ip
                # No client IP known (tests, odd transports): the username scope
                # is the only one, so it keeps the strict per-IP threshold.
                else settings.login_lockout_max_attempts
            )
            tripped = await self._count_failure(redis, fail_key, lock_key, threshold) or tripped
            return tripped
        except Exception as e:
            logger.error(f"Login lockout record_failure failed (ignored): {e}")
            return False

    async def clear(self, username: str, ip: str | None = None) -> None:
        """Clear state on a SUCCESSFUL login: the username backstop and this IP's
        scope. Another address's lock is left in place — a successful login from
        the owner's phone must not release the attacker's address.
        """
        if not settings.login_lockout_enabled:
            return
        user = _normalize(username)
        if not user:
            return
        ip = _normalize_ip(ip)
        try:
            redis = self._get_redis()
            keys = list(self._keys(user, None))
            if ip:
                keys.extend(self._keys(user, ip))
            await redis.delete(*keys)
        except Exception as e:
            logger.error(f"Login lockout clear failed (ignored): {e}")

    async def unlock(self, username: str) -> int:
        """Admin unlock: remove EVERY counter and lock of this username, all IPs.

        Returns the number of keys removed (0 when disabled / nothing held /
        Redis unreachable — the caller reports, never raises).
        """
        if not settings.login_lockout_enabled:
            return 0
        user = _normalize(username)
        if not user:
            return 0
        try:
            redis = self._get_redis()
            keys = list(self._keys(user, None))
            pattern_user = _glob_escape(user)
            for prefix in (FAIL_PREFIX, LOCK_PREFIX):
                async for key in redis.scan_iter(match=f"{prefix}{pattern_user}{_IP_SEP}*"):
                    keys.append(str(key))
            removed = await redis.delete(*keys)
            return int(removed or 0)
        except Exception as e:
            logger.error(f"Login lockout unlock failed (ignored): {e}")
            return 0


# Singleton instance
login_lockout = LoginLockout()
