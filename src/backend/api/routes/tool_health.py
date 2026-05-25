"""
Tool Health API — admin-only stats for the Phase-3 outcome tracker.

The /api/tool-health prefix surfaces the per-(user, tool) success/failure
counters maintained by ``services.tool_outcome_service``. Routes are
admin-only because the data exposes cross-user usage patterns that
shouldn't bleed to other deployed users.

Routes (prefix added by main.py):
  GET  /                      — listing of recent (user, tool) stats
  GET  /warnings/{user_id}    — preview the warning block a given user
                                would see at prompt-build time
"""

from datetime import datetime

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from models.database import User
from models.permissions import Permission
from services.api_rate_limiter import limiter
from services.auth_service import require_permission
from services.database import get_db
from services.tool_outcome_service import ToolOutcomeService
from utils.config import settings

router = APIRouter()


class ToolOutcomeStatResponse(BaseModel):
    user_id: int | None
    tool_name: str
    success_count: int
    failure_count: int
    success_rate: float
    last_used_at: datetime | None
    last_failure_at: datetime | None
    last_failure_summary: str | None


class WarningResponse(BaseModel):
    tool_name: str
    success_count: int
    failure_count: int
    total: int
    success_rate: float
    last_failure_at: datetime | None
    last_failure_summary: str | None


@router.get("", response_model=list[ToolOutcomeStatResponse])
@limiter.limit(settings.api_rate_limit_admin)
async def list_tool_stats(
    request: Request,
    user_id: int | None = None,
    limit: int = Query(default=200, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_permission(Permission.ADMIN)),
):
    svc = ToolOutcomeService(db)
    rows = await svc.list_stats(user_id=user_id, limit=limit)
    out: list[ToolOutcomeStatResponse] = []
    for r in rows:
        total = r.success_count + r.failure_count
        rate = r.success_count / total if total else 1.0
        out.append(ToolOutcomeStatResponse(
            user_id=r.user_id,
            tool_name=r.tool_name,
            success_count=r.success_count,
            failure_count=r.failure_count,
            success_rate=round(rate, 3),
            last_used_at=r.last_used_at,
            last_failure_at=r.last_failure_at,
            last_failure_summary=r.last_failure_summary,
        ))
    return out


@router.get("/warnings/{user_id}", response_model=list[WarningResponse])
@limiter.limit(settings.api_rate_limit_admin)
async def preview_warnings_for_user(
    request: Request,
    user_id: int,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_permission(Permission.ADMIN)),
):
    """What warnings would this user see in their next agent prompt?
    Useful for debugging surprising LLM behavior."""
    svc = ToolOutcomeService(db)
    warnings = await svc.get_health_warnings(user_id=user_id)
    return [WarningResponse(**w) for w in warnings]
