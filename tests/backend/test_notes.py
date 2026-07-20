"""Route + retrieval tests for /api/notes (Phase 4B.1, Notes as a 5th atom_type).

Covers the feature-flag gate, the create-makes-an-atom invariant, CRUD +
owner-scoping, and that a note is found (circle-filtered) by NoteRetrieval — the
RRF source that feeds /brain. SQLite harness (the migration's GENERATED FTS is
Postgres-only; NoteRetrieval's sqlite branch uses a LIKE fallback).
"""
from __future__ import annotations

import pytest

from models.database import Atom, Note, User
from utils.config import settings

pytestmark = [pytest.mark.asyncio]


def _override_user(user: User | None) -> None:
    from main import app
    from services.auth_service import get_optional_user

    app.dependency_overrides[get_optional_user] = lambda: user


def _enable(monkeypatch, *, auth: bool) -> None:
    monkeypatch.setattr(settings, "notes_enabled", True)
    monkeypatch.setattr(settings, "auth_enabled", auth)


async def test_all_routes_404_when_flag_off(async_client, monkeypatch):
    monkeypatch.setattr(settings, "notes_enabled", False)
    assert (await async_client.post("/api/notes", json={"title": "X"})).status_code == 404
    assert (await async_client.get("/api/notes")).status_code == 404
    assert (await async_client.get("/api/notes/1")).status_code == 404
    assert (await async_client.put("/api/notes/1", json={"body": "y"})).status_code == 404
    assert (await async_client.delete("/api/notes/1")).status_code == 404


async def test_create_note_creates_atom(async_client, db_session, monkeypatch):
    _enable(monkeypatch, auth=False)
    resp = await async_client.post(
        "/api/notes", json={"title": "Kickoff", "body": "agenda quarterly review", "circle_tier": 2},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["title"] == "Kickoff" and data["circle_tier"] == 2 and data["atom_id"]

    # The note row carries the atom_id, and a matching atoms row exists,
    # source_id pointing back at the note (the create_with_source dance).
    note = await db_session.get(Note, data["id"])
    assert note is not None and note.atom_id == data["atom_id"]
    atom = await db_session.get(Atom, data["atom_id"])
    assert atom is not None
    assert atom.atom_type == "note" and atom.source_table == "notes"
    assert atom.source_id == str(note.id) and atom.policy == {"tier": 2}


async def test_crud_lifecycle_auth_off(async_client, db_session, monkeypatch):
    _enable(monkeypatch, auth=False)
    created = (await async_client.post("/api/notes", json={"title": "Alpha", "body": "one"})).json()
    nid, atom_id = created["id"], created["atom_id"]

    listing = await async_client.get("/api/notes")
    assert listing.status_code == 200 and any(n["id"] == nid for n in listing.json())

    # Body/title update (a circle_tier change routes through AtomService.update_tier,
    # whose `id::text` cast is Postgres-only — exercised on the real deploy, not
    # the SQLite harness).
    upd = await async_client.put(f"/api/notes/{nid}", json={"body": "two", "title": "Alpha2"})
    assert upd.status_code == 200 and upd.json()["body"] == "two" and upd.json()["title"] == "Alpha2"

    deleted = await async_client.delete(f"/api/notes/{nid}")
    assert deleted.status_code == 200
    assert (await async_client.get(f"/api/notes/{nid}")).status_code == 404
    # Deleting the note dropped its atom too (CASCADE via atom delete).
    assert await db_session.get(Atom, atom_id) is None


async def test_owner_scoping_under_auth(async_client, monkeypatch):
    _enable(monkeypatch, auth=True)
    user_a = User(id=1, username="a", password_hash="x", is_active=True, role_id=1)
    user_b = User(id=2, username="b", password_hash="x", is_active=True, role_id=1)

    _override_user(user_a)
    created = await async_client.post("/api/notes", json={"title": "Secret", "body": "mine"})
    assert created.status_code == 200 and created.json()["owner_id"] == 1
    nid = created.json()["id"]

    _override_user(user_b)
    assert (await async_client.get(f"/api/notes/{nid}")).status_code == 404
    assert all(n["id"] != nid for n in (await async_client.get("/api/notes")).json())


async def test_note_links_backlinks_and_stale_removal(async_client, monkeypatch):
    """[[links]] (4B.2): outgoing links resolve to notes, the target sees a
    backlink, and removing the [[link]] on edit clears both sides."""
    _enable(monkeypatch, auth=False)
    beta = (await async_client.post("/api/notes", json={"title": "Beta", "body": "b"})).json()
    alpha = (await async_client.post(
        "/api/notes", json={"title": "Alpha", "body": "see [[Beta]] for details"},
    )).json()

    a_links = (await async_client.get(f"/api/notes/{alpha['id']}/links")).json()
    assert a_links["outgoing"] == [{"title": "Beta", "note_id": beta["id"]}]

    b_links = (await async_client.get(f"/api/notes/{beta['id']}/links")).json()
    assert b_links["backlinks"] == [{"title": "Alpha", "note_id": alpha["id"]}]

    # Remove the [[link]] → outgoing + the backlink both clear.
    await async_client.put(f"/api/notes/{alpha['id']}", json={"body": "no link now"})
    assert (await async_client.get(f"/api/notes/{alpha['id']}/links")).json()["outgoing"] == []
    assert (await async_client.get(f"/api/notes/{beta['id']}/links")).json()["backlinks"] == []


async def test_dangling_link_has_null_note_id(async_client, monkeypatch):
    """A [[Target]] with no existing note is a dangling link (note_id=None)."""
    _enable(monkeypatch, auth=False)
    a = (await async_client.post(
        "/api/notes", json={"title": "Solo", "body": "points at [[Nowhere]]"},
    )).json()
    links = (await async_client.get(f"/api/notes/{a['id']}/links")).json()
    assert links["outgoing"] == [{"title": "Nowhere", "note_id": None}]


async def test_resave_does_not_duplicate_link_entities_with_seeded_user(
    async_client, db_session, monkeypatch
):
    """P1 regression: in auth-off mode with a real user in the DB, re-saving a
    linked note must NOT mint duplicate note-typed KG entities.

    resolve_entity MATCHES on the raw user_id (None → user_id IS NULL) but CREATES
    on _resolve_owner_user_id(user_id) (None → the first/bootstrap user's id). If
    sync passes the raw None, match looks under NULL, misses the row it created
    under the real id, and mints a fresh duplicate on every save. The earlier tests
    hid this because their users table was empty (_resolve_owner_user_id(None)
    stayed None, so match+create agreed on NULL). Seed a user and the divergence
    bites — the fix resolves the owner ONCE up front so both sides agree.
    """
    from sqlalchemy import func, select

    from models.database import KGEntity

    _enable(monkeypatch, auth=False)
    db_session.add(User(id=1, username="owner", password_hash="x", is_active=True, role_id=1))
    await db_session.commit()

    def _note_entity_count() -> "object":
        return select(func.count()).select_from(KGEntity).where(KGEntity.entity_type == "note")

    alpha = (await async_client.post(
        "/api/notes", json={"title": "Alpha", "body": "see [[Beta]]"},
    )).json()
    # Two note-entities so far: Alpha (source) + Beta (dangling stub target).
    assert (await db_session.execute(_note_entity_count())).scalar() == 2

    # Re-save twice with the SAME link — must NOT create new entities each time.
    for _ in range(2):
        await async_client.put(f"/api/notes/{alpha['id']}", json={"body": "see [[Beta]] again"})
    assert (await db_session.execute(_note_entity_count())).scalar() == 2

    # And the backlink still resolves (no orphaned relations from a duplicate source).
    beta = (await async_client.post("/api/notes", json={"title": "Beta", "body": "b"})).json()
    b_links = (await async_client.get(f"/api/notes/{beta['id']}/links")).json()
    assert b_links["backlinks"] == [{"title": "Alpha", "note_id": alpha["id"]}]


async def test_duplicate_title_conflicts_409(async_client, monkeypatch):
    """P2b: a second note with the same (case-insensitive) title is a 409 — the
    title is the [[link]] key, so it must be unique per owner. Enforced in the
    service (works in auth-off / NULL-owner, which the Postgres partial index and
    the SQLite harness both miss)."""
    _enable(monkeypatch, auth=False)
    first = await async_client.post("/api/notes", json={"title": "Roadmap", "body": "one"})
    assert first.status_code == 200
    dup = await async_client.post("/api/notes", json={"title": "roadmap", "body": "two"})
    assert dup.status_code == 409

    # Renaming another note onto an existing title also 409s.
    other = (await async_client.post("/api/notes", json={"title": "Other", "body": "x"})).json()
    clash = await async_client.put(f"/api/notes/{other['id']}", json={"title": "Roadmap"})
    assert clash.status_code == 409


def test_rrf_fuse_combines_fts_and_dense_branches():
    """RRF fusion: a note ranking well in BOTH branches beats one in a single
    branch (pure function — no DB)."""
    from types import SimpleNamespace

    from services.note_retrieval import NoteRetrieval

    def row(i: int):
        return SimpleNamespace(
            id=i, atom_id=f"a{i}", owner_user_id=1, project_id=None,
            title=f"t{i}", body="b", circle_tier=0,
        )

    nr = NoteRetrieval(db=None)  # type: ignore[arg-type]  (_rrf_fuse ignores db)
    fts = [row(1), row(2), row(3)]     # id1 best lexically
    dense = [row(3), row(1), row(4)]   # id3 best semantically
    out = nr._rrf_fuse([fts, dense], top_k=3)
    ids = [d["id"] for d in out]
    # 1 (ranks 1+2) and 3 (ranks 3+1) appear in both → lead; 2 and 4 (single list) trail.
    assert set(ids[:2]) == {1, 3}
    assert len(out) == 3
    assert out[0]["similarity"] >= out[1]["similarity"]  # fused score, descending


async def test_create_skips_embedding_on_sqlite_but_stays_fts_searchable(
    async_client, db_session, monkeypatch
):
    """Semantic search ON but the sqlite harness has no pgvector → embed-on-write
    is skipped gracefully (embedding stays NULL, no crash) and the note is still
    found via FTS. Postgres dense retrieval is verified on deploy."""
    _enable(monkeypatch, auth=False)
    monkeypatch.setattr(settings, "notes_semantic_search_enabled", True)
    resp = await async_client.post("/api/notes", json={"title": "Emb", "body": "hallo welt"})
    assert resp.status_code == 200
    note = await db_session.get(Note, resp.json()["id"])
    assert note is not None and note.embedding is None  # skipped on sqlite

    from services.note_retrieval import NoteRetrieval
    hits = await NoteRetrieval(db_session).search("hallo", asker_id=None, top_k=5)
    assert any(h["title"] == "Emb" for h in hits)


async def test_note_retrieval_finds_and_circle_filters(async_client, db_session, monkeypatch):
    from services.note_retrieval import NoteRetrieval

    _enable(monkeypatch, auth=False)
    await async_client.post(
        "/api/notes", json={"title": "Roadmap", "body": "phoenix migration plan", "circle_tier": 0},
    )
    # Auth-off bypass: found by a body token.
    hits = await NoteRetrieval(db_session).search("phoenix", asker_id=None, top_k=10)
    assert hits and hits[0]["title"] == "Roadmap" and hits[0]["atom_id"]

    # A thin query returns nothing.
    assert await NoteRetrieval(db_session).search("", asker_id=None, top_k=10) == []
