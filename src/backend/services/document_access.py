"""Who may READ a document through the REST routes — the retrieval rule (#1469).

Until #1469 the knowledge routes decided document reads with KB permission
levels (``check_kb_access``) while retrieval and the atom resolver used the
``circle_sql`` document filter. A household-tier document that search returned
to a household member then answered 403 on ``GET /documents/{id}``, the batch
poll, search-in-document and the facts route.

Now a read is the SAME predicate retrieval applies
(``document_search.reachable_document_ids`` → ``documents_circles_filter``:
KB owner with the null-owner atom fallback, public tier, explicit grant on the
document, tier reach through the KB owner's circles, KB share), plus two REST
permissions on top:

- ``kb.all`` (the admin rung) reads every document — it is a permission, not a
  circle branch, and it already lists, counts and deletes everything.
- LEGACY, awaiting the owner's decision: a KB with ``is_public = true`` opens
  its documents to holders of ``kb.shared``. ``circle_sql`` knows no such
  branch (``is_public`` only seeds ``default_circle_tier``), so retrieval does
  not return those documents unless their tier is public. Kept so the fix does
  not silently narrow what ``kb.shared`` holders can open today; see
  ``docs/CIRCLES.md`` (#1469).

Write, reindex, tier and delete stay KB-level permissions on top
(``knowledge.check_document_access``).
"""
from __future__ import annotations

from collections.abc import Iterable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.database import Document, KnowledgeBase, User
from models.permissions import Permission, has_permission
from services.document_search import reachable_document_ids
from utils.config import settings


async def _public_kb_ids(db: AsyncSession, kb_ids: set[int]) -> set[int]:
    """The subset of ``kb_ids`` flagged ``is_public`` (the legacy branch)."""
    if not kb_ids:
        return set()
    rows = await db.execute(
        select(KnowledgeBase.id).where(
            KnowledgeBase.id.in_(kb_ids), KnowledgeBase.is_public.is_(True)
        )
    )
    return {r[0] for r in rows}


async def readable_document_ids(
    db: AsyncSession, documents: Iterable[Document], user: User | None
) -> set[int]:
    """The ids among ``documents`` that ``user`` may read — three queries at most
    for a whole page, not one per row. Auth off: all; no user (auth on): none."""
    docs = list(documents)
    if not settings.auth_enabled:
        return {d.id for d in docs}
    if user is None or not docs:
        return set()
    perms = user.get_permissions()
    if has_permission(perms, Permission.KB_ALL):
        return {d.id for d in docs}
    readable = await reachable_document_ids(db, [d.id for d in docs], user.id)
    if has_permission(perms, Permission.KB_SHARED):
        rest = [d for d in docs if d.id not in readable and d.knowledge_base_id]
        public = await _public_kb_ids(db, {d.knowledge_base_id for d in rest})
        readable |= {d.id for d in rest if d.knowledge_base_id in public}
    return readable


async def user_may_read_document(
    db: AsyncSession, document: Document, user: User | None
) -> bool:
    """Single-document form of :func:`readable_document_ids`."""
    return document.id in await readable_document_ids(db, [document], user)
