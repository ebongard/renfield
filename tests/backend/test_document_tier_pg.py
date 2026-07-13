"""Document tier-control endpoints against real Postgres.

The tier cascade (``AtomService.update_tier``) uses Postgres-only ``id::text``
syntax, so the PATCH/bulk/mint paths must run against real PG. Pure HTTP
validation (422/404/response-exposure) lives in ``test_document_tier.py``.

Drives the real endpoints through an ``AsyncClient`` whose ``get_db`` is bound
to the rolled-back ``pg_db_session`` (the endpoints' ``db.commit()`` is patched
to ``flush`` so writes stay inside the fixture's outer transaction).
"""
from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from models.database import Atom, Document, DocumentChunk, KnowledgeBase, Role, User

pytestmark = [pytest.mark.postgres, pytest.mark.asyncio]

_seq = 0


def _commit_as_flush(db, monkeypatch):
    """The endpoints call db.commit(); make it a flush so the pg_db_session
    outer transaction stays open and rolls back on teardown."""
    monkeypatch.setattr(db, "commit", db.flush)


@pytest.fixture
async def pg_client(pg_db_session, monkeypatch):
    from main import app
    from services.database import get_db

    _commit_as_flush(pg_db_session, monkeypatch)

    async def _override():
        yield pg_db_session

    app.dependency_overrides[get_db] = _override
    transport = ASGITransport(app=app)
    try:
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            yield client
    finally:
        app.dependency_overrides.pop(get_db, None)


async def _make_user(db: AsyncSession, name: str) -> User:
    role = Role(name=f"{name}_role")
    db.add(role)
    await db.flush()
    u = User(username=name, email=f"{name}@ex.test", password_hash="x",
             role_id=role.id, is_active=True)
    db.add(u)
    await db.flush()
    return u


async def _make_kb(db: AsyncSession, owner: User) -> KnowledgeBase:
    kb = KnowledgeBase(name="Tier KB", is_active=True, owner_id=owner.id)
    db.add(kb)
    await db.flush()
    return kb


async def _make_doc(
    db: AsyncSession, kb: KnowledgeBase, owner: User,
    *, tier: int = 0, chunks: int = 2, with_atom: bool = True,
) -> Document:
    global _seq
    _seq += 1
    atom_id = None
    if with_atom:
        atom_id = f"00000000-0000-0000-0000-{_seq:012d}"
        db.add(Atom(atom_id=atom_id, atom_type="kb_document", source_table="documents",
                    source_id=f"doc-{_seq}", owner_user_id=owner.id, policy={"tier": tier}))
        await db.flush()
    doc = Document(filename="d.pdf", file_path="/x/d.pdf", status="completed",
                   chunk_count=chunks, knowledge_base_id=kb.id, circle_tier=tier, atom_id=atom_id)
    db.add(doc)
    await db.flush()
    if with_atom:
        await db.execute(text("UPDATE atoms SET source_id = :s WHERE atom_id = :a"),
                         {"s": str(doc.id), "a": atom_id})
    for i in range(chunks):
        db.add(DocumentChunk(document_id=doc.id, content=f"c{i}", chunk_index=i,
                             chunk_type="paragraph", circle_tier=tier))
    await db.flush()
    return doc


async def _chunk_tiers(db: AsyncSession, doc_id: int) -> list[int]:
    return list((await db.execute(
        text("SELECT circle_tier FROM document_chunks WHERE document_id = :d"),
        {"d": doc_id})).scalars().all())


async def test_set_document_tier_cascades_to_chunks(pg_db_session, pg_client):
    owner = await _make_user(pg_db_session, "tc_cascade")
    kb = await _make_kb(pg_db_session, owner)
    doc = await _make_doc(pg_db_session, kb, owner, tier=0)

    resp = await pg_client.patch(f"/api/knowledge/documents/{doc.id}/tier", json={"tier": 4})

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["circle_tier"] == 4
    assert body["atom_id"]
    assert await _chunk_tiers(pg_db_session, doc.id) == [4, 4]


async def test_bulk_set_document_tier(pg_db_session, pg_client):
    owner = await _make_user(pg_db_session, "tc_bulk")
    kb = await _make_kb(pg_db_session, owner)
    docs = [await _make_doc(pg_db_session, kb, owner) for _ in range(3)]
    ids = [d.id for d in docs]

    resp = await pg_client.post("/api/knowledge/documents/tier",
                                json={"document_ids": ids, "tier": 4})

    assert resp.status_code == 200, resp.text
    assert resp.json()["updated_count"] == 3
    for d in docs:
        assert all(t == 4 for t in await _chunk_tiers(pg_db_session, d.id))


async def test_null_atom_document_mints_atom(pg_db_session, pg_client):
    owner = await _make_user(pg_db_session, "tc_mint")
    kb = await _make_kb(pg_db_session, owner)
    doc = await _make_doc(pg_db_session, kb, owner, with_atom=False)
    assert doc.atom_id is None

    resp = await pg_client.patch(f"/api/knowledge/documents/{doc.id}/tier", json={"tier": 3})

    assert resp.status_code == 200, resp.text
    assert resp.json()["atom_id"]
    await pg_db_session.refresh(doc)
    assert doc.atom_id is not None
    assert doc.circle_tier == 3
    assert all(t == 3 for t in await _chunk_tiers(pg_db_session, doc.id))
