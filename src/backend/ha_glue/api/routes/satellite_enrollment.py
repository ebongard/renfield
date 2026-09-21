"""Satellite enrollment admin API (security review H1).

Mints / lists / revokes the per-satellite enrollment PSK that the satellite
presents on its register frame. The plaintext token is returned exactly once
(at enroll/rotate) and never again — mirrors the folder/email ingest tokens and
the IRK store (`/api/presence/irks`). All endpoints are ADMIN-gated.

Mounted at its own prefix (`/api/satellite-enrollment`) rather than on the
existing `/api/satellites` monitoring router, whose `GET /{satellite_id}`
wildcard would otherwise shadow these GET endpoints.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from loguru import logger
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from models.database import Satellite, User
from models.permissions import Permission
from services.auth_service import require_permission
from services.database import get_db
from ha_glue.services import satellite_enrollment_service as enroll_svc
from ha_glue.services.satellite_manager import get_satellite_manager
from utils.config import settings

router = APIRouter(prefix="/api/satellite-enrollment")


class SatelliteResponse(BaseModel):
    id: int
    satellite_id: str
    room: str | None = None
    is_enabled: bool
    enrolled_at: str | None = None
    last_authenticated_at: str | None = None
    revoked_at: str | None = None
    connected: bool = False


class SatelliteEnrollRequest(BaseModel):
    satellite_id: str = Field(..., min_length=1, max_length=100, pattern=r"^[a-zA-Z0-9_.-]+$")
    room: str | None = Field(default=None, max_length=100)
    # Re-issue a fresh PSK for an already-enrolled satellite (rotation / reimage).
    rotate: bool = False


class SatelliteEnrollResponse(BaseModel):
    satellite_id: str
    # The plaintext PSK — shown ONCE. Provision it to the satellite, then it is
    # unrecoverable (only the bcrypt hash is stored).
    token: str
    rotated: bool = False


class EnrollmentStatusResponse(BaseModel):
    enabled: bool
    autoflip_enabled: bool
    enforcing: bool
    total_enrolled: int
    pending_first_auth: int


def _to_response(row: Satellite, connected: bool) -> SatelliteResponse:
    return SatelliteResponse(
        id=row.id,
        satellite_id=row.satellite_id,
        room=row.room,
        is_enabled=row.is_enabled,
        enrolled_at=row.enrolled_at.isoformat() if row.enrolled_at else None,
        last_authenticated_at=row.last_authenticated_at.isoformat() if row.last_authenticated_at else None,
        revoked_at=row.revoked_at.isoformat() if row.revoked_at else None,
        connected=connected,
    )


@router.get("", response_model=list[SatelliteResponse])
async def list_satellites(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_permission(Permission.ADMIN)),
):
    """List enrolled satellites + their status (the token is never returned)."""
    rows = (await db.execute(select(Satellite).order_by(Satellite.satellite_id))).scalars().all()
    manager = get_satellite_manager()
    return [_to_response(r, manager.is_connected(r.satellite_id)) for r in rows]


@router.get("/status", response_model=EnrollmentStatusResponse)
async def enrollment_status(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_permission(Permission.ADMIN)),
):
    """Fleet readiness for the rollout gate: how many enrolled satellites still
    need a first authentication before auto-flip can enforce."""
    base = select(func.count()).select_from(Satellite).where(
        Satellite.is_enabled.is_(True), Satellite.revoked_at.is_(None)
    )
    total = (await db.execute(base)).scalar_one()
    pending = (
        await db.execute(base.where(Satellite.last_authenticated_at.is_(None)))
    ).scalar_one()
    return EnrollmentStatusResponse(
        enabled=enroll_svc.enrollment_enabled(),
        autoflip_enabled=bool(settings.satellite_enrollment_autoflip_enabled),
        enforcing=await enroll_svc.is_enforcing(db),
        total_enrolled=int(total),
        pending_first_auth=int(pending),
    )


@router.post("/enroll", response_model=SatelliteEnrollResponse, status_code=201)
async def enroll_satellite(
    body: SatelliteEnrollRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_permission(Permission.ADMIN)),
):
    """Enroll (or rotate) a satellite and return its PSK exactly once."""
    already = (
        await db.execute(select(Satellite).where(Satellite.satellite_id == body.satellite_id))
    ).scalar_one_or_none()
    was_active = bool(already and already.is_enabled and already.revoked_at is None)

    token = await enroll_svc.enroll_satellite(
        db,
        body.satellite_id,
        enrolled_by_user_id=getattr(current_user, "id", None),
        room=body.room,
        rotate=body.rotate,
    )
    if token is None:
        raise HTTPException(
            status_code=409,
            detail="Satellite already enrolled; pass rotate=true to re-issue its token.",
        )
    return SatelliteEnrollResponse(satellite_id=body.satellite_id, token=token, rotated=was_active)


@router.post("/{satellite_id}/unlock", status_code=200)
async def unlock_satellite_handshake(
    satellite_id: str,
    current_user: User = Depends(require_permission(Permission.ADMIN)),
):
    """Clear the handshake lockout of one satellite (`SATELLITE_PSK_HANDSHAKE_ENABLED`).

    A satellite whose provisioned PSK is wrong locks its `sat:<id>` scope after
    the login-lockout threshold; that scope is not a user, so the users
    admin page can neither list nor unlock it. The operator fixes the
    provisioning, then clears the lock here instead of waiting for the TTL.
    Logged WARNING with the actor, like the user unlock.
    """
    from services.login_lockout import LockoutStoreUnavailable, login_lockout

    try:
        cleared = await login_lockout.unlock(f"sat:{satellite_id}")
    except LockoutStoreUnavailable:
        raise HTTPException(status_code=503, detail="lockout store unavailable")
    logger.warning(
        f"satellite handshake lockout cleared for '{satellite_id}' by user "
        f"{getattr(current_user, 'id', None)} ({cleared} key(s))"
    )
    return {"satellite_id": satellite_id, "cleared": cleared}


@router.delete("/{satellite_id}", status_code=204)
async def revoke_satellite(
    satellite_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_permission(Permission.ADMIN)),
):
    """Revoke a satellite's enrollment (it can no longer authenticate)."""
    if not await enroll_svc.revoke_satellite(db, satellite_id):
        raise HTTPException(status_code=404, detail="Satellite not found")
