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

    kb = KnowledgeBase(
        name=f"Projekt: {name} #{project.id}",
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
