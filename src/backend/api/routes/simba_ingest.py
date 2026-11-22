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
from services.simba_portal_limits import portal_limits
from utils.config import settings

router = APIRouter()

#: How long the list waits for a cold fetch of the portal's limits (a portal
#: login) before it answers without them; the fetch runs on and fills the cache.
_LIMITS_WAIT_SECONDS = 5.0


def _require_user(user) -> None:
    """401 an unauthenticated caller when auth is on (the review actions can
    trigger an irreversible upload — never reachable anonymously)."""
    if settings.auth_enabled and user is None:
        raise HTTPException(status_code=401, detail="Authentication required")


class SimbaUploadBlocked(BaseModel):
    """A file the portal can never accept -- data for the UI to render in the
    viewer's language, not a sentence."""

    code: str
    size_mb: float
    max_mb: float


def _blocked_out(blocked) -> SimbaUploadBlocked | None:
    if blocked is None:
        return None
    return SimbaUploadBlocked(code=blocked.code, size_mb=blocked.size_mb, max_mb=blocked.max_mb)


class SimbaProposalOut(BaseModel):
    id: int
    document_id: int
    filename: str
    suggested_category: str | None = None
    suggested_type: str | None = None
    suggested_description: str = ""
    # Booking period (Zeitraum) derived from the document's date, so the review
    # form defaults to the invoice month rather than the current one (#1167).
    suggested_month: int | None = None
    suggested_year: int | None = None
    # Why it can never land (over the portal's file size limit, #1609); the
    # form shows it instead of the confirm. None = it fits, or limits unknown.
    upload_blocked: SimbaUploadBlocked | None = None


class SimbaProposalsResponse(BaseModel):
    proposals: list[SimbaProposalOut]


class SimbaConfirmRequest(BaseModel):
    category: str
    type: str
    description: str | None = None
    month: int | None = None
    year: int | None = None
    force: bool = False


class SimbaActionResponse(BaseModel):
    success: bool
    message: str = ""
    proposal_id: int | None = None
    already_in_simba: bool = False
    existing: str | None = None
    # Populated by from-document so the doc-page "send to Simba" overlay can
    # prefill the confirm form (category/type suggestion + Bezeichnung) without a
    # second round-trip.
    suggested_category: str | None = None
    suggested_type: str | None = None
    suggested_description: str = ""
    suggested_month: int | None = None
    suggested_year: int | None = None
    upload_blocked: SimbaUploadBlocked | None = None


def _require_enabled() -> None:
    if not settings.folder_ingest_simba_enabled:
        raise HTTPException(status_code=404, detail="Simba review not enabled")


@router.post("/simba-ingest/from-document/{document_id}", response_model=SimbaActionResponse)
async def send_document_to_simba(
    document_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user=Depends(get_optional_user),
):
    """Queue an EXISTING knowledge-base document for Simba review (the
    'send to Simba' action). Creates a pending proposal on /brain/review;
    the actual irreversible upload still happens via the confirm step."""
    _require_user(user)
    _require_enabled()
    limits = await portal_limits(getattr(request.app.state, "mcp_manager", None))
    res = await review.create_proposal_for_document(db, document_id, user, limits=limits)
    if not res["success"]:
        if res["message"] == "not_found":
            raise HTTPException(status_code=404, detail="Document not found")
        if res["message"] == "too_large":
            raise HTTPException(status_code=422, detail=res["reason"])
        raise HTTPException(status_code=400, detail=res["message"])
    return SimbaActionResponse(
        success=True, message=res["message"], proposal_id=res.get("proposal_id"),
        suggested_category=res.get("suggested_category"),
        suggested_type=res.get("suggested_type"),
        suggested_description=res.get("suggested_description", ""),
        suggested_month=res.get("suggested_month"),
        suggested_year=res.get("suggested_year"),
        upload_blocked=_blocked_out(res.get("upload_blocked")),
    )


@router.get("/simba-ingest", response_model=SimbaProposalsResponse)
async def list_proposals(
    request: Request, db: AsyncSession = Depends(get_db), user=Depends(get_optional_user)
):
    _require_user(user)
    mgr = getattr(request.app.state, "mcp_manager", None)
    rows = await review.list_pending(
        db, user, limits=lambda: portal_limits(mgr, wait=_LIMITS_WAIT_SECONDS),
    )
    return SimbaProposalsResponse(
        proposals=[
            SimbaProposalOut(
                id=p.id,
                document_id=p.document_id,
                filename=p.filename,
                suggested_category=p.suggested_category,
                suggested_type=p.suggested_type,
                suggested_description=desc,
                suggested_month=month,
                suggested_year=year,
                upload_blocked=_blocked_out(blocked),
            )
            for (p, desc, month, year, blocked) in rows
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
    res = await review.confirm(
        db, proposal_id, body.category, body.type, user, mgr,
        description=body.description, month=body.month, year=body.year, force=body.force,
    )
    if not res["success"]:
        msg = res["message"]
        if msg == "not_found":
            raise HTTPException(status_code=404, detail="Proposal not found")
        if msg == "already_resolved":
            raise HTTPException(status_code=409, detail="Already resolved")
        if msg == "too_large":
            # A property of the file, refused before any upload (#1609).
            raise HTTPException(status_code=422, detail=res["reason"])
        if msg == "already_in_simba":
            # Not an error: an informative gate. The document appears to already
            # be in Simba — the UI warns and offers an explicit "force" re-confirm.
            return SimbaActionResponse(
                success=False, message="already_in_simba", already_in_simba=True,
                existing=res.get("existing"),
            )
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
