"""One rule for documents and their facts: the Python check and the SQL filter agree.

#1463: ``CircleResolver.can_access_atom`` decided a ``document_fact`` atom from the
fact atom alone (its owner, grants on the FACT atom, membership in the fact atom
owner's circles) and checked the KB share for ``kb_document`` atoms only, while
the retrieval filters (``circle_sql``) decide a fact by its parent document: KB
owner with the atom-owner fallback, grants on the DOCUMENT atom, the KB share and
membership in the KB owner's circles. A grantee of a shared KB found a fact by
search and was refused it by ``GET /api/atoms/{id}``.

With auth on, both atom types now go through the circle_sql predicate. This file
pins the expected answer per case AND asserts that the Python check gives exactly
the answer of the production SQL paths (``DocumentFactRetrieval.is_visible``,
the chunk filter as rag_retrieval composes it, the document-search gate).

Real Postgres: every branch here is Postgres SQL.
"""
from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from models.database import Atom, Document, DocumentFact, KnowledgeBase, Role, User
from services import kb_shares_service as ks
from utils.config import settings

pytestmark = [pytest.mark.backend, pytest.mark.database]

TIER_SELF = 0
TIER_HOUSEHOLD = 2
TIER_PUBLIC = 4


async def _user(db: AsyncSession, name: str) -> User:
    role = Role(name=f"role-{name}", permissions=["kb.own", "kb.shared"], is_system=False)
    db.add(role)
    await db.flush()
    user = User(username=name, password_hash="x", role_id=role.id)
    db.add(user)
    await db.commit()
    return user


@pytest.fixture
async def people(db_session: AsyncSession, monkeypatch):
    """alice owns the KBs; bob gets shares/grants; dave sits in alice's household
    circle; carol is a stranger; erin owns a drifted document atom."""
    monkeypatch.setattr(settings, "auth_enabled", True)
    names = ("alice", "bob", "carol", "dave", "erin")
    users = {n: await _user(db_session, n) for n in names}
    await db_session.execute(
        text("INSERT INTO circle_memberships "
             "(circle_owner_id, member_user_id, dimension, value, granted_by, granted_at) "
             "VALUES (:o, :m, 'tier', CAST('2' AS json), :o, NOW())"),
        {"o": users["alice"].id, "m": users["dave"].id},
    )
    await db_session.commit()
    from services.circle_resolver import CircleResolver
    CircleResolver.invalidate_for_membership(users["alice"].id, users["dave"].id)
    return users


async def _kb(db: AsyncSession, owner_id: int, name: str, tier: int = TIER_SELF) -> int:
    kb = KnowledgeBase(name=name, is_active=True, owner_id=owner_id, default_circle_tier=tier)
    db.add(kb)
    await db.commit()
    return kb.id


async def _doc(db: AsyncSession, kb_id: int | None, label: str, *, owner_id: int | None = None,
               tier: int | None = None) -> Document:
    """The real create path: atom owner = KB owner, tier = KB default (or the overrides)."""
    from services.rag_service import RAGService

    doc = await RAGService(db).create_document_record(
        file_path=f"/tmp/{label}.pdf", knowledge_base_id=kb_id,
        filename=f"{label}.pdf", file_hash=f"h-{label}",
        owner_user_id_override=owner_id, circle_tier_override=tier,
    )
    await db.execute(
        text("INSERT INTO document_chunks (document_id, content, chunk_index, circle_tier) "
             "VALUES (:d, :c, 0, :t)"),
        {"d": doc.id, "c": f"Inhalt {label}", "t": doc.circle_tier},
    )
    await db.commit()
    return doc


async def _fact(db: AsyncSession, doc: Document, *, tier: int | None = None) -> DocumentFact:
    """A fact through the extractor's own writer (AtomService, never a direct INSERT)."""
    from services.atom_service import AtomService
    from services.schicht_a_extractor import ExtractedFact, _store_fact

    owner = (await db.execute(
        select(Atom.owner_user_id).where(Atom.atom_id == doc.atom_id))).scalar_one()
    await _store_fact(
        db, AtomService(db), document_id=doc.id, owner_id=owner,
        fact=ExtractedFact(category="universal", kind="issuer", value=f"Absender {doc.id}"),
        tier=doc.circle_tier if tier is None else tier,
        tier_overridden=tier is not None,
    )
    await db.commit()
    return (await db.execute(
        select(DocumentFact).where(DocumentFact.document_id == doc.id))).scalars().one()


async def _grant_on_doc(db: AsyncSession, doc: Document, to_user: int, by_user: int) -> None:
    await db.execute(
        text("INSERT INTO atom_explicit_grants "
             "(atom_id, granted_to_user_id, permission_level, granted_by, granted_at) "
             "VALUES (:a, :to, 'read', :by, NOW())"),
        {"a": doc.atom_id, "to": to_user, "by": by_user},
    )
    await db.commit()


async def _python(db: AsyncSession, asker_id: int, atom_id: str) -> bool:
    from services.circle_resolver import CircleResolver, atom_from_orm

    atom = (await db.execute(select(Atom).where(Atom.atom_id == atom_id))).scalar_one()
    return await CircleResolver(db).can_access_atom(asker_id, atom_from_orm(atom))


async def _sql_fact(db: AsyncSession, asker_id: int, fact: DocumentFact) -> bool:
    from services.document_fact_retrieval import DocumentFactRetrieval

    return await DocumentFactRetrieval(db).is_visible(fact.id, asker_id)


async def _sql_doc(db: AsyncSession, asker_id: int, doc: Document) -> bool:
    """Both production document gates: chunks as rag_retrieval filters them, and
    the document-search gate. They must agree with each other as well."""
    from services.circle_sql import document_chunks_circles_filter
    from services.document_search import _visible_ids

    clause, params = document_chunks_circles_filter(asker_id)
    chunk_hit = (await db.execute(
        text("SELECT 1 FROM document_chunks dc JOIN documents d ON d.id = dc.document_id "
             "LEFT JOIN knowledge_bases kb ON kb.id = d.knowledge_base_id "
             f"WHERE d.id = :id AND ({clause}) LIMIT 1"),
        {**params, "id": doc.id},
    )).first() is not None
    gate_hit = doc.id in await _visible_ids(db, [doc.id], asker_id)
    assert chunk_hit == gate_hit, f"chunk filter and document gate disagree on doc {doc.id}"
    return chunk_hit


async def _assert_parity(db, users, cases):
    """``cases``: (label, document, fact, {name: (doc_expected, fact_expected)})."""
    for label, doc, fact, expected in cases:
        for name, (want_doc, want_fact) in expected.items():
            uid = users[name].id
            got = {
                "doc_py": await _python(db, uid, doc.atom_id),
                "doc_sql": await _sql_doc(db, uid, doc),
                "fact_py": await _python(db, uid, fact.atom_id),
                "fact_sql": await _sql_fact(db, uid, fact),
            }
            want = {"doc_py": want_doc, "doc_sql": want_doc,
                    "fact_py": want_fact, "fact_sql": want_fact}
            assert got == want, f"{label} / {name}: {got} != {want}"


# --------------------------------------------------------------------------- #
class TestPythonCheckAgreesWithSqlFilter:
    async def test_kb_share_opens_documents_and_facts_alike(self, db_session, people):
        """The issue's case: a share made before or after the upload."""
        u = people
        haus = await _kb(db_session, u["alice"].id, "Haus")
        before = await _doc(db_session, haus, "vorher")
        f_before = await _fact(db_session, before)
        await ks.share_kb(db_session, haus, u["bob"].id, "read", granted_by=u["alice"].id)
        later = await _doc(db_session, haus, "nachher")
        f_later = await _fact(db_session, later)

        reach = {"alice": (True, True), "bob": (True, True),
                 "carol": (False, False), "dave": (False, False)}
        await _assert_parity(db_session, u, [
            ("share/before", before, f_before, reach),
            ("share/after", later, f_later, reach),
        ])

    async def test_revoke_closes_both(self, db_session, people):
        u = people
        haus = await _kb(db_session, u["alice"].id, "Haus")
        await ks.share_kb(db_session, haus, u["bob"].id, "read", granted_by=u["alice"].id)
        doc = await _doc(db_session, haus, "brief")
        fact = await _fact(db_session, doc)
        assert await _python(db_session, u["bob"].id, fact.atom_id)

        await ks.revoke_kb_share(db_session, haus, u["bob"].id)

        await _assert_parity(db_session, u, [
            ("revoked", doc, fact, {"bob": (False, False), "alice": (True, True)}),
        ])

    async def test_legacy_grant_on_the_document_covers_its_facts(self, db_session, people):
        """A grant hangs on the DOCUMENT atom; the fact filter reads it there."""
        u = people
        haus = await _kb(db_session, u["alice"].id, "Haus")
        doc = await _doc(db_session, haus, "alt")
        fact = await _fact(db_session, doc)
        await _grant_on_doc(db_session, doc, u["bob"].id, u["alice"].id)

        await _assert_parity(db_session, u, [
            ("legacy grant", doc, fact, {"bob": (True, True), "carol": (False, False)}),
        ])

    async def test_tier_reach_and_public_fact_override(self, db_session, people):
        u = people
        haushalt = await _kb(db_session, u["alice"].id, "Haushalt", tier=TIER_HOUSEHOLD)
        privat = await _kb(db_session, u["alice"].id, "Privat")
        shared_doc = await _doc(db_session, haushalt, "strom")
        shared_fact = await _fact(db_session, shared_doc)
        private_doc = await _doc(db_session, privat, "arzt")
        public_fact = await _fact(db_session, private_doc, tier=TIER_PUBLIC)

        await _assert_parity(db_session, u, [
            ("household", shared_doc, shared_fact,
             {"dave": (True, True), "carol": (False, False), "bob": (False, False)}),
            # A fact re-tiered to public is more visible than its document.
            ("public fact on private doc", private_doc, public_fact,
             {"carol": (False, True), "dave": (False, True), "alice": (True, True)}),
        ])

    async def test_kb_owner_and_drifted_atom_owner_both_reach(self, db_session, people):
        """A document atom whose owner is not the KB owner (pre-#1460 moves): the
        filter lets both read it, and so does the Python check now."""
        u = people
        haus = await _kb(db_session, u["alice"].id, "Haus")
        doc = await _doc(db_session, haus, "drift", owner_id=u["erin"].id)
        fact = await _fact(db_session, doc)
        owner = (await db_session.execute(
            select(Atom.owner_user_id).where(Atom.atom_id == doc.atom_id))).scalar_one()
        assert owner == u["erin"].id

        await _assert_parity(db_session, u, [
            ("drift", doc, fact,
             {"alice": (True, True), "erin": (True, True), "carol": (False, False)}),
        ])

    async def test_kb_less_document_reaches_its_owner_only(self, db_session, people):
        """No KB: the owner branch falls back to the atom owner; the membership
        branch keys on kb.owner_id, which is NULL here, in both paths alike."""
        u = people
        doc = await _doc(db_session, None, "lose", owner_id=u["alice"].id, tier=TIER_HOUSEHOLD)
        fact = await _fact(db_session, doc)

        await _assert_parity(db_session, u, [
            ("no kb", doc, fact,
             {"alice": (True, True), "dave": (False, False), "carol": (False, False)}),
        ])

    async def test_atom_without_source_row_is_refused(self, db_session, people):
        """An orphan atom has nothing a retrieval path could return."""
        from services.atom_service import AtomService

        u = people
        atom_id = await AtomService(db_session).create_with_source(
            atom_type="kb_document", owner_user_id=u["alice"].id, tier=TIER_PUBLIC,
        )
        await db_session.commit()
        assert not await _python(db_session, u["alice"].id, atom_id)


class TestAuthOffKeepsTheBranchLogic:
    async def test_auth_off_does_not_use_the_sql_path(self, db_session, people, monkeypatch):
        """Auth off: the atom-based branches decide, unchanged. An orphan atom
        still reaches its owner (it would not through the SQL path)."""
        from services.atom_service import AtomService

        u = people
        atom_id = await AtomService(db_session).create_with_source(
            atom_type="document_fact", owner_user_id=u["alice"].id, tier=TIER_SELF,
        )
        await db_session.commit()
        monkeypatch.setattr(settings, "auth_enabled", False)

        assert await _python(db_session, u["alice"].id, atom_id)
        assert not await _python(db_session, u["carol"].id, atom_id)

    async def test_auth_off_branches_public_grant_share(self, db_session, people, monkeypatch):
        """The atom-based branches as before: public atom, a grant on the atom
        itself, a KB share for a ``kb_document`` atom, nothing for a stranger."""
        u = people
        haus = await _kb(db_session, u["alice"].id, "Haus")
        privat = await _kb(db_session, u["alice"].id, "Privat")
        shared = await _doc(db_session, haus, "geteilt")
        granted = await _doc(db_session, privat, "gewaehrt")
        closed = await _doc(db_session, privat, "zu")
        public_fact = await _fact(db_session, closed, tier=TIER_PUBLIC)
        await ks.share_kb(db_session, haus, u["bob"].id, "read", granted_by=u["alice"].id)
        await _grant_on_doc(db_session, granted, u["bob"].id, u["alice"].id)
        monkeypatch.setattr(settings, "auth_enabled", False)

        assert await _python(db_session, u["bob"].id, shared.atom_id)
        assert await _python(db_session, u["bob"].id, granted.atom_id)
        assert await _python(db_session, u["carol"].id, public_fact.atom_id)
        assert not await _python(db_session, u["bob"].id, closed.atom_id)


class TestOtherAtomTypesKeepTheBranchLogic:
    async def test_note_atom_membership_grant_and_caches(self, db_session, people):
        """Only documents and facts moved to the SQL path: any other atom type is
        still decided by the atom-based branches (membership, grant, caches)."""
        from services.atom_service import AtomService

        u = people
        svc = AtomService(db_session)
        household = await svc.create_with_source(
            atom_type="note", owner_user_id=u["alice"].id, tier=TIER_HOUSEHOLD,
        )
        private = await svc.create_with_source(
            atom_type="note", owner_user_id=u["alice"].id, tier=TIER_SELF,
        )
        await db_session.execute(
            text("INSERT INTO atom_explicit_grants "
                 "(atom_id, granted_to_user_id, permission_level, granted_by, granted_at) "
                 "VALUES (:a, :to, 'read', :by, NOW())"),
            {"a": private, "to": u["bob"].id, "by": u["alice"].id},
        )
        await db_session.commit()

        for _ in range(2):  # the second round is answered from the caches
            assert await _python(db_session, u["dave"].id, household)
            assert not await _python(db_session, u["dave"].id, private)
            assert not await _python(db_session, u["carol"].id, household)
            assert await _python(db_session, u["bob"].id, private)


class TestAtomRoute:
    async def test_grantee_of_a_shared_kb_opens_the_fact_atom(self, db_session, people):
        """``GET /api/atoms/{id}`` answered 404 for a fact the grantee found by search."""
        from main import app
        from services.auth_service import get_current_user, get_user_or_default
        from services.database import get_db

        u = people
        haus = await _kb(db_session, u["alice"].id, "Haus")
        await ks.share_kb(db_session, haus, u["bob"].id, "read", granted_by=u["alice"].id)
        doc = await _doc(db_session, haus, "rechnung")
        fact = await _fact(db_session, doc)

        async def _db():
            yield db_session

        async def _get(user: User, atom_id: str) -> int:
            app.dependency_overrides[get_db] = _db
            app.dependency_overrides[get_user_or_default] = lambda: user
            app.dependency_overrides[get_current_user] = lambda: user
            try:
                async with AsyncClient(transport=ASGITransport(app=app),
                                       base_url="http://test") as c:
                    return (await c.get(f"/api/atoms/{atom_id}")).status_code
            finally:
                app.dependency_overrides.clear()

        assert await _get(u["bob"], fact.atom_id) == 200
        assert await _get(u["bob"], doc.atom_id) == 200
        assert await _get(u["carol"], fact.atom_id) == 404
