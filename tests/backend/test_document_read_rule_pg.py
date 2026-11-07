"""The REST knowledge routes read a document by the retrieval rule (#1469).

Before, two access models decided whether a user may see a document:
retrieval (the ``circle_sql`` document filter) and the REST routes (KB
permission levels in ``check_kb_access``). A household-tier document that
search returned to a household member answered 403 on the detail route, the
batch poll, search-in-document and the facts route; a ``?knowledge_base_id``
filter on the list and on ``/search`` refused him outright. And a document
moved across owners before #1460 was readable as owner by its OLD atom owner
in retrieval, while the per-document gate refused her.

Now one rule: ``services.document_access`` (the ``circle_sql`` document
filter, plus ``kb.all``; the KB's ``is_public`` flag opens no document). Write, reindex and delete stay KB-level permissions on top.

Real Postgres: every branch is Postgres SQL.
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

import api.routes.knowledge as knowledge_mod
from models.database import Atom, Document, DocumentFact, KnowledgeBase, Role, User
from services import kb_shares_service as ks
from services.rag_service import RAGService
from utils.config import settings

pytestmark = [pytest.mark.backend, pytest.mark.database]

TIER_SELF, TIER_HOUSEHOLD, TIER_PUBLIC = 0, 2, 4


async def _user(db: AsyncSession, name: str, permissions: list[str]) -> User:
    role = Role(name=f"role-{name}", permissions=permissions, is_system=False)
    db.add(role)
    await db.flush()
    user = User(username=name, password_hash="x", role_id=role.id)
    db.add(user)
    await db.commit()
    await db.refresh(user, attribute_names=["role"])
    return user


@pytest.fixture
async def people(db_session: AsyncSession, monkeypatch):
    """alice owns the KBs; bob is in her household circle (tier 2); carol is a
    stranger with kb.shared; dora holds kb.own only; admin holds kb.all."""
    monkeypatch.setattr(settings, "auth_enabled", True)

    async def _no_progress(*_args, **_kwargs):
        return None

    monkeypatch.setattr(knowledge_mod, "_augment_with_progress", _no_progress)
    alice = await _user(db_session, "alice", ["kb.own", "kb.shared"])
    bob = await _user(db_session, "bob", ["kb.own", "kb.shared"])
    carol = await _user(db_session, "carol", ["kb.own", "kb.shared"])
    dora = await _user(db_session, "dora", ["kb.own"])
    admin = await _user(db_session, "admin-x", ["kb.all"])
    await db_session.execute(
        text("INSERT INTO circle_memberships "
             "(circle_owner_id, member_user_id, dimension, value, granted_by, granted_at) "
             "VALUES (:o, :m, 'tier', :v, :o, NOW())"),
        {"o": alice.id, "m": bob.id, "v": str(TIER_HOUSEHOLD)},
    )
    await db_session.commit()
    return alice, bob, carol, dora, admin


async def _kb(db: AsyncSession, owner_id: int | None, name: str, *,
              tier: int = TIER_SELF, public: bool = False) -> int:
    kb = KnowledgeBase(name=name, is_active=True, owner_id=owner_id,
                       default_circle_tier=tier, is_public=public)
    db.add(kb)
    await db.commit()
    return kb.id


async def _doc(db: AsyncSession, kb_id: int, label: str) -> Document:
    """The real create path: atom owner = KB owner, tier = KB default; one chunk."""
    doc = await RAGService(db).create_document_record(
        file_path=f"/tmp/{label}.pdf", knowledge_base_id=kb_id,
        filename=f"{label}.pdf", file_hash=f"h-{label}",
    )
    await db.execute(
        text("INSERT INTO document_chunks (document_id, content, chunk_index, circle_tier) "
             "VALUES (:d, :c, 0, :t)"),
        {"d": doc.id, "c": f"Inhalt {label}", "t": doc.circle_tier},
    )
    doc.status = "completed"
    await db.commit()
    return doc


async def _retrieval_sees(db: AsyncSession, asker_id: int, doc_id: int) -> bool:
    """The chunk filter as rag_retrieval composes it."""
    from services.circle_sql import document_chunks_circles_filter

    clause, params = document_chunks_circles_filter(asker_id)
    return (await db.execute(
        text("SELECT 1 FROM document_chunks dc JOIN documents d ON d.id = dc.document_id "
             "LEFT JOIN knowledge_bases kb ON kb.id = d.knowledge_base_id "
             f"WHERE d.id = :id AND ({clause}) LIMIT 1"),
        {**params, "id": doc_id},
    )).first() is not None


async def _detail(db: AsyncSession, user: User | None, doc_id: int) -> int:
    """Status of GET /api/knowledge/documents/{id}."""
    try:
        await knowledge_mod.get_document(doc_id, rag=RAGService(db), user=user, db=db)
    except HTTPException as exc:
        return exc.status_code
    return 200


async def _batch(db: AsyncSession, user: User | None, ids: list[int]) -> set[int]:
    rows = await knowledge_mod.get_documents_batch(
        ids=",".join(map(str, ids)), rag=RAGService(db), user=user, db=db)
    return {r.id for r in rows}


async def _in_doc_search(db: AsyncSession, user: User, doc_id: int, monkeypatch) -> int:
    async def _no_search(self, **_kwargs):
        return []

    monkeypatch.setattr(RAGService, "search_by_document", _no_search)
    try:
        await knowledge_mod.search_in_document(
            doc_id, query="x", top_k=5, rag=RAGService(db), user=user, db=db)
    except HTTPException as exc:
        return exc.status_code
    return 200


async def _facts(db: AsyncSession, user: User, doc_id: int) -> int:
    from api.routes.atoms import get_document_facts

    try:
        await get_document_facts(doc_id, db=db, current_user=user)
    except HTTPException as exc:
        return exc.status_code
    return 200


async def _listed(db: AsyncSession, user: User, kb_id: int | None) -> set[int]:
    rows = await knowledge_mod.list_documents(
        knowledge_base_id=kb_id, status=None, q=None, sort=None, order="desc",
        limit=100, offset=0, rag=RAGService(db), user=user, db=db)
    return {r.id for r in rows}


class TestHouseholdTierReadsLikeRetrieval:
    async def test_member_opens_what_search_returns(self, db_session, people, monkeypatch):
        alice, bob, carol, _, _ = people
        haus = await _kb(db_session, alice.id, "Haus", tier=TIER_HOUSEHOLD)
        doc = await _doc(db_session, haus, "nebenkosten")

        # Retrieval returns it to bob (tier reach), not to carol.
        assert await _retrieval_sees(db_session, bob.id, doc.id)
        assert not await _retrieval_sees(db_session, carol.id, doc.id)

        # Before #1469 every one of these was 403 for bob: he has no KB share.
        assert await _detail(db_session, bob, doc.id) == 200
        assert await _batch(db_session, bob, [doc.id]) == {doc.id}
        assert await _in_doc_search(db_session, bob, doc.id, monkeypatch) == 200
        assert await _facts(db_session, bob, doc.id) == 200
        assert await _listed(db_session, bob, haus) == {doc.id}

        # The stranger gets none of it.
        assert await _detail(db_session, carol, doc.id) == 403
        assert await _batch(db_session, carol, [doc.id]) == set()
        assert await _in_doc_search(db_session, carol, doc.id, monkeypatch) == 403
        assert await _facts(db_session, carol, doc.id) == 403
        assert await _listed(db_session, carol, haus) == set()

    async def test_reads_do_not_open_writes(self, db_session, people):
        """Reach is a read; reindex/delete stay KB-level permissions."""
        from api.routes.knowledge import check_document_access

        alice, bob, _, _, _ = people
        haus = await _kb(db_session, alice.id, "Haus", tier=TIER_HOUSEHOLD)
        doc = await _doc(db_session, haus, "nebenkosten")
        assert await check_document_access(doc, bob, "read", db_session)
        assert not await check_document_access(doc, bob, "write", db_session)
        assert not await check_document_access(doc, bob, "delete", db_session)

    async def test_kb_filtered_list_shows_only_reached_documents(self, db_session, people):
        """Bob reaches one household document of a KB that also holds a tier-self
        one: the KB filter lists the one, not the KB (no share), and no 403."""
        alice, bob, _, _, _ = people
        haus = await _kb(db_session, alice.id, "Haus", tier=TIER_HOUSEHOLD)
        offen = await _doc(db_session, haus, "offen")
        privat = await _doc(db_session, haus, "privat")
        await db_session.execute(text(
            "UPDATE documents SET circle_tier = 0 WHERE id = :d"), {"d": privat.id})
        await db_session.execute(text(
            "UPDATE document_chunks SET circle_tier = 0 WHERE document_id = :d"),
            {"d": privat.id})
        await db_session.commit()

        assert await _listed(db_session, bob, haus) == {offen.id}
        assert await _listed(db_session, alice, haus) == {offen.id, privat.id}
        assert await _batch(db_session, bob, [offen.id, privat.id]) == {offen.id}

    async def test_kb_filter_without_kb_own_stays_refused(self, db_session, people):
        alice, _, _, _, _ = people
        haus = await _kb(db_session, alice.id, "Haus", tier=TIER_HOUSEHOLD)
        reader = await _user(db_session, "reader", ["kb.none"])
        with pytest.raises(HTTPException) as exc:
            await _listed(db_session, reader, haus)
        assert exc.value.status_code == 403
        with pytest.raises(HTTPException) as exc:
            await _listed(db_session, reader, None)
        assert exc.value.status_code == 403

    async def test_a_kb_share_still_lists_the_whole_kb(self, db_session, people):
        alice, _, carol, _, _ = people
        haus = await _kb(db_session, alice.id, "Haus")
        a = await _doc(db_session, haus, "a")
        b = await _doc(db_session, haus, "b")
        await ks.share_kb(db_session, haus, carol.id, "read", granted_by=alice.id)
        assert await _listed(db_session, carol, haus) == {a.id, b.id}


class TestDocumentGrantOpensItsDocumentOnly:
    async def test_grant_holder_reads_the_document_not_its_kb(self, db_session, people):
        alice, _, carol, _, _ = people
        haus = await _kb(db_session, alice.id, "Haus")
        granted = await _doc(db_session, haus, "freigegeben")
        other = await _doc(db_session, haus, "anderes")
        await db_session.execute(
            text("INSERT INTO atom_explicit_grants "
                 "(atom_id, granted_to_user_id, permission_level, granted_by, granted_at) "
                 "VALUES (:a, :to, 'read', :by, NOW())"),
            {"a": granted.atom_id, "to": carol.id, "by": alice.id},
        )
        await db_session.commit()

        assert await _retrieval_sees(db_session, carol.id, granted.id)
        assert await _detail(db_session, carol, granted.id) == 200
        assert await _detail(db_session, carol, other.id) == 403
        assert await _listed(db_session, carol, haus) == {granted.id}


class TestTwoOwnerDocumentHasOneRule:
    async def test_old_atom_owner_neither_retrieves_nor_opens_it(self, db_session, people):
        """Moved across owners before #1460: atom owner Alice, KB owner Carol.
        The KB that names an owner decides alone — in retrieval AND in REST."""
        from services.circle_resolver import CircleResolver, atom_from_orm

        alice, _, carol, _, _ = people
        privat = await _kb(db_session, alice.id, "Privat")
        carols = await _kb(db_session, carol.id, "Carols")
        doc = await _doc(db_session, privat, "doppelt")
        doc.knowledge_base_id = carols  # the pre-#1460 move: only the KB changes
        await db_session.commit()

        assert not await _retrieval_sees(db_session, alice.id, doc.id)
        assert await _detail(db_session, alice, doc.id) == 403
        atom = (await db_session.execute(
            select(Atom).where(Atom.atom_id == doc.atom_id))).scalar_one()
        assert not await CircleResolver(db_session).can_access_atom(
            alice.id, atom_from_orm(atom))

        assert await _retrieval_sees(db_session, carol.id, doc.id)
        assert await _detail(db_session, carol, doc.id) == 200

    async def test_kb_less_and_ownerless_kb_keep_the_atom_owner(self, db_session, people):
        alice, _, carol, _, _ = people
        geteilt = await _kb(db_session, None, "Geteilt")
        ownerless = await _doc(db_session, geteilt, "ohne-eigner")
        loose = await _doc(db_session, geteilt, "lose")
        loose.knowledge_base_id = None
        await db_session.commit()
        await db_session.execute(text(
            "UPDATE atoms SET owner_user_id = :u WHERE atom_id IN (:a, :b)"),
            {"u": alice.id, "a": ownerless.atom_id, "b": loose.atom_id})
        await db_session.commit()

        for d in (ownerless, loose):
            assert await _retrieval_sees(db_session, alice.id, d.id)
            assert await _detail(db_session, alice, d.id) == 200
            assert not await _retrieval_sees(db_session, carol.id, d.id)
            assert await _detail(db_session, carol, d.id) == 403


class TestPermissionsOnTop:
    async def test_kb_all_reads_everything(self, db_session, people):
        alice, _, _, _, admin = people
        privat = await _kb(db_session, alice.id, "Privat")
        doc = await _doc(db_session, privat, "geheim")
        assert await _detail(db_session, admin, doc.id) == 200
        assert await _batch(db_session, admin, [doc.id]) == {doc.id}

    async def test_auth_off_reads_everything_and_no_user_reads_nothing(
        self, db_session, people, monkeypatch,
    ):
        from services.document_access import readable_document_ids

        alice, _, _, _, _ = people
        privat = await _kb(db_session, alice.id, "Privat")
        doc = await _doc(db_session, privat, "geheim")
        assert await readable_document_ids(db_session, [doc], None) == set()
        assert await _batch(db_session, None, [doc.id]) == set()
        assert await readable_document_ids(db_session, [], alice) == set()
        monkeypatch.setattr(settings, "auth_enabled", False)
        assert await readable_document_ids(db_session, [doc], None) == {doc.id}

    async def test_public_kb_flag_opens_no_document(self, db_session, people, monkeypatch):
        """``is_public`` + ``kb.shared`` opened every document of the KB over
        REST while retrieval did not return them. Retired (#1469, owner
        decision 2026-11-06): the flag opens nothing a search would not."""
        alice, _, carol, dora, _ = people
        offen = await _kb(db_session, alice.id, "Offen", public=True)
        doc = await _doc(db_session, offen, "aushang")

        assert not await _retrieval_sees(db_session, carol.id, doc.id)
        for reader in (carol, dora):                     # kb.shared, kb.own only
            assert await _detail(db_session, reader, doc.id) == 403
            assert await _batch(db_session, reader, [doc.id]) == set()
            assert await _in_doc_search(db_session, reader, doc.id, monkeypatch) == 403
            assert await _facts(db_session, reader, doc.id) == 403
            assert await _listed(db_session, reader, offen) == set()

    async def test_public_kb_at_public_tier_reads_like_retrieval(self, db_session, people):
        """What retrieval returns stays open: a public-tier document of a
        public KB is read by ``kb.shared`` and ``kb.own`` holders alike."""
        alice, _, carol, dora, _ = people
        offen = await _kb(db_session, alice.id, "Offen", tier=TIER_PUBLIC, public=True)
        doc = await _doc(db_session, offen, "aushang")

        assert await _retrieval_sees(db_session, carol.id, doc.id)
        for reader in (carol, dora):
            assert await _detail(db_session, reader, doc.id) == 200
            assert await _listed(db_session, reader, offen) == {doc.id}


class TestNoKbRungReadsNothing:
    @pytest.mark.parametrize("permissions", [["kb.none"], [], ["ha.full"]])
    async def test_guest_and_kiosk_open_no_public_document(
        self, db_session, people, monkeypatch, permissions,
    ):
        """``Gast`` (kb.none) and the ``Kiosk`` device role (no KB rung) are
        refused like on ``/search`` and the list — even a public-tier document
        that the circle filter alone would hand to any asker."""
        alice, _, _, _, _ = people
        haus = await _kb(db_session, alice.id, "Haus", tier=TIER_PUBLIC)
        doc = await _doc(db_session, haus, "aushang")
        guest = await _user(db_session, f"guest-{len(permissions)}-{'-'.join(permissions)}",
                            permissions)
        assert await _retrieval_sees(db_session, guest.id, doc.id)  # tier 4 alone

        assert await _detail(db_session, guest, doc.id) == 403
        assert await _batch(db_session, guest, [doc.id]) == set()
        assert await _in_doc_search(db_session, guest, doc.id, monkeypatch) == 403
        assert await _facts(db_session, guest, doc.id) == 403

    async def test_a_kb_share_does_not_lift_the_rung_gate_on_the_list(self, db_session, people):
        """A share granted to a Gast must not list the KB he may not read."""
        alice, _, _, _, _ = people
        haus = await _kb(db_session, alice.id, "Haus")
        await _doc(db_session, haus, "a")
        guest = await _user(db_session, "guest-shared", ["kb.none"])
        await ks.share_kb(db_session, haus, guest.id, "read", granted_by=alice.id)

        with pytest.raises(HTTPException) as exc:
            await _listed(db_session, guest, haus)
        assert exc.value.status_code == 403

    async def test_a_kb_rung_still_reads_the_public_document(self, db_session, people):
        alice, _, _, dora, _ = people
        haus = await _kb(db_session, alice.id, "Haus", tier=TIER_PUBLIC)
        doc = await _doc(db_session, haus, "aushang")
        assert await _detail(db_session, dora, doc.id) == 200


class TestSearchWithKbFilter:
    async def test_no_kb_level_refusal_the_search_filters_per_chunk(
        self, db_session, people, monkeypatch,
    ):
        """``/search`` with a KB filter no longer 403s a caller without a KB
        share: it hands his id to rag.search, whose chunk filter decides."""
        from api.routes.knowledge_schemas import SearchRequest

        alice, bob, _, _, _ = people
        haus = await _kb(db_session, alice.id, "Haus", tier=TIER_HOUSEHOLD)
        calls: list[dict] = []

        async def _search(self, **kwargs):
            calls.append(kwargs)
            return []

        monkeypatch.setattr(RAGService, "search", _search)
        await knowledge_mod.search_knowledge_get(
            q="miete", top_k=5, knowledge_base_id=haus, threshold=None,
            rag=RAGService(db_session), user=bob, db=db_session)
        await knowledge_mod.search_knowledge(
            SearchRequest(query="miete", knowledge_base_id=haus),
            rag=RAGService(db_session), user=bob, db=db_session)
        assert [(c["knowledge_base_id"], c["user_id"]) for c in calls] == [
            (haus, bob.id), (haus, bob.id)]


async def test_facts_route_admits_a_household_member_to_a_fact(db_session, people):
    """The facts of a household document reach the member through the route."""
    from api.routes.atoms import get_document_facts
    from services.atom_service import AtomService

    alice, bob, _, _, _ = people
    haus = await _kb(db_session, alice.id, "Haus", tier=TIER_HOUSEHOLD)
    doc = await _doc(db_session, haus, "vertrag")
    svc = AtomService(db_session)
    atom_id = await svc.create_with_source(
        atom_type="document_fact", owner_user_id=alice.id, tier=TIER_HOUSEHOLD)
    fact = DocumentFact(document_id=doc.id, category="universal", kind="vertragsnummer",
                        value="V-1", atom_id=atom_id, circle_tier=TIER_HOUSEHOLD)
    db_session.add(fact)
    await db_session.flush()
    await svc.finalize_source_id(atom_id, fact.id)
    await db_session.commit()

    rows = await get_document_facts(doc.id, db=db_session, current_user=bob)
    assert [r.value for r in rows] == ["V-1"]
