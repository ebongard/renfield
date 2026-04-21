"""
Circles API — per-user circle configuration + membership management.

All endpoints under /api/circles/me operate on the AUTHENTICATED user's
own circles. Modifying another user's circles is forbidden (no owner
override; admins use the dedicated admin tooling per Lane C).

Endpoints:
- GET    /api/circles/me/settings              dimension config + default capture policy
- PATCH  /api/circles/me/settings              update default capture policy (per Finding 7A)
- GET    /api/circles/me/members               list members of my circles
- POST   /api/circles/me/members               add a member at a tier/dimension value
- PATCH  /api/circles/me/members/{user_id}     change a member's tier/value
- DELETE /api/circles/me/members/{user_id}     remove a member from a dimension
- GET    /api/circles/me/atoms-for-review      Brain Review Queue
                                                 (atoms <=7d old, owner-only,
                                                  per design-review Pass 1)

Single-user mode (AUTH_ENABLED=false) handling: per Pass 2A1 the UI hides
tier surfaces in single-user mode; backend endpoints stay reachable but
return effectively empty/owner-only data when no other users exist.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from loguru import logger
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.database import (
    Atom as AtomModel,
    Circle,
    CircleMembership,
    ConversationMemory,
    DocumentChunk,
    KGEntity,
    KGRelation,
    User,
)
from services.auth_service import get_user_or_default
from services.circle_resolver import CircleResolver
from services.database import get_db

router = APIRouter()


# =============================================================================
# Schemas
# =============================================================================


class CircleSettingsResponse(BaseModel):
    """Current user's circle config + default capture policy."""
    owner_user_id: int
    dimension_config: dict[str, Any]
    default_capture_policy: dict[str, Any]
    created_at: datetime
    updated_at: datetime


class UpdateSettingsRequest(BaseModel):
    """PATCH body for /settings — partial update of capture policy."""
    default_capture_policy: dict[str, Any] | None = Field(None)
    dimension_config: dict[str, Any] | None = Field(None)


class MembershipResponse(BaseModel):
    """One member's entry across one or more dimensions."""
    member_user_id: int
    member_username: str | None = None
    dimensions: dict[str, Any]  # dimension -> value
    granted_at: datetime


class AddMemberRequest(BaseModel):
    """POST body — add a member at a single (dimension, value) pair."""
    member_user_id: int
    dimension: str = Field(..., min_length=1, max_length=32)
    value: Any = Field(..., description="Int for ladder (depth index), str for set")


class UpdateMemberRequest(BaseModel):
    """PATCH body — change one dimension's value for a member."""
    dimension: str
    value: Any


class AtomReviewResponse(BaseModel):
    """Atom in the Brain Review Queue — owner-only view.

    `title` + `preview` are human-readable fields the UI renders
    instead of the raw atom_id UUID. An owner gating an atom's tier
    needs to see WHAT is being gated, not a hex id.

    Resolution per atom_type:
      - kg_node        → entity.name                + entity.description
      - kg_edge        → "subj predicate obj"       + (empty preview)
      - kb_chunk       → documents.filename         + chunk.content[:200]
      - conversation_memory → "Conversation 2026-04-19" + content[:200]
      - unknown        → atom_type label            + (empty preview)
    """
    atom_id: str
    atom_type: str
    policy: dict[str, Any]
    tier: int
    created_at: datetime
    title: str
    preview: str | None = None


# =============================================================================
# Settings
# =============================================================================


@router.get("/me/settings", response_model=CircleSettingsResponse)
async def get_settings(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_user_or_default),
):
    """Return the authenticated user's circle settings."""
    circle = await _get_or_create_circle(db, current_user.id)
    return CircleSettingsResponse(
        owner_user_id=circle.owner_user_id,
        dimension_config=circle.dimension_config or {},
        default_capture_policy=circle.default_capture_policy or {"tier": 0},
        created_at=circle.created_at,
        updated_at=circle.updated_at,
    )


@router.patch("/me/settings", response_model=CircleSettingsResponse)
async def update_settings(
    body: UpdateSettingsRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_user_or_default),
):
    """Partial update of dimension_config and/or default_capture_policy."""
    circle = await _get_or_create_circle(db, current_user.id)
    if body.dimension_config is not None:
        circle.dimension_config = body.dimension_config
    if body.default_capture_policy is not None:
        circle.default_capture_policy = body.default_capture_policy
    circle.updated_at = _utcnow()
    await db.commit()
    await db.refresh(circle)
    return CircleSettingsResponse(
        owner_user_id=circle.owner_user_id,
        dimension_config=circle.dimension_config or {},
        default_capture_policy=circle.default_capture_policy or {"tier": 0},
        created_at=circle.created_at,
        updated_at=circle.updated_at,
    )


# =============================================================================
# Members
# =============================================================================


@router.get("/me/members", response_model=list[MembershipResponse])
async def list_members(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_user_or_default),
):
    """
    List every member across every dimension of my circles.

    Returns one entry per (member, *all dimensions*) tuple — multiple dimension
    rows for the same member collapse into a single response item with a
    `dimensions: {tier: 2, tenant: "acme"}` map.
    """
    rows = (await db.execute(
        select(CircleMembership).where(CircleMembership.circle_owner_id == current_user.id)
    )).scalars().all()

    # Group by member_user_id
    by_member: dict[int, dict[str, Any]] = {}
    granted_at_by_member: dict[int, datetime] = {}
    for row in rows:
        if row.member_user_id not in by_member:
            by_member[row.member_user_id] = {}
            granted_at_by_member[row.member_user_id] = row.granted_at
        by_member[row.member_user_id][row.dimension] = row.value

    # Look up usernames in one query
    member_ids = list(by_member.keys())
    users_by_id: dict[int, str] = {}
    if member_ids:
        user_rows = (await db.execute(
            select(User.id, User.username).where(User.id.in_(member_ids))
        )).all()
        users_by_id = {r.id: r.username for r in user_rows}

    return [
        MembershipResponse(
            member_user_id=mid,
            member_username=users_by_id.get(mid),
            dimensions=dims,
            granted_at=granted_at_by_member[mid],
        )
        for mid, dims in by_member.items()
    ]


@router.post("/me/members", response_model=MembershipResponse, status_code=status.HTTP_201_CREATED)
async def add_member(
    body: AddMemberRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_user_or_default),
):
    """Add a member to one of my dimensions (or update if already present)."""
    if body.member_user_id == current_user.id:
        raise HTTPException(status_code=400, detail="Cannot add yourself to your own circles")

    # Verify the target user exists
    target = (await db.execute(
        select(User).where(User.id == body.member_user_id)
    )).scalar_one_or_none()
    if target is None:
        raise HTTPException(status_code=404, detail="User not found")

    # Upsert: if a row already exists for (owner, member, dimension), update value
    existing = (await db.execute(
        select(CircleMembership).where(
            CircleMembership.circle_owner_id == current_user.id,
            CircleMembership.member_user_id == body.member_user_id,
            CircleMembership.dimension == body.dimension,
        )
    )).scalar_one_or_none()

    if existing is not None:
        existing.value = body.value
    else:
        existing = CircleMembership(
            circle_owner_id=current_user.id,
            member_user_id=body.member_user_id,
            dimension=body.dimension,
            value=body.value,
            granted_by=current_user.id,
        )
        db.add(existing)

    await db.commit()
    await db.refresh(existing)

    # Invalidate resolver cache so the next access check picks up the new membership.
    CircleResolver(db).invalidate_for_membership(current_user.id, body.member_user_id)

    return MembershipResponse(
        member_user_id=existing.member_user_id,
        member_username=target.username,
        dimensions={existing.dimension: existing.value},
        granted_at=existing.granted_at,
    )


@router.patch("/me/members/{user_id}", response_model=MembershipResponse)
async def update_member(
    user_id: int,
    body: UpdateMemberRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_user_or_default),
):
    """Change one dimension's value for a member."""
    existing = (await db.execute(
        select(CircleMembership).where(
            CircleMembership.circle_owner_id == current_user.id,
            CircleMembership.member_user_id == user_id,
            CircleMembership.dimension == body.dimension,
        )
    )).scalar_one_or_none()

    if existing is None:
        raise HTTPException(
            status_code=404,
            detail=f"Member {user_id} has no membership in dimension '{body.dimension}'",
        )

    existing.value = body.value
    await db.commit()
    await db.refresh(existing)

    CircleResolver(db).invalidate_for_membership(current_user.id, user_id)

    target = (await db.execute(
        select(User).where(User.id == user_id)
    )).scalar_one_or_none()
    return MembershipResponse(
        member_user_id=user_id,
        member_username=target.username if target else None,
        dimensions={existing.dimension: existing.value},
        granted_at=existing.granted_at,
    )


@router.delete("/me/members/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_member(
    user_id: int,
    dimension: str | None = None,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_user_or_default),
):
    """
    Remove a member from a specific dimension (?dimension=tier),
    or from ALL dimensions if no dimension query param given.
    """
    query = select(CircleMembership).where(
        CircleMembership.circle_owner_id == current_user.id,
        CircleMembership.member_user_id == user_id,
    )
    if dimension is not None:
        query = query.where(CircleMembership.dimension == dimension)

    rows = (await db.execute(query)).scalars().all()
    if not rows:
        raise HTTPException(status_code=404, detail="No matching memberships found")

    for row in rows:
        await db.delete(row)
    await db.commit()

    CircleResolver(db).invalidate_for_membership(current_user.id, user_id)
    return None


# =============================================================================
# Brain Review Queue
# =============================================================================


@router.get("/me/atoms-for-review", response_model=list[AtomReviewResponse])
async def atoms_for_review(
    days: int = 7,
    limit: int = 50,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_user_or_default),
):
    """
    Brain Review Queue — atoms captured in the last N days, owner-only.

    Per design-review Pass 1: returns atoms <=7 days old by default. Caller
    can override via ?days=N. Capped at limit=50 to keep the queue scannable.
    """
    if days < 1 or days > 90:
        raise HTTPException(status_code=400, detail="days must be 1-90")
    if limit < 1 or limit > 200:
        raise HTTPException(status_code=400, detail="limit must be 1-200")

    cutoff = _utcnow() - timedelta(days=days)
    rows = (await db.execute(
        select(AtomModel)
        .where(
            AtomModel.owner_user_id == current_user.id,
            AtomModel.created_at >= cutoff,
        )
        .order_by(AtomModel.created_at.desc())
        .limit(limit)
    )).scalars().all()

    # Enrich with human-readable title + preview per source table. One
    # bulk query per atom_type keeps the N+1 risk in check even when
    # the review queue is filled with a mix of kg + chunk atoms.
    labels = await _resolve_review_labels(db, rows)
    return [
        AtomReviewResponse(
            atom_id=r.atom_id,
            atom_type=r.atom_type,
            policy=r.policy or {"tier": 0},
            tier=int((r.policy or {"tier": 0}).get("tier", 0)),
            created_at=r.created_at,
            title=labels[r.atom_id][0],
            preview=labels[r.atom_id][1],
        )
        for r in rows
    ]


async def _resolve_review_labels(
    db: AsyncSession, atoms: list[AtomModel],
) -> dict[str, tuple[str, str | None]]:
    """Bulk-resolve (title, preview) for a list of atoms by atom_type.

    Returns a dict keyed by atom_id. Falls back to
    ("unknown {atom_type}", None) for atoms whose source row is
    missing (e.g., source table was deleted after the atom was
    written — cleanup job not wired yet).

    Kept private to this module because the mapping is review-UI-
    specific: `kg_edge` renders the predicate, not the row id; that's
    not how the knowledge-graph page labels relations.
    """
    PREVIEW_MAX = 200

    # Group atoms by source_table so we can do one SELECT per table.
    by_table: dict[str, list[AtomModel]] = {}
    for a in atoms:
        by_table.setdefault(a.source_table, []).append(a)

    out: dict[str, tuple[str, str | None]] = {}

    def _truncate(s: str | None) -> str | None:
        if not s:
            return None
        s = s.strip()
        return s if len(s) <= PREVIEW_MAX else s[: PREVIEW_MAX - 1] + "…"

    # --- kg_entities ---
    kg_node_atoms = by_table.get("kg_entities", [])
    if kg_node_atoms:
        source_ids = [int(a.source_id) for a in kg_node_atoms if a.source_id.isdigit()]
        ents = {}
        if source_ids:
            rows = (await db.execute(
                select(KGEntity).where(KGEntity.id.in_(source_ids))
            )).scalars().all()
            ents = {e.id: e for e in rows}
        for a in kg_node_atoms:
            e = ents.get(int(a.source_id)) if a.source_id.isdigit() else None
            if e is not None:
                out[a.atom_id] = (e.name, _truncate(e.description))
            else:
                out[a.atom_id] = (f"Unknown entity ({a.source_id})", None)

    # --- kg_relations ---
    kg_edge_atoms = by_table.get("kg_relations", [])
    if kg_edge_atoms:
        source_ids = [int(a.source_id) for a in kg_edge_atoms if a.source_id.isdigit()]
        rels = {}
        if source_ids:
            # Eager-load subject + object so we can read their names.
            from sqlalchemy.orm import selectinload
            rows = (await db.execute(
                select(KGRelation)
                .where(KGRelation.id.in_(source_ids))
                .options(
                    selectinload(KGRelation.subject),
                    selectinload(KGRelation.object),
                )
            )).scalars().all()
            rels = {r.id: r for r in rows}
        for a in kg_edge_atoms:
            r = rels.get(int(a.source_id)) if a.source_id.isdigit() else None
            if r is not None and r.subject is not None and r.object is not None:
                title = f"{r.subject.name} {r.predicate} {r.object.name}"
                out[a.atom_id] = (title, None)
            else:
                out[a.atom_id] = (f"Unknown relation ({a.source_id})", None)

    # --- document_chunks ---
    chunk_atoms = by_table.get("document_chunks", [])
    if chunk_atoms:
        source_ids = [int(a.source_id) for a in chunk_atoms if a.source_id.isdigit()]
        chunks = {}
        if source_ids:
            from sqlalchemy.orm import selectinload
            rows = (await db.execute(
                select(DocumentChunk)
                .where(DocumentChunk.id.in_(source_ids))
                .options(selectinload(DocumentChunk.document))
            )).scalars().all()
            chunks = {c.id: c for c in rows}
        for a in chunk_atoms:
            c = chunks.get(int(a.source_id)) if a.source_id.isdigit() else None
            if c is not None:
                doc = getattr(c, "document", None)
                title = (doc.title if doc and getattr(doc, "title", None) else None) \
                    or (doc.filename if doc else None) \
                    or "Document chunk"
                out[a.atom_id] = (title, _truncate(c.content))
            else:
                out[a.atom_id] = (f"Unknown chunk ({a.source_id})", None)

    # --- conversation_memories ---
    memory_atoms = by_table.get("conversation_memories", [])
    if memory_atoms:
        source_ids = [int(a.source_id) for a in memory_atoms if a.source_id.isdigit()]
        mems = {}
        if source_ids:
            rows = (await db.execute(
                select(ConversationMemory).where(ConversationMemory.id.in_(source_ids))
            )).scalars().all()
            mems = {m.id: m for m in rows}
        for a in memory_atoms:
            m = mems.get(int(a.source_id)) if a.source_id.isdigit() else None
            if m is not None:
                stamp = m.created_at.strftime("%Y-%m-%d %H:%M") if m.created_at else "?"
                out[a.atom_id] = (f"Memory · {stamp}", _truncate(m.content))
            else:
                out[a.atom_id] = (f"Unknown memory ({a.source_id})", None)

    # Backstop — any atom_type we don't have a resolver for gets a
    # generic label so the row still renders useful info.
    for a in atoms:
        out.setdefault(a.atom_id, (f"Unknown {a.atom_type}", None))

    return out


# =============================================================================
# Helpers
# =============================================================================


def _utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


async def _get_or_create_circle(db: AsyncSession, owner_user_id: int) -> Circle:
    """
    Get the user's circles row, creating one with home defaults if it doesn't exist.

    Idempotent — safe to call from any endpoint that needs the user's
    dimension config or default capture policy.

    Concurrency: SELECT-then-INSERT is wrapped in try/except IntegrityError
    per PR #402 review SHOULD-FIX #7. Two simultaneous first hits to /settings
    from the same user used to crash with PK collision; the loser now re-SELECTs
    after the winner's INSERT and returns the existing row.
    """
    from sqlalchemy.exc import IntegrityError

    existing = (await db.execute(
        select(Circle).where(Circle.owner_user_id == owner_user_id)
    )).scalar_one_or_none()
    if existing is not None:
        return existing

    new_circle = Circle(
        owner_user_id=owner_user_id,
        dimension_config={
            "tier": {
                "shape": "ladder",
                "values": ["self", "trusted", "household", "extended", "public"],
            },
        },
        default_capture_policy={"tier": 0},
    )
    db.add(new_circle)
    try:
        await db.commit()
        await db.refresh(new_circle)
        return new_circle
    except IntegrityError:
        # Concurrent writer beat us; re-SELECT and return the existing row.
        await db.rollback()
        existing = (await db.execute(
            select(Circle).where(Circle.owner_user_id == owner_user_id)
        )).scalar_one_or_none()
        if existing is None:
            raise  # Shouldn't happen; surface the error
        return existing
