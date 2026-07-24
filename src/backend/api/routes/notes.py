"""Notes API — Phase 4B.1 (Notes as a 5th atom_type).

CRUD over hand-authored :class:`Note` rows. Each note is a first-class atom
(created via ``NoteService`` → ``AtomService``), so it is circle-tiered and
surfaces in the unified ``/brain`` search via ``polymorphic_atom_store``. This
route owns owner-scoping + the feature gate; the atom invariant + tier changes
live in ``services/note_service.py``.

Gated by ``settings.notes_enabled`` — every route 404s when off. Owner-scoped
when auth is on; auth-disabled single-user mode sees all (mirrors projects.py).
``[[link]]`` resolution + a markdown editor are Phase 4B.2 / 4B.3.
"""
from __future__ import annotations

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from models.database import Note, User
from services.auth_service import get_optional_user
from services.database import get_db
from services.note_service import (
    NoteTitleConflict,
    create_note,
    delete_note,
    embed_note_by_id,
    update_note,
)
from utils.config import settings

router = APIRouter()


def _require_enabled() -> None:
    if not settings.notes_enabled:
        raise HTTPException(status_code=404, detail="Notes feature is not enabled")


class NoteCreate(BaseModel):
    title: str = Field(..., min_length=1, max_length=255)
    body: str = Field(default="", max_length=100_000)
    circle_tier: int = Field(default=0, ge=0, le=4)
    project_id: int | None = None


class NoteUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=255)
    body: str | None = Field(default=None, max_length=100_000)
    circle_tier: int | None = Field(default=None, ge=0, le=4)


class NoteResponse(BaseModel):
    id: int
    title: str
    body: str
    circle_tier: int
    project_id: int | None
    owner_id: int | None
    atom_id: str
    created_at: str
    updated_at: str


def _to_response(n: Note) -> NoteResponse:
    return NoteResponse(
        id=n.id,
        title=n.title,
        body=n.body or "",
        circle_tier=n.circle_tier,
        project_id=n.project_id,
        owner_id=n.owner_user_id,
        atom_id=n.atom_id,
        created_at=n.created_at.isoformat() if n.created_at else "",
        updated_at=n.updated_at.isoformat() if n.updated_at else "",
    )


async def _get_owned_note(note_id: int, user: User | None, db: AsyncSession) -> Note:
    note = await db.get(Note, note_id)
    if note is None:
        raise HTTPException(status_code=404, detail="Note not found")
    if settings.auth_enabled:
        if not user:
            raise HTTPException(status_code=401, detail="Authentication required")
        if note.owner_user_id != user.id:
            raise HTTPException(status_code=404, detail="Note not found")
    return note


@router.post("", response_model=NoteResponse)
async def create_note_route(
    data: NoteCreate,
    background_tasks: BackgroundTasks,
    user: User | None = Depends(get_optional_user),
    db: AsyncSession = Depends(get_db),
) -> NoteResponse:
    """Create a note (+ its atom). 409 if the owner already has a note with this
    title (the title is the [[link]] key, unique per owner)."""
    _require_enabled()
    if settings.auth_enabled and not user:
        raise HTTPException(status_code=401, detail="Authentication required")
    try:
        note = await create_note(
            db,
            owner_id=user.id if user else None,
            title=data.title.strip(),
            body=data.body,
            circle_tier=data.circle_tier,
            project_id=data.project_id,
        )
        await db.commit()
    except NoteTitleConflict:
        await db.rollback()
        raise HTTPException(status_code=409, detail="A note with this title already exists")
    except IntegrityError:
        # Backstop for the concurrent-insert race the service check can't see.
        await db.rollback()
        raise HTTPException(status_code=409, detail="A note with this title already exists")
    await db.refresh(note)
    # Dense embedding runs AFTER the response (own session) — never on the request tx.
    if settings.notes_semantic_search_enabled:
        background_tasks.add_task(embed_note_by_id, note.id)
    return _to_response(note)


@router.get("", response_model=list[NoteResponse])
async def list_notes_route(
    limit: int = Query(200, ge=1, le=500),
    user: User | None = Depends(get_optional_user),
    db: AsyncSession = Depends(get_db),
) -> list[NoteResponse]:
    """List the caller's own notes, newest-updated first (owner-scoped)."""
    _require_enabled()
    stmt = select(Note).order_by(Note.updated_at.desc()).limit(limit)
    if settings.auth_enabled:
        if not user:
            raise HTTPException(status_code=401, detail="Authentication required")
        stmt = stmt.where(Note.owner_user_id == user.id)
    result = await db.execute(stmt)
    return [_to_response(n) for n in result.scalars().all()]


@router.get("/{note_id}", response_model=NoteResponse)
async def get_note_route(
    note_id: int,
    user: User | None = Depends(get_optional_user),
    db: AsyncSession = Depends(get_db),
) -> NoteResponse:
    """Get one note (owner-gated 404)."""
    _require_enabled()
    note = await _get_owned_note(note_id, user, db)
    return _to_response(note)


class NoteLink(BaseModel):
    title: str
    note_id: int | None  # None = a dangling [[link]] to a not-yet-written note


class NoteLinksResponse(BaseModel):
    outgoing: list[NoteLink]   # this note's [[Target]] links
    backlinks: list[NoteLink]  # notes that link TO this note


@router.get("/{note_id}/links", response_model=NoteLinksResponse)
async def get_note_links_route(
    note_id: int,
    user: User | None = Depends(get_optional_user),
    db: AsyncSession = Depends(get_db),
) -> NoteLinksResponse:
    """The note's outgoing ``[[links]]`` + its backlinks (Phase 4B.2, KG-substrate).
    Owner-gated 404."""
    _require_enabled()
    note = await _get_owned_note(note_id, user, db)
    from services.note_links import backlinks, outgoing_links

    owner = note.owner_user_id
    out = await outgoing_links(db, note, owner_id=owner)
    back = await backlinks(db, note, owner_id=owner)
    return NoteLinksResponse(
        outgoing=[NoteLink(**x) for x in out],
        backlinks=[NoteLink(**x) for x in back],
    )


@router.put("/{note_id}", response_model=NoteResponse)
async def update_note_route(
    note_id: int,
    data: NoteUpdate,
    background_tasks: BackgroundTasks,
    user: User | None = Depends(get_optional_user),
    db: AsyncSession = Depends(get_db),
) -> NoteResponse:
    """Patch a note's title/body/tier (owner-gated 404). 409 on a title clash."""
    _require_enabled()
    note = await _get_owned_note(note_id, user, db)
    try:
        await update_note(
            db, note,
            title=data.title.strip() if data.title is not None else None,
            body=data.body,
            circle_tier=data.circle_tier,
        )
        await db.commit()
    except NoteTitleConflict:
        await db.rollback()
        raise HTTPException(status_code=409, detail="A note with this title already exists")
    except IntegrityError:
        # Backstop for the concurrent-update race the service check can't see.
        await db.rollback()
        raise HTTPException(status_code=409, detail="A note with this title already exists")
    await db.refresh(note)
    # Re-embed off the request tx when the content (title/body) changed.
    if settings.notes_semantic_search_enabled and (data.title is not None or data.body is not None):
        background_tasks.add_task(embed_note_by_id, note.id)
    return _to_response(note)


@router.delete("/{note_id}")
async def delete_note_route(
    note_id: int,
    user: User | None = Depends(get_optional_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Delete a note + its atom (owner-gated 404)."""
    _require_enabled()
    note = await _get_owned_note(note_id, user, db)
    await delete_note(db, note)
    await db.commit()
    return {"status": "deleted", "id": note_id}
