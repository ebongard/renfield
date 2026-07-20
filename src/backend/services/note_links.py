"""Note ``[[bidirectional links]]`` on the KG substrate (Phase 4B.2).

A note mirrors to a ``kg_entities`` row (``entity_type='note'``) and each
``[[Target]]`` in its body becomes a ``kg_relations`` row
(``predicate='note_link'``). This reuses the KG machinery — ``resolve_entity``
(dedup + surface-forms + dangling-stub creation), ``save_relation`` (dedup + the
edge atom + tier = MIN(endpoints)), the tier cascade, graph_expansion, and the
3D ``/wissen`` graph — instead of a parallel ``note_links`` table.

Scoping is the safety property: ``resolve_entity(user_id=owner, entity_type="note",
match_entity_type=True)`` confines exact/surface-form matches to the SAME owner's
note-typed entities (see knowledge_graph_service.resolve_entity Step 1), so two
owners' notes titled "Roadmap" never collide, and a note "Bonn" never links the
place entity. **note→note only** for 4B.2 (note→any-KG-entity is v2). Links are
synced idempotently on every save; a stale link (removed from the body) is
deactivated, not deleted.
"""
from __future__ import annotations

import re
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from models.database import KGEntity, KGRelation, Note

# [[Target]] — no nested brackets; the inner text is the target note title.
_LINK_RE = re.compile(r"\[\[([^\]\[]+)\]\]")
_MAX_LINKS = 100
NOTE_LINK_PREDICATE = "note_link"
_NOTE_TYPE = "note"


def parse_links(body: str) -> list[str]:
    """Extract unique (case-insensitive) ``[[Target]]`` titles from a note body,
    in first-appearance order, capped."""
    seen: set[str] = set()
    out: list[str] = []
    for raw in _LINK_RE.findall(body or ""):
        title = raw.strip()
        key = title.lower()
        if title and key not in seen:
            seen.add(key)
            out.append(title)
            if len(out) >= _MAX_LINKS:
                break
    return out


async def _resolve_note_entity(kg, name: str, tier: int, eff_owner: int | None) -> KGEntity:
    """Resolve (or create) a note-mirror entity. use_embedding=False — notes
    resolve by exact title / surface-form, never fuzzy similarity."""
    return await kg.resolve_entity(
        name=name,
        entity_type=_NOTE_TYPE,
        user_id=eff_owner,
        create_tier=tier,
        match_entity_type=True,
        use_embedding=False,
    )


async def sync_note_links(db: AsyncSession, note: Note, *, owner_id: int | None) -> None:
    """Materialize the note's ``[[links]]`` as ``note_link`` relations, idempotently.

    Resolves the note's own entity + each target entity (dangling targets get a
    stub note-entity), saves a ``note_link`` edge to each, and deactivates any
    prior ``note_link`` whose target is no longer in the body. Caller commits.
    Best-effort: never raises into the note write path (links are additive).
    """
    from services.knowledge_graph_service import KnowledgeGraphService

    kg = KnowledgeGraphService(db)
    # Resolve the concrete owner ONCE and reuse it for every resolve_entity call.
    # resolve_entity's MATCH keys on the raw user_id (None → user_id IS NULL) but
    # its CREATE keys on _resolve_owner_user_id(user_id) (None → bootstrap admin) —
    # so passing the raw None in auth-off makes match miss the admin-owned row and
    # mint a DUPLICATE note-entity on every re-save (orphaning prior note_links).
    # Passing the already-resolved owner makes match + create agree.
    eff = await kg._resolve_owner_user_id(owner_id)
    src = await _resolve_note_entity(kg, note.title, note.circle_tier, eff)

    target_ids: set[int] = set()
    for title in parse_links(note.body or ""):
        if title.lower() == (note.title or "").lower():
            continue  # a note doesn't link to itself
        tgt = await _resolve_note_entity(kg, title, note.circle_tier, eff)
        if tgt.id == src.id:
            continue
        await kg.save_relation(
            subject_id=src.id, predicate=NOTE_LINK_PREDICATE, object_id=tgt.id,
            user_id=owner_id,
        )
        target_ids.add(tgt.id)

    # Deactivate stale outgoing links (removed from the body since last save).
    existing = (await db.execute(
        select(KGRelation).where(
            KGRelation.subject_id == src.id,
            KGRelation.predicate == NOTE_LINK_PREDICATE,
            KGRelation.is_active == True,  # noqa: E712
        )
    )).scalars().all()
    for rel in existing:
        if rel.object_id not in target_ids:
            rel.is_active = False
    await db.flush()


async def deactivate_note_links(db: AsyncSession, note: Note, *, owner_id: int | None) -> None:
    """On note delete: deactivate the note's OUTGOING ``note_link`` relations.

    The note-entity itself is left as a stub (incoming backlinks then resolve to
    a titled-but-note-less stub — Obsidian's dangling-link behavior; a reconciler
    can GC orphan note-entities later). Best-effort. Caller commits."""
    ent = await _find_note_entity(db, note.title, owner_id)
    if ent is None:
        return
    rels = (await db.execute(
        select(KGRelation).where(
            KGRelation.subject_id == ent.id,
            KGRelation.predicate == NOTE_LINK_PREDICATE,
            KGRelation.is_active == True,  # noqa: E712
        )
    )).scalars().all()
    for rel in rels:
        rel.is_active = False
    await db.flush()


async def _find_note_entity(
    db: AsyncSession, title: str, owner_id: int | None
) -> KGEntity | None:
    """Resolve an existing note-entity by exact title + owner WITHOUT creating."""
    conds = [
        func.lower(KGEntity.name) == (title or "").lower(),
        KGEntity.entity_type == _NOTE_TYPE,
        KGEntity.is_active == True,  # noqa: E712
        KGEntity.canonical_id.is_(None),
    ]
    if owner_id is not None:
        conds.append(KGEntity.user_id == owner_id)
    return (await db.execute(select(KGEntity).where(*conds).limit(1))).scalars().first()


async def outgoing_links(
    db: AsyncSession, note: Note, *, owner_id: int | None
) -> list[dict[str, Any]]:
    """The note's ``[[Target]]`` links (parsed from the body), each mapped to an
    existing note by title+owner (``note_id=None`` = a dangling link to a
    not-yet-written note)."""
    out: list[dict[str, Any]] = []
    for title in parse_links(note.body or ""):
        if title.lower() == (note.title or "").lower():
            continue
        nconds = [func.lower(Note.title) == title.lower()]
        if owner_id is not None:
            nconds.append(Note.owner_user_id == owner_id)
        tgt = (await db.execute(select(Note).where(*nconds).limit(1))).scalars().first()
        out.append({"title": title, "note_id": tgt.id if tgt else None})
    return out


async def backlinks(
    db: AsyncSession, note: Note, *, owner_id: int | None
) -> list[dict[str, Any]]:
    """Notes that link TO this note: active ``note_link`` relations whose object is
    this note's entity, mapped back to the source note (by title + owner)."""
    ent = await _find_note_entity(db, note.title, owner_id)
    if ent is None:
        return []
    rows = (await db.execute(
        select(KGEntity.name)
        .join(KGRelation, KGRelation.subject_id == KGEntity.id)
        .where(
            KGRelation.object_id == ent.id,
            KGRelation.predicate == NOTE_LINK_PREDICATE,
            KGRelation.is_active == True,  # noqa: E712
        )
    )).all()
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for (subj_name,) in rows:
        key = (subj_name or "").lower()
        if not subj_name or key in seen:
            continue
        seen.add(key)
        nconds = [func.lower(Note.title) == key]
        if owner_id is not None:
            nconds.append(Note.owner_user_id == owner_id)
        src_note = (await db.execute(select(Note).where(*nconds).limit(1))).scalars().first()
        out.append({"title": subj_name, "note_id": src_note.id if src_note else None})
    return out
