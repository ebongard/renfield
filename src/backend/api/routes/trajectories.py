"""
Trajectories API — admin-only listing + JSONL export.

Self-learning Phase 2 surface. The export endpoint streams the corpus
so a 50k-row dump doesn't buffer in memory. The producer
(``_post_turn_skill_bookkeeping`` in agent_service.py) writes rows
asynchronously; the cleanup scheduler in lifecycle.py purges expired
non-flagged rows on a slow cadence.

Routes (prefix `/api/trajectories` added by main.py):

  GET  /                     — paged listing (admin only)
  GET  /export.jsonl         — line-delimited JSON stream (admin only)
  POST /{id}/flag            — flag/unflag a single row for retention
  GET  /stats                — aggregate counts by outcome + last 7d
"""

from datetime import datetime, timedelta, UTC
import json
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from loguru import logger
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from models.database import AgentTrajectory, User
from models.permissions import Permission
from services.auth_service import require_permission
from services.database import get_db
from services.trajectory_service import TrajectoryService
from utils.config import settings

router = APIRouter()


# --------------------------------------------------------------- schemas
class TrajectoryResponse(BaseModel):
    id: int
    user_id: int | None
    conversation_id: int | None
    outcome: str
    tool_count: int
    distinct_tool_count: int
    token_count: int | None
    extracted_skill_id: int | None
    used_skill_ids: list[int]
    flagged_for_retention: bool
    created_at: datetime


class FlagRequest(BaseModel):
    flagged: bool = Field(...)


class StatsResponse(BaseModel):
    total: int
    by_outcome: dict[str, int]
    last_7d: int
    flagged_total: int
    capture_enabled: bool
    retention_days: int


def _to_summary(row: AgentTrajectory) -> TrajectoryResponse:
    return TrajectoryResponse(
        id=row.id,
        user_id=row.user_id,
        conversation_id=row.conversation_id,
        outcome=row.outcome,
        tool_count=row.tool_count,
        distinct_tool_count=row.distinct_tool_count,
        token_count=row.token_count,
        extracted_skill_id=row.extracted_skill_id,
        used_skill_ids=row.used_skill_ids or [],
        flagged_for_retention=row.flagged_for_retention,
        created_at=row.created_at,
    )


# ----------------------------------------------------------------- list
@router.get("", response_model=list[TrajectoryResponse])
async def list_trajectories(
    user_id: int | None = None,
    outcome: str | None = None,
    flagged_only: bool = False,
    since_days: int | None = Query(default=None, ge=1, le=3650),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_permission(Permission.ADMIN)),
):
    """Paged list. Admin-only — trajectories carry full user message
    payloads and tool traces, so this is not a per-user surface."""
    since = None
    if since_days is not None:
        since = datetime.now(UTC).replace(tzinfo=None) - timedelta(days=since_days)

    svc = TrajectoryService(db)
    rows = await svc.list_for_admin(
        user_id=user_id,
        outcome=outcome,
        flagged_only=flagged_only,
        since=since,
        limit=limit,
        offset=offset,
    )
    return [_to_summary(r) for r in rows]


# --------------------------------------------------------------- export
@router.get("/export.jsonl")
async def export_jsonl(
    outcome: str | None = None,
    since_days: int | None = Query(default=None, ge=1, le=3650),
    flagged_only: bool = False,
    require_redacted: bool = False,
    _: User = Depends(require_permission(Permission.ADMIN)),
):
    """Stream the corpus as line-delimited JSON. Each line is one turn.

    ``require_redacted=true`` skips rows whose ``redacted_payload`` is
    NULL — gate for downstream consumers that won't accept raw payloads
    (Phase-4-ready; v1 always has NULL redacted so this returns nothing
    today, which is intentional).

    No ``Depends(get_db)``: the request-scoped session would be closed
    by FastAPI as soon as this handler returns the StreamingResponse
    object, but the generator inside continues to run while the body
    streams. The pre-fix pattern produced 'session is closed' errors
    on long exports — and worse, the closed session could be checked
    back out by another request and leak rows across the pool. We open
    a dedicated session INSIDE the generator and expunge between
    batches so the identity map stays bounded for a 50k-row dump.
    """
    if not settings.trajectory_capture_enabled:
        raise HTTPException(
            status_code=409,
            detail="trajectory_capture_enabled=false — nothing to export",
        )

    since = None
    if since_days is not None:
        since = datetime.now(UTC).replace(tzinfo=None) - timedelta(days=since_days)

    async def _gen():
        from services.database import AsyncSessionLocal
        async with AsyncSessionLocal() as stream_db:
            svc = TrajectoryService(stream_db)
            async for obj in svc.export_jsonl(
                outcome=outcome,
                since=since,
                flagged_only=flagged_only,
                require_redacted=require_redacted,
            ):
                yield json.dumps(obj, ensure_ascii=False) + "\n"

    return StreamingResponse(
        _gen(),
        media_type="application/jsonl",
        headers={"Content-Disposition": "attachment; filename=trajectories.jsonl"},
    )


# ------------------------------------------------------------------ flag
@router.post("/{trajectory_id}/flag", response_model=TrajectoryResponse)
async def flag_trajectory(
    trajectory_id: int,
    body: FlagRequest,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_permission(Permission.ADMIN)),
):
    """Pin/unpin a trajectory so the cleanup scheduler skips it."""
    row = (await db.execute(
        select(AgentTrajectory).where(AgentTrajectory.id == trajectory_id)
    )).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="Trajectory not found")
    row.flagged_for_retention = bool(body.flagged)
    await db.commit()
    return _to_summary(row)


# ----------------------------------------------------------------- stats
@router.get("/stats", response_model=StatsResponse)
async def trajectory_stats(
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_permission(Permission.ADMIN)),
):
    """Quick dashboard: total / by-outcome / last-week / flagged counts."""
    total = (await db.execute(
        select(func.count(AgentTrajectory.id))
    )).scalar() or 0

    by_outcome_rows = (await db.execute(
        select(AgentTrajectory.outcome, func.count(AgentTrajectory.id))
        .group_by(AgentTrajectory.outcome)
    )).all()
    by_outcome: dict[str, int] = {row[0]: row[1] for row in by_outcome_rows}

    week_ago = datetime.now(UTC).replace(tzinfo=None) - timedelta(days=7)
    last_7d = (await db.execute(
        select(func.count(AgentTrajectory.id))
        .where(AgentTrajectory.created_at >= week_ago)
    )).scalar() or 0

    flagged = (await db.execute(
        select(func.count(AgentTrajectory.id))
        .where(AgentTrajectory.flagged_for_retention.is_(True))
    )).scalar() or 0

    return StatsResponse(
        total=total,
        by_outcome=by_outcome,
        last_7d=last_7d,
        flagged_total=flagged,
        capture_enabled=settings.trajectory_capture_enabled,
        retention_days=settings.trajectory_retention_days,
    )
