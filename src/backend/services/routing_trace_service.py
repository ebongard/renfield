"""
Routing traces (#1524) -- one row per agent-router decision.

The chat handler fires the ``post_routing`` hook after every classification.
Until #1524 only the Reva plugin consumed it (into its own table), so the admin
page ``/admin/routing`` called two routes the platform never had. This module is
the platform's own consumer: ``record_routing_trace`` is registered on
``post_routing`` when ``ROUTING_TRACES_ENABLED`` is on, and the admin routes in
``api/routes/routing_traces.py`` read through ``list_traces`` / ``trace_stats``.

The row holds the FULL user message (decided 2026-11-10): admin-only reads,
purged after ``ROUTING_TRACES_RETENTION_DAYS`` by the ``routing_trace_cleanup``
built-in task.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from loguru import logger
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from models.database import RoutingTrace
from utils.config import settings

# Mirrors the String(64) columns; a longer role/sub-intent name is cut, not refused.
_NAME_MAX = 64


async def record_routing_trace(
    message: str = "",
    domain: str = "",
    user_id: int | None = None,
    layer: str | None = None,
    confidence: float | None = None,
    sub_intent: str | None = None,
    entity_matches: list[dict] | None = None,
    **_ignored,
) -> None:
    """``post_routing`` hook consumer: persist one decision.

    Re-asserts the flag (H4) so a handler left registered never writes once the
    switch is off. Best-effort: a failed write is logged, never raised into the
    turn (``run_hooks`` would swallow it anyway, but quietly).
    """
    if not settings.routing_traces_enabled or not message or not domain:
        return
    try:
        from services.database import AsyncSessionLocal

        async with AsyncSessionLocal() as db:
            db.add(RoutingTrace(
                user_id=user_id,
                message=message,
                domain=domain[:_NAME_MAX],
                sub_intent=sub_intent[:_NAME_MAX] if sub_intent else None,
                # The router stamps every exit; an unstamped role came from a
                # path that bypassed it.
                layer=layer or "unknown",
                confidence=float(confidence) if confidence is not None else None,
                entity_matches=entity_matches or None,
            ))
            await db.commit()
    except Exception as e:
        logger.warning(f"Routing trace not recorded: {e}")


async def list_traces(
    db: AsyncSession, domain: str | None = None, limit: int = 100,
) -> list[RoutingTrace]:
    """The newest decisions first, optionally for one domain (role)."""
    query = select(RoutingTrace)
    if domain:
        query = query.where(RoutingTrace.domain == domain)
    query = query.order_by(RoutingTrace.created_at.desc(), RoutingTrace.id.desc()).limit(limit)
    return list((await db.execute(query)).scalars().all())


async def trace_stats(db: AsyncSession) -> dict:
    """Counts by domain and by layer over every retained row."""
    by_domain = {
        name: count for name, count in (await db.execute(
            select(RoutingTrace.domain, func.count(RoutingTrace.id))
            .group_by(RoutingTrace.domain)
            .order_by(func.count(RoutingTrace.id).desc())
        )).all()
    }
    by_layer = {
        name: count for name, count in (await db.execute(
            select(RoutingTrace.layer, func.count(RoutingTrace.id))
            .group_by(RoutingTrace.layer)
            .order_by(func.count(RoutingTrace.id).desc())
        )).all()
    }
    return {"total": sum(by_domain.values()), "by_domain": by_domain, "by_layer": by_layer}


async def purge_expired(db: AsyncSession) -> int:
    """Delete rows older than the retention window; returns how many."""
    cutoff = datetime.now(UTC).replace(tzinfo=None) - timedelta(
        days=settings.routing_traces_retention_days,
    )
    result = await db.execute(delete(RoutingTrace).where(RoutingTrace.created_at < cutoff))
    await db.commit()
    return result.rowcount or 0
