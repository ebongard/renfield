"""
Atoms API — read/edit access to atoms (kb_chunks, kg_nodes, kg_edges,
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

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from loguru import logger
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.database import Atom as AtomModel, User
from services.atom_service import AtomService
from services.atom_types import Atom
from services.auth_service import get_user_or_default
from services.circle_resolver import CircleResolver, atom_from_orm
from services.database import get_db
from services.polymorphic_atom_store import PolymorphicAtomStore

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
