"""A document grant opens that document, never its KB (#1446) — and the backfill
that carries legacy shares over into ``kb_shares`` refuses what it cannot trust.

Owner decision (a): the KB level (KB list, upload / write right, the owner's
share list) comes ONLY from ``kb_shares``. A grant in ``atom_explicit_grants`` on
one document keeps opening that document and its facts through the circle
filters (#1463), nothing more. Before, ONE grant put the whole KB into the
grantee's list at the grant's level.

The old ``share_kb`` materialised a share as one grant per document of the KB, in
one statement. ``bin/backfill_kb_shares.py`` turns those into ``kb_shares`` rows —
but before #1464 a document MOVE kept its grants, so a grant may have travelled
into another KB. Such pairs are suspicious: listed, never converted, adoptable by
the owner one by one (logged, revertible).

Real Postgres: every branch here is Postgres SQL.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from models.database import Atom, Document, KnowledgeBase, Role, User
from services import kb_shares_service as ks
from utils.config import settings

pytestmark = [pytest.mark.backend, pytest.mark.database]

TIER_SELF = 0


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
    """alice owns the KBs, bob is the grantee (kb.own like every household
    member), admin holds kb.all. No circle memberships."""
    monkeypatch.setattr(settings, "auth_enabled", True)
    alice = await _user(db_session, "alice", ["kb.own", "kb.shared"])
    bob = await _user(db_session, "bob", ["kb.own", "kb.shared"])
    admin = await _user(db_session, "admin-x", ["kb.all"])
    return alice, bob, admin


async def _kb(db: AsyncSession, owner_id: int, name: str) -> KnowledgeBase:
    kb = KnowledgeBase(name=name, is_active=True, owner_id=owner_id,
                       default_circle_tier=TIER_SELF)
    db.add(kb)
    await db.commit()
    return kb


async def _doc(db: AsyncSession, kb: KnowledgeBase, label: str,
               created_at: datetime | None = None) -> Document:
    from services.rag_service import RAGService

    doc = await RAGService(db).create_document_record(
        file_path=f"/tmp/{label}.pdf", knowledge_base_id=kb.id,
        filename=f"{label}.pdf", file_hash=f"h-{label}",
    )
    await db.execute(
        text("INSERT INTO document_chunks (document_id, content, chunk_index, circle_tier) "
             "VALUES (:d, :c, 0, :t)"),
        {"d": doc.id, "c": f"Inhalt {label}", "t": doc.circle_tier},
    )
    if created_at is not None:
        await db.execute(text("UPDATE documents SET created_at = :at WHERE id = :d"),
                         {"at": created_at, "d": doc.id})
    await db.commit()
    return doc


async def _old_share_kb(db: AsyncSession, kb: KnowledgeBase, to_user: int, by_user: int,
                        level: str, at: datetime) -> None:
    """The pre-kb_shares ``share_kb`` statement, verbatim in effect: one grant per
    document currently in the KB, one shared ``granted_at``."""
    await db.execute(
        text("INSERT INTO atom_explicit_grants "
             "  (atom_id, granted_to_user_id, permission_level, granted_by, granted_at) "
             "SELECT a.atom_id, :target, :perm, :granter, :now "
             "FROM atoms a JOIN documents d ON a.source_id = d.id::text "
             "WHERE a.source_table = 'documents' AND d.knowledge_base_id = :kb_id"),
        {"target": to_user, "perm": level, "granter": by_user, "now": at, "kb_id": kb.id},
    )
    await db.commit()


async def _move_pre_1464(db: AsyncSession, doc: Document, kb: KnowledgeBase) -> None:
    """A move as it was before #1464: only knowledge_base_id changed, grants stayed."""
    await db.execute(text("UPDATE documents SET knowledge_base_id = :k WHERE id = :d"),
                     {"k": kb.id, "d": doc.id})
    await db.commit()


async def _sees_doc(db: AsyncSession, asker_id: int, doc: Document) -> bool:
    """Chunk filter as rag_retrieval composes it AND the Python check — they agree."""
    from services.circle_resolver import CircleResolver, atom_from_orm
    from services.circle_sql import document_chunks_circles_filter

    clause, params = document_chunks_circles_filter(asker_id)
    sql_hit = (await db.execute(
        text("SELECT 1 FROM document_chunks dc JOIN documents d ON d.id = dc.document_id "
             "LEFT JOIN knowledge_bases kb ON kb.id = d.knowledge_base_id "
             f"WHERE d.id = :id AND ({clause}) LIMIT 1"),
        {**params, "id": doc.id},
    )).first() is not None
    atom = (await db.execute(select(Atom).where(Atom.atom_id == doc.atom_id))).scalar_one()
    assert await CircleResolver(db).can_access_atom(asker_id, atom_from_orm(atom)) == sql_hit
    return sql_hit


async def _listed_kbs(db: AsyncSession, user: User) -> dict[int, str | None]:
    """``GET /api/knowledge/bases`` as the route computes it: {kb_id: permission}."""
    from api.routes.knowledge import list_knowledge_bases
    from services.rag_service import RAGService

    rows = await list_knowledge_bases(rag=RAGService(db), user=user, db=db)
    return {r.id: r.permission for r in rows}


async def _kb_rights(db: AsyncSession, kb: KnowledgeBase, user: User) -> tuple[bool, bool]:
    from api.routes.knowledge import check_kb_access

    return (await check_kb_access(kb, user, "read", db),
            await check_kb_access(kb, user, "write", db))


def _t(minutes: int) -> datetime:
    return datetime.now(UTC).replace(tzinfo=None) - timedelta(minutes=minutes)


# --------------------------------------------------------------------------- #
class TestDocumentGrantScope:
    async def test_document_grant_opens_the_document_not_the_kb(self, db_session, people):
        alice, bob, _ = people
        haus = await _kb(db_session, alice.id, "Haus")
        brief = await _doc(db_session, haus, "brief")
        other = await _doc(db_session, haus, "anderes")
        await _old_share_kb(db_session, haus, bob.id, alice.id, "admin", _t(0))
        # Keep the grant on ONE document only — a single-document grant.
        await db_session.execute(
            text("DELETE FROM atom_explicit_grants WHERE atom_id = :a"), {"a": other.atom_id})
        await db_session.commit()

        assert haus.id not in await _listed_kbs(db_session, bob)
        assert await _kb_rights(db_session, haus, bob) == (False, False)  # no upload right
        assert await ks.list_kb_shares(db_session, haus.id) == []
        assert await _sees_doc(db_session, bob.id, brief)
        assert not await _sees_doc(db_session, bob.id, other)

    async def test_kb_share_behaves_as_before(self, db_session, people):
        alice, bob, _ = people
        haus = await _kb(db_session, alice.id, "Haus")
        brief = await _doc(db_session, haus, "brief")
        await ks.share_kb(db_session, haus.id, bob.id, "write", granted_by=alice.id)

        assert (await _listed_kbs(db_session, bob))[haus.id] == "write"
        assert await _kb_rights(db_session, haus, bob) == (True, True)
        assert [s["permission"] for s in await ks.list_kb_shares(db_session, haus.id)] == ["write"]
        assert await _sees_doc(db_session, bob.id, brief)


class TestBackfillCarriesLegacySharesOver:
    async def test_after_the_backfill_reach_is_as_before(self, db_session, people):
        """A clean legacy share (old share_kb, by the owner, every document)
        becomes a kb_shares row: the KB list, the level and the documents are
        what the legacy grants gave before #1446 — plus later documents."""
        alice, bob, _ = people
        haus = await _kb(db_session, alice.id, "Haus")
        d1 = await _doc(db_session, haus, "a", created_at=_t(30))
        d2 = await _doc(db_session, haus, "b", created_at=_t(20))
        await _old_share_kb(db_session, haus, bob.id, alice.id, "write", _t(10))
        later = await _doc(db_session, haus, "spaeter")  # after the grant: not partial

        before = await ks.uncovered_legacy_pairs(db_session)
        assert [(p.kb_id, p.user_id, p.reasons) for p in before.clean] == [(haus.id, bob.id, [])]
        assert before.suspicious == []
        assert haus.id not in await _listed_kbs(db_session, bob)  # the precondition gap

        run = await ks.backfill_kb_shares(db_session, dry_run=False)

        assert [(r["kb_id"], r["user_id"]) for r in run.inserted] == [(haus.id, bob.id)]
        assert (await _listed_kbs(db_session, bob))[haus.id] == "write"
        assert await _kb_rights(db_session, haus, bob) == (True, True)
        for doc in (d1, d2, later):
            assert await _sees_doc(db_session, bob.id, doc)
        after = await ks.uncovered_legacy_pairs(db_session)
        assert after.clean == [] and after.suspicious == []

    async def test_a_grant_that_moved_with_its_document_is_never_converted(
        self, db_session, people,
    ):
        """Alice shared "Haus" with Bob, then (before #1464) moved a document to
        "Privat". Converting the moved grant would share all of "Privat"."""
        alice, bob, _ = people
        haus = await _kb(db_session, alice.id, "Haus")
        privat = await _kb(db_session, alice.id, "Privat")
        d1 = await _doc(db_session, haus, "bleibt", created_at=_t(40))
        d2 = await _doc(db_session, haus, "wandert", created_at=_t(40))
        geheim = await _doc(db_session, privat, "geheim", created_at=_t(40))
        await _old_share_kb(db_session, haus, bob.id, alice.id, "read", _t(30))
        await _move_pre_1464(db_session, d2, privat)

        dry = await ks.backfill_kb_shares(db_session, dry_run=True)
        flagged = {(s["kb_id"], s["user_id"]): set(s["reasons"]) for s in dry.suspicious}
        assert flagged[(privat.id, bob.id)] >= {"partial", "split_batch"}
        # The source KB is flagged too (same split statement): a false alarm the
        # owner resolves — preferred over a false share.
        assert "split_batch" in flagged[(haus.id, bob.id)]
        assert dry.clean_missing == 0
        assert set(dry.suspicious[0]) == {"kb_id", "user_id", "grants", "permission", "reasons"}

        run = await ks.backfill_kb_shares(db_session, dry_run=False)

        assert run.inserted == []
        assert (await db_session.execute(text("SELECT COUNT(*) FROM kb_shares"))).scalar() == 0
        assert privat.id not in await _listed_kbs(db_session, bob)
        assert not await _sees_doc(db_session, bob.id, geheim)
        # The grants themselves still open exactly their documents.
        assert await _sees_doc(db_session, bob.id, d1)
        assert await _sees_doc(db_session, bob.id, d2)

    async def test_a_share_made_by_someone_else_than_the_owner_is_suspicious(
        self, db_session, people,
    ):
        alice, bob, admin = people
        haus = await _kb(db_session, alice.id, "Haus")
        await _doc(db_session, haus, "a", created_at=_t(20))
        await _old_share_kb(db_session, haus, bob.id, admin.id, "read", _t(10))

        pairs = await ks.uncovered_legacy_pairs(db_session)

        assert pairs.clean == []
        assert [(p.kb_id, p.reasons) for p in pairs.suspicious] == [(haus.id, ["foreign_granter"])]

    async def test_owner_adopts_a_suspicious_pair_and_reverts_it(self, db_session, people):
        alice, bob, admin = people
        haus = await _kb(db_session, alice.id, "Haus")
        d1 = await _doc(db_session, haus, "a", created_at=_t(20))
        await _old_share_kb(db_session, haus, bob.id, admin.id, "write", _t(10))
        unknown = (haus.id + 1000, bob.id)

        dry = await ks.adopt_kb_share_pairs(db_session, [(haus.id, bob.id), unknown], dry_run=True)
        assert [(s["kb_id"], s["user_id"]) for s in dry.suspicious] == [(haus.id, bob.id)]
        assert dry.skipped == [{"kb_id": unknown[0], "user_id": unknown[1]}]
        assert dry.inserted == []
        assert await ks.get_user_kb_permission_level(db_session, haus.id, bob.id) is None

        run = await ks.adopt_kb_share_pairs(
            db_session, [(haus.id, bob.id), (haus.id, bob.id)], dry_run=False)
        assert [(r["kb_id"], r["user_id"]) for r in run.inserted] == [(haus.id, bob.id)]
        assert await ks.get_user_kb_permission_level(db_session, haus.id, bob.id) == "write"
        # Adopted = no longer uncovered; adopting again changes nothing.
        assert (await ks.uncovered_legacy_pairs(db_session)).suspicious == []
        again = await ks.adopt_kb_share_pairs(db_session, [(haus.id, bob.id)], dry_run=False)
        assert again.inserted == [] and again.skipped == [{"kb_id": haus.id, "user_id": bob.id}]

        assert await ks.revert_kb_share_backfill(db_session, run.inserted) == 1
        assert await ks.get_user_kb_permission_level(db_session, haus.id, bob.id) is None
        assert await _sees_doc(db_session, bob.id, d1)  # the grant still opens its document


class TestBootWarning:
    async def test_warns_with_counts_only(self, db_session, people, monkeypatch):
        from contextlib import asynccontextmanager

        from loguru import logger

        import api.lifecycle as lifecycle

        alice, bob, admin = people
        haus = await _kb(db_session, alice.id, "Haus")
        werk = await _kb(db_session, alice.id, "Werkstatt")
        await _doc(db_session, haus, "a", created_at=_t(20))
        await _doc(db_session, werk, "w", created_at=_t(20))
        await _old_share_kb(db_session, haus, bob.id, alice.id, "read", _t(10))
        await _old_share_kb(db_session, werk, bob.id, admin.id, "read", _t(9))

        @asynccontextmanager
        async def _session():
            yield db_session

        monkeypatch.setattr(lifecycle, "AsyncSessionLocal", _session)
        lines: list[str] = []
        sink = logger.add(lambda m: lines.append(str(m)), level="WARNING")
        try:
            await lifecycle._warn_uncovered_kb_shares()
            monkeypatch.setattr(settings, "auth_enabled", False)
            await lifecycle._warn_uncovered_kb_shares()  # auth off: no-op
        finally:
            logger.remove(sink)

        assert len(lines) == 1
        assert "1 legacy (KB, user) pair(s)" in lines[0]
        assert "1 suspicious pair(s)" in lines[0]

    async def test_a_failing_check_only_warns(self, monkeypatch, people):
        from loguru import logger

        import api.lifecycle as lifecycle

        def _boom():
            raise RuntimeError("db down")

        monkeypatch.setattr(lifecycle, "AsyncSessionLocal", _boom)
        lines: list[str] = []
        sink = logger.add(lambda m: lines.append(str(m)), level="WARNING")
        try:
            await lifecycle._warn_uncovered_kb_shares()
        finally:
            logger.remove(sink)
        assert any("kb_shares precondition check failed" in line for line in lines)


class TestTierOnlyPolicy:
    async def test_document_and_fact_atoms_refuse_extra_policy_keys(self, db_session, people):
        from main import app
        from services.atom_service import AtomService
        from services.auth_service import get_current_user, get_user_or_default
        from services.database import get_db

        alice, _, _ = people
        haus = await _kb(db_session, alice.id, "Haus")
        doc = await _doc(db_session, haus, "a")
        fact_atom = await AtomService(db_session).create_with_source(
            atom_type="document_fact", owner_user_id=alice.id, tier=TIER_SELF)
        await db_session.commit()

        async def _db():
            yield db_session

        app.dependency_overrides[get_db] = _db
        app.dependency_overrides[get_user_or_default] = lambda: alice
        app.dependency_overrides[get_current_user] = lambda: alice
        try:
            async with AsyncClient(transport=ASGITransport(app=app),
                                   base_url="http://test") as c:
                async def _patch(atom_id: str, policy: dict) -> int:
                    return (await c.patch(f"/api/atoms/{atom_id}/tier",
                                          json={"policy": policy})).status_code

                assert await _patch(doc.atom_id, {"tier": 2, "tenant": "acme"}) == 422
                assert await _patch(doc.atom_id, {"tier": 9}) == 422
                assert await _patch(doc.atom_id, {"tier": True}) == 422
                assert await _patch(fact_atom, {"tier": "2"}) == 422
                assert await _patch(fact_atom, {}) == 422
                assert await _patch(doc.atom_id, {"tier": 2}) == 200
        finally:
            app.dependency_overrides.clear()

        policy = (await db_session.execute(
            select(Atom.policy).where(Atom.atom_id == doc.atom_id))).scalar_one()
        assert policy == {"tier": 2}
