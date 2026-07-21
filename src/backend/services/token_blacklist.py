"""
JWT Token Blacklist Service

Redis-based token blacklist for logout/revocation.
Stores JTI (JWT ID) with TTL matching the token's remaining lifetime.
"""
import redis.asyncio as aioredis
from loguru import logger

from utils.config import settings

BLACKLIST_PREFIX = "blacklist:"


class TokenBlacklist:
    """Redis-backed JWT token blacklist."""

    def __init__(self):
        self._redis: aioredis.Redis | None = None

    def _get_redis(self) -> aioredis.Redis:
        if self._redis is None:
            self._redis = aioredis.from_url(
                settings.redis_url, decode_responses=True
            )
        return self._redis

    async def add(self, jti: str, ttl_seconds: int) -> bool:
        """
        Blacklist a token JTI.

        Args:
            jti: The JWT ID to blacklist
            ttl_seconds: Time-to-live in seconds (token's remaining lifetime)

        Returns:
            True if the revocation was persisted (or was a no-op because the token
            had already expired, ttl<=0), False if the write FAILED (Redis
            unreachable). Security audit M6: callers (logout) must not report
            success when the revocation could not be persisted — otherwise the
            token silently resurrects once Redis recovers.
        """
        if ttl_seconds <= 0:
            return True
        try:
            redis = self._get_redis()
            await redis.setex(f"{BLACKLIST_PREFIX}{jti}", ttl_seconds, "1")
            return True
        except Exception as e:
            logger.error(f"Failed to blacklist token: {e}")
            return False

    async def is_blacklisted(self, jti: str) -> bool:
        """
        Check if a token JTI is blacklisted.

        Args:
            jti: The JWT ID to check

        Returns:
            True if the token has been revoked
        """
        try:
            redis = self._get_redis()
            return await redis.exists(f"{BLACKLIST_PREFIX}{jti}") > 0
        except Exception as e:
            logger.error(f"Token blacklist check failed — failing CLOSED: {e}")
            # Fail CLOSED: if the revocation store is unreachable we cannot prove
            # the token is NOT revoked, so treat it as revoked rather than honor
            # an unverifiable token. A Redis outage then rejects requests (loud,
            # operator-visible) instead of silently accepting revoked JWTs. (#698)
            return True


# Singleton instance
token_blacklist = TokenBlacklist()
