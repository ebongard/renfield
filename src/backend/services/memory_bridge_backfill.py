"""Backfill logic for conversation_memories.subject_entity_id (Phase 3b).

Importable core so it can be unit-tested against ``pg_db_session`` and reused;
the thin CLI lives in ``bin/backfill_subject_entity_ids.py``.

Phase 2 populated ``subject_name`` (a string) but left ``subject_entity_id``
(FK to kg_entities) NULL. This links each decomposable memory (category
fact/preference) that carries a subject_name to its canonical KG entity —
linking existing entities and creating one for subjects with no entity yet.

Safety (Phase 3 eng-review):
  * ``dry_run_backfill`` writes nothing; it estimates link-vs-create counts.
  * type-scoped + tier-pinned resolution: ``create_tier = memory.circle_tier``
    (never more public), ``match_entity_type=True`` (no wrong-type links).
  * per-row create+link committed in one transaction -> no orphan entity on crash.
  * idempotent: already-linked rows are excluded by the query.
  * failure isolation: one bad row is logged and skipped; the batch continues.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from models.database import (
    MEMORY_CATEGORY_FACT,
    MEMORY_CATEGORY_PREFERENCE,
    ConversationMemory,
    KGEntity,
)
from services.knowledge_graph_service import KnowledgeGraphService
from utils.log_safety import error_text_without_content, logged_content

logger = logging.getLogger("memory_bridge_backfill")

_DECOMPOSABLE = (MEMORY_CATEGORY_FACT, MEMORY_CATEGORY_PREFERENCE)


@dataclass
class BackfillReport:
    candidates: int = 0
    linked: int = 0
    created: int = 0
    failed: int = 0
    # dry-run estimates:
    would_link: int = 0
    would_create: int = 0
    distinct_subjects: int = 0


def _candidate_query(user_id: int | None):
    q = (
        select(ConversationMemory)
        .where(
            ConversationMemory.is_active == True,  # noqa: E712
            ConversationMemory.subject_name.is_not(None),
            ConversationMemory.subject_entity_id.is_(None),
            ConversationMemory.category.in_(_DECOMPOSABLE),
        )
        .order_by(ConversationMemory.id.asc())
    )
    if user_id is not None:
        q = q.where(ConversationMemory.user_id == user_id)
    return q


async def _existing_person_entity_id(db: AsyncSession, name: str, user_id: int | None) -> int | None:
    """Cheap exact-name person match (the resolve fast-path) for the dry-run estimate."""
    row = (await db.execute(
        select(KGEntity.id).where(
            func.lower(KGEntity.name) == name.strip().lower(),
            KGEntity.is_active == True,  # noqa: E712
            KGEntity.canonical_id.is_(None),
            KGEntity.entity_type == "person",
            (KGEntity.user_id == user_id) | (KGEntity.user_id.is_(None)),
        ).limit(1)
    )).first()
    return int(row[0]) if row else None


async def dry_run_backfill(db: AsyncSession, user_id: int | None = None, limit: int | None = None) -> BackfillReport:
    rows = (await db.execute(_candidate_query(user_id))).scalars().all()
    if limit is not None:
        rows = rows[:limit]
    rep = BackfillReport(candidates=len(rows))
    if not rows:
        return rep
    seen: dict[tuple[int | None, str], bool] = {}
    for m in rows:
        key = (m.user_id, (m.subject_name or "").strip().lower())
        if key in seen:
            rep.would_link += 1  # same subject as an earlier row this run
            continue
        eid = await _existing_person_entity_id(db, m.subject_name, m.user_id)
        seen[key] = eid is not None
        if eid is not None:
            rep.would_link += 1
        else:
            rep.would_create += 1
    rep.distinct_subjects = len(seen)
    return rep


async def run_backfill(db: AsyncSession, user_id: int | None = None, limit: int | None = None) -> BackfillReport:
    rows = (await db.execute(_candidate_query(user_id))).scalars().all()
    if limit is not None:
        rows = rows[:limit]
    rep = BackfillReport(candidates=len(rows))
    if not rows:
        return rep
    # Monotonic serial id snapshot to detect freshly-created entities.
    pre_max = (await db.execute(select(func.max(KGEntity.id)))).scalar() or 0
    kg = KnowledgeGraphService(db)
    for m in rows:
        try:
            ent = await kg.resolve_entity(
                m.subject_name, "person", m.user_id,
                create_tier=m.circle_tier,
                match_entity_type=True,
                use_embedding=False,  # bare names embed-conflate across people (Jutta→Anna); exact/surface/create only
            )
            m.subject_entity_id = ent.id
            await db.commit()  # per-row: create+link atomic, no orphan on crash
            rep.linked += 1
            if ent.id > pre_max:
                rep.created += 1
                logger.info("created entity #%d %r (tier=%d) for memory #%d",
                            ent.id, logged_content(m.subject_name), m.circle_tier, m.id)
        except Exception as e:
            await db.rollback()
            rep.failed += 1
            logger.warning("skip memory #%d subject=%r: %s", m.id, logged_content(m.subject_name), error_text_without_content(e))
    return rep
