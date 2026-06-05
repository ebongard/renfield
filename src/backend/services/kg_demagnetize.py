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
  * only person rows (primary OR multi-type) whose description is WHOLE-STRING
    equal to a generic phrase are touched — a description that merely contains
    a generic phrase plus identity text is preserved (see
    ``is_generic_person_description``). The CLI dumps every old description to a
    file before mutating (fail-closed), so the NULL is recoverable.
  * per-row commit: a re-embed failure isolates to that row, batch continues.
  * each row is re-asserted live/canonical/still-generic immediately before its
    commit, so a concurrent merge/extraction can't be clobbered (skipped count).
  * idempotent: a row whose description is already NULL is not a candidate.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

from sqlalchemy import and_, cast, func, or_, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession

from models.database import KGEntity
from services.knowledge_graph_service import (
    GENERIC_PERSON_DESCRIPTIONS,
    KnowledgeGraphService,
    is_generic_person_description,
)

logger = logging.getLogger("kg_demagnetize")

# WHOLE-STRING match variants for the SQL candidate filter: each generic phrase
# with and without a trailing period, matched against LOWER(TRIM(description)).
# This mirrors is_generic_person_description() (whole-string, not substring) so a
# real description that merely CONTAINS a generic phrase plus identity text
# (e.g. "Vollständiger Name laut Ausweis: Anna B.") is NOT selected — the
# substring approach this replaces would have nulled it.
_GENERIC_SQL_VARIANTS = [
    v for phrase in GENERIC_PERSON_DESCRIPTIONS for v in (phrase, phrase + ".")
]


@dataclass
class DemagnetizeReport:
    candidates: int = 0
    updated: int = 0
    failed: int = 0
    skipped: int = 0  # row changed under us (tombstoned/deactivated) before its commit
    samples: list[tuple[int, str, str]] = field(default_factory=list)  # (id, name, old_desc)


def _candidate_query(user_id: int | None):
    # Person by primary type OR multi-type membership (a person carried as a
    # secondary type, e.g. entity_types=["organization","person"], is the exact
    # edge the magnet bug came from).
    # entity_types is JSON().with_variant(JSONB) — base JSON, so .contains() emits
    # a string LIKE. Cast to JSONB and use @> for real array containment.
    is_person = or_(
        KGEntity.entity_type == "person",
        cast(KGEntity.entity_types, JSONB).op("@>")(cast(["person"], JSONB)),
    )
    is_generic = func.lower(func.trim(KGEntity.description)).in_(_GENERIC_SQL_VARIANTS)
    q = select(KGEntity).where(
        and_(
            is_person,
            KGEntity.is_active == True,  # noqa: E712
            KGEntity.canonical_id.is_(None),
            KGEntity.description.is_not(None),
            is_generic,
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
        # Re-assert the row is still a live, canonical, still-generic candidate: a
        # concurrent extraction/merge between the SELECT and this row's turn could
        # have tombstoned it (canonical_id set), deactivated it, or replaced the
        # description. Re-embedding a tombstone or clobbering a fresh description
        # would be a regression — skip those rather than write stale state.
        await db.refresh(e)
        if (e.canonical_id is not None or not e.is_active
                or not is_generic_person_description(e.description)):
            rep.skipped += 1
            logger.info("skip entity #%d %r: changed under backfill (canonical_id=%s active=%s desc=%r)",
                        e.id, e.name, e.canonical_id, e.is_active, e.description)
            continue
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
