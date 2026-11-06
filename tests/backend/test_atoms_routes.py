"""Route-level tests for the Schicht A fact endpoints on /api/atoms.

HTTP-boundary surface for Finding 4: the facts route's 404 / 403 / 200-[] /
200-with-facts gating and the obligations list endpoint (+ due_before filter,
+ route ordering so /obligations isn't captured by /{atom_id}).

Postgres-backed (per the "test against real Postgres" rule): get_db is pointed
at pg_db_session. The fact routes are read-only, so the fixture's outer-txn
rollback keeps tests isolated. Skips when RENFIELD_TEST_PG_URL is unset.
"""
from __future__ import annotations

import datetime as dt

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from models.database import Atom, Document, DocumentFact, Role, User
from utils.config import settings

pytestmark = [pytest.mark.postgres, pytest.mark.asyncio]


_seq = 0


async def _mk_user(session: AsyncSession, username: str) -> User:
    role = Role(name=f"{username}-role", description="t", permissions=[])
    session.add(role)
    await session.flush()
    user = User(username=username, password_hash="x", is_active=True, role_id=role.id)
    session.add(user)
    await session.flush()
    # The auth dependencies load the role eagerly (selectinload); the facts
    # gate reads user.get_permissions() like every knowledge route.
    await session.refresh(user, attribute_names=["role"])
    return user


async def _mk_doc_with_atom(session: AsyncSession, owner_id: int, *, tier: int = 0) -> Document:
    global _seq
    _seq += 1
    atom_id = f"00000000-0000-0000-0000-{_seq:012d}"
    # Atom first (documents.atom_id is a FK into atoms); source_id is patched to
    # the real doc id after the doc flush. uq_atoms_source needs it unique, so
    # the placeholder is the atom_id itself.
    atom = Atom(
        atom_id=atom_id, atom_type="kb_document", source_table="documents",
        source_id=atom_id, owner_user_id=owner_id, policy={"tier": tier},
    )
    session.add(atom)
    await session.flush()
    doc = Document(
        knowledge_base_id=None, filename="d.pdf", file_path="/x/d.pdf",
        status="completed", circle_tier=tier, atom_id=atom_id,
    )
    session.add(doc)
    await session.flush()
    atom.source_id = str(doc.id)
    await session.flush()
    return doc


async def _mk_fact(session: AsyncSession, *, doc_id: int, owner_id: int, **kw) -> int:
    global _seq
    _seq += 1
    atom_id = f"00000000-0000-0000-0000-{_seq:012d}"
    session.add(Atom(
        atom_id=atom_id, atom_type="document_fact", source_table="document_facts",
        source_id=f"f-{_seq}", owner_user_id=owner_id, policy={"tier": kw.get("tier", 0)},
    ))
    await session.flush()
    fact = DocumentFact(
        document_id=doc_id, atom_id=atom_id,
        category=kw.get("category", "universal"), kind=kw.get("kind", "issuer"),
        value=kw.get("value", "V"), normalized_value=kw.get("normalized_value"),
        excerpt=kw.get("excerpt"), obligation_date=kw.get("obligation_date"),
        amount_value=kw.get("amount_value"), amount_currency=kw.get("amount_currency"),
        legal_gate=kw.get("legal_gate", False), source=kw.get("source", "deterministic"),
        circle_tier=kw.get("tier", 0),
    )
    session.add(fact)
    await session.flush()
    return fact.id


@pytest.fixture
async def pg_app(pg_db_session: AsyncSession):
    """FastAPI app with get_db pointed at the real-Postgres test session."""
    from main import app
    from services.database import get_db

    async def _override_db():
        yield pg_db_session

    app.dependency_overrides[get_db] = _override_db
    yield app
    app.dependency_overrides.clear()


def _auth_as(app, user: User) -> None:
    from services.auth_service import get_current_user, get_user_or_default
    app.dependency_overrides[get_user_or_default] = lambda: user
    app.dependency_overrides[get_current_user] = lambda: user


async def _client(app) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


# =========================================================================== #
# GET /api/atoms/documents/{id}/facts
# =========================================================================== #
class TestDocumentFactsRoute:
    async def test_200_with_facts(self, pg_app, pg_db_session):
        owner = await _mk_user(pg_db_session, "facts-owner")
        doc = await _mk_doc_with_atom(pg_db_session, owner.id)
        await _mk_fact(pg_db_session, doc_id=doc.id, owner_id=owner.id, value="Finanzamt NRW")
        _auth_as(pg_app, owner)
        async with await _client(pg_app) as c:
            resp = await c.get(f"/api/atoms/documents/{doc.id}/facts")
        assert resp.status_code == 200
        body = resp.json()
        assert [f["value"] for f in body] == ["Finanzamt NRW"]

    async def test_200_empty_when_factless(self, pg_app, pg_db_session):
        owner = await _mk_user(pg_db_session, "factless-owner")
        doc = await _mk_doc_with_atom(pg_db_session, owner.id)
        _auth_as(pg_app, owner)
        async with await _client(pg_app) as c:
            resp = await c.get(f"/api/atoms/documents/{doc.id}/facts")
        assert resp.status_code == 200
        assert resp.json() == []

    async def test_404_when_document_missing(self, pg_app, pg_db_session):
        owner = await _mk_user(pg_db_session, "missing-doc-owner")
        _auth_as(pg_app, owner)
        async with await _client(pg_app) as c:
            resp = await c.get("/api/atoms/documents/99999999/facts")
        assert resp.status_code == 404

    async def test_403_when_no_access(self, pg_app, pg_db_session, monkeypatch):
        monkeypatch.setattr(settings, "auth_enabled", True)
        owner = await _mk_user(pg_db_session, "secret-owner")
        other = await _mk_user(pg_db_session, "outsider")
        doc = await _mk_doc_with_atom(pg_db_session, owner.id, tier=0)  # self-tier
        await _mk_fact(pg_db_session, doc_id=doc.id, owner_id=owner.id, value="Secret")
        _auth_as(pg_app, other)
        async with await _client(pg_app) as c:
            resp = await c.get(f"/api/atoms/documents/{doc.id}/facts")
        assert resp.status_code == 403


# =========================================================================== #
# GET /api/atoms/obligations
# =========================================================================== #
class TestObligationsRoute:
    async def test_200_list_sorted(self, pg_app, pg_db_session):
        owner = await _mk_user(pg_db_session, "ob-owner")
        doc = await _mk_doc_with_atom(pg_db_session, owner.id)
        await _mk_fact(pg_db_session, doc_id=doc.id, owner_id=owner.id,
                       category="obligation", kind="zahlung", value="Late",
                       obligation_date=dt.date(2026, 7, 1))
        await _mk_fact(pg_db_session, doc_id=doc.id, owner_id=owner.id,
                       category="obligation", kind="zahlung", value="Soon",
                       obligation_date=dt.date(2026, 6, 10))
        _auth_as(pg_app, owner)
        async with await _client(pg_app) as c:
            resp = await c.get("/api/atoms/obligations")
        assert resp.status_code == 200
        assert [f["value"] for f in resp.json()] == ["Soon", "Late"]

    async def test_due_before_filter(self, pg_app, pg_db_session):
        owner = await _mk_user(pg_db_session, "ob-owner2")
        doc = await _mk_doc_with_atom(pg_db_session, owner.id)
        await _mk_fact(pg_db_session, doc_id=doc.id, owner_id=owner.id,
                       category="obligation", kind="zahlung", value="Late",
                       obligation_date=dt.date(2026, 7, 1))
        await _mk_fact(pg_db_session, doc_id=doc.id, owner_id=owner.id,
                       category="obligation", kind="zahlung", value="Soon",
                       obligation_date=dt.date(2026, 6, 10))
        _auth_as(pg_app, owner)
        async with await _client(pg_app) as c:
            resp = await c.get("/api/atoms/obligations", params={"due_before": "2026-06-20"})
        assert resp.status_code == 200
        assert [f["value"] for f in resp.json()] == ["Soon"]

    async def test_offset_pages_the_stable_order(self, pg_app, pg_db_session):
        """D9 "Mehr laden": offset pages further into the soonest-first order
        without skipping or repeating (the (obligation_date, id) sort is total)."""
        owner = await _mk_user(pg_db_session, "ob-offset")
        doc = await _mk_doc_with_atom(pg_db_session, owner.id)
        for i, day in enumerate([10, 11, 12]):
            await _mk_fact(pg_db_session, doc_id=doc.id, owner_id=owner.id,
                           category="obligation", kind="zahlung", value=f"f{i}",
                           obligation_date=dt.date(2026, 6, day))
        _auth_as(pg_app, owner)
        async with await _client(pg_app) as c:
            page1 = await c.get("/api/atoms/obligations", params={"limit": 2, "offset": 0})
            page2 = await c.get("/api/atoms/obligations", params={"limit": 2, "offset": 2})
        assert [f["value"] for f in page1.json()] == ["f0", "f1"]
        assert [f["value"] for f in page2.json()] == ["f2"]

    async def test_negative_offset_rejected(self, pg_app, pg_db_session):
        owner = await _mk_user(pg_db_session, "ob-negoffset")
        _auth_as(pg_app, owner)
        async with await _client(pg_app) as c:
            resp = await c.get("/api/atoms/obligations", params={"offset": -1})
        assert resp.status_code == 400

    async def test_obligations_not_captured_by_atom_id_route(self, pg_app, pg_db_session):
        """Route ordering guard: /obligations must hit the list endpoint, not
        GET /{atom_id} (which would 404 'Atom not found' for atom_id=obligations).
        """
        owner = await _mk_user(pg_db_session, "ob-owner3")
        _auth_as(pg_app, owner)
        async with await _client(pg_app) as c:
            resp = await c.get("/api/atoms/obligations")
        assert resp.status_code == 200
        assert resp.json() == []  # list shape, not a 404 atom error
