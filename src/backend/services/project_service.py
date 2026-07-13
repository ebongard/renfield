"""Project service — business-instance Phase 1.

Owns the one invariant that the CRUD route must not get wrong: **each Project
gets exactly ONE fresh KnowledgeBase**, owned by the project's creator and
tier-scoped to the project's ``circle_tier``. Meetings / timeline / minutes are
later phases (business-instance plan §7) and live
nowhere in here.

``KnowledgeBase.name`` is UNIQUE, so the per-project KB name embeds the freshly
minted project id (guaranteed unique) to avoid a collision on same-named projects.
"""
from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from models.database import Document, KnowledgeBase, Project


async def create_project(
    db: AsyncSession,
    *,
    name: str,
    description: str | None,
    owner_id: int | None,
    circle_tier: int,
    status: str = "active",
) -> Project:
    """Create a Project and its dedicated (1:1) KnowledgeBase, then link them.

    The project row is flushed first so its id can seed the unique KB name.
    Commits once at the end; the caller gets a refreshed row with
    ``knowledge_base_id`` populated.
    """
    project = Project(
        name=name,
        description=description,
        owner_id=owner_id,
        circle_tier=circle_tier,
        status=status,
    )
    db.add(project)
    await db.flush()  # assign project.id for the unique KB name

    # KnowledgeBase.name is String(255). The `#<id>` suffix is what guarantees
    # uniqueness, so cap ONLY the human part to keep the composed name within
    # the column limit — a 255-char project name must not overflow it (→ 500).
    prefix, suffix = "Projekt: ", f" #{project.id}"
    max_name = 255 - len(prefix) - len(suffix)
    kb = KnowledgeBase(
        name=f"{prefix}{name[:max_name]}{suffix}",
        description=f"Wissensbasis für Projekt '{name}'",
        owner_id=owner_id,
        default_circle_tier=circle_tier,
    )
    db.add(kb)
    await db.flush()  # assign kb.id

    project.knowledge_base_id = kb.id
    await db.commit()
    await db.refresh(project)
    return project


async def document_count_for_kb(db: AsyncSession, kb_id: int | None) -> int:
    """Count documents in a project's KnowledgeBase (0 when the KB is unset)."""
    if kb_id is None:
        return 0
    result = await db.execute(
        select(func.count(Document.id)).where(Document.knowledge_base_id == kb_id)
    )
    return int(result.scalar_one() or 0)


async def document_counts_for_kbs(
    db: AsyncSession, kb_ids: list[int | None]
) -> dict[int, int]:
    """Batch document counts for many KBs in ONE grouped query (avoids the N+1
    the list route would otherwise fire — one COUNT per project)."""
    ids = [k for k in kb_ids if k is not None]
    if not ids:
        return {}
    result = await db.execute(
        select(Document.knowledge_base_id, func.count(Document.id))
        .where(Document.knowledge_base_id.in_(ids))
        .group_by(Document.knowledge_base_id)
    )
    return {kb_id: int(count) for kb_id, count in result.all()}
