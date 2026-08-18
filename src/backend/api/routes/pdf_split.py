"""PDF-split review routes (PR2 of docs/design/pdf-split.md).

Owner review queue for uncertain multi-document PDF splits, surfaced on
/brain/review. All routes 404 when the feature flag is off, and are scoped to
the caller's OWN proposals (single-user mode — AUTH_ENABLED=false — sees
everything, consistent with the KG merge-proposal queue).

Approve/reject only persist state + re-enqueue the parent on the document
queue; the split itself always executes in the WORKER via the stored plan.
"""
from __future__ import annotations

import asyncio
import io

from fastapi import APIRouter, Depends, HTTPException, Response
from loguru import logger
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.database import (
    PDF_SPLIT_PROPOSAL_PENDING,
    Document,
    PdfSplitProposal,
    User,
)
from services.auth_service import get_optional_user
from services.database import get_db
from services.pdf_split_proposals import (
    ProposalRangeError,
    ProposalStateError,
    approve_proposal,
    reject_proposal,
)
from utils.config import settings

router = APIRouter()


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

class ProposalPiece(BaseModel):
    start_page: int
    end_page: int
    title: str = ""
    doc_type: str = ""
    confidence: float = 0.0


class PageSignalOut(BaseModel):
    page: int
    snippet: str = ""
    quality_ok: bool = True
    via_vlm: bool = False


class ProposalResponse(BaseModel):
    id: int
    document_id: int
    document_filename: str
    status: str
    page_count: int
    overall_confidence: float
    created_at: str
    documents: list[ProposalPiece]


class ProposalDetailResponse(ProposalResponse):
    page_signals: list[PageSignalOut]


class ProposalListResponse(BaseModel):
    proposals: list[ProposalResponse]
    total: int


class ApproveRequest(BaseModel):
    # None → approve the stored plan as-is; else owner-edited ranges.
    documents: list[ProposalPiece] | None = Field(default=None)


# ---------------------------------------------------------------------------
# Guards / helpers
# ---------------------------------------------------------------------------

def _require_enabled() -> None:
    if not settings.pdf_split_enabled:
        raise HTTPException(status_code=404, detail="Not found")


def _require_user(user: User | None) -> None:
    if settings.auth_enabled and user is None:
        raise HTTPException(status_code=401, detail="Authentication required")


async def _owned_proposal(
    db: AsyncSession, proposal_id: int, user: User | None
) -> PdfSplitProposal:
    """Ownership-gated fetch — a foreign proposal 404s (not 403: don't leak
    existence). Single-user mode sees everything."""
    row = await db.get(PdfSplitProposal, proposal_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Vorschlag nicht gefunden")
    if settings.auth_enabled and user is not None and row.user_id != user.id:
        raise HTTPException(status_code=404, detail="Vorschlag nicht gefunden")
    return row


def _to_response(row: PdfSplitProposal, filename: str) -> dict:
    return {
        "id": row.id,
        "document_id": row.document_id,
        "document_filename": filename,
        "status": row.status,
        "page_count": row.page_count,
        "overall_confidence": row.overall_confidence,
        "created_at": row.created_at.isoformat() if row.created_at else "",
        "documents": [
            p for p in (row.proposal or []) if isinstance(p, dict)
        ],
    }


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@router.get("/proposals", response_model=ProposalListResponse)
async def list_proposals(
    db: AsyncSession = Depends(get_db),
    user: User | None = Depends(get_optional_user),
):
    """The caller's PENDING split proposals, newest first."""
    _require_enabled()
    _require_user(user)
    q = (
        select(PdfSplitProposal, Document.filename)
        .join(Document, Document.id == PdfSplitProposal.document_id)
        .where(PdfSplitProposal.status == PDF_SPLIT_PROPOSAL_PENDING)
        .order_by(PdfSplitProposal.created_at.desc())
    )
    if settings.auth_enabled and user is not None:
        q = q.where(PdfSplitProposal.user_id == user.id)
    rows = (await db.execute(q)).all()
    proposals = [_to_response(row, filename) for row, filename in rows]
    return ProposalListResponse(proposals=proposals, total=len(proposals))


@router.get("/proposals/{proposal_id}", response_model=ProposalDetailResponse)
async def get_proposal(
    proposal_id: int,
    db: AsyncSession = Depends(get_db),
    user: User | None = Depends(get_optional_user),
):
    """Proposal detail incl. the per-page evidence snippets for the review UI."""
    _require_enabled()
    _require_user(user)
    row = await _owned_proposal(db, proposal_id, user)
    doc = await db.get(Document, row.document_id)
    out = _to_response(row, doc.filename if doc else "")
    out["page_signals"] = [
        s for s in (row.page_signals or []) if isinstance(s, dict)
    ]
    return ProposalDetailResponse(**out)


def _render_page_png(file_path: str, page_number: int) -> bytes | None:
    """Render ONE page of the parent PDF to PNG (blocking — executor)."""
    try:
        import pypdfium2 as pdfium

        pdf = pdfium.PdfDocument(file_path)
        try:
            if page_number < 1 or page_number > len(pdf):
                return None
            pil = pdf[page_number - 1].render(scale=1.5).to_pil()
            buf = io.BytesIO()
            pil.save(buf, format="PNG")
            return buf.getvalue()
        finally:
            pdf.close()
    except Exception as e:  # noqa: BLE001
        logger.warning(f"pdf-split: page render failed for {file_path}: {e}")
        return None


@router.get("/proposals/{proposal_id}/pages/{page_number}")
async def get_proposal_page(
    proposal_id: int,
    page_number: int,
    db: AsyncSession = Depends(get_db),
    user: User | None = Depends(get_optional_user),
):
    """On-demand page thumbnail from the parent's archived bytes — makes the
    boundary decision visually decidable. Ownership-gated like the proposal."""
    _require_enabled()
    _require_user(user)
    row = await _owned_proposal(db, proposal_id, user)
    doc = await db.get(Document, row.document_id)
    if doc is None:
        raise HTTPException(status_code=404, detail="Dokument nicht gefunden")
    loop = asyncio.get_running_loop()
    png = await loop.run_in_executor(
        None, _render_page_png, doc.file_path, page_number
    )
    if png is None:
        raise HTTPException(status_code=404, detail="Seite nicht gefunden")
    return Response(
        content=png,
        media_type="image/png",
        headers={"Cache-Control": "private, max-age=3600"},
    )


@router.post("/proposals/{proposal_id}/approve", response_model=ProposalResponse)
async def approve(
    proposal_id: int,
    body: ApproveRequest | None = None,
    db: AsyncSession = Depends(get_db),
    user: User | None = Depends(get_optional_user),
):
    """Approve (optionally with edited ranges): the plan is persisted and the
    parent re-enqueued — the worker executes the split via the stored plan."""
    _require_enabled()
    _require_user(user)
    row = await _owned_proposal(db, proposal_id, user)
    from api.routes.knowledge import _worker_is_alive

    if not await _worker_is_alive():
        raise HTTPException(
            status_code=503,
            detail="Dokument-Worker nicht verfügbar — bitte gleich erneut versuchen.",
        )
    override = (
        [p.model_dump() for p in body.documents]
        if body and body.documents is not None
        else None
    )
    try:
        await approve_proposal(
            db, row,
            documents_override=override,
            resolved_by=user.id if user else None,
        )
    except ProposalRangeError as e:
        raise HTTPException(status_code=422, detail=str(e))
    except ProposalStateError as e:
        raise HTTPException(status_code=409, detail=str(e))
    doc = await db.get(Document, row.document_id)
    return ProposalResponse(**_to_response(row, doc.filename if doc else ""))


@router.post("/proposals/{proposal_id}/reject", response_model=ProposalResponse)
async def reject(
    proposal_id: int,
    db: AsyncSession = Depends(get_db),
    user: User | None = Depends(get_optional_user),
):
    """Treat-as-single: the parent re-enters normal ingest with the skip_split
    loop-breaker."""
    _require_enabled()
    _require_user(user)
    row = await _owned_proposal(db, proposal_id, user)
    from api.routes.knowledge import _worker_is_alive

    if not await _worker_is_alive():
        raise HTTPException(
            status_code=503,
            detail="Dokument-Worker nicht verfügbar — bitte gleich erneut versuchen.",
        )
    try:
        await reject_proposal(db, row, resolved_by=user.id if user else None)
    except ProposalStateError as e:
        raise HTTPException(status_code=409, detail=str(e))
    doc = await db.get(Document, row.document_id)
    return ProposalResponse(**_to_response(row, doc.filename if doc else ""))
