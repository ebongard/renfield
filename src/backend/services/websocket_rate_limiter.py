"""
WebSocket Rate Limiter for Renfield

Provides per-client rate limiting for WebSocket messages.
Uses a sliding window algorithm for accurate rate limiting.
"""

from collections import defaultdict
from datetime import UTC, datetime, timedelta

from loguru import logger

from utils.config import settings


class WSRateLimiter:
    """
    Rate limiter for WebSocket messages.

    Uses sliding window counters for per-second and per-minute limits.
    Thread-safe through the GIL (for asyncio single-threaded usage).
    """

    def __init__(
        self,
        per_second: int | None = None,
        per_minute: int | None = None,
        enabled: bool | None = None
    ):
        """
        Initialize rate limiter.

        Args:
            per_second: Max messages per second (default from settings)
            per_minute: Max messages per minute (default from settings)
            enabled: Whether rate limiting is enabled (default from settings)
        """
        self.per_second = per_second if per_second is not None else settings.ws_rate_limit_per_second
        self.per_minute = per_minute if per_minute is not None else settings.ws_rate_limit_per_minute
        self.enabled = enabled if enabled is not None else settings.ws_rate_limit_enabled

        # Timestamps of messages per client
        self._timestamps: dict[str, list[datetime]] = defaultdict(list)

        # Track violations for logging
        self._violations: dict[str, int] = defaultdict(int)

    def check(self, client_id: str) -> tuple[bool, str]:
        """
        Check if a client is allowed to send a message.

        Args:
            client_id: Unique identifier for the client (device_id or IP)

        Returns:
            Tuple of (allowed: bool, reason: str)
        """
        if not self.enabled:
            return True, ""

        now = datetime.now(UTC).replace(tzinfo=None)

        # Clean up old timestamps
        second_ago = now - timedelta(seconds=1)
        minute_ago = now - timedelta(minutes=1)

        self._timestamps[client_id] = [
            t for t in self._timestamps[client_id]
            if t > minute_ago
        ]

        # Count recent messages
        recent_second = sum(1 for t in self._timestamps[client_id] if t > second_ago)
        recent_minute = len(self._timestamps[client_id])

        # Check limits
        if recent_second >= self.per_second:
            self._violations[client_id] += 1
            if self._violations[client_id] <= 3:  # Log first 3 violations
                logger.warning(f"Rate limit exceeded (per second) for {client_id}")
            return False, f"Rate limit exceeded: max {self.per_second} messages per second"

        if recent_minute >= self.per_minute:
            self._violations[client_id] += 1
            if self._violations[client_id] <= 3:
                logger.warning(f"Rate limit exceeded (per minute) for {client_id}")
            return False, f"Rate limit exceeded: max {self.per_minute} messages per minute"

        # Allow and record timestamp
        self._timestamps[client_id].append(now)
        return True, ""

    def reset(self, client_id: str):
        """Reset rate limit counters for a client."""
        self._timestamps.pop(client_id, None)
        self._violations.pop(client_id, None)

    def cleanup(self):
        """Remove stale entries from all clients."""
        now = datetime.now(UTC).replace(tzinfo=None)
        minute_ago = now - timedelta(minutes=1)

        stale_clients = []
        for client_id in list(self._timestamps.keys()):
            self._timestamps[client_id] = [
                t for t in self._timestamps[client_id]
                if t > minute_ago
            ]
            if not self._timestamps[client_id]:
                stale_clients.append(client_id)

        for client_id in stale_clients:
            del self._timestamps[client_id]
            self._violations.pop(client_id, None)

    def get_stats(self, client_id: str) -> dict[str, int]:
        """Get rate limit stats for a client."""
        now = datetime.now(UTC).replace(tzinfo=None)
        second_ago = now - timedelta(seconds=1)

        timestamps = self._timestamps.get(client_id, [])
        return {
            "messages_last_second": sum(1 for t in timestamps if t > second_ago),
            "messages_last_minute": len(timestamps),
            "limit_per_second": self.per_second,
            "limit_per_minute": self.per_minute,
            "violations": self._violations.get(client_id, 0)
        }


class WSConnectionLimiter:
    """
    Connection limiter for WebSocket connections.

    Limits the number of concurrent connections per IP address.
    """

    def __init__(self, max_per_ip: int | None = None):
        """
        Initialize connection limiter.

        Args:
            max_per_ip: Max connections per IP (default from settings)
        """
        self.max_per_ip = max_per_ip if max_per_ip is not None else settings.ws_max_connections_per_ip

        # IP -> set of device_ids
        self._connections: dict[str, set] = defaultdict(set)

    def can_connect(self, ip_address: str, device_id: str) -> tuple[bool, str]:
        """
        Check if a new connection is allowed.

        Args:
            ip_address: Client IP address
            device_id: Device identifier

        Returns:
            Tuple of (allowed: bool, reason: str)
        """
        if not ip_address:
            return True, ""

        current = self._connections.get(ip_address, set())

        # Allow if device is already connected (reconnection)
        if device_id in current:
            return True, ""

        # Check limit
        if len(current) >= self.max_per_ip:
            logger.warning(f"Connection limit exceeded for IP {ip_address}: {len(current)} connections")
            return False, f"Too many connections from this IP (max: {self.max_per_ip})"

        return True, ""

    def add_connection(self, ip_address: str, device_id: str):
        """Record a new connection."""
        if ip_address:
            self._connections[ip_address].add(device_id)

    def remove_connection(self, ip_address: str, device_id: str):
        """Remove a connection."""
        if ip_address and ip_address in self._connections:
            self._connections[ip_address].discard(device_id)
            if not self._connections[ip_address]:
                del self._connections[ip_address]

    def get_connection_count(self, ip_address: str) -> int:
        """Get number of connections from an IP."""
        return len(self._connections.get(ip_address, set()))


# Global singleton instances
_rate_limiter: WSRateLimiter | None = None
_connection_limiter: WSConnectionLimiter | None = None


def get_rate_limiter() -> WSRateLimiter:
    """Get or create the global rate limiter."""
    global _rate_limiter
    if _rate_limiter is None:
        _rate_limiter = WSRateLimiter()
    return _rate_limiter


def get_connection_limiter() -> WSConnectionLimiter:
    """Get or create the global connection limiter."""
    global _connection_limiter
    if _connection_limiter is None:
        _connection_limiter = WSConnectionLimiter()
    return _connection_limiter
