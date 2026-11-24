"""An edge is never more open than its source document (#1625).

Real Postgres: every path that writes ``kg_relations.circle_tier`` honours
``tier_cap`` — creation, re-assertion, entity tier change (with and without an
atom), merge, and a tier change on the source document. Plus a source guard: no
copy of the cap-blind recompute may come back.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from models.database import Atom, Document, KGEntity, KnowledgeBase, Role, User
from services.atom_service import AtomService
from services.kg_edge_tier import capped_edge_tier, lowered_cap
from services.knowledge_graph_service import (
    KnowledgeGraphService,
    _reconcile_document_edges,
    _source_tier_cap,
)

_seq = 0


def _next() -> int:
    global _seq
    _seq += 1
    return _seq


def _commit_as_flush(db, monkeypatch):
    monkeypatch.setattr(db, "commit", db.flush)


async def _make_user(db: AsyncSession, name: str) -> User:
    role = Role(name=f"{name}_role")
    db.add(role)
    await db.flush()
    u = User(username=name, email=f"{name}@ex.test", password_hash="x",
             role_id=role.id, is_active=True)
    db.add(u)
    await db.flush()
    return u


async def _entity(db, owner, name, *, tier: int, with_atom: bool = False) -> KGEntity:
    atom_id = None
    if with_atom:
        atom_id = f"00000000-0000-0000-1625-{_next():012d}"
        db.add(Atom(atom_id=atom_id, atom_type="kg_node", source_table="kg_entities",
                    source_id="0", owner_user_id=owner.id, policy={"tier": tier}))
        await db.flush()
    e = KGEntity(user_id=owner.id, name=name, entity_type="organization",
                 circle_tier=tier, atom_id=atom_id)
    db.add(e)
    await db.flush()
    if with_atom:
        await db.execute(text("UPDATE atoms SET source_id = :s WHERE atom_id = :a"),
                         {"s": str(e.id), "a": atom_id})
    return e


async def _doc(db, owner, *, tier: int) -> Document:
    kb = KnowledgeBase(name=f"KB {_next()}", is_active=True, owner_id=owner.id)
    db.add(kb)
    await db.flush()
    atom_id = f"00000000-0000-0000-1626-{_next():012d}"
    db.add(Atom(atom_id=atom_id, atom_type="kb_document", source_table="documents",
                source_id="0", owner_user_id=owner.id, policy={"tier": tier}))
    await db.flush()
    doc = Document(filename="d.pdf", file_path="/x/d.pdf", status="completed",
                   chunk_count=0, knowledge_base_id=kb.id, circle_tier=tier, atom_id=atom_id)
    db.add(doc)
    await db.flush()
    await db.execute(text("UPDATE atoms SET source_id = :s WHERE atom_id = :a"),
                     {"s": str(doc.id), "a": atom_id})
    return doc


async def _edge_state(db, rel_id: int) -> tuple:
    """(edge tier, cap, atom policy tier)."""
    return (await db.execute(text(
        "SELECT r.circle_tier, r.tier_cap, (a.policy->>'tier')::int "
        "FROM kg_relations r LEFT JOIN atoms a ON a.atom_id = r.atom_id WHERE r.id = :r"),
        {"r": rel_id})).one()


class TestPureRule:
    @pytest.mark.unit
    def test_capped_edge_tier(self):
        assert capped_edge_tier([2, 2], None) == 2
        assert capped_edge_tier([2, 2], 0) == 0
        assert capped_edge_tier([1, 3], 2) == 1
        assert capped_edge_tier([], None) == 0

    @pytest.mark.unit
    def test_cap_only_goes_down(self):
        assert lowered_cap(None, 2) == 2
        assert lowered_cap(0, 3) == 0
        assert lowered_cap(3, 1) == 1


class TestSaveRelation:
    pytestmark = [pytest.mark.postgres, pytest.mark.asyncio]

    async def test_new_edge_is_capped_by_the_document(self, pg_db_session):
        owner = await _make_user(pg_db_session, "cap_new")
        a = await _entity(pg_db_session, owner, "Org A", tier=2)
        b = await _entity(pg_db_session, owner, "Org B", tier=2)
        rel = await KnowledgeGraphService(pg_db_session).save_relation(
            subject_id=a.id, predicate="beliefert", object_id=b.id,
            user_id=owner.id, source_session_id="doc:1", tier_cap=0,
        )
        assert await _edge_state(pg_db_session, rel.id) == (0, 0, 0)

    async def test_no_cap_keeps_the_endpoint_rule(self, pg_db_session):
        owner = await _make_user(pg_db_session, "cap_none")
        a = await _entity(pg_db_session, owner, "Org C", tier=2)
        b = await _entity(pg_db_session, owner, "Org D", tier=3)
        rel = await KnowledgeGraphService(pg_db_session).save_relation(
            subject_id=a.id, predicate="kennt", object_id=b.id, user_id=owner.id,
        )
        assert await _edge_state(pg_db_session, rel.id) == (2, None, 2)

    async def test_reassertion_lowers_and_never_raises(self, pg_db_session):
        owner = await _make_user(pg_db_session, "cap_again")
        a = await _entity(pg_db_session, owner, "Org E", tier=2)
        b = await _entity(pg_db_session, owner, "Org F", tier=2)
        svc = KnowledgeGraphService(pg_db_session)
        first = await svc.save_relation(
            subject_id=a.id, predicate="kennt", object_id=b.id, user_id=owner.id,
        )
        assert await _edge_state(pg_db_session, first.id) == (2, None, 2)
        # A private document asserts the same triple: the edge drops to it.
        again = await svc.save_relation(
            subject_id=a.id, predicate="kennt", object_id=b.id, user_id=owner.id,
            source_session_id="doc:2", tier_cap=0,
        )
        assert again.id == first.id
        assert await _edge_state(pg_db_session, first.id) == (0, 0, 0)
        # A more open document later changes nothing.
        await svc.save_relation(
            subject_id=a.id, predicate="kennt", object_id=b.id, user_id=owner.id,
            source_session_id="doc:3", tier_cap=4,
        )
        assert await _edge_state(pg_db_session, first.id) == (0, 0, 0)


class TestRecomputeKeepsTheCap:
    pytestmark = [pytest.mark.postgres, pytest.mark.asyncio]

    async def test_entity_tier_change_without_atom(self, pg_db_session, monkeypatch):
        _commit_as_flush(pg_db_session, monkeypatch)
        owner = await _make_user(pg_db_session, "cap_ent")
        a = await _entity(pg_db_session, owner, "Org G", tier=2)
        b = await _entity(pg_db_session, owner, "Org H", tier=2)
        svc = KnowledgeGraphService(pg_db_session)
        rel = await svc.save_relation(
            subject_id=a.id, predicate="kennt", object_id=b.id, user_id=owner.id, tier_cap=1,
        )
        await svc.update_entity_circle_tier(a.id, 4)
        await svc.update_entity_circle_tier(b.id, 4)
        assert await _edge_state(pg_db_session, rel.id) == (1, 1, 1)
        # Below the cap the endpoints still win.
        await svc.update_entity_circle_tier(a.id, 0)
        assert await _edge_state(pg_db_session, rel.id) == (0, 1, 0)

    async def test_entity_tier_change_through_the_atom(self, pg_db_session, monkeypatch):
        _commit_as_flush(pg_db_session, monkeypatch)
        owner = await _make_user(pg_db_session, "cap_atom")
        a = await _entity(pg_db_session, owner, "Org I", tier=2, with_atom=True)
        b = await _entity(pg_db_session, owner, "Org J", tier=2, with_atom=True)
        rel = await KnowledgeGraphService(pg_db_session).save_relation(
            subject_id=a.id, predicate="kennt", object_id=b.id, user_id=owner.id, tier_cap=0,
        )
        svc = AtomService(pg_db_session)
        await svc.update_tier(a.atom_id, {"tier": 4})
        await svc.update_tier(b.atom_id, {"tier": 4})
        assert await _edge_state(pg_db_session, rel.id) == (0, 0, 0)

    async def test_merge_keeps_the_lowest_cap(self, pg_db_session, monkeypatch):
        _commit_as_flush(pg_db_session, monkeypatch)
        owner = await _make_user(pg_db_session, "cap_merge")
        a = await _entity(pg_db_session, owner, "Org K", tier=2)
        b = await _entity(pg_db_session, owner, "Org K GmbH", tier=2)
        c = await _entity(pg_db_session, owner, "Org L", tier=2)
        svc = KnowledgeGraphService(pg_db_session)
        monkeypatch.setattr(svc, "_get_embedding", _no_embedding)
        uncapped = await svc.save_relation(
            subject_id=a.id, predicate="kennt", object_id=c.id, user_id=owner.id,
        )
        capped = await svc.save_relation(
            subject_id=b.id, predicate="kennt", object_id=c.id, user_id=owner.id, tier_cap=0,
        )
        # Folding b into a makes the two edges one triple; the survivor (lowest
        # id) must carry the cap of the edge it absorbed.
        assert await svc.merge_entities(loser_id=b.id, winner_id=a.id) is not None
        assert uncapped.id < capped.id
        assert await _edge_state(pg_db_session, uncapped.id) == (0, 0, 0)


async def _no_embedding(_text):
    return None


class TestDocumentTierChange:
    pytestmark = [pytest.mark.postgres, pytest.mark.asyncio]

    async def test_lowering_the_document_lowers_its_edges_raising_does_not(
        self, pg_db_session, monkeypatch,
    ):
        _commit_as_flush(pg_db_session, monkeypatch)
        owner = await _make_user(pg_db_session, "cap_doc")
        doc = await _doc(pg_db_session, owner, tier=2)
        a = await _entity(pg_db_session, owner, "Org M", tier=2)
        b = await _entity(pg_db_session, owner, "Org N", tier=2)
        other = await _entity(pg_db_session, owner, "Org O", tier=2)
        svc = KnowledgeGraphService(pg_db_session)
        rel = await svc.save_relation(
            subject_id=a.id, predicate="kennt", object_id=b.id, user_id=owner.id,
            source_session_id=f"doc:{doc.id}", tier_cap=2,
        )
        unrelated = await svc.save_relation(
            subject_id=a.id, predicate="kennt", object_id=other.id, user_id=owner.id,
            source_session_id="doc:999999", tier_cap=2,
        )
        atoms = AtomService(pg_db_session)
        await atoms.update_tier(doc.atom_id, {"tier": 0})
        assert await _edge_state(pg_db_session, rel.id) == (0, 0, 0)
        assert await _edge_state(pg_db_session, unrelated.id) == (2, 2, 2)
        await atoms.update_tier(doc.atom_id, {"tier": 4})
        assert await _edge_state(pg_db_session, rel.id) == (0, 0, 0)


class TestDirectEdgeTierEdit:
    """PATCH /api/atoms/{id}/tier on a kg_edge atom goes through update_tier."""

    pytestmark = [pytest.mark.postgres, pytest.mark.asyncio]

    async def test_cannot_lift_an_edge_above_its_cap(self, pg_db_session, monkeypatch):
        _commit_as_flush(pg_db_session, monkeypatch)
        owner = await _make_user(pg_db_session, "cap_patch")
        a = await _entity(pg_db_session, owner, "Org P", tier=4)
        b = await _entity(pg_db_session, owner, "Org Q", tier=4)
        rel = await KnowledgeGraphService(pg_db_session).save_relation(
            subject_id=a.id, predicate="kennt", object_id=b.id, user_id=owner.id, tier_cap=0,
        )
        await AtomService(pg_db_session).update_tier(rel.atom_id, {"tier": 4})
        assert await _edge_state(pg_db_session, rel.id) == (0, 0, 0)

    async def test_can_lower_an_edge(self, pg_db_session, monkeypatch):
        _commit_as_flush(pg_db_session, monkeypatch)
        owner = await _make_user(pg_db_session, "cap_lower")
        a = await _entity(pg_db_session, owner, "Org R", tier=2)
        b = await _entity(pg_db_session, owner, "Org S", tier=2)
        rel = await KnowledgeGraphService(pg_db_session).save_relation(
            subject_id=a.id, predicate="kennt", object_id=b.id, user_id=owner.id,
        )
        await AtomService(pg_db_session).update_tier(rel.atom_id, {"tier": 1})
        assert await _edge_state(pg_db_session, rel.id) == (1, None, 1)


class TestHookReconcilesAtTheEnd:
    pytestmark = [pytest.mark.postgres, pytest.mark.asyncio]

    async def test_document_lowered_during_extraction(self, pg_db_session, monkeypatch):
        """The edge was saved with the cap read at the START of the hook; the
        document was lowered meanwhile. The end-of-hook pass catches it."""
        _commit_as_flush(pg_db_session, monkeypatch)
        owner = await _make_user(pg_db_session, "cap_race")
        doc = await _doc(pg_db_session, owner, tier=2)
        a = await _entity(pg_db_session, owner, "Org T", tier=2)
        b = await _entity(pg_db_session, owner, "Org U", tier=2)
        rel = await KnowledgeGraphService(pg_db_session).save_relation(
            subject_id=a.id, predicate="kennt", object_id=b.id, user_id=owner.id,
            source_session_id=f"doc:{doc.id}", tier_cap=2,
        )
        await pg_db_session.execute(
            text("UPDATE documents SET circle_tier = 0 WHERE id = :d"), {"d": doc.id})
        await _reconcile_document_edges(pg_db_session, doc.id)
        assert await _edge_state(pg_db_session, rel.id) == (0, 0, 0)


class TestHookReadsTheDocumentTier:
    pytestmark = [pytest.mark.postgres, pytest.mark.asyncio]

    async def test_tier_from_the_document(self, pg_db_session):
        owner = await _make_user(pg_db_session, "cap_hook")
        doc = await _doc(pg_db_session, owner, tier=3)
        assert await _source_tier_cap(pg_db_session, doc.id) == 3

    async def test_fail_closed_without_a_document(self, pg_db_session):
        assert await _source_tier_cap(pg_db_session, None) == 0
        assert await _source_tier_cap(pg_db_session, 987654321) == 0


# --- source guard --------------------------------------------------------------

_CAP_BLIND = re.compile(r"LEAST\(\s*s\.circle_tier\s*,\s*o\.circle_tier\s*\)")


def _cap_blind_sites(root: Path) -> list[str]:
    hits = []
    for path in root.rglob("*.py"):
        rel = path.relative_to(root).as_posix()
        if rel.startswith("alembic/"):  # committed history, never edited
            continue
        if _CAP_BLIND.search(path.read_text(encoding="utf-8")):
            hits.append(rel)
    return sorted(hits)


@pytest.mark.unit
def test_no_cap_blind_edge_tier_recompute():
    """Every edge-tier recompute goes through services/kg_edge_tier.py."""
    import services.database as _db

    root = Path(_db.__file__).resolve().parents[1]
    assert _cap_blind_sites(root) == []


@pytest.mark.unit
def test_cap_blind_guard_finds_the_old_pattern(tmp_path):
    """Negative control: the guard really sees the cap-blind recompute."""
    (tmp_path / "svc.py").write_text(
        '"UPDATE kg_relations r SET circle_tier = LEAST(s.circle_tier, o.circle_tier) "\n'
    )
    (tmp_path / "ok.py").write_text(
        '"LEAST(s.circle_tier, o.circle_tier, COALESCE(r.tier_cap, 4))"\n'
    )
    assert _cap_blind_sites(tmp_path) == ["svc.py"]
