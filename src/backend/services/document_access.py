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

- A caller without a KB rung above ``kb.none`` (the ``Gast`` role, the
  ``Kiosk`` device role, any role with no KB rung at all) reads NO document —
  the same gate ``/search`` and the document list apply
  (``holds_no_rung_above``). Without it a public-tier document opened to every
  token.
- ``kb.all`` (the admin rung) reads every document — it is a permission, not a
  circle branch, and it already lists, counts and deletes everything.

``is_public`` on the KB opens NO document: ``circle_sql`` knows no public-KB
branch (the flag only seeds ``default_circle_tier`` at creation), so a
``kb.shared`` holder reads exactly what retrieval returns him. The flag still
makes the KB itself visible in the KB list (#1469, owner decision 2026-11-06).

Write, reindex, tier and delete stay KB-level permissions on top
(``knowledge.check_document_access``).
"""
from __future__ import annotations

from collections.abc import Iterable

from sqlalchemy.ext.asyncio import AsyncSession

from models.database import Document, User
from models.permissions import Permission, has_permission, holds_no_rung_above
from services.document_search import reachable_document_ids
from utils.config import settings


async def readable_document_ids(
    db: AsyncSession, documents: Iterable[Document], user: User | None
) -> set[int]:
    """The ids among ``documents`` that ``user`` may read — one query for a
    whole page, not one per row. Auth off: all; no user (auth on): none."""
    docs = list(documents)
    if not settings.auth_enabled:
        return {d.id for d in docs}
    if user is None or not docs:
        return set()
    perms = user.get_permissions()
    if has_permission(perms, Permission.KB_ALL):
        return {d.id for d in docs}
    if holds_no_rung_above(perms, Permission.KB_NONE):
        return set()
    return await reachable_document_ids(db, [d.id for d in docs], user.id)


async def user_may_read_document(
    db: AsyncSession, document: Document, user: User | None
) -> bool:
    """Single-document form of :func:`readable_document_ids`."""
    return document.id in await readable_document_ids(db, [document], user)
