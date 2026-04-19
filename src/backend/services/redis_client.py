"""Shared async Redis client for request-path callers.

Per-request aioredis connections add roundtrip latency that compounds
when the upload endpoint needs both a heartbeat check and a stream
XADD on the same request. Lazily construct one client per process,
share it between handlers. ``decode_responses=True`` matches the
convention set in ``services.task_queue``.

Closed on FastAPI shutdown from ``api.lifecycle``.
"""
from __future__ import annotations

import redis.asyncio as aioredis

from utils.config import settings

_client: aioredis.Redis | None = None


def get_redis() -> aioredis.Redis:
    """Return the process-wide Redis client, creating it lazily."""
    global _client
    if _client is None:
        _client = aioredis.from_url(settings.redis_url, decode_responses=True)
    return _client


async def close_redis() -> None:
    """Called from the FastAPI shutdown hook."""
    global _client
    if _client is not None:
        try:
            await _client.aclose()
        finally:
            _client = None
