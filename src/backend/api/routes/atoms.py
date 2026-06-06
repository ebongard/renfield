"""
Atoms API — read/edit access to atoms (kb_documents, kg_nodes, kg_edges,
conversation_memories) through the unified circles framework.

All endpoints require authentication. Access checks use CircleResolver:
- GET /api/atoms              query atoms accessible to current user (owner + tier reach + explicit grant)
- GET /api/atoms/{atom_id}    fetch one atom (uniform 404 on not-found AND not-authorized)
- PATCH /api/atoms/{atom_id}/tier  change atom's circle policy (owner-only)
- DELETE /api/atoms/{atom_id} soft-delete the atom (owner-only)

Per the design doc:
- get_atom returns uniform 404 for not-found AND not-authorized (existence
  oracle defense). Owners always see their own atoms.
- update_tier on a kg_node cascades to incident kg_relations (per CEO Finding E)
  inside AtomService.update_tier.
- Brain Review Queue lives at /api/circles/me/atoms-for-review (separate file).
"""
from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Response, status
from loguru import logger
from pydantic import BaseModel, Field
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from models.database import (
    Atom as AtomModel,
    Document,
    DocumentFact,
    KnowledgeBase,
    OBLIGATION_MILESTONE_CONFIRMED,
    ObligationAcknowledgement,
    User,
)
from services.atom_service import AtomService
from services.atom_types import Atom
from services.auth_service import get_user_or_default
from services.circle_resolver import CircleResolver, atom_from_orm
from services.database import get_db
from services.document_fact_retrieval import DocumentFactRetrieval
from services.polymorphic_atom_store import PolymorphicAtomStore
from utils.config import settings

router = APIRouter()


# =============================================================================
# Schemas
# =============================================================================


class AtomResponse(BaseModel):
    """Atom serialization for API responses."""
    atom_id: str
    atom_type: str
    owner_user_id: int
    policy: dict[str, Any]
    tier: int
    created_at: datetime
    updated_at: datetime
    payload: dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def from_atom(cls, atom: Atom) -> "AtomResponse":
        return cls(
            atom_id=atom.atom_id,
            atom_type=atom.atom_type,
            owner_user_id=atom.owner_user_id,
            policy=atom.policy,
            tier=atom.tier,
            created_at=atom.created_at,
            updated_at=atom.updated_at,
            payload=atom.payload or {},
        )


class AtomMatchResponse(BaseModel):
    """Search-result atom + retrieval metadata."""
    atom: AtomResponse
    score: float
    snippet: str
    rank: int


class UpdateTierRequest(BaseModel):
    """PATCH body for changing an atom's circle policy."""
    policy: dict[str, Any] = Field(
        ...,
        description="New policy dict, e.g. {'tier': 2} for ladder dimension or "
                    "{'tier': 1, 'tenant': 'acme'} for multi-dim.",
    )


class DocumentFactResponse(BaseModel):
    """A single Schicht A fact. Extra keys from the retrieval dict (e.g.
    ``similarity``) are ignored by Pydantic's default extra='ignore'."""
    id: int
    document_id: int
    atom_id: str | None = None
    category: str
    kind: str
    value: str
    normalized_value: str | None = None
    excerpt: str | None = None
    obligation_date: str | None = None
    amount_value: float | None = None
    amount_currency: str | None = None
    legal_gate: bool = False
    payment_method: str | None = None
    confidence: float | None = None
    source: str | None = None
    circle_tier: int = 0
    confirmed: bool = False  # the asker's per-user Bestätigt state (server ledger)
    tier_overridden: bool = False  # fact's tier set independently of the parent doc


class ObligationConfirmResponse(BaseModel):
    """Result of confirming / reopening an obligation (the agenda's Bestätigen)."""
    document_fact_id: int
    confirmed: bool


class FactTierResetResponse(BaseModel):
    """Result of resetting a fact's per-fact tier override to the document tier."""
    document_fact_id: int
    circle_tier: int
    tier_overridden: bool


# =============================================================================
# Routes
# =============================================================================


@router.get("", response_model=list[AtomMatchResponse])
async def query_atoms(
    q: str = "",
    top_k: int = 20,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_user_or_default),
):
    """
    Query atoms accessible to the current user.

    For now this returns un-filtered top-k matches across all sources;
    the per-source circle_tier filter is wired up in Lane C alongside the
    legacy-consumer rewrite. Until then `q` is passed through to the
    Lane-A retrieval modules and results come back un-circle-filtered.
    """
    if top_k < 1 or top_k > 100:
        raise HTTPException(status_code=400, detail="top_k must be between 1 and 100")

    store = PolymorphicAtomStore(db)
    # max_visible_tier=4 (public) until per-owner filtering is wired in Lane C.
    matches = await store.query(
        q,
        asker_id=current_user.id,
        max_visible_tier=4,
        top_k=top_k,
    )
    return [
        AtomMatchResponse(
            atom=AtomResponse.from_atom(m.atom),
            score=m.score,
            snippet=m.snippet,
            rank=m.rank,
        )
        for m in matches
    ]


# NOTE: the two routes below MUST be declared before `GET /{atom_id}` — a
# literal path like `/obligations` would otherwise be captured by the
# `/{atom_id}` parameter route (the route-order class of bug from #615).


@router.get("/obligations", response_model=list[DocumentFactResponse])
async def get_obligations(
    due_before: date | None = None,
    limit: int = 50,
    offset: int = 0,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_user_or_default),
):
    """
    List circle-visible obligation facts (bills + Behörde deadlines) with a
    printed Frist, soonest first. Optional ``due_before`` caps the horizon;
    ``offset`` pages further into the stable soonest-first order (the agenda's
    "Mehr laden").

    List endpoint — circle-filter only (returns what the asker can see; no
    404/403, consistent with the list-vs-single-resource convention).
    """
    if limit < 1 or limit > 200:
        raise HTTPException(status_code=400, detail="limit must be between 1 and 200")
    if offset < 0:
        raise HTTPException(status_code=400, detail="offset must be >= 0")
    facts = await DocumentFactRetrieval(db).obligations(
        asker_id=current_user.id, due_before=due_before, limit=limit, offset=offset,
    )
    return [DocumentFactResponse(**f) for f in facts]


def _ics_escape(s: str) -> str:
    """Escape a value for an iCalendar TEXT field (RFC 5545 §3.3.11).

    Backslash first, then collapse CR / CRLF / LF to the literal ``\\n`` escape —
    a bare ``\\r`` is a line separator in iCalendar, so leaving it unescaped would
    let a crafted (LLM-extracted) value inject new content-lines (e.g. forge an
    ``END:VEVENT`` / ``ATTENDEE``). Other control chars are stripped.
    """
    s = s.replace("\\", "\\\\")
    s = s.replace("\r\n", "\\n").replace("\r", "\\n").replace("\n", "\\n")
    s = s.replace(";", "\\;").replace(",", "\\,")
    # Drop remaining control characters (keep tab) that could confuse parsers.
    return "".join(ch for ch in s if ch >= " " or ch == "\t")


@router.get("/obligations/export.ics")
async def export_obligations_ics(
    due_before: date | None = None,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_user_or_default),
):
    """Export the caller's circle-visible obligations as an iCalendar (.ics) feed
    — one all-day VEVENT per obligation on its printed Frist, so deadlines can be
    pulled into a calendar app. Circle-filtered (same read path as the agenda);
    capped at 500 events. Browser-native download (mirrors the trajectory export).
    """
    facts = await DocumentFactRetrieval(db).obligations(
        asker_id=current_user.id, due_before=due_before, limit=500, offset=0,
    )
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    lines = [
        "BEGIN:VCALENDAR", "VERSION:2.0",
        "PRODID:-//Renfield//Obligations//EN", "CALSCALE:GREGORIAN",
    ]
    for f in facts:
        iso = f.get("obligation_date")
        if not iso:
            continue
        ymd = str(iso).replace("-", "")[:8]
        kind = f.get("kind") or "Frist"
        summary = f"Frist: {kind}"
        amount = f.get("amount_value")
        if amount is not None:
            cur = (f.get("amount_currency") or "").strip()
            summary += f" ({amount}{(' ' + cur) if cur else ''})"
        if f.get("legal_gate"):
            summary = "⚠ " + summary  # ⚑ legal
        desc = f.get("value") or f.get("excerpt") or ""
        lines += [
            "BEGIN:VEVENT",
            f"UID:obligation-{f.get('id')}@renfield",
            f"DTSTAMP:{stamp}",
            f"DTSTART;VALUE=DATE:{ymd}",
            f"SUMMARY:{_ics_escape(summary)}",
        ]
        if desc:
            lines.append(f"DESCRIPTION:{_ics_escape(str(desc))}")
        lines.append("END:VEVENT")
    lines.append("END:VCALENDAR")
    body = "\r\n".join(lines) + "\r\n"
    return Response(
        content=body,
        media_type="text/calendar; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="fristen.ics"'},
    )


@router.post("/obligations/{fact_id}/confirm", response_model=ObligationConfirmResponse)
async def confirm_obligation(
    fact_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_user_or_default),
):
    """Mark an obligation handled for the current user (the agenda's Bestätigen).

    Server home for the agenda's former per-device localStorage state: writes a
    ``(fact, user, "confirmed")`` row in the shared obligation ledger, which both
    persists the agenda's Bestätigt across devices AND tells the deadline
    notifier to stop firing further milestones for this obligation. Idempotent.
    404 (not 403) when the fact is not circle-visible — existence-oracle defense.
    """
    retrieval = DocumentFactRetrieval(db)
    if not await retrieval.is_visible(fact_id, current_user.id):
        raise HTTPException(status_code=404, detail="Obligation not found")
    existing = await db.execute(
        select(ObligationAcknowledgement).where(
            ObligationAcknowledgement.document_fact_id == fact_id,
            ObligationAcknowledgement.user_id == current_user.id,
            ObligationAcknowledgement.milestone == OBLIGATION_MILESTONE_CONFIRMED,
        )
    )
    if existing.scalar_one_or_none() is None:
        db.add(ObligationAcknowledgement(
            document_fact_id=fact_id,
            user_id=current_user.id,
            milestone=OBLIGATION_MILESTONE_CONFIRMED,
        ))
        try:
            await db.commit()
        except IntegrityError:
            await db.rollback()  # concurrent confirm — already recorded, still confirmed
    return ObligationConfirmResponse(document_fact_id=fact_id, confirmed=True)


@router.delete("/obligations/{fact_id}/confirm", response_model=ObligationConfirmResponse)
async def reopen_obligation(
    fact_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_user_or_default),
):
    """Reopen a previously-confirmed obligation for the current user (the
    agenda's „Wieder öffnen"). Deletes the user's ``confirmed`` ledger row.
    Idempotent; 404 when the fact is not circle-visible."""
    retrieval = DocumentFactRetrieval(db)
    if not await retrieval.is_visible(fact_id, current_user.id):
        raise HTTPException(status_code=404, detail="Obligation not found")
    await db.execute(
        delete(ObligationAcknowledgement).where(
            ObligationAcknowledgement.document_fact_id == fact_id,
            ObligationAcknowledgement.user_id == current_user.id,
            ObligationAcknowledgement.milestone == OBLIGATION_MILESTONE_CONFIRMED,
        )
    )
    await db.commit()
    return ObligationConfirmResponse(document_fact_id=fact_id, confirmed=False)


@router.get("/documents/{document_id}/facts", response_model=list[DocumentFactResponse])
async def get_document_facts(
    document_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_user_or_default),
):
    """
    All Schicht A facts of one document, circle-access-gated on the parent
    Document (mirrors the house convention in knowledge.py): 404 if the doc
    doesn't exist, 403 if the asker can't reach it, else the facts ([] means
    genuinely factless, NOT inaccessible).
    """
    document = (await db.execute(
        select(Document).where(Document.id == document_id)
    )).scalar_one_or_none()
    if document is None:
        raise HTTPException(status_code=404, detail="Dokument nicht gefunden")

    if settings.auth_enabled:
        if document.atom_id is not None:
            # Facts inherit the document atom's policy — get_atom returns None
            # for both not-found and not-authorized (the circle check).
            atom = await PolymorphicAtomStore(db).get_atom(
                document.atom_id, asker_id=current_user.id,
            )
            if atom is None:
                raise HTTPException(status_code=403, detail="No access to this document")
        else:
            # Pre-atom legacy doc (no atom to gate on): fall back to KB
            # ownership and fail closed. Such docs predate Schicht A and have
            # no facts, but we deny rather than leak.
            owner_id = None
            if document.knowledge_base_id is not None:
                kb = (await db.execute(
                    select(KnowledgeBase).where(KnowledgeBase.id == document.knowledge_base_id)
                )).scalar_one_or_none()
                owner_id = kb.owner_id if kb else None
            if owner_id != current_user.id:
                raise HTTPException(status_code=403, detail="No access to this document")

    facts = await DocumentFactRetrieval(db).facts_for_document(
        document_id, asker_id=current_user.id,
    )
    return [DocumentFactResponse(**f) for f in facts]


@router.post("/documents/facts/{fact_id}/reset-tier", response_model=FactTierResetResponse)
async def reset_fact_tier(
    fact_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_user_or_default),
):
    """Clear a fact's per-fact tier override, restoring it to the parent
    document's current tier. Owner-only (the fact atom's owner); 404 otherwise —
    same existence-oracle defense as the tier PATCH. Idempotent."""
    fact = (await db.execute(
        select(DocumentFact).where(DocumentFact.id == fact_id)
    )).scalar_one_or_none()
    if fact is None:
        raise HTTPException(status_code=404, detail="Fact not found")
    # Lock the fact's atom + verify ownership (mirrors the tier-PATCH route).
    atom_orm = (await db.execute(
        select(AtomModel).where(AtomModel.atom_id == fact.atom_id).with_for_update()
    )).scalar_one_or_none()
    if atom_orm is None or atom_orm.owner_user_id != current_user.id:
        raise HTTPException(status_code=404, detail="Fact not found")

    new_tier = await AtomService(db).reset_fact_tier(fact_id)
    return FactTierResetResponse(
        document_fact_id=fact_id, circle_tier=new_tier or 0, tier_overridden=False,
    )


@router.get("/{atom_id}", response_model=AtomResponse)
async def get_atom(
    atom_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_user_or_default),
):
    """
    Fetch a single atom by ID.

    Returns 404 for both not-found AND not-authorized (uniform — defends
    against existence-oracle attacks). Audit log records the difference
    server-side.
    """
    service = AtomService(db)
    atom = await service.get_atom(atom_id, asker_id=current_user.id)
    if atom is None:
        raise HTTPException(status_code=404, detail="Atom not found")
    return AtomResponse.from_atom(atom)


@router.patch("/{atom_id}/tier", response_model=AtomResponse)
async def update_atom_tier(
    atom_id: str,
    body: UpdateTierRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_user_or_default),
):
    """
    Change an atom's circle policy. Owner-only.

    For kg_node atoms, this cascades to all incident kg_relations
    (each relation's circle_tier becomes MIN(subject.tier, object.tier)).

    Concurrency: uses SELECT FOR UPDATE on the AtomModel row to lock it
    until the transaction commits, eliminating the TOCTOU between the
    owner check and the update_tier call (per PR #402 review SHOULD-FIX #6).
    """
    # SELECT FOR UPDATE locks the row until commit; concurrent deletes/owner
    # changes block until we're done.
    atom_orm = (await db.execute(
        select(AtomModel).where(AtomModel.atom_id == atom_id).with_for_update()
    )).scalar_one_or_none()
    if atom_orm is None:
        raise HTTPException(status_code=404, detail="Atom not found")
    if atom_orm.owner_user_id != current_user.id:
        # Uniform 404 to avoid leaking owner identity.
        raise HTTPException(status_code=404, detail="Atom not found")

    service = AtomService(db)
    await service.update_tier(atom_id, body.policy)

    updated = await service.get_atom(atom_id, asker_id=current_user.id)
    if updated is None:
        # Shouldn't happen — we held the row lock through update_tier — defend.
        logger.error(f"Atom {atom_id} disappeared after update_tier — race?")
        raise HTTPException(status_code=500, detail="Atom update failed")
    return AtomResponse.from_atom(updated)


@router.delete("/{atom_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_atom(
    atom_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_user_or_default),
):
    """
    Soft-delete an atom (marks source row inactive). Owner-only.
    The atoms row stays for audit trail.
    """
    atom_orm = (await db.execute(
        select(AtomModel).where(AtomModel.atom_id == atom_id)
    )).scalar_one_or_none()
    if atom_orm is None:
        raise HTTPException(status_code=404, detail="Atom not found")
    if atom_orm.owner_user_id != current_user.id:
        raise HTTPException(status_code=404, detail="Atom not found")

    service = AtomService(db)
    await service.soft_delete(atom_id)
    return None
