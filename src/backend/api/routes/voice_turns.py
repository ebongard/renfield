"""
Voice turn stats API (#1613) -- admin-only aggregates over ``voice_turn_traces``.

Routes (prefix ``/api/admin`` added by main.py):

  GET /voice-turns/stats?days=14 -- counts by source x reason, resumed rate,
                                    recording-length p50/p90, counts per room

The rows are content-free, but they describe the household's voice use per
room, so the route requires ``Permission.ADMIN``. With
``VOICE_TURN_TRACES_ENABLED`` off it still answers (``enabled: false``), like
the routing-trace routes, so a reader can tell "off" from "no turns".
"""
from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from models.database import User
from models.permissions import Permission
from services.api_rate_limiter import limiter
from services.auth_service import require_permission
from services.database import get_db
from services.voice_turn_trace_service import turn_stats
from utils.config import settings

router = APIRouter()


class VoiceTurnStatsResponse(BaseModel):
    days: int
    total: int
    by_source_reason: dict[str, dict[str, int]]
    resumed: int
    resumed_rate: float | None
    audio_seconds_p50: float | None
    audio_seconds_p90: float | None
    by_room: dict[str, int]
    resumed_rate_by_source: dict[str, float]
    enabled: bool
    retention_days: int


@router.get("/voice-turns/stats", response_model=VoiceTurnStatsResponse)
@limiter.limit(settings.api_rate_limit_admin)
async def get_voice_turn_stats(
    request: Request,
    days: int = Query(default=14, ge=1, le=365),
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_permission(Permission.ADMIN)),
):
    stats = await turn_stats(db, days)
    return VoiceTurnStatsResponse(
        **stats,
        enabled=settings.voice_turn_traces_enabled,
        retention_days=settings.voice_turn_traces_retention_days,
    )
