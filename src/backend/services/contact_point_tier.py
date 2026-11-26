"""The tier of a contact point: one rule, one place (#1240).

A contact point is never more visible than its entity, and never more visible
than the source it came from:

    kg_entity_contact_points.circle_tier = LEAST(entity tier, source_tier)

``source_tier`` only ever goes DOWN. A document made more private lowers the
``source_tier`` of every value it is the recorded source of. A document made
more open does not lift it, mirroring ``kg_relations.tier_cap`` (``kg_edge_tier.py``).

Every recompute of a contact-point tier goes through this module. It is called
wherever an entity tier changes (the three ``recompute_incident_edge_tiers`` call
sites: ``AtomService.update_tier``, ``update_entity_circle_tier`` without an atom,
``merge_entities``) and from the ``kb_document`` cascade.
``tests/backend/test_kg_contact_points.py`` pins that pairing.
"""
from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

CONTACT_POINT_TIER_SQL = "LEAST(e.circle_tier, cp.source_tier)"


def contact_point_tier(entity_tier: int, source_tier: int) -> int:
    """Python twin of ``CONTACT_POINT_TIER_SQL`` for a value being created."""
    return min(int(entity_tier), int(source_tier))


async def recompute_entity_contact_point_tiers(db: AsyncSession, entity_id: int) -> None:
    """Recompute every contact point of ``entity_id`` (no commit)."""
    await db.execute(
        text(
            f"UPDATE kg_entity_contact_points cp SET circle_tier = {CONTACT_POINT_TIER_SQL} "
            "FROM kg_entities e WHERE cp.entity_id = e.id AND cp.entity_id = :eid"
        ),
        {"eid": int(entity_id)},
    )


async def cap_document_contact_points(db: AsyncSession, document_id: int, tier: int) -> None:
    """Lower the source tier of the document's values to ``tier`` and recompute (no commit)."""
    params = {"doc": int(document_id), "tier": int(tier)}
    await db.execute(
        text(
            "UPDATE kg_entity_contact_points SET source_tier = LEAST(source_tier, :tier) "
            "WHERE document_id = :doc"
        ),
        params,
    )
    await db.execute(
        text(
            f"UPDATE kg_entity_contact_points cp SET circle_tier = {CONTACT_POINT_TIER_SQL} "
            "FROM kg_entities e WHERE cp.entity_id = e.id AND cp.document_id = :doc"
        ),
        {"doc": int(document_id)},
    )
