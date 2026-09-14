"""Dependency checks for the ``/health/ready`` readiness probe.

Two decisions this module exists to enforce (MCP self-detection Phase 3, review
2026-09-14):

1. **Readiness reflects DB REACHABILITY, not pool saturation.** The probe used the
   app pool (``AsyncSessionLocal``). When a burst exhausts that pool — the
   2026-07-01 watch-folder backlog — every replica's ``SELECT 1`` waits for a pooled
   connection, times out, and ALL replicas leave the Service at once although the
   DB is perfectly healthy: slow turns into 503. The check therefore runs on its
   OWN short-lived connection (a lazily created ``NullPool`` engine: one connect
   per probe, nothing kept open), with connect and statement timeouts bounded.
2. **Optional dependencies are bounded and never fail readiness.** A Redis that
   accepts TCP but never answers, or a hook that hangs, used to hold the probe past
   the kubelet's timeout — which fails readiness on every replica simultaneously,
   the exact opposite of "Redis never causes 503".
"""
from __future__ import annotations

import asyncio
from typing import Any

from loguru import logger

from utils.config import settings

_engine = None
_redis_client = None


def _engine_url() -> str:
    return (settings.database_url or "").replace("postgresql://", "postgresql+asyncpg://")


def _get_engine():
    """The probe's own engine — created once, lazily; NullPool, so it never holds
    a connection between probes and never competes with the app pool."""
    global _engine
    if _engine is None:
        from sqlalchemy.ext.asyncio import create_async_engine
        from sqlalchemy.pool import NullPool

        url = _engine_url()
        connect_args: dict[str, Any] = {}
        if url.startswith("postgresql+asyncpg"):
            bound = settings.health_ready_db_timeout_seconds
            connect_args = {
                "timeout": bound,            # TCP connect + auth
                "command_timeout": bound,    # client-side bound per statement
                "server_settings": {
                    "statement_timeout": str(int(bound * 1000)),
                    "application_name": "renfield-readiness",
                },
            }
        _engine = create_async_engine(url, poolclass=NullPool, connect_args=connect_args)
    return _engine


async def check_database() -> None:
    """Raise unless the DB answers ``SELECT 1`` on a fresh connection within the bound."""
    from sqlalchemy import text

    async with asyncio.timeout(settings.health_ready_db_timeout_seconds):
        async with _get_engine().connect() as conn:
            await conn.execute(text("SELECT 1"))


def _get_redis_client():
    global _redis_client
    if _redis_client is None:
        import redis.asyncio as aioredis

        bound = settings.health_ready_aux_timeout_seconds
        _redis_client = aioredis.from_url(
            settings.redis_url, socket_connect_timeout=bound, socket_timeout=bound
        )
    return _redis_client


async def check_redis() -> dict:
    """Optional: a failing or hanging Redis reports degraded, never raises."""
    try:
        async with asyncio.timeout(settings.health_ready_aux_timeout_seconds):
            await _get_redis_client().ping()
        return {"status": "healthy"}
    except Exception as e:  # noqa: BLE001 — incl. TimeoutError
        logger.warning(f"Health check: redis degraded: {type(e).__name__}: {e}")
        return {"status": "degraded", "error": "connection failed"}


async def device_summary() -> dict:
    """Optional: the ha_glue device-summary hook, bounded. Unknown on any problem."""
    try:
        from utils.hooks import run_hooks

        async with asyncio.timeout(settings.health_ready_aux_timeout_seconds):
            results = await run_hooks("get_connected_device_summary")
        for result in results:
            if isinstance(result, dict):
                return {"status": "healthy", **result}
    except Exception as e:  # noqa: BLE001 — incl. TimeoutError
        logger.debug(f"Health check: device summary unavailable: {type(e).__name__}")
    return {"status": "unknown"}


async def dispose() -> None:
    """Shutdown: dispose the probe engine and close its Redis client."""
    global _engine, _redis_client
    if _engine is not None:
        try:
            await _engine.dispose()
        except Exception as e:  # noqa: BLE001
            logger.debug(f"health engine dispose failed: {e}")
        _engine = None
    if _redis_client is not None:
        try:
            await _redis_client.aclose()
        except Exception as e:  # noqa: BLE001
            logger.debug(f"health redis close failed: {e}")
        _redis_client = None
