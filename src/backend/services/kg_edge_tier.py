"""The tier of a knowledge-graph edge: one rule, one place (#1625).

An edge is never more visible than either endpoint, and never more visible than
the document it was extracted from:

    kg_relations.circle_tier = LEAST(subject tier, object tier, tier_cap)

``tier_cap`` is the tier of the most private document that asserted the edge
(NULL = no document source, no cap). It only ever goes DOWN: a document made
more open later does not raise it, because the edge may also come from a source
that is not recorded (``source_session_id`` holds the first source only).

Every recompute of an edge tier goes through this module. A copy of the old
endpoints-only ``LEAST`` elsewhere would silently undo the cap on the next
entity tier change or merge (``tests/backend/test_kg_edge_tier.py`` greps for it).
"""
from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from models.database import ATOM_TYPE_KG_EDGE, TIER_PUBLIC

# The tier of edge ``r`` from its endpoints ``s`` / ``o`` and its own cap.
EDGE_TIER_SQL = f"LEAST(s.circle_tier, o.circle_tier, COALESCE(r.tier_cap, {TIER_PUBLIC}))"


def document_source_ref(document_id: int) -> str:
    """The ``source_session_id`` a document-extracted edge carries."""
    return f"doc:{int(document_id)}"


def capped_edge_tier(endpoint_tiers: list[int], tier_cap: int | None) -> int:
    """Python twin of ``EDGE_TIER_SQL`` for an edge being created."""
    tiers = [int(t) for t in endpoint_tiers]
    if tier_cap is not None:
        tiers.append(int(tier_cap))
    return min(tiers) if tiers else 0


def lowered_cap(current_cap: int | None, new_cap: int) -> int:
    """A cap only goes down; NULL (no cap yet) takes the new one."""
    return int(new_cap) if current_cap is None else min(int(current_cap), int(new_cap))


async def sync_edge_atom_policy(db: AsyncSession, relation_id: int) -> None:
    """Mirror one edge's ``circle_tier`` into its atom policy (no commit)."""
    await db.execute(
        text(
            "UPDATE atoms SET policy = json_build_object('tier', r.circle_tier), updated_at = NOW() "
            "FROM kg_relations r "
            "WHERE atoms.atom_type = :edge AND atoms.source_id = r.id::text AND r.id = :rid"
        ),
        {"edge": ATOM_TYPE_KG_EDGE, "rid": int(relation_id)},
    )


async def recompute_incident_edge_tiers(db: AsyncSession, entity_id: int) -> None:
    """Recompute every edge touching ``entity_id`` and sync its atom policy (no commit).

    Called after an entity's tier changed or after a merge reparented edges onto it.
    """
    params = {"entity_id": int(entity_id), "edge": ATOM_TYPE_KG_EDGE}
    await db.execute(
        text(
            f"UPDATE kg_relations r SET circle_tier = {EDGE_TIER_SQL} "
            "FROM kg_entities s, kg_entities o "
            "WHERE r.subject_id = s.id AND r.object_id = o.id "
            "AND (r.subject_id = :entity_id OR r.object_id = :entity_id)"
        ),
        params,
    )
    await db.execute(
        text(
            "UPDATE atoms SET policy = json_build_object('tier', r.circle_tier), updated_at = NOW() "
            "FROM kg_relations r "
            "WHERE atoms.atom_type = :edge AND atoms.source_id = r.id::text "
            "AND (r.subject_id = :entity_id OR r.object_id = :entity_id)"
        ),
        params,
    )


async def clamp_edge_tier(db: AsyncSession, relation_id: int) -> int | None:
    """Clamp one edge to the rule after a direct tier write (no commit).

    A direct edit may LOWER an edge freely; it cannot lift it above its
    endpoints or its cap. Returns the resulting tier (None: no such edge).
    """
    return (await db.execute(
        text(
            f"UPDATE kg_relations r SET circle_tier = LEAST(r.circle_tier, {EDGE_TIER_SQL}) "
            "FROM kg_entities s, kg_entities o "
            "WHERE r.subject_id = s.id AND r.object_id = o.id AND r.id = :rid "
            "RETURNING r.circle_tier"
        ),
        {"rid": int(relation_id)},
    )).scalar_one_or_none()


async def cap_document_edges(db: AsyncSession, document_id: int, tier: int) -> list[str]:
    """Carry a document's tier onto the edges it is the recorded source of (no commit).

    The cap goes down only (``lowered_cap``) and the edge tier follows it down,
    never up: after an explicit tier change no edge of the document is more open
    than the document. That includes an uncapped edge from before the cutover
    (D-2a) — the owner touched the document's tier, so the rule now applies.
    Returns the atom ids of the touched edges, for the caller to invalidate after
    its commit.
    """
    rows = (await db.execute(
        text(
            "UPDATE kg_relations r "
            "SET tier_cap = LEAST(COALESCE(r.tier_cap, :tier), :tier), "
            "    circle_tier = LEAST(r.circle_tier, :tier) "
            "WHERE r.source_session_id = :ref "
            "  AND (r.tier_cap IS NULL OR r.tier_cap > :tier) "
            "RETURNING r.id, r.atom_id"
        ),
        {"tier": int(tier), "ref": document_source_ref(document_id)},
    )).fetchall()
    if not rows:
        return []
    await db.execute(
        text(
            "UPDATE atoms SET policy = json_build_object('tier', r.circle_tier), updated_at = NOW() "
            "FROM kg_relations r "
            "WHERE atoms.atom_type = :edge AND atoms.source_id = r.id::text "
            "AND r.id = ANY(:ids)"
        ),
        {"edge": ATOM_TYPE_KG_EDGE, "ids": [row.id for row in rows]},
    )
    return [row.atom_id for row in rows if row.atom_id]
