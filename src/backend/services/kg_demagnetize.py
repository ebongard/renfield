"""De-magnetize person KG entities whose description is a generic meta-description.

Background: the entity embedding is built from ``f"{name}: {description}"``
(see ``KnowledgeGraphService._embed_input``). When the extractor filled
``description`` with a generic phrase about the TYPE rather than the entity
(e.g. "Vollständiger Name einer Person" / "full name of a person"), the row's
embedding collapsed toward a generic-person centroid. Any bare given name then
embedded >= ``kg_similarity_threshold`` from it, so the old resolve cascade
folded DIFFERENT people into that row — that is how entity #11 became a
127-mention magnet hub.

The resolve cascade no longer embedding-matches persons at all, so this stops
NEW conflation. This pass repairs the EXISTING rows so they also stop polluting
**retrieval** (a "Jutta" query would still semantically hit the Anna centroid)
and the **reconciler's** same-name embedding dedup: it NULLs the generic
description and re-embeds the row from the bare name.

Importable core (testable against ``pg_db_session``); the thin CLI lives in
``bin/demagnetize_person_entities.py``.

Safety:
  * ``dry_run`` writes nothing; lists exactly which rows would change.
  * only ``entity_type='person'`` canonical/live rows with a description that
    matches the curated generic-phrase list are touched (tight by design —
    the prompt fix prevents new ones, so this is a one-shot cleanup).
  * per-row commit: a re-embed failure isolates to that row, batch continues.
  * idempotent: a row whose description is already NULL is not a candidate.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from models.database import KGEntity
from services.knowledge_graph_service import KnowledgeGraphService

logger = logging.getLogger("kg_demagnetize")

# Curated generic meta-descriptions (case-insensitive substring match). These
# describe the TYPE, not the entity, so they carry no identity signal and must
# not contribute to the embedding. Kept tight: the extraction-prompt fix stops
# new ones, so over-broad matching would only risk nulling a real description.
GENERIC_DESC_PATTERNS = (
    "name einer person",          # "Vollständiger Name einer Person"
    "vollstaendiger name",
    "vollständiger name",
    "full name of a person",
    "name of a person",
    "a person's full name",
    "person's name",
    "eine person",
    "a person.",                  # trailing dot = the whole description is just "A person."
)


@dataclass
class DemagnetizeReport:
    candidates: int = 0
    updated: int = 0
    failed: int = 0
    samples: list[tuple[int, str, str]] = field(default_factory=list)  # (id, name, old_desc)


def _candidate_query(user_id: int | None):
    desc = func.lower(KGEntity.description)
    conds = or_(*[desc.like(f"%{p.lower()}%") for p in GENERIC_DESC_PATTERNS])
    q = select(KGEntity).where(
        and_(
            KGEntity.entity_type == "person",
            KGEntity.is_active == True,  # noqa: E712
            KGEntity.canonical_id.is_(None),
            KGEntity.description.is_not(None),
            conds,
        )
    ).order_by(KGEntity.mention_count.desc(), KGEntity.id.asc())
    if user_id is not None:
        q = q.where(KGEntity.user_id == user_id)
    return q


async def dry_run(db: AsyncSession, user_id: int | None = None) -> DemagnetizeReport:
    rows = (await db.execute(_candidate_query(user_id))).scalars().all()
    rep = DemagnetizeReport(candidates=len(rows))
    rep.samples = [(e.id, e.name, e.description or "") for e in rows]
    return rep


async def run(db: AsyncSession, user_id: int | None = None) -> DemagnetizeReport:
    rows = (await db.execute(_candidate_query(user_id))).scalars().all()
    rep = DemagnetizeReport(candidates=len(rows))
    if not rows:
        return rep
    kg = KnowledgeGraphService(db)
    for e in rows:
        old = e.description or ""
        try:
            # Re-embed from the bare name only (no description) so the row sits at
            # its own name, not the generic-person centroid.
            new_emb = await kg._get_embedding(kg._embed_input(e.name, None))
            e.description = None
            e.embedding = new_emb
            await db.commit()
            rep.updated += 1
            rep.samples.append((e.id, e.name, old))
            logger.info("de-magnetized entity #%d %r (dropped desc=%r)", e.id, e.name, old)
        except Exception as exc:  # noqa: BLE001 — isolate one bad row, keep going
            await db.rollback()
            rep.failed += 1
            logger.warning("skip entity #%d %r: %s", e.id, e.name, exc)
    return rep
