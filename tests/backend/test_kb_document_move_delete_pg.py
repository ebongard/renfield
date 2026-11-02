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


async def _add_doc(db: AsyncSession, kb_id: int, label: str) -> Document:
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


class TestDeleteNeedsMoreThanRead:
    """``check_kb_access`` mapped the action "delete" to the READ level, so a read
    grantee could delete the owner's documents and the whole KB."""

    async def test_read_and_write_grantees_cannot_delete(self, db_session, people):
        from api.routes.knowledge import check_document_access, check_kb_access

        alice, bob, _ = people
        haus = await _kb(db_session, alice.id, "Haus")
        doc = await _add_doc(db_session, haus, "bleibt")
        kb = (await db_session.execute(
            select(KnowledgeBase).where(KnowledgeBase.id == haus))).scalar_one()

        for level in ("read", "write"):
            await ks.share_kb(db_session, haus, bob.id, level, granted_by=alice.id)
            assert not await check_document_access(doc, bob, "delete", db_session), level
            assert not await check_kb_access(kb, bob, "delete", db_session), level

        await ks.share_kb(db_session, haus, bob.id, "admin", granted_by=alice.id)
        assert await check_document_access(doc, bob, "delete", db_session)
        assert await check_kb_access(kb, bob, "delete", db_session)
        assert await check_document_access(doc, alice, "delete", db_session)

    async def test_unknown_action_is_denied(self, db_session, people):
        from api.routes.knowledge import check_kb_access

        alice, bob, _ = people
        haus = await _kb(db_session, alice.id, "Haus")
        await ks.share_kb(db_session, haus, bob.id, "admin", granted_by=alice.id)
        kb = (await db_session.execute(
            select(KnowledgeBase).where(KnowledgeBase.id == haus))).scalar_one()

        assert not await check_kb_access(kb, bob, "purge", db_session)


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
