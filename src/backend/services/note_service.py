"""Note writer (Phase 4B.1).

Creates/updates/deletes :class:`Note` rows as first-class atoms — every note is
born with an :class:`Atom` via ``AtomService.create_with_source`` +
``finalize_source_id`` (the ``notes.atom_id`` FK is NOT NULL, same chicken-and-egg
as ``document_facts`` / ``conversation_memories``). Never a direct INSERT.

The route owns owner-scoping + the feature gate; this owns the atom invariant +
tier changes. ``[[link]]`` resolution onto the KG substrate is Phase 4B.2 — not
here. FTS ``search_vector`` is a GENERATED column, so writes never touch it.
"""
from __future__ import annotations

from loguru import logger
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from models.database import ATOM_TYPE_NOTE, Note
from services.atom_service import AtomService
from utils.config import settings


class NoteTitleConflict(Exception):
    """The owner already has a note with this (case-insensitive) title. The
    title is the [[link]] key, so it must resolve to ONE note per owner — the
    Postgres unique index treats a NULL owner (auth-off) as distinct and the
    sqlite harness builds no index at all, so uniqueness is enforced HERE too."""


async def _title_taken(
    db: AsyncSession, *, title: str, owner_id: int | None, exclude_id: int | None = None
) -> bool:
    conds = [func.lower(Note.title) == title.lower()]
    conds.append(Note.owner_user_id.is_(None) if owner_id is None else Note.owner_user_id == owner_id)
    if exclude_id is not None:
        conds.append(Note.id != exclude_id)
    row = (await db.execute(select(Note.id).where(*conds).limit(1))).first()
    return row is not None


async def _sync_links_best_effort(db: AsyncSession, note: Note, owner_id: int | None) -> None:
    """Re-materialize the note's [[links]] on the KG substrate (4B.2), inside a
    SAVEPOINT so a KG hiccup rolls back only the link sync — never the note write
    (which is already flushed). Best-effort: additive, re-synced on next save."""
    from services.note_links import sync_note_links

    try:
        async with db.begin_nested():
            await sync_note_links(db, note, owner_id=owner_id)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"note {note.id}: [[link]] sync failed (note saved anyway): {e}")


async def _embed_note_best_effort(db: AsyncSession, note: Note) -> None:
    """Compute + store the note's dense embedding from title+body (Postgres only).

    Best-effort: an unreachable embed model leaves ``embedding`` NULL and the FTS
    branch still covers the note. Skipped on sqlite (no pgvector — the column is
    Text) and when ``notes_semantic_search_enabled`` is off. The value is written
    on the same flush the caller later commits."""
    if not settings.notes_semantic_search_enabled:
        return
    if db.bind is None or db.bind.dialect.name != "postgresql":
        return
    text_in = f"{note.title}\n{note.body or ''}".strip()
    if not text_in:
        return
    try:
        from utils.llm_client import get_embed_client
        client = get_embed_client()
        resp = await client.embeddings(model=settings.ollama_embed_model, prompt=text_in)
        note.embedding = resp.embedding
        await db.flush()
    except Exception as e:  # noqa: BLE001
        logger.warning(f"note {note.id}: embedding failed (FTS still covers it): {e}")


async def create_note(
    db: AsyncSession,
    *,
    owner_id: int | None,
    title: str,
    body: str,
    circle_tier: int,
    project_id: int | None = None,
) -> Note:
    """Create a note + its atom in one transaction. Caller commits.

    ``owner_id`` may be None only in auth-disabled single-user mode; the atom
    requires an owner, so we fall back to 0 there (mirrors how other single-user
    atom writers seed ownership).
    """
    if await _title_taken(db, title=title, owner_id=owner_id):
        raise NoteTitleConflict(title)
    atom_svc = AtomService(db)
    atom_id = await atom_svc.create_with_source(
        atom_type=ATOM_TYPE_NOTE,
        owner_user_id=owner_id if owner_id is not None else 0,
        tier=circle_tier,
    )
    note = Note(
        owner_user_id=owner_id,
        project_id=project_id,
        title=title,
        body=body,
        atom_id=atom_id,
        circle_tier=circle_tier,
    )
    db.add(note)
    await db.flush()
    await atom_svc.finalize_source_id(atom_id, note.id)
    await _embed_note_best_effort(db, note)
    await _sync_links_best_effort(db, note, owner_id)
    return note


async def update_note(
    db: AsyncSession,
    note: Note,
    *,
    title: str | None = None,
    body: str | None = None,
    circle_tier: int | None = None,
    project_id: int | None = None,
    project_id_set: bool = False,
) -> Note:
    """Patch a note's content/tier. Tier changes route through
    ``AtomService.update_tier`` so the atom policy + denormalized column stay in
    lockstep. ``project_id_set`` distinguishes "clear the project" (None) from
    "leave unchanged". Caller commits."""
    if title is not None and title.lower() != (note.title or "").lower():
        if await _title_taken(db, title=title, owner_id=note.owner_user_id, exclude_id=note.id):
            raise NoteTitleConflict(title)
    if title is not None:
        note.title = title
    if body is not None:
        note.body = body
    if project_id_set:
        note.project_id = project_id
    if circle_tier is not None and circle_tier != note.circle_tier:
        await AtomService(db).update_tier(note.atom_id, {"tier": circle_tier})
        # update_tier writes notes.circle_tier via its generic single-row UPDATE,
        # but the in-session ORM object is stale — refresh it to match.
        await db.refresh(note, attribute_names=["circle_tier"])
    await db.flush()
    # Re-embed + re-sync links when the content (title/body) changed.
    if title is not None or body is not None:
        await _embed_note_best_effort(db, note)
        await _sync_links_best_effort(db, note, note.owner_user_id)
    return note


async def delete_note(db: AsyncSession, note: Note) -> None:
    """Delete a note AND its atom explicitly (not via DB CASCADE — SQLite doesn't
    enforce FK cascades by default, and dialect-dependence is fragile). The note
    row goes first (it FK-references the atom), then the atoms row (which also
    drops any ``atom_explicit_grants`` via their own CASCADE on Postgres). Caller
    commits."""
    from sqlalchemy import delete as sa_delete

    from models.database import Atom

    # Deactivate the note's outgoing [[links]] first (best-effort, own savepoint).
    try:
        async with db.begin_nested():
            from services.note_links import deactivate_note_links
            await deactivate_note_links(db, note, owner_id=note.owner_user_id)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"note {note.id}: link teardown failed (deleting anyway): {e}")

    atom_id = note.atom_id
    await db.delete(note)
    await db.flush()
    await db.execute(sa_delete(Atom).where(Atom.atom_id == atom_id))
