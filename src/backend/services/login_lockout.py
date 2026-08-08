"""
Login Account-Lockout Service

Redis-backed per-username failed-login throttle (#693). Complements the per-IP
REST rate limit (``API_RATE_LIMIT_AUTH``): the rate limit caps request VOLUME
from one IP, while this locks a USERNAME after repeated failures regardless of
source IP — so an attacker rotating IPs against one account is still stopped.

Keyed on the normalized username (lower-cased, stripped), never on a secret.
Two Redis keys per username:
  - ``login_fail:<user>``  — a counter with a rolling TTL (the failure window).
  - ``login_lock:<user>``  — a short-lived marker set once the counter trips.

Fail-OPEN by design: if Redis is unreachable, ``is_locked`` returns False and
``record_failure`` is a no-op. A revocation store (token blacklist) fails CLOSED
because honoring a revoked token is the dangerous direction; a lockout store
must fail OPEN because a Redis blip locking out the entire household is the
dangerous direction here. The per-IP rate limit remains as the backstop.

The lockout response stays OPAQUE (the caller returns the same 401 as bad
credentials) so it never becomes a username-enumeration oracle; the event is
surfaced via logging + the ``login_failure_total{reason="locked_out"}`` metric.
"""
from loguru import logger

from services.redis_client import get_redis
from utils.config import settings

FAIL_PREFIX = "login_fail:"
LOCK_PREFIX = "login_lock:"
FAIL_IPS_PREFIX = "login_fail_ips:"


def _normalize(username: str) -> str:
    return (username or "").strip().lower()


class LoginLockout:
    """Redis-backed per-username login-attempt lockout."""

    def _get_redis(self):
        # Reuse the process-wide pooled client so the connection is set up once
        # and closed by lifecycle.close_redis() on shutdown (no leaked private
        # connection). decode_responses=True matches this module's string ops.
        return get_redis()

    async def is_locked(self, username: str) -> bool:
        """Return True if this username is currently locked out.

        Fails OPEN (returns False) when disabled or on a Redis error.
        """
        if not settings.login_lockout_enabled:
            return False
        user = _normalize(username)
        if not user:
            return False
        try:
            redis = self._get_redis()
            return await redis.exists(f"{LOCK_PREFIX}{user}") > 0
        except Exception as e:
            logger.error(f"Login lockout check failed — failing OPEN: {e}")
            return False

    async def record_failure(self, username: str, source_ip: str | None = None) -> bool:
        """Record a failed login for this username.

        Increments the rolling-window failure counter; the username is locked for
        ``login_lockout_duration_seconds`` once the counter reaches
        ``login_lockout_max_attempts`` **and** failures have come from at least
        ``login_lockout_min_distinct_ips`` distinct source IPs. The distinct-IP
        gate is the anti-DoS control (security audit): without it any single
        attacker could lock out a known username at will; a single-IP brute force
        is already throttled by the per-IP rate limit, so the per-username lock
        only needs to fire on the IP-rotating attack it was built for.

        Returns True if THIS failure tripped the lock (so the caller can log the
        transition). Best-effort — a Redis error is swallowed (fail-open).
        """
        if not settings.login_lockout_enabled:
            return False
        user = _normalize(username)
        if not user:
            return False
        try:
            redis = self._get_redis()
            fail_key = f"{FAIL_PREFIX}{user}"
            ips_key = f"{FAIL_IPS_PREFIX}{user}"
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

            # Track the distinct source IPs contributing to this window, on the
            # same TTL as the counter. A missing/unresolved IP is bucketed under
            # a sentinel so it still counts as one source.
            distinct_ips = 1
            min_ips = max(1, int(settings.login_lockout_min_distinct_ips))
            if min_ips > 1:
                await redis.sadd(ips_key, source_ip or "-")
                if await redis.ttl(ips_key) < 0:
                    await redis.expire(ips_key, settings.login_lockout_window_seconds)
                distinct_ips = await redis.scard(ips_key)

            if count >= settings.login_lockout_max_attempts and distinct_ips >= min_ips:
                await redis.setex(
                    f"{LOCK_PREFIX}{user}",
                    settings.login_lockout_duration_seconds,
                    "1",
                )
                return True
            return False
        except Exception as e:
            logger.error(f"Login lockout record_failure failed (ignored): {e}")
            return False

    async def clear(self, username: str) -> None:
        """Clear the failure counter + lock for a username (call on success)."""
        if not settings.login_lockout_enabled:
            return
        user = _normalize(username)
        if not user:
            return
        try:
            redis = self._get_redis()
            await redis.delete(
                f"{FAIL_PREFIX}{user}",
                f"{LOCK_PREFIX}{user}",
                f"{FAIL_IPS_PREFIX}{user}",
            )
        except Exception as e:
            logger.error(f"Login lockout clear failed (ignored): {e}")


# Singleton instance
login_lockout = LoginLockout()
