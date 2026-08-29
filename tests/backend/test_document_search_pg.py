"""Real-Postgres tests for the document search — FTS name ranking + the
circle-visibility gate (D2). The sqlite suite (test_document_search.py) covers
the ILIKE path/route/reachability; FTS and circles are Postgres-only.

create_all gives `documents.search_vector` a PLAIN tsvector (not the migration's
GENERATED column), so these tests populate it manually with the same multilingual
expression the migration uses.
"""
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from models.database import Atom, Document, KnowledgeBase, Role, User
from services.document_search import search_documents
from services.fts_languages import build_generated_tsvector_expression

pytestmark = pytest.mark.postgres

_seq = 0

_TSV_EXPR = build_generated_tsvector_expression(
    "(coalesce(generated_title, '') || ' ' || coalesce(title, '') || ' ' || coalesce(filename, ''))"
)


async def _user(db: AsyncSession, name: str) -> User:
    role = Role(name=f"{name}_role")
    db.add(role)
    await db.flush()
    u = User(username=name, email=f"{name}@ex.test", password_hash="x", role_id=role.id, is_active=True)
    db.add(u)
    await db.flush()
    return u


async def _kb(db: AsyncSession, owner: User) -> KnowledgeBase:
    global _seq
    _seq += 1
    kb = KnowledgeBase(name=f"Docs KB {_seq}", is_active=True, owner_id=owner.id)
    db.add(kb)
    await db.flush()
    return kb


async def _doc(db: AsyncSession, kb, owner, *, generated_title: str, tier: int = 0) -> Document:
    global _seq
    _seq += 1
    atom_id = f"00000000-0000-0000-0000-{_seq:012d}"
    db.add(Atom(atom_id=atom_id, atom_type="kb_document", source_table="documents",
                source_id=f"doc-{_seq}", owner_user_id=owner.id, policy={"tier": tier}))
    await db.flush()
    doc = Document(filename=f"f{_seq}.pdf", file_path=f"/x/f{_seq}.pdf", status="completed",
                   knowledge_base_id=kb.id, circle_tier=tier, atom_id=atom_id,
                   generated_title=generated_title)
    db.add(doc)
    await db.flush()
    await db.execute(text("UPDATE atoms SET source_id = :s WHERE atom_id = :a"),
                     {"s": str(doc.id), "a": atom_id})
    return doc


async def _populate_fts(db: AsyncSession, ids: list[int]) -> None:
    await db.execute(
        text(f"UPDATE documents SET search_vector = {_TSV_EXPR} WHERE id = ANY(:ids)"),
        {"ids": ids},
    )
    await db.flush()


async def test_fts_name_search_ranks_the_matching_doc(pg_db_session: AsyncSession):
    owner = await _user(pg_db_session, "ds_owner")
    kb = await _kb(pg_db_session, owner)
    hit = await _doc(pg_db_session, kb, owner, generated_title="Invoice Arkadon 2026")
    miss = await _doc(pg_db_session, kb, owner, generated_title="Rechnung Telekom")
    await _populate_fts(pg_db_session, [hit.id, miss.id])

    res = await search_documents(pg_db_session, "Arkadon", asker_id=owner.id, enforce_circles=True)
    assert [d.id for d in res] == [hit.id]


async def test_circle_gate_excludes_another_users_private_doc(pg_db_session: AsyncSession):
    """D2: the FTS name signal is not circle-filtered on its own; the fused-id
    gate must drop a document the asker can't see."""
    a = await _user(pg_db_session, "ds_a")
    b = await _user(pg_db_session, "ds_b")
    kb_a = await _kb(pg_db_session, a)
    kb_b = await _kb(pg_db_session, b)
    doc_a = await _doc(pg_db_session, kb_a, a, generated_title="Arkadon Invoice A", tier=0)  # A's private
    doc_b = await _doc(pg_db_session, kb_b, b, generated_title="Arkadon Contract B", tier=0)  # B's private
    await _populate_fts(pg_db_session, [doc_a.id, doc_b.id])

    # A searches "Arkadon" → only A's doc (B's tier-0 private doc is filtered out).
    as_a = await search_documents(pg_db_session, "Arkadon", asker_id=a.id, enforce_circles=True)
    assert [d.id for d in as_a] == [doc_a.id]
    # B sees only B's.
    as_b = await search_documents(pg_db_session, "Arkadon", asker_id=b.id, enforce_circles=True)
    assert [d.id for d in as_b] == [doc_b.id]


async def test_reachability_pg_beyond_recency(pg_db_session: AsyncSession):
    """An old doc (would be past a recency window) is found by FTS regardless."""
    owner = await _user(pg_db_session, "ds_reach")
    kb = await _kb(pg_db_session, owner)
    ids = []
    for i in range(6):
        d = await _doc(pg_db_session, kb, owner, generated_title=f"Rechnung {i}")
        ids.append(d.id)
    target = await _doc(pg_db_session, kb, owner, generated_title="Uniquetoken Vertrag")
    ids.append(target.id)
    await _populate_fts(pg_db_session, ids)

    res = await search_documents(pg_db_session, "Uniquetoken", asker_id=owner.id, enforce_circles=True)
    assert [d.id for d in res] == [target.id]
