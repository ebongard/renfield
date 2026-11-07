"""Moving and deleting documents of a knowledge base keeps ownership and atoms honest.

- #1462: a write grantee on Alice's shared KB could move one of her documents
  into a KB of his own. The document filters take the owner from ``kb.owner_id``,
  so he then read it as owner — even after Alice revoked the share. A move that
  hands a document to ANOTHER owner now needs the document's owner (or ``kb.all``).
- #1460: a move only changed ``documents.knowledge_base_id``. The atom kept the
  old owner (who kept reading it through the atom-owner fallback) and the old
  KB's tier. A move to another owner now re-homes the atom: owner + the target
  KB's default tier, cascaded to chunks, facts and fact atoms.
- #1461: deleting a document left its ``kb_document`` atom (and the grants on
  it) behind. It is purged in the same transaction now; existing orphans are
  found and purged by ``services.orphan_document_atoms`` (owner-run script).
- Delete in a shared KB is the owner's separate switch ``kb_shares.can_delete``
  (pc20261102), never a level; only the KB owner or kb.all sets it.

Real Postgres: the filters and the cascades are Postgres SQL.
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from models.database import Atom, Document, DocumentFact, KnowledgeBase, Role, User
from services import kb_shares_service as ks
from services.rag_service import RAGService
from utils.config import settings

pytestmark = [pytest.mark.backend, pytest.mark.database]

TIER_SELF = 0
TIER_HOUSEHOLD = 2
TIER_PUBLIC = 4


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
    """Alice and Bob are ordinary KB users (kb.own), admin holds kb.all. No
    circle memberships: only the owner branch and shares open a tier-0 row."""
    monkeypatch.setattr(settings, "auth_enabled", True)
    alice = await _user(db_session, "alice", ["kb.own", "kb.shared"])
    bob = await _user(db_session, "bob", ["kb.own", "kb.shared"])
    admin = await _user(db_session, "admin-x", ["kb.all"])
    return alice, bob, admin


async def _kb(db: AsyncSession, owner_id: int | None, name: str, tier: int = TIER_SELF) -> int:
    kb = KnowledgeBase(name=name, is_active=True, owner_id=owner_id, default_circle_tier=tier)
    db.add(kb)
    await db.commit()
    return kb.id


async def _add_doc(db: AsyncSession, kb_id: int, label: str, *,
                   status: str = "completed", file_path: str | None = None) -> Document:
    """The real create path: atom owner = KB owner, tier = KB default; one chunk.
    Then the state the worker leaves it in (``completed``) — the record starts
    ``pending``, and a move refuses a document still in processing."""
    doc = await RAGService(db).create_document_record(
        file_path=file_path or f"/tmp/{label}.pdf", knowledge_base_id=kb_id,
        filename=f"{label}.pdf", file_hash=f"h-{label}",
    )
    await db.execute(
        text("INSERT INTO document_chunks (document_id, content, chunk_index, circle_tier) "
             "VALUES (:d, :c, 0, :t)"),
        {"d": doc.id, "c": f"Inhalt {label}", "t": doc.circle_tier},
    )
    doc.status = status
    await db.commit()
    return doc


async def _add_fact(db: AsyncSession, doc: Document, kind: str, *,
                    tier: int | None = None) -> DocumentFact:
    """A Schicht-A fact through the sanctioned atom path. ``tier`` set = a per-fact
    override (``tier_overridden``), as the per-fact tier PATCH leaves it."""
    from services.atom_service import AtomService

    owner = (await db.execute(
        select(Atom.owner_user_id).where(Atom.atom_id == doc.atom_id))).scalar_one()
    fact_tier = doc.circle_tier if tier is None else tier
    svc = AtomService(db)
    atom_id = await svc.create_with_source(
        atom_type="document_fact", owner_user_id=owner, tier=fact_tier)
    fact = DocumentFact(
        document_id=doc.id, category="universal", kind=kind, value=f"Wert {kind}",
        atom_id=atom_id, circle_tier=fact_tier, tier_overridden=tier is not None,
    )
    db.add(fact)
    await db.flush()
    await svc.finalize_source_id(atom_id, fact.id)
    await db.commit()
    return fact


async def _grant(db: AsyncSession, atom_id: str, to_user: int, by_user: int) -> None:
    await db.execute(
        text("INSERT INTO atom_explicit_grants "
             "(atom_id, granted_to_user_id, permission_level, granted_by, granted_at) "
             "VALUES (:a, :to, 'read', :by, NOW())"),
        {"a": atom_id, "to": to_user, "by": by_user},
    )
    await db.commit()


async def _visible_docs(db: AsyncSession, asker_id: int) -> set[int]:
    """Document ids whose chunks pass the filter as rag_retrieval composes it."""
    from services.circle_sql import document_chunks_circles_filter

    clause, params = document_chunks_circles_filter(asker_id)
    rows = (await db.execute(
        text("SELECT DISTINCT d.id FROM document_chunks dc "
             "JOIN documents d ON d.id = dc.document_id "
             "LEFT JOIN knowledge_bases kb ON kb.id = d.knowledge_base_id "
             f"WHERE ({clause})"),
        params,
    )).all()
    return {r[0] for r in rows}


async def _resolver_sees(db: AsyncSession, asker_id: int, atom_id: str) -> bool:
    from services.circle_resolver import CircleResolver, atom_from_orm

    atom = (await db.execute(select(Atom).where(Atom.atom_id == atom_id))).scalar_one()
    return await CircleResolver(db).can_access_atom(asker_id, atom_from_orm(atom))


async def _atom(db: AsyncSession, atom_id: str) -> tuple[int, int] | None:
    """(owner, policy tier) of an atom, ``None`` when it is gone. Reads the DB,
    not the session's identity map (the move writes through SQL)."""
    row = (await db.execute(
        text("SELECT owner_user_id, (policy->>'tier')::int FROM atoms WHERE atom_id = :a"),
        {"a": atom_id},
    )).first()
    return None if row is None else (row[0], row[1])


async def _route_move(db: AsyncSession, user: User, doc_ids: list[int], target: int) -> dict:
    from api.routes.knowledge import move_documents
    from api.routes.knowledge_schemas import MoveDocumentsRequest

    req = MoveDocumentsRequest(document_ids=doc_ids, target_knowledge_base_id=target)
    return await move_documents(req, rag=RAGService(db), user=user, db=db)


async def _kb_of(db: AsyncSession, doc_id: int) -> int | None:
    return (await db.execute(
        text("SELECT knowledge_base_id FROM documents WHERE id = :d"), {"d": doc_id})).scalar()


# --------------------------------------------------------------------------- #1462
class TestMoveAcrossOwnersNeedsTheOwner:
    async def test_write_grantee_cannot_move_the_owners_document_into_his_kb(
        self, db_session, people,
    ):
        alice, bob, _ = people
        haus = await _kb(db_session, alice.id, "Haus")
        bobs = await _kb(db_session, bob.id, "Bobs")
        await ks.share_kb(db_session, haus, bob.id, "write", granted_by=alice.id)
        doc = await _add_doc(db_session, haus, "rechnung")

        with pytest.raises(HTTPException) as exc:
            await _route_move(db_session, bob, [doc.id], bobs)

        assert exc.value.status_code == 403
        assert await _kb_of(db_session, doc.id) == haus
        assert await _atom(db_session, doc.atom_id) == (alice.id, TIER_SELF)
        # Once Alice revokes the share, Bob reads nothing of hers.
        await ks.revoke_kb_share(db_session, haus, bob.id)
        assert doc.id not in await _visible_docs(db_session, bob.id)

    async def test_one_foreign_document_refuses_the_whole_batch(self, db_session, people):
        """Bob's own document in the same request does not carry Alice's along."""
        alice, bob, _ = people
        haus = await _kb(db_session, alice.id, "Haus")
        bobs = await _kb(db_session, bob.id, "Bobs")
        bobs_alt = await _kb(db_session, bob.id, "Bobs-alt")
        await ks.share_kb(db_session, haus, bob.id, "write", granted_by=alice.id)
        hers = await _add_doc(db_session, haus, "ihres")
        his = await _add_doc(db_session, bobs_alt, "seines")

        with pytest.raises(HTTPException) as exc:
            await _route_move(db_session, bob, [his.id, hers.id], bobs)

        assert exc.value.status_code == 403
        assert (await _kb_of(db_session, hers.id), await _kb_of(db_session, his.id)) == (
            haus, bobs_alt)

    async def test_write_grantee_may_move_within_the_owners_kbs(self, db_session, people):
        """Same owner on both sides: nothing changes hands, the share suffices."""
        alice, bob, _ = people
        haus = await _kb(db_session, alice.id, "Haus")
        werk = await _kb(db_session, alice.id, "Werkstatt")
        await ks.share_kb(db_session, haus, bob.id, "write", granted_by=alice.id)
        await ks.share_kb(db_session, werk, bob.id, "write", granted_by=alice.id)
        doc = await _add_doc(db_session, haus, "plan")

        result = await _route_move(db_session, bob, [doc.id], werk)

        assert result["moved_count"] == 1
        assert await _kb_of(db_session, doc.id) == werk
        assert await _atom(db_session, doc.atom_id) == (alice.id, TIER_SELF)

    async def test_owner_may_hand_her_document_to_a_kb_shared_with_her(self, db_session, people):
        alice, bob, _ = people
        privat = await _kb(db_session, alice.id, "Privat")
        bobs = await _kb(db_session, bob.id, "Bobs", tier=TIER_HOUSEHOLD)
        await ks.share_kb(db_session, bobs, alice.id, "write", granted_by=bob.id)
        doc = await _add_doc(db_session, privat, "uebergabe")

        result = await _route_move(db_session, alice, [doc.id], bobs)

        assert result["moved_count"] == 1
        assert await _atom(db_session, doc.atom_id) == (bob.id, TIER_HOUSEHOLD)

    async def test_admin_may_move_across_owners(self, db_session, people):
        alice, bob, admin = people
        haus = await _kb(db_session, alice.id, "Haus")
        bobs = await _kb(db_session, bob.id, "Bobs")
        doc = await _add_doc(db_session, haus, "akte")

        result = await _route_move(db_session, admin, [doc.id], bobs)

        assert result["moved_count"] == 1
        assert await _atom(db_session, doc.atom_id) == (bob.id, TIER_SELF)

    async def test_a_document_without_atom_takes_its_kb_owner_as_owner(self, db_session, people):
        """Legacy atom-less rows: the owner branch reads ``kb.owner_id``, so the
        gate does too."""
        alice, bob, _ = people
        haus = await _kb(db_session, alice.id, "Haus")
        bobs = await _kb(db_session, bob.id, "Bobs")
        await ks.share_kb(db_session, haus, bob.id, "write", granted_by=alice.id)
        legacy = Document(filename="alt.pdf", file_path="/tmp/alt.pdf", status="completed",
                          knowledge_base_id=haus, circle_tier=TIER_SELF)
        db_session.add(legacy)
        await db_session.commit()

        with pytest.raises(HTTPException) as exc:
            await _route_move(db_session, bob, [legacy.id], bobs)
        assert exc.value.status_code == 403

        # The KB owner is its owner and may hand it over (into a KB she may write).
        await ks.share_kb(db_session, bobs, alice.id, "write", granted_by=bob.id)
        assert (await _route_move(db_session, alice, [legacy.id], bobs))["moved_count"] == 1


# --------------------------------------------------------------------------- #1460
class TestMoveRehomesTheAtom:
    async def test_move_to_another_owner_rehomes_owner_and_tier_everywhere(
        self, db_session, people,
    ):
        alice, bob, _ = people
        haus = await _kb(db_session, alice.id, "Haus", tier=TIER_SELF)
        bobs = await _kb(db_session, bob.id, "Bobs", tier=TIER_HOUSEHOLD)
        doc = await _add_doc(db_session, haus, "wechsel")
        plain = await _add_fact(db_session, doc, "issuer")
        pinned = await _add_fact(db_session, doc, "steuernummer", tier=TIER_PUBLIC)
        assert await _resolver_sees(db_session, alice.id, doc.atom_id)  # warm the cache

        moved = await RAGService(db_session).move_documents([doc.id], bobs)

        assert moved == 1
        assert await _atom(db_session, doc.atom_id) == (bob.id, TIER_HOUSEHOLD)
        tiers = (await db_session.execute(text(
            "SELECT d.circle_tier, (SELECT array_agg(DISTINCT circle_tier) FROM document_chunks "
            "WHERE document_id = d.id) FROM documents d WHERE d.id = :d"), {"d": doc.id})).one()
        assert (tiers[0], tiers[1]) == (TIER_HOUSEHOLD, [TIER_HOUSEHOLD])
        # Every fact follows the new owner; a per-fact override set by the OLD
        # owner is reset to the new document tier (it was a choice about her circles).
        facts = (await db_session.execute(text(
            "SELECT f.id, f.circle_tier, f.tier_overridden FROM document_facts f "
            "WHERE f.document_id = :d ORDER BY f.id"), {"d": doc.id})).all()
        assert [(r[1], r[2]) for r in facts] == [(TIER_HOUSEHOLD, False)] * 2
        for fact in (plain, pinned):
            assert await _atom(db_session, fact.atom_id) == (bob.id, TIER_HOUSEHOLD)
        # The old owner no longer reads it — neither in SQL nor through the resolver.
        assert doc.id not in await _visible_docs(db_session, alice.id)
        assert not await _resolver_sees(db_session, alice.id, doc.atom_id)
        assert doc.id in await _visible_docs(db_session, bob.id)

    async def test_move_between_kbs_of_one_owner_keeps_the_tier(self, db_session, people):
        """Same owner: the document's tier is the owner's choice; filing it
        elsewhere does not widen or narrow it to the target KB's default."""
        alice, _, _ = people
        privat = await _kb(db_session, alice.id, "Privat", tier=TIER_SELF)
        familie = await _kb(db_session, alice.id, "Familie", tier=TIER_HOUSEHOLD)
        doc = await _add_doc(db_session, privat, "bleibt")
        fact = await _add_fact(db_session, doc, "issuer")

        assert await RAGService(db_session).move_documents([doc.id], familie) == 1

        assert await _atom(db_session, doc.atom_id) == (alice.id, TIER_SELF)
        assert await _atom(db_session, fact.atom_id) == (alice.id, TIER_SELF)
        doc_tier = (await db_session.execute(
            text("SELECT circle_tier FROM documents WHERE id = :d"), {"d": doc.id})).scalar()
        assert doc_tier == TIER_SELF

    async def test_move_into_an_ownerless_kb_keeps_the_owner(self, db_session, people):
        """No KB owner to hand it to: the atom owner stays (atom-owner fallback)."""
        alice, _, _ = people
        privat = await _kb(db_session, alice.id, "Privat")
        geteilt = await _kb(db_session, None, "Geteilt", tier=TIER_PUBLIC)
        doc = await _add_doc(db_session, privat, "ohne-eigner")

        assert await RAGService(db_session).move_documents([doc.id], geteilt) == 1

        assert await _atom(db_session, doc.atom_id) == (alice.id, TIER_SELF)
        assert doc.id in await _visible_docs(db_session, alice.id)


# --------------------------------------------------------------------------- #1461
class TestDeleteDocumentPurgesItsAtom:
    async def test_delete_purges_the_document_atom_its_grants_and_fact_atoms(
        self, db_session, people,
    ):
        alice, bob, _ = people
        haus = await _kb(db_session, alice.id, "Haus")
        doc = await _add_doc(db_session, haus, "weg")
        fact = await _add_fact(db_session, doc, "issuer")
        await _grant(db_session, doc.atom_id, bob.id, alice.id)

        assert await RAGService(db_session).delete_document(doc.id)

        assert await _atom(db_session, doc.atom_id) is None
        assert await _atom(db_session, fact.atom_id) is None
        grants = (await db_session.execute(text(
            "SELECT COUNT(*) FROM atom_explicit_grants WHERE atom_id = :a"),
            {"a": doc.atom_id})).scalar()
        assert grants == 0

    async def test_delete_through_the_route(self, db_session, people):
        from api.routes.knowledge import delete_document

        alice, _, _ = people
        haus = await _kb(db_session, alice.id, "Haus")
        doc = await _add_doc(db_session, haus, "route")

        await delete_document(doc.id, rag=RAGService(db_session), user=alice, db=db_session)

        assert await _atom(db_session, doc.atom_id) is None

    async def test_deleting_a_kb_purges_the_atoms_of_its_documents(self, db_session, people):
        alice, _, _ = people
        haus = await _kb(db_session, alice.id, "Haus")
        doc = await _add_doc(db_session, haus, "im-kb")
        fact = await _add_fact(db_session, doc, "issuer")

        assert await RAGService(db_session).delete_knowledge_base(haus)

        assert (await db_session.execute(
            text("SELECT COUNT(*) FROM documents WHERE id = :d"), {"d": doc.id})).scalar() == 0
        assert await _atom(db_session, doc.atom_id) is None
        assert await _atom(db_session, fact.atom_id) is None


class TestDeleteIsTheOwnersSwitch:
    """Delete in a shared KB = the share's ``can_delete``, whatever the level.

    ``check_kb_access`` used to map "delete" to the READ level: every grantee could
    delete the owner's documents and the whole KB."""

    async def _route_delete_doc(self, db, user, doc_id):
        from api.routes.knowledge import delete_document

        return await delete_document(doc_id, rag=RAGService(db), user=user, db=db)

    async def _route_delete_kb(self, db, user, kb_id):
        from api.routes.knowledge import delete_knowledge_base

        return await delete_knowledge_base(kb_id, rag=RAGService(db), user=user, db=db)

    async def test_owner_deletes(self, db_session, people):
        alice, _, _ = people
        haus = await _kb(db_session, alice.id, "Haus")
        doc = await _add_doc(db_session, haus, "eigen")
        await self._route_delete_doc(db_session, alice, doc.id)
        await self._route_delete_kb(db_session, alice, haus)
        assert await _kb_of(db_session, doc.id) is None

    async def test_kb_all_deletes(self, db_session, people):
        alice, _, admin = people
        haus = await _kb(db_session, alice.id, "Haus")
        doc = await _add_doc(db_session, haus, "fremd")
        await self._route_delete_doc(db_session, admin, doc.id)
        await self._route_delete_kb(db_session, admin, haus)
        assert await _atom(db_session, doc.atom_id) is None

    @pytest.mark.parametrize("level", ["read", "write", "admin"])
    async def test_no_level_without_the_switch_deletes(self, db_session, people, level):
        alice, bob, _ = people
        haus = await _kb(db_session, alice.id, "Haus")
        doc = await _add_doc(db_session, haus, "bleibt")
        await ks.share_kb(db_session, haus, bob.id, level, granted_by=alice.id)

        with pytest.raises(HTTPException) as exc:
            await self._route_delete_doc(db_session, bob, doc.id)
        assert exc.value.status_code == 403
        with pytest.raises(HTTPException) as exc:
            await self._route_delete_kb(db_session, bob, haus)
        assert exc.value.status_code == 403
        assert await _kb_of(db_session, doc.id) == haus

    async def test_read_share_with_the_switch_deletes(self, db_session, people):
        alice, bob, _ = people
        haus = await _kb(db_session, alice.id, "Haus")
        doc = await _add_doc(db_session, haus, "darf")
        await ks.share_kb(db_session, haus, bob.id, "read", granted_by=alice.id,
                          can_delete=True)

        await self._route_delete_doc(db_session, bob, doc.id)
        assert await _atom(db_session, doc.atom_id) is None

    @pytest.mark.parametrize("level", ["read", "write", "admin"])
    async def test_the_switch_never_deletes_the_kb(self, db_session, people, level):
        """The switch covers DOCUMENTS; the KB itself is the owner's (and kb.all's)."""
        from api.routes.knowledge import check_kb_access

        alice, bob, _ = people
        haus = await _kb(db_session, alice.id, "Haus")
        await ks.share_kb(db_session, haus, bob.id, level, granted_by=alice.id, can_delete=True)

        with pytest.raises(HTTPException) as exc:
            await self._route_delete_kb(db_session, bob, haus)
        assert exc.value.status_code == 403
        kb = (await db_session.execute(
            select(KnowledgeBase).where(KnowledgeBase.id == haus))).scalar_one()
        assert await check_kb_access(kb, bob, "delete", db_session)
        assert not await check_kb_access(kb, bob, "delete_kb", db_session)
        assert await check_kb_access(kb, alice, "delete_kb", db_session)

    async def test_a_legacy_document_grant_never_deletes(self, db_session, people):
        """Atom grants have no delete concept, and since #1446 no KB level
        either: an admin-level legacy grant neither writes nor deletes through
        the KB ACL (it opens its document via circle_sql only)."""
        from api.routes.knowledge import check_document_access

        alice, bob, _ = people
        haus = await _kb(db_session, alice.id, "Haus")
        doc = await _add_doc(db_session, haus, "alt")
        await db_session.execute(text(
            "INSERT INTO atom_explicit_grants (atom_id, granted_to_user_id, permission_level, "
            "granted_by, granted_at) VALUES (:a, :to, 'admin', :by, NOW())"),
            {"a": doc.atom_id, "to": bob.id, "by": alice.id})
        await db_session.commit()

        assert not await check_document_access(doc, bob, "write", db_session)
        assert not await check_document_access(doc, bob, "delete", db_session)

    async def test_unknown_action_is_denied(self, db_session, people):
        from api.routes.knowledge import check_kb_access

        alice, bob, _ = people
        haus = await _kb(db_session, alice.id, "Haus")
        await ks.share_kb(db_session, haus, bob.id, "admin", granted_by=alice.id,
                          can_delete=True)
        kb = (await db_session.execute(
            select(KnowledgeBase).where(KnowledgeBase.id == haus))).scalar_one()

        assert not await check_kb_access(kb, bob, "purge", db_session)

    async def test_kb_list_reports_the_delete_right(self, db_session, people):
        from api.routes.knowledge import list_knowledge_bases

        alice, bob, admin = people
        haus = await _kb(db_session, alice.id, "Haus")
        werk = await _kb(db_session, alice.id, "Werkstatt")
        await ks.share_kb(db_session, haus, bob.id, "read", granted_by=alice.id, can_delete=True)
        await ks.share_kb(db_session, werk, bob.id, "admin", granted_by=alice.id)

        def flags(rows):
            return {r.id: (r.can_delete, r.can_delete_kb) for r in rows if r.id in (haus, werk)}

        assert flags(await list_knowledge_bases(
            rag=RAGService(db_session), user=bob, db=db_session)) == {
            haus: (True, False), werk: (False, False)}
        assert flags(await list_knowledge_bases(
            rag=RAGService(db_session), user=alice, db=db_session)) == {
            haus: (True, True), werk: (True, True)}
        assert flags(await list_knowledge_bases(
            rag=RAGService(db_session), user=admin, db=db_session)) == {
            haus: (True, True), werk: (True, True)}
        # The batch helper on no KBs asks nothing.
        assert await ks.get_user_kb_delete_switches(db_session, bob.id, []) == set()


class TestWhoSetsTheDeleteSwitch:
    async def _share(self, db, actor, kb_id, target, level, can_delete=None):
        from api.routes.knowledge import share_knowledge_base
        from api.routes.knowledge_schemas import KBPermissionCreate

        body = {"user_id": target, "permission": level}
        if can_delete is not None:
            body["can_delete"] = can_delete
        return await share_knowledge_base(
            kb_id, KBPermissionCreate(**body), rag=RAGService(db), user=actor, db=db)

    async def _switch(self, db, kb_id, user_id):
        return (await db.execute(text(
            "SELECT can_delete FROM kb_shares WHERE kb_id = :k AND user_id = :u"),
            {"k": kb_id, "u": user_id})).scalar()

    async def test_owner_sets_and_clears_it(self, db_session, people):
        from api.routes.knowledge import list_kb_permissions

        alice, bob, _ = people
        haus = await _kb(db_session, alice.id, "Haus")

        resp = await self._share(db_session, alice, haus, bob.id, "read", can_delete=True)
        assert resp.can_delete is True
        assert await self._switch(db_session, haus, bob.id) is True
        listed = await list_kb_permissions(haus, rag=RAGService(db_session), user=alice,
                                           db=db_session)
        assert [(p.user_id, p.can_delete) for p in listed] == [(bob.id, True)]

        await self._share(db_session, alice, haus, bob.id, "read", can_delete=False)
        assert await self._switch(db_session, haus, bob.id) is False

    async def test_new_share_without_the_field_has_it_off(self, db_session, people):
        alice, bob, _ = people
        haus = await _kb(db_session, alice.id, "Haus")
        resp = await self._share(db_session, alice, haus, bob.id, "admin")
        assert resp.can_delete is False
        assert await self._switch(db_session, haus, bob.id) is False

    async def test_kb_all_sets_it(self, db_session, people):
        alice, bob, admin = people
        haus = await _kb(db_session, alice.id, "Haus")
        await self._share(db_session, admin, haus, bob.id, "write", can_delete=True)
        assert await self._switch(db_session, haus, bob.id) is True

    @pytest.mark.parametrize("value", [True, False])
    async def test_admin_grantee_may_not_name_the_switch(self, db_session, people, value):
        alice, bob, _ = people
        carol = await _user(db_session, "carol", ["kb.own", "kb.shared"])
        haus = await _kb(db_session, alice.id, "Haus")
        await ks.share_kb(db_session, haus, bob.id, "admin", granted_by=alice.id)
        await ks.share_kb(db_session, haus, carol.id, "read", granted_by=alice.id,
                          can_delete=True)

        for target in (bob.id, carol.id):  # neither for itself nor for others
            with pytest.raises(HTTPException) as exc:
                await self._share(db_session, bob, haus, target, "read", can_delete=value)
            assert exc.value.status_code == 403
        assert await self._switch(db_session, haus, bob.id) is False
        assert await self._switch(db_session, haus, carol.id) is True

    async def test_admin_grantee_manages_unswitched_shares_only(self, db_session, people):
        """Managing shares stays the admin grantee's job — except a share that
        carries the switch: changing (or replacing) it is the owner's."""
        from api.routes.knowledge import revoke_kb_permission

        alice, bob, admin = people
        carol = await _user(db_session, "carol", ["kb.own", "kb.shared"])
        dave = await _user(db_session, "dave", ["kb.own", "kb.shared"])
        haus = await _kb(db_session, alice.id, "Haus")
        await ks.share_kb(db_session, haus, bob.id, "admin", granted_by=alice.id)
        await ks.share_kb(db_session, haus, carol.id, "read", granted_by=alice.id,
                          can_delete=True)

        # Unswitched: create, change, revoke as before; the switch stays off.
        await self._share(db_session, bob, haus, dave.id, "read")
        resp = await self._share(db_session, bob, haus, dave.id, "write")
        assert resp.permission == "write" and resp.can_delete is False
        await revoke_kb_permission(haus, dave.id, rag=RAGService(db_session), user=bob,
                                   db=db_session)
        assert await self._switch(db_session, haus, dave.id) is None

        # Switched: neither change nor revoke.
        with pytest.raises(HTTPException) as exc:
            await self._share(db_session, bob, haus, carol.id, "write")
        assert exc.value.status_code == 403
        with pytest.raises(HTTPException) as exc:
            await revoke_kb_permission(haus, carol.id, rag=RAGService(db_session), user=bob,
                                       db=db_session)
        assert exc.value.status_code == 403
        assert await self._switch(db_session, haus, carol.id) is True
        assert await ks.get_user_kb_permission_level(db_session, haus, carol.id) == "read"

        # The owner and kb.all may.
        await self._share(db_session, admin, haus, carol.id, "write")
        assert await self._switch(db_session, haus, carol.id) is True
        await revoke_kb_permission(haus, carol.id, rag=RAGService(db_session), user=alice,
                                   db=db_session)
        assert await self._switch(db_session, haus, carol.id) is None

    async def test_revoke_takes_the_switch_with_it(self, db_session, people):
        from api.routes.knowledge import revoke_kb_permission

        alice, bob, _ = people
        haus = await _kb(db_session, alice.id, "Haus")
        await ks.share_kb(db_session, haus, bob.id, "read", granted_by=alice.id, can_delete=True)
        await revoke_kb_permission(haus, bob.id, rag=RAGService(db_session), user=alice,
                                   db=db_session)
        await ks.share_kb(db_session, haus, bob.id, "read", granted_by=alice.id)
        assert await self._switch(db_session, haus, bob.id) is False

    async def test_backfill_writes_it_off_and_revert_spares_a_switched_row(
        self, db_session, people,
    ):
        alice, bob, _ = people
        carol = await _user(db_session, "carol", ["kb.own", "kb.shared"])
        haus = await _kb(db_session, alice.id, "Haus")
        # A legacy share's KB is older than its grants (else the backfill flags
        # the pair kb_younger_than_grant and leaves it to the owner, #1446).
        await db_session.execute(text(
            "UPDATE knowledge_bases SET created_at = '2026-09-01' WHERE id = :k"), {"k": haus})
        doc = await _add_doc(db_session, haus, "alt")
        for grantee in (bob.id, carol.id):
            await db_session.execute(text(
                "INSERT INTO atom_explicit_grants (atom_id, granted_to_user_id, "
                "permission_level, granted_by, granted_at) "
                "VALUES (:a, :to, 'admin', :by, '2026-10-01')"),
                {"a": doc.atom_id, "to": grantee, "by": alice.id})
        await db_session.commit()

        run = await ks.backfill_kb_shares(db_session, dry_run=False)
        assert len(run.inserted) == 2
        assert await self._switch(db_session, haus, bob.id) is False
        assert await self._switch(db_session, haus, carol.id) is False
        # The owner gives Carol the switch by SQL that keeps granted_at (the route
        # would bump it): the revert must still spare her row.
        await db_session.execute(text(
            "UPDATE kb_shares SET can_delete = TRUE WHERE user_id = :u"), {"u": carol.id})
        await db_session.commit()

        assert await ks.revert_kb_share_backfill(db_session, run.inserted) == 1
        assert await self._switch(db_session, haus, bob.id) is None
        assert await self._switch(db_session, haus, carol.id) is True


class TestOrphanDocumentAtomCleanup:
    async def _orphan(self, db: AsyncSession, kb_id: int, label: str, grantee: int, owner: int):
        """A document deleted the old way: the row goes, its atoms and grants stay."""
        doc = await _add_doc(db, kb_id, label)
        fact = await _add_fact(db, doc, "issuer")
        await _grant(db, doc.atom_id, grantee, owner)
        # The pre-#1461 delete path minus the fact purge (as it was before that fix).
        await db.execute(text("DELETE FROM document_facts WHERE document_id = :d"), {"d": doc.id})
        await db.execute(text("DELETE FROM document_chunks WHERE document_id = :d"), {"d": doc.id})
        await db.execute(text("DELETE FROM documents WHERE id = :d"), {"d": doc.id})
        await db.commit()
        return doc, fact

    async def test_dry_run_counts_commit_purges_only_orphans(self, db_session, people):
        from services.orphan_document_atoms import purge_orphan_document_atoms

        alice, bob, _ = people
        haus = await _kb(db_session, alice.id, "Haus")
        gone, gone_fact = await self._orphan(db_session, haus, "verwaist", bob.id, alice.id)
        alive = await _add_doc(db_session, haus, "lebt")
        alive_fact = await _add_fact(db_session, alive, "issuer")
        await _grant(db_session, alive.atom_id, bob.id, alice.id)

        dry = await purge_orphan_document_atoms(db_session, dry_run=True)
        assert (dry.document_atoms, dry.fact_atoms, dry.grants) == (1, 1, 1)
        assert dry.purged == []
        assert await _atom(db_session, gone.atom_id) is not None

        run = await purge_orphan_document_atoms(db_session, dry_run=False)
        assert sorted(p["atom_id"] for p in run.purged) == sorted(
            [gone.atom_id, gone_fact.atom_id])
        logged = next(p for p in run.purged if p["atom_id"] == gone.atom_id)
        assert logged["grants"] == [{"granted_to_user_id": bob.id, "permission_level": "read"}]
        assert await _atom(db_session, gone.atom_id) is None
        assert await _atom(db_session, gone_fact.atom_id) is None
        # Negative control: a living document keeps its atoms and grant.
        assert await _atom(db_session, alive.atom_id) is not None
        assert await _atom(db_session, alive_fact.atom_id) is not None
        assert (await db_session.execute(text(
            "SELECT COUNT(*) FROM atom_explicit_grants WHERE atom_id = :a"),
            {"a": alive.atom_id})).scalar() == 1

        again = await purge_orphan_document_atoms(db_session, dry_run=False)
        assert (again.document_atoms, again.fact_atoms, again.purged) == (0, 0, [])

    async def test_a_pending_placeholder_is_not_an_orphan(self, db_session, people):
        """An in-flight create holds a ``__pending__`` source id — the placeholder
        reaper owns those, this cleanup must not race an upload."""
        from services.atom_service import AtomService
        from services.orphan_document_atoms import purge_orphan_document_atoms

        alice, _, _ = people
        pending = await AtomService(db_session).create_with_source(
            atom_type="kb_document", owner_user_id=alice.id, tier=TIER_SELF)
        await db_session.commit()

        run = await purge_orphan_document_atoms(db_session, dry_run=False)

        assert run.document_atoms == 0
        assert await _atom(db_session, pending) is not None

    async def test_an_atom_a_live_document_still_uses_is_never_purged(self, db_session, people):
        """Negative control: a mismatched ``source_id`` alone is not an orphan —
        the atom is still the access unit of a living document."""
        from services.orphan_document_atoms import purge_orphan_document_atoms

        alice, _, _ = people
        haus = await _kb(db_session, alice.id, "Haus")
        doc = await _add_doc(db_session, haus, "schief")
        fact = await _add_fact(db_session, doc, "issuer")
        await db_session.execute(text(
            "UPDATE atoms SET source_id = '999999999' WHERE atom_id IN (:a, :f)"),
            {"a": doc.atom_id, "f": fact.atom_id})
        await db_session.commit()

        run = await purge_orphan_document_atoms(db_session, dry_run=False)

        assert (run.document_atoms, run.fact_atoms) == (0, 0)
        assert await _atom(db_session, doc.atom_id) is not None
        assert await _atom(db_session, fact.atom_id) is not None

    async def test_the_list_is_handed_over_before_the_commit(self, db_session, people, monkeypatch):
        """The log callback runs BEFORE the commit: when the commit fails, the
        attempted erasure is already named, and nothing was purged."""
        from services.orphan_document_atoms import purge_orphan_document_atoms

        alice, bob, _ = people
        haus = await _kb(db_session, alice.id, "Haus")
        gone, _ = await self._orphan(db_session, haus, "verwaist", bob.id, alice.id)
        gone_atom = gone.atom_id  # read now: the rollback below expires the object
        seen: list[list[dict]] = []

        async def failing_commit():
            raise RuntimeError("commit failed")

        def record(entries):
            seen.append(entries)
            monkeypatch.setattr(db_session, "commit", failing_commit)

        with pytest.raises(RuntimeError):
            await purge_orphan_document_atoms(db_session, dry_run=False, on_before_commit=record)
        await db_session.rollback()
        monkeypatch.undo()

        assert gone_atom in {e["atom_id"] for e in seen[0]}
        assert await _atom(db_session, gone_atom) is not None  # rolled back

    async def test_script_log_lists_the_purge_before_the_commit(self, tmp_path, monkeypatch):
        """bin/purge_orphan_document_atoms.py: when the commit fails after the
        callback, the log already holds the full list, marked incomplete."""
        import argparse
        import importlib.util
        import json
        from pathlib import Path

        path = Path(__file__).resolve().parents[2] / "bin" / "purge_orphan_document_atoms.py"
        spec = importlib.util.spec_from_file_location("_purge_script_under_test", path)
        script = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(script)

        class _Session:
            async def __aenter__(self):
                return object()

            async def __aexit__(self, *exc):
                return False

        async def fake_purge(session, *, dry_run, on_before_commit):
            assert not dry_run
            on_before_commit([{"atom_id": "a-1", "atom_type": "kb_document", "grants": []}])
            raise RuntimeError("commit failed")

        monkeypatch.setattr(script, "AsyncSessionLocal", _Session)
        monkeypatch.setattr(script, "purge_orphan_document_atoms", fake_purge)
        log = tmp_path / "purge.json"

        with pytest.raises(RuntimeError):
            await script._run(argparse.Namespace(commit=True, log=str(log)))

        data = json.loads(log.read_text())
        assert data["complete"] is False
        assert [e["atom_id"] for e in data["purged"]] == ["a-1"]


# --------------------------------------------------------------------------- review
class TestMoveEdgeCases:
    async def test_into_an_ownerless_kb_needs_the_owner(self, db_session, people):
        """Nobody owns it there: the content leaves the owner's hands, so the gate
        treats it as a change of owner."""
        alice, bob, _ = people
        haus = await _kb(db_session, alice.id, "Haus")
        geteilt = await _kb(db_session, None, "Geteilt")
        await ks.share_kb(db_session, haus, bob.id, "write", granted_by=alice.id)
        await ks.share_kb(db_session, geteilt, bob.id, "write", granted_by=alice.id)
        await ks.share_kb(db_session, geteilt, alice.id, "write", granted_by=bob.id)
        doc = await _add_doc(db_session, haus, "hinaus")

        with pytest.raises(HTTPException) as exc:
            await _route_move(db_session, bob, [doc.id], geteilt)
        assert exc.value.status_code == 403

        assert (await _route_move(db_session, alice, [doc.id], geteilt))["moved_count"] == 1
        assert await _atom(db_session, doc.atom_id) == (alice.id, TIER_SELF)

    @pytest.mark.parametrize("status", ["pending", "processing", "split_review"])
    async def test_a_document_in_processing_is_not_moved(self, db_session, people, status):
        alice, _, _ = people
        privat = await _kb(db_session, alice.id, "Privat")
        werk = await _kb(db_session, alice.id, "Werkstatt")
        doc = await _add_doc(db_session, privat, "laeuft", status=status)

        with pytest.raises(HTTPException) as exc:
            await _route_move(db_session, alice, [doc.id], werk)

        assert exc.value.status_code == 409
        assert str(doc.id) in exc.value.detail
        assert await _kb_of(db_session, doc.id) == privat

    @pytest.mark.parametrize("status", ["failed", "split_archived"])
    async def test_a_terminal_document_may_be_moved(self, db_session, people, status):
        alice, _, _ = people
        privat = await _kb(db_session, alice.id, "Privat")
        werk = await _kb(db_session, alice.id, "Werkstatt")
        doc = await _add_doc(db_session, privat, f"ende-{status}", status=status)
        assert (await _route_move(db_session, alice, [doc.id], werk))["moved_count"] == 1

    async def test_atom_less_document_across_owners_takes_the_tier(self, db_session, people):
        alice, bob, _ = people
        haus = await _kb(db_session, alice.id, "Haus", tier=TIER_SELF)
        bobs = await _kb(db_session, bob.id, "Bobs", tier=TIER_HOUSEHOLD)
        legacy = Document(filename="alt.pdf", file_path="/tmp/alt.pdf", status="completed",
                          knowledge_base_id=haus, circle_tier=TIER_SELF)
        db_session.add(legacy)
        await db_session.commit()
        await db_session.execute(text(
            "INSERT INTO document_chunks (document_id, content, chunk_index, circle_tier) "
            "VALUES (:d, 'alt', 0, 0)"), {"d": legacy.id})
        await db_session.commit()

        assert await RAGService(db_session).move_documents([legacy.id], bobs) == 1

        row = (await db_session.execute(text(
            "SELECT d.circle_tier, (SELECT array_agg(circle_tier) FROM document_chunks "
            "WHERE document_id = d.id) FROM documents d WHERE d.id = :d"),
            {"d": legacy.id})).one()
        assert (row[0], row[1]) == (TIER_HOUSEHOLD, [TIER_HOUSEHOLD])

    async def test_a_document_with_two_owners(self, db_session, people):
        """Moved across owners before #1460: atom owner Alice in Bob's KB. Reads
        follow the KB owner alone (#1469: the atom-owner fallback applies only to
        a KB without owner), but the MOVE gate still names both as owners and
        does not pick one; a move into one owner's KB settles it."""
        alice, bob, _ = people
        carol = await _user(db_session, "carol", ["kb.own", "kb.shared"])
        privat = await _kb(db_session, alice.id, "Privat")
        bobs = await _kb(db_session, bob.id, "Bobs")
        bobs_archiv = await _kb(db_session, bob.id, "Bobs-Archiv", tier=TIER_HOUSEHOLD)
        doc = await _add_doc(db_session, privat, "doppelt")
        # The pre-#1460 move: only the KB changes.
        doc.knowledge_base_id = bobs
        await db_session.commit()
        await ks.share_kb(db_session, bobs, carol.id, "write", granted_by=bob.id)
        await ks.share_kb(db_session, bobs_archiv, carol.id, "write", granted_by=bob.id)
        assert doc.id not in await _visible_docs(db_session, alice.id)
        assert doc.id in await _visible_docs(db_session, bob.id)

        owners = await RAGService(db_session).document_owners([doc])
        assert owners[doc.id] == frozenset({alice.id, bob.id})
        # A write grantee of Bob cannot move it, even within Bob's KBs: it would
        # take it away from Alice.
        with pytest.raises(HTTPException) as exc:
            await _route_move(db_session, carol, [doc.id], bobs_archiv)
        assert exc.value.status_code == 403
        # Bob (one of its owners) can; the atom then follows his KB.
        assert (await _route_move(db_session, bob, [doc.id], bobs_archiv))["moved_count"] == 1
        assert await _atom(db_session, doc.atom_id) == (bob.id, TIER_HOUSEHOLD)
        assert doc.id not in await _visible_docs(db_session, alice.id)


class TestKbDeleteAndFiles:
    async def test_kb_with_an_indexed_chat_upload_is_deleted(self, db_session, people):
        from models.database import ChatUpload

        alice, _, _ = people
        haus = await _kb(db_session, alice.id, "Haus")
        doc = await _add_doc(db_session, haus, "anhang")
        upload = ChatUpload(session_id="s-1", filename="anhang.pdf", document_id=doc.id,
                            knowledge_base_id=haus, status="completed")
        db_session.add(upload)
        await db_session.commit()

        assert await RAGService(db_session).delete_knowledge_base(haus)

        row = (await db_session.execute(text(
            "SELECT document_id, knowledge_base_id FROM chat_uploads WHERE id = :u"),
            {"u": upload.id})).one()
        assert tuple(row) == (None, None)  # the upload stays, it names no KB/document
        assert await _atom(db_session, doc.atom_id) is None

    async def test_files_go_only_after_the_commit(self, db_session, people, tmp_path, monkeypatch):
        alice, _, _ = people
        haus = await _kb(db_session, alice.id, "Haus")
        kept = tmp_path / "bleibt.pdf"
        kept.write_bytes(b"x")
        doc = await _add_doc(db_session, haus, "bleibt", file_path=str(kept))
        doc_id = doc.id  # read now: the rollbacks below expire the object

        async def failing_commit():
            raise RuntimeError("commit failed")

        monkeypatch.setattr(db_session, "commit", failing_commit)
        with pytest.raises(RuntimeError):
            await RAGService(db_session).delete_document(doc_id)
        assert kept.exists()  # the rows stayed, so does the file
        await db_session.rollback()
        with pytest.raises(RuntimeError):
            await RAGService(db_session).delete_knowledge_base(haus)
        assert kept.exists()
        monkeypatch.undo()
        await db_session.rollback()

        assert await RAGService(db_session).delete_document(doc_id)
        assert not kept.exists()

        gone = tmp_path / "weg.pdf"
        gone.write_bytes(b"x")
        await _add_doc(db_session, haus, "weg", file_path=str(gone))
        assert await RAGService(db_session).delete_knowledge_base(haus)
        assert not gone.exists()


class TestTwoOwnerDocumentAccess:
    """A document moved across owners before #1460: atom owner Alice, KB owner Bob.
    The per-document gate lets the atom owner through only where she is the KB's
    owner (or there is none); otherwise the KB rule decides."""

    async def _two_owner_doc(self, db, alice, bob):
        privat = await _kb(db, alice.id, "Privat")
        bobs = await _kb(db, bob.id, "Bobs")
        doc = await _add_doc(db, privat, "doppelt")
        doc.knowledge_base_id = bobs  # the pre-#1460 move: only the KB changes
        await db.commit()
        return doc, bobs

    async def test_the_old_atom_owner_falls_to_the_kb_rule(self, db_session, people):
        from api.routes.knowledge import check_document_access, delete_document

        alice, bob, _ = people
        doc, _ = await self._two_owner_doc(db_session, alice, bob)

        for action in ("read", "write", "delete"):
            assert not await check_document_access(doc, alice, action, db_session), action
        with pytest.raises(HTTPException) as exc:
            await delete_document(doc.id, rag=RAGService(db_session), user=alice, db=db_session)
        assert exc.value.status_code == 403

        await delete_document(doc.id, rag=RAGService(db_session), user=bob, db=db_session)
        assert await _kb_of(db_session, doc.id) is None

    async def test_a_share_still_opens_it_for_the_old_owner(self, db_session, people):
        from api.routes.knowledge import check_document_access

        alice, bob, _ = people
        doc, bobs = await self._two_owner_doc(db_session, alice, bob)
        await ks.share_kb(db_session, bobs, alice.id, "read", granted_by=bob.id)

        assert await check_document_access(doc, alice, "read", db_session)
        assert not await check_document_access(doc, alice, "write", db_session)

    async def test_kb_less_and_ownerless_kb_documents_keep_the_atom_owner(
        self, db_session, people,
    ):
        from api.routes.knowledge import check_document_access

        alice, bob, _ = people
        geteilt = await _kb(db_session, None, "Geteilt")
        doc = await _add_doc(db_session, geteilt, "ohne-eigner")
        loose = await _add_doc(db_session, geteilt, "lose")
        loose.knowledge_base_id = None
        await db_session.commit()

        async def own(user):
            await db_session.execute(text(
                "UPDATE atoms SET owner_user_id = :u WHERE atom_id IN (:a, :b)"),
                {"u": user.id, "a": doc.atom_id, "b": loose.atom_id})
            await db_session.commit()

        # Whoever the atom names reaches both — and only they.
        for owner, other in ((alice, bob), (bob, alice)):
            await own(owner)
            for d in (doc, loose):
                assert await check_document_access(d, owner, "delete", db_session)
                assert not await check_document_access(d, other, "delete", db_session)


class TestFileRemovalFailure:
    async def test_a_file_that_cannot_be_removed_is_an_error_with_id_and_path(
        self, db_session, people, tmp_path, monkeypatch,
    ):
        import services.rag_service as rs

        alice, _, _ = people
        haus = await _kb(db_session, alice.id, "Haus")
        stuck = tmp_path / "klemmt.pdf"
        stuck.write_bytes(b"x")
        doc = await _add_doc(db_session, haus, "klemmt", file_path=str(stuck))
        doc_id = doc.id

        errors: list[str] = []

        class _Log:
            def error(self, msg, *a, **k):
                errors.append(msg)

            def __getattr__(self, name):  # info, warning, debug, …: discarded
                return lambda *a, **k: None

        def refuse(path):
            raise PermissionError("read-only volume")

        monkeypatch.setattr(rs, "logger", _Log())
        monkeypatch.setattr(rs.os, "remove", refuse)

        assert await RAGService(db_session).delete_document(doc_id)
        # The rows are gone (the delete held), the file stays — and is named.
        assert await _kb_of(db_session, doc_id) is None
        assert stuck.exists()
        assert len(errors) == 1
        assert str(doc_id) in errors[0] and str(stuck) in errors[0]


class TestDocumentListDeleteRights:
    """The document list's per-row ``can_delete`` is the batch form of
    ``check_document_access(..., "delete")`` — compared row by row."""

    async def _list(self, db, user):
        from api.routes.knowledge import list_documents

        return await list_documents(
            knowledge_base_id=None, status=None, q=None, sort=None, order="desc",
            limit=100, offset=0, rag=RAGService(db), user=user, db=db,
        )

    async def test_it_matches_the_per_document_gate(self, db_session, people):
        from api.routes.knowledge import check_document_access

        alice, bob, admin = people
        carol = await _user(db_session, "carol", ["kb.own", "kb.shared"])
        privat = await _kb(db_session, alice.id, "Privat")
        bobs = await _kb(db_session, bob.id, "Bobs")
        schalter = await _kb(db_session, bob.id, "Schalter")
        geteilt = await _kb(db_session, None, "Geteilt")
        await ks.share_kb(db_session, bobs, carol.id, "admin", granted_by=bob.id)
        await ks.share_kb(db_session, schalter, carol.id, "read", granted_by=bob.id,
                          can_delete=True)

        docs = [
            await _add_doc(db_session, privat, "eigen"),
            await _add_doc(db_session, bobs, "fremd"),
            await _add_doc(db_session, schalter, "geschaltet"),
            await _add_doc(db_session, geteilt, "eigner-los"),
        ]
        two = await _add_doc(db_session, privat, "doppelt")
        two.knowledge_base_id = bobs  # pre-#1460 move: atom owner Alice, KB owner Bob
        loose = await _add_doc(db_session, privat, "lose")
        loose.knowledge_base_id = None  # KB-less, atom owner Alice
        await db_session.commit()
        docs += [two, loose]

        from api.routes.knowledge import _document_delete_rights

        for user in (alice, bob, carol, admin):
            # The batch helper, on EVERY case — including documents this user
            # does not get listed (which rows the list returns is the
            # visibility filter's business, not the delete rule's).
            rights = await _document_delete_rights(db_session, user, docs)
            for d in docs:
                expected = await check_document_access(d, user, "delete", db_session)
                assert rights[d.id] is expected, (user.username, d.filename)
            # The list carries exactly that value on the rows it returns.
            listed = await self._list(db_session, user)
            for r in listed:
                assert r.can_delete is rights[r.id], (user.username, r.filename)

        # Spot checks of the rule itself.
        carol_rights = await _document_delete_rights(db_session, carol, docs)
        assert carol_rights[docs[2].id] is True   # the switch
        assert carol_rights[docs[1].id] is False  # admin share, no switch
        alice_rights = await _document_delete_rights(db_session, alice, docs)
        assert alice_rights[two.id] is False and alice_rights[loose.id] is True
        # The list reaches the rows through it: Alice's own KB is listed for her.
        own = await self._list(db_session, alice)
        assert any(r.id == docs[0].id and r.can_delete is True for r in own)

    async def test_auth_off_deletes_everything(self, db_session, people, monkeypatch):
        alice, _, _ = people
        privat = await _kb(db_session, alice.id, "Privat")
        await _add_doc(db_session, privat, "eigen")
        monkeypatch.setattr(settings, "auth_enabled", False)
        assert all(r.can_delete for r in await self._list(db_session, None))
