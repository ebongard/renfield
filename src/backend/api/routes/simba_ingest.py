"""
Simba-ingest review routes (xidra).

Owner reviews watch-folder PDFs the classifier proposed for the Simba tax portal:
list pending proposals, confirm (→ real, irreversible upload) with a possibly
edited category/type, or reject. See services/simba_ingest_review.py.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from services import simba_ingest_review as review
from services.auth_service import get_optional_user
from services.database import get_db
from utils.config import settings

router = APIRouter()


def _require_user(user) -> None:
    """401 an unauthenticated caller when auth is on (the review actions can
    trigger an irreversible upload — never reachable anonymously)."""
    if settings.auth_enabled and user is None:
        raise HTTPException(status_code=401, detail="Authentication required")


class SimbaProposalOut(BaseModel):
    id: int
    document_id: int
    filename: str
    suggested_category: str | None = None
    suggested_type: str | None = None


class SimbaProposalsResponse(BaseModel):
    proposals: list[SimbaProposalOut]


class SimbaConfirmRequest(BaseModel):
    category: str
    type: str


class SimbaActionResponse(BaseModel):
    success: bool
    message: str = ""


@router.get("/simba-ingest", response_model=SimbaProposalsResponse)
async def list_proposals(
    db: AsyncSession = Depends(get_db), user=Depends(get_optional_user)
):
    _require_user(user)
    rows = await review.list_pending(db, user)
    return SimbaProposalsResponse(
        proposals=[
            SimbaProposalOut(
                id=p.id,
                document_id=p.document_id,
                filename=p.filename,
                suggested_category=p.suggested_category,
                suggested_type=p.suggested_type,
            )
            for p in rows
        ]
    )


@router.post("/simba-ingest/{proposal_id}/confirm", response_model=SimbaActionResponse)
async def confirm_proposal(
    proposal_id: int,
    body: SimbaConfirmRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user=Depends(get_optional_user),
):
    _require_user(user)
    mgr = getattr(request.app.state, "mcp_manager", None)
    res = await review.confirm(db, proposal_id, body.category, body.type, user, mgr)
    if not res["success"]:
        msg = res["message"]
        if msg == "not_found":
            raise HTTPException(status_code=404, detail="Proposal not found")
        if msg == "already_resolved":
            raise HTTPException(status_code=409, detail="Already resolved")
        raise HTTPException(status_code=502, detail=msg)
    return SimbaActionResponse(success=True, message=res["message"])


@router.post("/simba-ingest/{proposal_id}/reject", response_model=SimbaActionResponse)
async def reject_proposal(
    proposal_id: int,
    db: AsyncSession = Depends(get_db),
    user=Depends(get_optional_user),
):
    _require_user(user)
    ok = await review.reject(db, proposal_id, user)
    if not ok:
        raise HTTPException(status_code=404, detail="Proposal not found or already resolved")
    return SimbaActionResponse(success=True)
