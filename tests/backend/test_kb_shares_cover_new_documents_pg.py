"""A KB share covers EVERY document of the KB — also the ones added later.

Before ``kb_shares`` (pc20261101) a share was only one ``atom_explicit_grants``
row per document present at share time. Alice shares "Haus" with Bob (write);
Bob's upload into it is owned by Alice (the KB owner) at the KB's default tier,
and no grant covered it — Bob could not read his own upload. Sharing an EMPTY KB
stored nothing at all, so Bob could not even upload.

The share is now a ``kb_shares`` row, evaluated at query time by the document
filters in ``circle_sql`` and by ``CircleResolver.can_access_atom``. Legacy
per-document grants are kept and still count until the owner-run backfill.

Real Postgres: every branch here is Postgres SQL.
"""
from __future__ import annotations

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from models.database import Atom, Document, KnowledgeBase
from services import kb_shares_service as ks
from tests.backend.dbrows import ensure_role, ensure_user
from utils.config import settings

pytestmark = [pytest.mark.backend, pytest.mark.database]

TIER_SELF = 0


@pytest.fixture
async def people(db_session: AsyncSession, monkeypatch):
    """Alice (KB owner), Bob (grantee), Carol (stranger) — no circle memberships,
    so only the owner branch and the share can open a tier-0 document."""
    monkeypatch.setattr(settings, "auth_enabled", True)
    await ensure_role(db_session)
    alice = await ensure_user(db_session, 1, username="alice")
    bob = await ensure_user(db_session, 2, username="bob")
    carol = await ensure_user(db_session, 3, username="carol")
    await db_session.commit()
    return alice, bob, carol


async def _kb(db: AsyncSession, owner_id: int, name: str) -> int:
    kb = KnowledgeBase(name=name, is_active=True, owner_id=owner_id,
                       default_circle_tier=TIER_SELF)
    db.add(kb)
    await db.commit()
    return kb.id


async def _add_doc(db: AsyncSession, kb_id: int, label: str) -> Document:
    """The real create path (every upload / ingest goes through it): the atom
    owner is the KB OWNER, the tier the KB default — whoever uploads."""
    from services.rag_service import RAGService

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


async def _legacy_grant(db: AsyncSession, doc: Document, to_user: int, by_user: int,
                        level: str = "read") -> None:
    """A grant exactly as the pre-kb_shares share_kb wrote it."""
    await db.execute(
        text("INSERT INTO atom_explicit_grants "
             "(atom_id, granted_to_user_id, permission_level, granted_by, granted_at) "
             "VALUES (:a, :to, :lvl, :by, NOW())"),
        {"a": doc.atom_id, "to": to_user, "lvl": level, "by": by_user},
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


async def _visible_facts_docs(db: AsyncSession, asker_id: int, doc_ids: list[int]) -> set[int]:
    """The facts filter over a synthetic fact row per document (same branches)."""
    from services.circle_sql import document_facts_circles_filter

    clause, params = document_facts_circles_filter(asker_id)
    rows = (await db.execute(
        text("SELECT d.id FROM documents d "
             "CROSS JOIN LATERAL (SELECT d.circle_tier AS circle_tier) df "
             "LEFT JOIN knowledge_bases kb ON kb.id = d.knowledge_base_id "
             f"WHERE d.id = ANY(:ids) AND ({clause})"),
        {**params, "ids": doc_ids},
    )).all()
    return {r[0] for r in rows}


async def _resolver_sees(db: AsyncSession, asker_id: int, doc: Document) -> bool:
    from services.circle_resolver import CircleResolver, atom_from_orm

    atom = (await db.execute(select(Atom).where(Atom.atom_id == doc.atom_id))).scalar_one()
    return await CircleResolver(db).can_access_atom(asker_id, atom_from_orm(atom))


# --------------------------------------------------------------------------- #
class TestShareCoversLaterDocuments:
    async def test_empty_kb_shared_then_bob_uploads_and_reads_it(self, db_session, people):
        alice, bob, carol = people
        haus = await _kb(db_session, alice.id, "Haus")

        await ks.share_kb(db_session, haus, bob.id, "write", granted_by=alice.id)

        # The share EXISTS although the KB is empty — and it opens the upload ACL.
        assert await ks.get_user_kb_permission_level(db_session, haus, bob.id) == "write"
        assert [s["user_id"] for s in await ks.list_kb_shares(db_session, haus)] == [bob.id]

        upload = await _add_doc(db_session, haus, "rechnung-x")
        # Ownership semantics unchanged: the KB owner owns the document atom.
        owner = (await db_session.execute(
            select(Atom.owner_user_id).where(Atom.atom_id == upload.atom_id))).scalar_one()
        assert owner == alice.id

        assert upload.id in await _visible_docs(db_session, bob.id)
        assert upload.id in await _visible_facts_docs(db_session, bob.id, [upload.id])
        assert await _resolver_sees(db_session, bob.id, upload)
        # Negative control: the stranger still sees nothing.
        assert upload.id not in await _visible_docs(db_session, carol.id)
        assert not await _resolver_sees(db_session, carol.id, upload)

    async def test_document_added_by_owner_after_the_share_is_covered(self, db_session, people):
        alice, bob, carol = people
        haus = await _kb(db_session, alice.id, "Haus")
        before = await _add_doc(db_session, haus, "vorher")
        await ks.share_kb(db_session, haus, bob.id, "read", granted_by=alice.id)
        later = await _add_doc(db_session, haus, "nachher")

        assert {before.id, later.id} <= await _visible_docs(db_session, bob.id)
        assert await _resolver_sees(db_session, bob.id, later)
        assert await _visible_docs(db_session, carol.id) & {before.id, later.id} == set()

    async def test_knowledge_tool_title_lookup_follows_the_share(self, db_session, people):
        """The third hand-built document filter (knowledge_tool) carries the branch too."""
        from services.knowledge_tool import _visible_document_meta

        alice, bob, carol = people
        haus = await _kb(db_session, alice.id, "Haus")
        await ks.share_kb(db_session, haus, bob.id, "read", granted_by=alice.id)
        doc = await _add_doc(db_session, haus, "brief")

        assert doc.id in await _visible_document_meta(db_session, {doc.id}, bob.id)
        assert doc.id not in await _visible_document_meta(db_session, {doc.id}, carol.id)

    async def test_share_of_another_kb_does_not_leak(self, db_session, people):
        alice, bob, _ = people
        haus = await _kb(db_session, alice.id, "Haus")
        privat = await _kb(db_session, alice.id, "Privat")
        await ks.share_kb(db_session, haus, bob.id, "read", granted_by=alice.id)
        secret = await _add_doc(db_session, privat, "geheim")

        assert secret.id not in await _visible_docs(db_session, bob.id)
        assert not await _resolver_sees(db_session, bob.id, secret)
        assert await ks.get_user_kb_permission_level(db_session, privat, bob.id) is None


class TestRevokeAndLevels:
    async def test_revoke_removes_access_to_all_documents_and_legacy_grants(self, db_session, people):
        alice, bob, _ = people
        haus = await _kb(db_session, alice.id, "Haus")
        old = await _add_doc(db_session, haus, "alt")
        await _legacy_grant(db_session, old, bob.id, alice.id)
        await ks.share_kb(db_session, haus, bob.id, "write", granted_by=alice.id)
        new = await _add_doc(db_session, haus, "neu")
        # Warm the process-wide grant cache: it must not outlive the revoke.
        assert await _resolver_sees(db_session, bob.id, old)

        removed = await ks.revoke_kb_share(db_session, haus, bob.id)

        assert removed == 2  # the kb_shares row + the legacy grant
        assert await _visible_docs(db_session, bob.id) & {old.id, new.id} == set()
        assert not await _resolver_sees(db_session, bob.id, old)
        assert not await _resolver_sees(db_session, bob.id, new)
        assert await ks.get_user_kb_permission_level(db_session, haus, bob.id) is None
        assert await ks.list_kb_shares(db_session, haus) == []
        assert await ks.revoke_kb_share(db_session, haus, bob.id) == 0

    async def test_revoke_of_an_empty_kb_share_finds_it(self, db_session, people):
        alice, bob, _ = people
        haus = await _kb(db_session, alice.id, "Haus")
        await ks.share_kb(db_session, haus, bob.id, "read", granted_by=alice.id)
        assert await ks.revoke_kb_share(db_session, haus, bob.id) == 1

    async def test_level_change_up_and_down(self, db_session, people):
        alice, bob, _ = people
        haus = await _kb(db_session, alice.id, "Haus")
        await ks.share_kb(db_session, haus, bob.id, "read", granted_by=alice.id)
        await ks.share_kb(db_session, haus, bob.id, "admin", granted_by=alice.id)
        assert await ks.get_user_kb_permission_level(db_session, haus, bob.id) == "admin"
        await ks.share_kb(db_session, haus, bob.id, "read", granted_by=alice.id)
        assert await ks.get_user_kb_permission_level(db_session, haus, bob.id) == "read"
        assert (await ks.list_kb_shares(db_session, haus))[0]["permission"] == "read"

    async def test_downgrade_takes_effect_over_a_legacy_write_grant(self, db_session, people):
        """ACL level = MAX(kb_shares, grants) — so share_kb aligns the legacy grants."""
        alice, bob, _ = people
        haus = await _kb(db_session, alice.id, "Haus")
        doc = await _add_doc(db_session, haus, "alt")
        await _legacy_grant(db_session, doc, bob.id, alice.id, level="write")
        assert await ks.get_user_kb_permission_level(db_session, haus, bob.id) == "write"

        await ks.share_kb(db_session, haus, bob.id, "read", granted_by=alice.id)

        assert await ks.get_user_kb_permission_level(db_session, haus, bob.id) == "read"
        assert await ks.get_user_kb_permission_levels(db_session, bob.id) == {haus: "read"}


class TestLegacyGrantsAndBackfill:
    async def test_legacy_grant_counts_before_the_backfill(self, db_session, people):
        alice, bob, _ = people
        haus = await _kb(db_session, alice.id, "Haus")
        old = await _add_doc(db_session, haus, "alt")
        await _legacy_grant(db_session, old, bob.id, alice.id, level="write")

        assert old.id in await _visible_docs(db_session, bob.id)
        assert await ks.get_user_kb_permission_level(db_session, haus, bob.id) == "write"
        assert await ks.list_user_shared_kb_ids(db_session, bob.id) == {haus}
        assert [s["permission"] for s in await ks.list_kb_shares(db_session, haus)] == ["write"]
        # The gap the backfill closes: a later document is not covered by a
        # legacy-only share.
        later = await _add_doc(db_session, haus, "spaeter")
        assert later.id not in await _visible_docs(db_session, bob.id)

    async def test_backfill_dry_run_commit_idempotent_and_revert(self, db_session, people):
        alice, bob, carol = people
        haus = await _kb(db_session, alice.id, "Haus")
        d1 = await _add_doc(db_session, haus, "a")
        d2 = await _add_doc(db_session, haus, "b")
        await _legacy_grant(db_session, d1, bob.id, alice.id, level="read")
        await _legacy_grant(db_session, d2, bob.id, alice.id, level="write")
        # A share already made through the UI must stay untouched.
        werk = await _kb(db_session, alice.id, "Werkstatt")
        w1 = await _add_doc(db_session, werk, "w")
        await _legacy_grant(db_session, w1, carol.id, alice.id, level="write")
        await ks.share_kb(db_session, werk, carol.id, "read", granted_by=alice.id)
        carol_row_before = (await db_session.execute(text(
            "SELECT permission_level, granted_at FROM kb_shares WHERE user_id = :u"),
            {"u": carol.id})).one()

        dry = await ks.backfill_kb_shares(db_session, dry_run=True)
        assert (dry.pairs_with_grants, dry.already_shared, dry.missing) == (2, 1, 1)
        assert dry.inserted == []
        assert (await db_session.execute(text("SELECT COUNT(*) FROM kb_shares"))).scalar() == 1

        run = await ks.backfill_kb_shares(db_session, dry_run=False)
        assert len(run.inserted) == 1 and run.inserted[0]["kb_id"] == haus
        row = (await db_session.execute(text(
            "SELECT permission_level FROM kb_shares WHERE kb_id = :k AND user_id = :u"),
            {"k": haus, "u": bob.id})).scalar_one()
        assert row == "write"  # MAX over the user's grants on the KB
        assert (await db_session.execute(text(
            "SELECT permission_level, granted_at FROM kb_shares WHERE user_id = :u"),
            {"u": carol.id})).one() == carol_row_before
        grants = (await db_session.execute(
            text("SELECT COUNT(*) FROM atom_explicit_grants"))).scalar()
        assert grants == 3  # legacy grants untouched

        # Now the share covers a later document.
        later = await _add_doc(db_session, haus, "spaeter")
        assert later.id in await _visible_docs(db_session, bob.id)

        again = await ks.backfill_kb_shares(db_session, dry_run=False)
        assert again.inserted == [] and again.missing == 0

        deleted = await ks.revert_kb_share_backfill(db_session, run.inserted)
        assert deleted == 1
        assert (await db_session.execute(text("SELECT COUNT(*) FROM kb_shares"))).scalar() == 1
        # Back to legacy-only: the grants still give access to the old documents.
        assert {d1.id, d2.id} <= await _visible_docs(db_session, bob.id)
        assert later.id not in await _visible_docs(db_session, bob.id)

    async def test_revert_spares_a_share_changed_since_the_backfill(self, db_session, people):
        alice, bob, _ = people
        haus = await _kb(db_session, alice.id, "Haus")
        d1 = await _add_doc(db_session, haus, "a")
        await _legacy_grant(db_session, d1, bob.id, alice.id)
        run = await ks.backfill_kb_shares(db_session, dry_run=False)
        await ks.share_kb(db_session, haus, bob.id, "write", granted_by=alice.id)

        assert await ks.revert_kb_share_backfill(db_session, run.inserted) == 0
        assert await ks.get_user_kb_permission_level(db_session, haus, bob.id) == "write"


class TestMoveAndDelete:
    async def test_move_out_of_a_shared_kb_removes_access(self, db_session, people):
        from services.rag_service import RAGService

        alice, bob, _ = people
        haus = await _kb(db_session, alice.id, "Haus")
        privat = await _kb(db_session, alice.id, "Privat")
        await ks.share_kb(db_session, haus, bob.id, "read", granted_by=alice.id)
        doc = await _add_doc(db_session, haus, "wandert")
        legacy = await _add_doc(db_session, haus, "wandert-alt")
        await _legacy_grant(db_session, legacy, bob.id, alice.id)
        assert {doc.id, legacy.id} <= await _visible_docs(db_session, bob.id)
        assert await _resolver_sees(db_session, bob.id, legacy)  # warm the cache

        moved = await RAGService(db_session).move_documents([doc.id, legacy.id], privat)

        assert moved == 2
        assert await _visible_docs(db_session, bob.id) & {doc.id, legacy.id} == set()
        assert not await _resolver_sees(db_session, bob.id, doc)
        assert not await _resolver_sees(db_session, bob.id, legacy)

    async def test_move_into_a_shared_kb_grants_access(self, db_session, people):
        from services.rag_service import RAGService

        alice, bob, _ = people
        haus = await _kb(db_session, alice.id, "Haus")
        privat = await _kb(db_session, alice.id, "Privat")
        await ks.share_kb(db_session, haus, bob.id, "read", granted_by=alice.id)
        doc = await _add_doc(db_session, privat, "kommt")
        assert doc.id not in await _visible_docs(db_session, bob.id)

        await RAGService(db_session).move_documents([doc.id], haus)

        assert doc.id in await _visible_docs(db_session, bob.id)
        assert await _resolver_sees(db_session, bob.id, doc)

    async def test_deleting_a_document_keeps_the_share(self, db_session, people):
        from services.rag_service import RAGService

        alice, bob, _ = people
        haus = await _kb(db_session, alice.id, "Haus")
        await ks.share_kb(db_session, haus, bob.id, "write", granted_by=alice.id)
        doc = await _add_doc(db_session, haus, "weg")

        assert await RAGService(db_session).delete_document(doc.id)

        assert doc.id not in await _visible_docs(db_session, bob.id)
        # The KB is empty again — the share survives (it used to vanish).
        assert await ks.get_user_kb_permission_level(db_session, haus, bob.id) == "write"

    async def test_deleting_the_kb_removes_its_shares(self, db_session, people):
        from services.rag_service import RAGService

        alice, bob, _ = people
        haus = await _kb(db_session, alice.id, "Haus")
        await ks.share_kb(db_session, haus, bob.id, "read", granted_by=alice.id)

        assert await RAGService(db_session).delete_knowledge_base(haus)

        assert (await db_session.execute(text("SELECT COUNT(*) FROM kb_shares"))).scalar() == 0


class TestAuthOffAndFederation:
    async def test_auth_off_share_without_a_granter(self, db_session, people, monkeypatch):
        """Auth off: the route passes granted_by=None. The old per-document
        insert hit atom_explicit_grants.granted_by NOT NULL on a non-empty KB."""
        alice, bob, _ = people
        monkeypatch.setattr(settings, "auth_enabled", False)
        haus = await _kb(db_session, alice.id, "Haus")
        await _add_doc(db_session, haus, "a")

        await ks.share_kb(db_session, haus, bob.id, "read", granted_by=None)

        shares = await ks.list_kb_shares(db_session, haus)
        assert [(s["user_id"], s["granted_by"]) for s in shares] == [(bob.id, None)]

    async def test_auth_off_kb_acl_is_unchanged(self, db_session, people, monkeypatch):
        from api.routes.knowledge import get_user_kb_permission

        alice, bob, _ = people
        monkeypatch.setattr(settings, "auth_enabled", False)
        haus = await _kb(db_session, alice.id, "Haus")
        kb = (await db_session.execute(
            select(KnowledgeBase).where(KnowledgeBase.id == haus))).scalar_one()
        assert await get_user_kb_permission(kb, bob, db_session) == "admin"

    def test_federated_filter_has_no_share_branch(self):
        """A peer is no local person; `kb_shares.user_id = :asker` must not appear."""
        from services.circle_sql import (
            PeerScope,
            document_chunks_circles_filter,
            document_facts_circles_filter,
        )

        for build in (document_chunks_circles_filter, document_facts_circles_filter):
            local, _ = build(2)
            peer, params = build(2, peer=PeerScope(owner_id=1, tier=4))
            assert "kb_shares" in local
            assert "kb_shares" not in peer
            assert "asker_id" not in params
