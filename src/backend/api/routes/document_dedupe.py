"""KB near-duplicate document review routes (#1170).

Owner surfaces the near-duplicate DOCUMENT pairs the detector proposed: list the
pending pairs, trigger a fresh scan, and (Phase 2) approve — choosing per-pair
SUPERSEDE (recoverable) vs DELETE for the loser — or reject.

All routes are owner-scoped and gated on ``document_dedupe_enabled`` (404 when
off — dark by default). See services/document_dedupe_service.py.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.database import (
    DOC_DUP_PROPOSAL_PENDING,
    DOC_DUP_RESOLUTION_DELETE,
    DOC_DUP_RESOLUTION_SUPERSEDE,
    Document,
    DocumentDuplicateProposal,
)
from services.auth_service import get_optional_user
from services.database import get_db
from services.document_dedupe_service import DocumentDedupeService
from utils.config import settings

router = APIRouter()


def _require_enabled() -> None:
    if not settings.document_dedupe_enabled:
        raise HTTPException(status_code=404, detail="Document dedupe not enabled")


def _require_user(user) -> None:
    if settings.auth_enabled and user is None:
        raise HTTPException(status_code=401, detail="Authentication required")


def _uid(user) -> int | None:
    return getattr(user, "id", None) if user is not None else None


def _is_admin(user) -> bool:
    if user is None:
        return False
    try:
        from models.permissions import Permission, has_permission

        return has_permission(user.get_permissions(), Permission.ADMIN)
    except Exception:
        return False


async def _owned_proposal(db: AsyncSession, proposal_id: int, user) -> DocumentDuplicateProposal:
    """Load a proposal, gating on ownership (uniform 404). Admin sees all; a
    non-admin only their own; auth-off sees all."""
    p = (
        await db.execute(
            select(DocumentDuplicateProposal).where(DocumentDuplicateProposal.id == proposal_id)
        )
    ).scalar_one_or_none()
    if p is None:
        raise HTTPException(status_code=404, detail="Proposal not found")
    if settings.auth_enabled and not _is_admin(user) and p.user_id != _uid(user):
        raise HTTPException(status_code=404, detail="Proposal not found")
    return p


class DupDocBrief(BaseModel):
    id: int
    name: str
    paperless_document_id: int | None = None
    created_at: str | None = None


class DuplicateProposalOut(BaseModel):
    id: int
    signal: str
    shared_key: str | None = None
    similarity: float
    suggested_survivor_id: int | None = None
    document_a: DupDocBrief
    document_b: DupDocBrief


class DuplicateProposalsResponse(BaseModel):
    proposals: list[DuplicateProposalOut]


class RunResponse(BaseModel):
    candidates: int
    newly_proposed: int
    pending_pairs: int


def _display_name(doc: Document | None) -> str:
    if doc is None:
        return "?"
    return doc.generated_title or doc.title or doc.filename or f"Dokument {doc.id}"


async def _brief(db: AsyncSession, doc_id: int) -> DupDocBrief:
    doc = (await db.execute(select(Document).where(Document.id == doc_id))).scalar_one_or_none()
    return DupDocBrief(
        id=doc_id,
        name=_display_name(doc),
        paperless_document_id=(doc.paperless_document_id if doc is not None else None),
        created_at=(doc.created_at.isoformat() if doc is not None and doc.created_at else None),
    )


async def _pending_for_user(db: AsyncSession, user_id: int | None) -> list[DuplicateProposalOut]:
    q = select(DocumentDuplicateProposal).where(
        DocumentDuplicateProposal.status == DOC_DUP_PROPOSAL_PENDING
    )
    if settings.auth_enabled:
        q = q.where(DocumentDuplicateProposal.user_id == user_id)
    q = q.order_by(DocumentDuplicateProposal.id.desc())
    proposals = (await db.execute(q)).scalars().all()
    out: list[DuplicateProposalOut] = []
    for p in proposals:
        out.append(
            DuplicateProposalOut(
                id=p.id,
                signal=p.signal,
                shared_key=p.shared_key,
                similarity=p.similarity,
                suggested_survivor_id=p.suggested_survivor_id,
                document_a=await _brief(db, p.document_a_id),
                document_b=await _brief(db, p.document_b_id),
            )
        )
    return out


@router.get("/document-duplicates", response_model=DuplicateProposalsResponse)
async def list_document_duplicates(
    db: AsyncSession = Depends(get_db),
    user=Depends(get_optional_user),
) -> DuplicateProposalsResponse:
    _require_enabled()
    _require_user(user)
    return DuplicateProposalsResponse(proposals=await _pending_for_user(db, _uid(user)))


@router.post("/document-duplicates/run", response_model=RunResponse)
async def run_document_dedupe(
    db: AsyncSession = Depends(get_db),
    user=Depends(get_optional_user),
) -> RunResponse:
    _require_enabled()
    _require_user(user)
    uid = _uid(user)
    report = await DocumentDedupeService(db).run_for_user(uid)
    pending = await _pending_for_user(db, uid)
    return RunResponse(
        candidates=report.candidates,
        newly_proposed=report.proposed,
        pending_pairs=len(pending),
    )


class ResolveRequest(BaseModel):
    # 'supersede' (recoverable: loser hidden from retrieval) or 'delete' (loser
    # removed via the standard document-delete path).
    resolution: str = DOC_DUP_RESOLUTION_SUPERSEDE
    # Optional survivor override (must be one of the pair); else the suggestion.
    survivor_id: int | None = None


class ResolveResponse(BaseModel):
    status: str
    resolution: str | None = None
    survivor_id: int | None = None
    loser_id: int | None = None


@router.post("/document-duplicates/{proposal_id}/approve", response_model=ResolveResponse)
async def approve_document_duplicate(
    proposal_id: int,
    body: ResolveRequest,
    db: AsyncSession = Depends(get_db),
    user=Depends(get_optional_user),
) -> ResolveResponse:
    _require_enabled()
    _require_user(user)
    if body.resolution not in (DOC_DUP_RESOLUTION_SUPERSEDE, DOC_DUP_RESOLUTION_DELETE):
        raise HTTPException(status_code=422, detail="resolution must be 'supersede' or 'delete'")
    p = await _owned_proposal(db, proposal_id, user)
    if p.status != DOC_DUP_PROPOSAL_PENDING:
        raise HTTPException(status_code=409, detail="Proposal already resolved")
    res = await DocumentDedupeService(db).resolve_proposal(
        p, user_id=_uid(user), resolution=body.resolution, survivor_id=body.survivor_id
    )
    return ResolveResponse(
        status=res.get("status", "invalid"),
        resolution=res.get("resolution"),
        survivor_id=res.get("survivor_id"),
        loser_id=res.get("loser_id"),
    )


@router.post("/document-duplicates/{proposal_id}/reject", response_model=ResolveResponse)
async def reject_document_duplicate(
    proposal_id: int,
    db: AsyncSession = Depends(get_db),
    user=Depends(get_optional_user),
) -> ResolveResponse:
    _require_enabled()
    _require_user(user)
    p = await _owned_proposal(db, proposal_id, user)
    if p.status != DOC_DUP_PROPOSAL_PENDING:
        raise HTTPException(status_code=409, detail="Proposal already resolved")
    res = await DocumentDedupeService(db).reject_proposal(p, user_id=_uid(user))
    return ResolveResponse(status=res.get("status", "invalid"))
