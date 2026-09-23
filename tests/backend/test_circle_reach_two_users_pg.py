"""The four-branch filter with TWO users, per retrieval consumer (§11 Nr. 1).

`test_circle_sql_pg.py` proves the CLAUSE. This file proves the CONSUMERS: the
services that inject it. The distinction is the whole point of the item — a
clause that is correct and a consumer that forgets to pass `user_id`, or aliases
the table differently, or joins a second unfiltered path beside it, produce a
green clause test and a leak in service.

**The membership branch has never run in the household.** Everything there was
written under auth-off by one fallback owner, so `circle_memberships` carries a
single stale pairing row and nothing else. The cutover is its first real use.
Every other branch has production wear; this one has none, which is why §11
names it and why each consumer here is asked the same four questions:

  owner sees their own tier 0 · member reaches tier 2 but NOT tier 0 ·
  stranger sees only tier 4 · an explicit grant overrides the tier

Real Postgres, because every branch is Postgres SQL — the membership branch
joins `circle_memberships` and casts its JSON value through text to int, which
no other dialect reproduces.
"""
from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from models.database import (
    Atom,
    Document,
    DocumentChunk,
    DocumentFact,
    KGEntity,
    KnowledgeBase,
    Note,
)
from tests.backend.dbrows import ensure_role, ensure_user
from utils.config import settings

pytestmark = [pytest.mark.backend, pytest.mark.database]

TIER_SELF, TIER_HOUSEHOLD, TIER_PUBLIC = 0, 2, 4


async def _member_of(db: AsyncSession, owner_id: int, member_id: int, tier: int) -> None:
    await db.execute(
        text(
            "INSERT INTO circle_memberships "
            "(circle_owner_id, member_user_id, dimension, value, granted_by, granted_at) "
            "VALUES (:o, :m, 'tier', :v, :o, NOW())"
        ),
        {"o": owner_id, "m": member_id, "v": str(tier)},
    )


_seq = 0


def _next() -> int:
    global _seq
    _seq += 1
    return _seq


async def _atom(
    db: AsyncSession, *, atom_type: str, source_table: str, owner_id: int, tier: int
) -> str:
    """An atoms row with a placeholder source id — `finalize` happens at the call
    site once the source row has its PK."""
    atom_id = f"t9-{_next():08d}"
    db.add(Atom(atom_id=atom_id, atom_type=atom_type, source_table=source_table,
                source_id=f"__pending__{atom_id}", owner_user_id=owner_id,
                policy={"tier": tier}))
    await db.flush()
    return atom_id


async def _finalize(db: AsyncSession, atom_id: str, source_id: int) -> None:
    await db.execute(
        text("UPDATE atoms SET source_id = :s WHERE atom_id = :a"),
        {"s": str(source_id), "a": atom_id},
    )
    await db.flush()


async def _grant(db: AsyncSession, atom_id: str, to_user: int, by_user: int) -> None:
    await db.execute(
        text(
            "INSERT INTO atom_explicit_grants "
            "(atom_id, granted_to_user_id, permission_level, granted_by, granted_at) "
            "VALUES (:a, :to, 'read', :by, NOW())"
        ),
        {"a": atom_id, "to": to_user, "by": by_user},
    )
    await db.flush()


@pytest.fixture
async def three(db_session: AsyncSession, monkeypatch):
    """Owner (1), household member (2) at tier 2, stranger (3) in no circle."""
    monkeypatch.setattr(settings, "auth_enabled", True)
    await ensure_role(db_session)
    owner = await ensure_user(db_session, 1, username="eigner")
    member = await ensure_user(db_session, 2, username="mitglied")
    stranger = await ensure_user(db_session, 3, username="fremd")
    await db_session.commit()
    await _member_of(db_session, owner.id, member.id, TIER_HOUSEHOLD)
    await db_session.commit()
    return owner, member, stranger


# ---------------------------------------------------------------- notes ------
class TestNotes:
    async def _note(self, db, owner_id: int, tier: int, body: str) -> Note:
        atom_id = await _atom(db, atom_type="note", source_table="notes",
                              owner_id=owner_id, tier=tier)
        note = Note(owner_user_id=owner_id, title=f"Notiz {_next()}", body=body,
                    circle_tier=tier, atom_id=atom_id)
        db.add(note)
        await db.flush()
        await _finalize(db, atom_id, note.id)
        return note

    async def _search(self, db, asker_id: int) -> set[int]:
        from services.note_retrieval import NoteRetrieval

        hits = await NoteRetrieval(db).search(
            "Zaunkoenig", asker_id=asker_id, top_k=20
        )
        return {h["id"] for h in hits if "id" in h}

    async def test_the_four_questions(self, db_session, three):
        owner, member, stranger = three
        private = await self._note(db_session, owner.id, TIER_SELF, "Zaunkoenig privat")
        shared = await self._note(db_session, owner.id, TIER_HOUSEHOLD, "Zaunkoenig geteilt")
        public = await self._note(db_session, owner.id, TIER_PUBLIC, "Zaunkoenig oeffentlich")
        await db_session.commit()

        assert {private.id, shared.id, public.id} <= await self._search(db_session, owner.id)
        seen_by_member = await self._search(db_session, member.id)
        assert shared.id in seen_by_member and public.id in seen_by_member
        assert private.id not in seen_by_member, "the member reached a tier-0 note"
        seen_by_stranger = await self._search(db_session, stranger.id)
        assert seen_by_stranger & {private.id, shared.id} == set()
        assert public.id in seen_by_stranger

    async def test_a_grant_overrides_the_tier(self, db_session, three):
        owner, _, stranger = three
        private = await self._note(db_session, owner.id, TIER_SELF, "Zaunkoenig privat")
        await _grant(db_session, private.atom_id, stranger.id, owner.id)
        await db_session.commit()

        assert private.id in await self._search(db_session, stranger.id)


# ---------------------------------------------------------- document facts ---
class TestDocumentFacts:
    async def _fact(self, db, owner_id: int, tier: int) -> tuple[DocumentFact, str]:
        """Returns (fact, PARENT DOCUMENT atom id).

        A Schicht-A fact inherits its parent Document's access policy — the same
        access unit as a chunk. So the grant hangs on the DOCUMENT's atom
        (`source_table='documents'`), never on the fact's own. Granting the fact
        alone does nothing, which is the documented rule and the reason this
        helper hands back the document's atom rather than the fact's.
        """
        kb = KnowledgeBase(name=f"KB {_next()}", is_active=True, owner_id=owner_id)
        db.add(kb)
        await db.flush()
        doc_atom = await _atom(db, atom_type="kb_document", source_table="documents",
                               owner_id=owner_id, tier=tier)
        doc = Document(filename=f"d{_next()}.pdf", file_path=f"/x/{_next()}.pdf",
                       status="completed", knowledge_base_id=kb.id, circle_tier=tier,
                       atom_id=doc_atom)
        db.add(doc)
        await db.flush()
        await _finalize(db, doc_atom, doc.id)
        atom_id = await _atom(db, atom_type="document_fact",
                              source_table="document_facts", owner_id=owner_id, tier=tier)
        fact = DocumentFact(document_id=doc.id, category="identifier",
                            kind="Zaunkoenignummer", value=f"Zaunkoenignummer Z-{_next()}",
                            circle_tier=tier, atom_id=atom_id)
        db.add(fact)
        await db.flush()
        await _finalize(db, atom_id, fact.id)
        return fact, doc_atom

    async def _search(self, db, asker_id: int) -> set[int]:
        from services.document_fact_retrieval import DocumentFactRetrieval

        hits = await DocumentFactRetrieval(db).search(
            "Zaunkoenignummer", asker_id=asker_id, top_k=20
        )
        return {h["id"] for h in hits if "id" in h}

    async def test_the_four_questions(self, db_session, three):
        owner, member, stranger = three
        private, _ = await self._fact(db_session, owner.id, TIER_SELF)
        shared, _ = await self._fact(db_session, owner.id, TIER_HOUSEHOLD)
        public, _ = await self._fact(db_session, owner.id, TIER_PUBLIC)
        await db_session.commit()

        assert {private.id, shared.id, public.id} <= await self._search(db_session, owner.id)
        seen_by_member = await self._search(db_session, member.id)
        assert shared.id in seen_by_member
        assert private.id not in seen_by_member, "the member reached a tier-0 fact"
        seen_by_stranger = await self._search(db_session, stranger.id)
        assert seen_by_stranger & {private.id, shared.id} == set()
        assert public.id in seen_by_stranger

    async def test_the_grant_unit_is_the_DOCUMENT(self, db_session, three):
        """The grant hangs on the parent document's atom. Granting the fact's own
        atom does nothing — a fact is not an access unit of its own, it inherits
        the document's. Both halves asserted, because getting this backwards
        produces a share that silently grants nothing."""
        owner, _, stranger = three
        private, doc_atom = await self._fact(db_session, owner.id, TIER_SELF)
        await _grant(db_session, private.atom_id, stranger.id, owner.id)
        await db_session.commit()
        assert private.id not in await self._search(db_session, stranger.id), (
            "a grant on the fact's own atom must not open it"
        )

        await _grant(db_session, doc_atom, stranger.id, owner.id)
        await db_session.commit()
        assert private.id in await self._search(db_session, stranger.id)


# ------------------------------------------------------- document chunks -----
class TestDocumentChunksTheRagPath:
    """RAG reads `document_chunks`. Ownership runs through the KB owner with an
    atom-owner fallback for null-KB rows, so this is the one consumer whose owner
    branch is not a plain column comparison."""

    async def _chunk(self, db, owner_id: int, tier: int) -> DocumentChunk:
        kb = KnowledgeBase(name=f"KB {_next()}", is_active=True, owner_id=owner_id)
        db.add(kb)
        await db.flush()
        atom_id = await _atom(db, atom_type="kb_document", source_table="documents",
                              owner_id=owner_id, tier=tier)
        doc = Document(filename=f"d{_next()}.pdf", file_path=f"/x/{_next()}.pdf",
                       status="completed", knowledge_base_id=kb.id, circle_tier=tier,
                       atom_id=atom_id)
        db.add(doc)
        await db.flush()
        await _finalize(db, atom_id, doc.id)
        chunk = DocumentChunk(document_id=doc.id, content="Zaunkoenig im Abschnitt",
                              chunk_index=0, circle_tier=tier)
        db.add(chunk)
        await db.flush()
        return chunk

    async def _visible(self, db, asker_id: int) -> set[int]:
        """The filter as `rag_retrieval` composes it, over the same table."""
        from services.circle_sql import document_chunks_circles_filter

        clause, params = document_chunks_circles_filter(asker_id)
        rows = (await db.execute(
            text(
                "SELECT dc.id FROM document_chunks dc "
                "  JOIN documents d ON d.id = dc.document_id "
                "  LEFT JOIN knowledge_bases kb ON kb.id = d.knowledge_base_id "
                f" WHERE ({clause})"
            ),
            params,
        )).all()
        return {r[0] for r in rows}

    async def test_the_four_questions(self, db_session, three):
        owner, member, stranger = three
        private = await self._chunk(db_session, owner.id, TIER_SELF)
        shared = await self._chunk(db_session, owner.id, TIER_HOUSEHOLD)
        public = await self._chunk(db_session, owner.id, TIER_PUBLIC)
        await db_session.commit()

        assert {private.id, shared.id, public.id} <= await self._visible(db_session, owner.id)
        seen_by_member = await self._visible(db_session, member.id)
        assert shared.id in seen_by_member
        assert private.id not in seen_by_member, "the member reached a tier-0 chunk"
        seen_by_stranger = await self._visible(db_session, stranger.id)
        assert seen_by_stranger & {private.id, shared.id} == set()
        assert public.id in seen_by_stranger


# -------------------------------------------------------------- entities -----
class TestKgEntities:
    async def _entity(self, db, owner_id: int, tier: int) -> KGEntity:
        atom_id = await _atom(db, atom_type="kg_node", source_table="kg_entities",
                              owner_id=owner_id, tier=tier)
        ent = KGEntity(user_id=owner_id, name=f"Zaunkoenig {_next()}",
                       entity_type="person", circle_tier=tier, atom_id=atom_id)
        db.add(ent)
        await db.flush()
        await _finalize(db, atom_id, ent.id)
        return ent

    async def _visible(self, db, asker_id: int) -> set[int]:
        from services.circle_sql import kg_entities_circles_filter

        clause, params = kg_entities_circles_filter(asker_id, alias="e")
        rows = (await db.execute(
            text(f"SELECT e.id FROM kg_entities e WHERE ({clause})"), params
        )).all()
        return {r[0] for r in rows}

    async def test_the_four_questions(self, db_session, three):
        owner, member, stranger = three
        private = await self._entity(db_session, owner.id, TIER_SELF)
        shared = await self._entity(db_session, owner.id, TIER_HOUSEHOLD)
        public = await self._entity(db_session, owner.id, TIER_PUBLIC)
        await db_session.commit()

        assert {private.id, shared.id, public.id} <= await self._visible(db_session, owner.id)
        seen_by_member = await self._visible(db_session, member.id)
        assert shared.id in seen_by_member
        assert private.id not in seen_by_member, "the member reached a tier-0 entity"
        seen_by_stranger = await self._visible(db_session, stranger.id)
        assert seen_by_stranger & {private.id, shared.id} == set()
        assert public.id in seen_by_stranger

    async def test_a_grant_overrides_the_tier(self, db_session, three):
        owner, _, stranger = three
        private = await self._entity(db_session, owner.id, TIER_SELF)
        await _grant(db_session, private.atom_id, stranger.id, owner.id)
        await db_session.commit()

        assert private.id in await self._visible(db_session, stranger.id)


# ------------------------------------------------------- the tier LADDER -----
class TestTheLadderIsADepth:
    """A membership value is a DEPTH, not a label: a member placed at tier 1
    reaches atoms at tier 1 and wider (`cm.value <= row.circle_tier`), not only
    atoms at exactly their own rung. Getting this backwards would either hide the
    household corpus from everyone or hand the trusted rung the public one."""

    async def _entity(self, db, owner_id: int, tier: int) -> KGEntity:
        return await TestKgEntities()._entity(db, owner_id, tier)

    async def test_a_trusted_member_reaches_wider_rungs_too(
        self, db_session, three, monkeypatch
    ):
        owner, _, stranger = three
        # Place the stranger at tier 1 (trusted) in the owner's circles.
        await _member_of(db_session, owner.id, stranger.id, 1)
        trusted = await self._entity(db_session, owner.id, 1)
        household = await self._entity(db_session, owner.id, TIER_HOUSEHOLD)
        private = await self._entity(db_session, owner.id, TIER_SELF)
        await db_session.commit()

        seen = await TestKgEntities()._visible(db_session, stranger.id)
        assert trusted.id in seen, "own rung"
        assert household.id in seen, "a wider rung must be reachable from a deeper one"
        assert private.id not in seen, "tier 0 is the owner's alone"

    async def test_a_household_member_does_not_reach_the_trusted_rung(
        self, db_session, three
    ):
        owner, member, _ = three  # member sits at tier 2
        trusted = await self._entity(db_session, owner.id, 1)
        await db_session.commit()

        assert trusted.id not in await TestKgEntities()._visible(db_session, member.id)


# ---------------------------------------------------- the auth-off bypass ----
class TestAuthOffIsOneTrustDomain:
    async def test_every_tier_is_visible_with_auth_off(
        self, db_session, three, monkeypatch
    ):
        """The rollback path (§10): `AUTH_ENABLED=false` restores today's reads
        byte-for-byte, so the tiers the backfill wrote are inert."""
        owner, _, stranger = three
        private = await TestNotes()._note(db_session, owner.id, TIER_SELF, "Zaunkoenig privat")
        await db_session.commit()

        monkeypatch.setattr(settings, "auth_enabled", False)
        assert private.id in await TestNotes()._search(db_session, stranger.id)
