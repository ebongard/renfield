"""
Routing traces API (#1524) -- admin-only reads for ``/admin/routing``.

Routes (prefix ``/api/admin`` added by main.py):

  GET /routing-traces   -- newest decisions first, optional ``domain`` filter
  GET /routing-stats    -- counts by domain and by layer

Rows hold the full user message, so both routes require ``Permission.ADMIN``.
With ``ROUTING_TRACES_ENABLED`` off they still answer (``enabled: false``) so
the page can say why it is empty instead of looking broken.
"""
from datetime import datetime

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from models.database import RoutingTrace, User
from models.permissions import Permission
from services.api_rate_limiter import limiter
from services.auth_service import require_permission
from services.database import get_db
from services.routing_trace_service import list_traces, trace_stats
from utils.config import settings

router = APIRouter()


class RoutingTraceResponse(BaseModel):
    id: int
    created_at: datetime
    user_id: int | None
    message: str
    domain: str
    sub_intent: str | None
    layer: str
    confidence: float | None
    entity_matches: list[dict]


class RoutingTracesResponse(BaseModel):
    traces: list[RoutingTraceResponse]
    enabled: bool
    retention_days: int


class RoutingStatsResponse(BaseModel):
    total: int
    by_domain: dict[str, int]
    by_layer: dict[str, int]
    enabled: bool
    retention_days: int


def _to_response(row: RoutingTrace) -> RoutingTraceResponse:
    return RoutingTraceResponse(
        id=row.id,
        created_at=row.created_at,
        user_id=row.user_id,
        message=row.message,
        domain=row.domain,
        sub_intent=row.sub_intent,
        layer=row.layer,
        confidence=row.confidence,
        entity_matches=row.entity_matches or [],
    )


@router.get("/routing-traces", response_model=RoutingTracesResponse)
@limiter.limit(settings.api_rate_limit_admin)
async def get_routing_traces(
    request: Request,
    domain: str | None = Query(default=None, max_length=64),
    limit: int = Query(default=100, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_permission(Permission.ADMIN)),
):
    rows = await list_traces(db, domain=domain or None, limit=limit)
    return RoutingTracesResponse(
        traces=[_to_response(r) for r in rows],
        enabled=settings.routing_traces_enabled,
        retention_days=settings.routing_traces_retention_days,
    )


@router.get("/routing-stats", response_model=RoutingStatsResponse)
@limiter.limit(settings.api_rate_limit_admin)
async def get_routing_stats(
    request: Request,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_permission(Permission.ADMIN)),
):
    stats = await trace_stats(db)
    return RoutingStatsResponse(
        **stats,
        enabled=settings.routing_traces_enabled,
        retention_days=settings.routing_traces_retention_days,
    )
