"""#875 Stufe 1 — the validity filter on every live-relation read path, real Postgres.

For each consumer (docs/design/kg-bitemporal-edges.md §6, §11):
- an edge whose ``valid_to`` lies in the PAST disappears when the filter is on,
- the same edge is still there when the filter is off (Stufe 1 is inert by default),
- an edge whose ``valid_to`` lies in the FUTURE stays visible,
plus a circle-leak guard (R2): turning validity on never widens what a
non-member sees.

``valid_to`` is set by hand — Stufe 1 has no detector that would set it.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from models.database import (
    EMBEDDING_DIMENSION,
    KG_MERGE_PROPOSAL_PENDING,
    KGEntity,
    KgMergeProposal,
    KGRelation,
    Role,
    User,
)
from services import graph_expansion
from services.atom_types import Atom, AtomMatch
from services.kg_graph_service import KGGraphService
from services.kg_retrieval import KGRetrieval
from services.knowledge_graph_service import KnowledgeGraphService
from utils.config import settings

pytestmark = [pytest.mark.postgres, pytest.mark.asyncio]

_NOW = datetime.now(UTC).replace(tzinfo=None)
_PAST = _NOW - timedelta(days=30)
_FUTURE = _NOW + timedelta(days=30)


def _unit(i: int) -> list[float]:
    v = [0.0] * EMBEDDING_DIMENSION
    v[i % EMBEDDING_DIMENSION] = 1.0
    return v


async def _user(db: AsyncSession, name: str) -> User:
    role = Role(name=f"{name}_role")
    db.add(role)
    await db.flush()
    u = User(username=name, email=f"{name}@ex.test", password_hash="x",
             role_id=role.id, is_active=True)
    db.add(u)
    await db.flush()
    return u


async def _entity(db, owner, name, *, tier=0, emb=None) -> KGEntity:
    e = KGEntity(user_id=owner.id, name=name, entity_type="person", circle_tier=tier,
                 is_active=True, embedding=emb)
    db.add(e)
    await db.flush()
    return e


async def _rel(db, owner, s, o, pred="wohnt_in", *, valid_to=None) -> KGRelation:
    r = KGRelation(user_id=owner.id, subject_id=s.id, predicate=pred, object_id=o.id,
                   circle_tier=min(s.circle_tier, o.circle_tier), is_active=True,
                   valid_to=valid_to)
    db.add(r)
    await db.flush()
    return r


def _filter(monkeypatch, on: bool) -> None:
    monkeypatch.setattr(settings, "kg_validity_filter_enabled", on)
    monkeypatch.setattr(settings, "auth_enabled", False)


def _pivot(e: KGEntity) -> AtomMatch:
    return AtomMatch(
        atom=Atom(atom_id=f"kg_node:{e.id}", atom_type="kg_node", owner_user_id=0,
                  policy={"tier": e.circle_tier}, created_at=_NOW, updated_at=_NOW,
                  payload={"entity_id": e.id, "name": e.name, "entity_type": e.entity_type}),
        score=0.9, snippet=e.name, rank=1,
    )


# ---------------------------------------------------------------------------
# graph_expansion — expand_fused (per-hop BFS) + _edges_within (edge atoms)
# ---------------------------------------------------------------------------
class TestGraphExpansion:
    async def _run(self, db, monkeypatch, *, on: bool, valid_to):
        _filter(monkeypatch, on)
        monkeypatch.setattr(settings, "graph_expansion_enabled", True)
        owner = await _user(db, f"gx875_{on}_{valid_to is not None}")
        erika = await _entity(db, owner, "Erika")
        bonn = await _entity(db, owner, "Bonn")
        await _rel(db, owner, erika, bonn, valid_to=valid_to)
        out = await graph_expansion.expand_fused([_pivot(erika)], owner.id, db)
        nodes = {m.atom.payload["entity_id"] for m in out if m.atom.atom_type == "kg_node"}
        return bonn.id in nodes

    async def test_expired_edge_not_traversed_when_on(self, pg_db_session, monkeypatch):
        assert await self._run(pg_db_session, monkeypatch, on=True, valid_to=_PAST) is False

    async def test_expired_edge_traversed_when_off(self, pg_db_session, monkeypatch):
        assert await self._run(pg_db_session, monkeypatch, on=False, valid_to=_PAST) is True

    async def test_future_valid_to_still_traversed(self, pg_db_session, monkeypatch):
        assert await self._run(pg_db_session, monkeypatch, on=True, valid_to=_FUTURE) is True

    async def test_expired_edge_atom_dropped_between_visible_nodes(self, pg_db_session, monkeypatch):
        """_edges_within: both endpoints reachable via a LIVE edge, the expired
        parallel edge between them must not surface as a kg_edge atom."""
        _filter(monkeypatch, True)
        monkeypatch.setattr(settings, "graph_expansion_enabled", True)
        owner = await _user(pg_db_session, "gx875_within")
        a = await _entity(pg_db_session, owner, "Anna")
        b = await _entity(pg_db_session, owner, "Berlin")
        await _rel(pg_db_session, owner, a, b, pred="wohnt_in")
        await _rel(pg_db_session, owner, a, b, pred="arbeitet_in", valid_to=_PAST)
        out = await graph_expansion.expand_fused([_pivot(a)], owner.id, pg_db_session)
        preds = {m.atom.payload.get("predicate") for m in out if m.atom.atom_type == "kg_edge"}
        assert "wohnt_in" in preds
        assert "arbeitet_in" not in preds


# ---------------------------------------------------------------------------
# kg_retrieval — get_relevant_context (agent context) + get_relevant_atoms
# ---------------------------------------------------------------------------
def _retrieval(db, monkeypatch, seed_vec) -> KGRetrieval:
    kg = KGRetrieval(db)
    monkeypatch.setattr(kg, "_extract_query_entities", AsyncMock(return_value=[]))
    monkeypatch.setattr(kg, "_get_embedding", AsyncMock(return_value=seed_vec))
    return kg


class TestKgRetrieval:
    async def _seed(self, db, name):
        owner = await _user(db, name)
        erika = await _entity(db, owner, "Erika", emb=_unit(7))
        bonn = await _entity(db, owner, "Bonn", emb=_unit(8))
        berlin = await _entity(db, owner, "Berlin", emb=_unit(9))
        await _rel(db, owner, erika, bonn, valid_to=_PAST)
        await _rel(db, owner, erika, berlin)
        return owner, bonn, berlin

    async def test_context_hides_expired_when_on(self, pg_db_session, monkeypatch):
        _filter(monkeypatch, True)
        monkeypatch.setattr(settings, "graph_expansion_enabled", False)
        owner, _, _ = await self._seed(pg_db_session, "kr875_on")
        out = await _retrieval(pg_db_session, monkeypatch, _unit(7)).get_relevant_context(
            "Wo wohnt Erika?", user_id=owner.id)
        assert "Berlin" in (out or "")
        assert "Bonn" not in (out or "")

    async def test_context_keeps_expired_when_off(self, pg_db_session, monkeypatch):
        _filter(monkeypatch, False)
        monkeypatch.setattr(settings, "graph_expansion_enabled", False)
        owner, _, _ = await self._seed(pg_db_session, "kr875_off")
        out = await _retrieval(pg_db_session, monkeypatch, _unit(7)).get_relevant_context(
            "Wo wohnt Erika?", user_id=owner.id)
        assert "Berlin" in (out or "") and "Bonn" in (out or "")

    async def test_atoms_hide_expired_when_on(self, pg_db_session, monkeypatch):
        owner, bonn, berlin = await self._seed(pg_db_session, "kr875_atoms")
        kg = _retrieval(pg_db_session, monkeypatch, _unit(7))
        _filter(monkeypatch, False)
        off = await kg.get_relevant_atoms("Erika", user_id=owner.id)
        _filter(monkeypatch, True)
        on = await kg.get_relevant_atoms("Erika", user_id=owner.id)
        assert {r["object_id"] for r in off["relations"]} == {bonn.id, berlin.id}
        assert {r["object_id"] for r in on["relations"]} == {berlin.id}


# ---------------------------------------------------------------------------
# kg_graph_service — focus (_relations_touching) + _load_relations
# ---------------------------------------------------------------------------
class TestKgGraphService:
    async def test_focus_hides_expired_neighbour_when_on(self, pg_db_session, monkeypatch):
        _filter(monkeypatch, True)
        owner = await _user(pg_db_session, "kgs875_focus")
        erika = await _entity(pg_db_session, owner, "Erika")
        bonn = await _entity(pg_db_session, owner, "Bonn")
        berlin = await _entity(pg_db_session, owner, "Berlin")
        await _rel(pg_db_session, owner, erika, bonn, valid_to=_PAST)
        await _rel(pg_db_session, owner, erika, berlin)
        out = await KGGraphService(pg_db_session).focus(erika.id, asker_id=None, hops=1)
        text = repr(out)
        assert "Berlin" in text and "Bonn" not in text

    async def test_load_relations_off_vs_on(self, pg_db_session, monkeypatch):
        owner = await _user(pg_db_session, "kgs875_load")
        a = await _entity(pg_db_session, owner, "A")
        b = await _entity(pg_db_session, owner, "B")
        await _rel(pg_db_session, owner, a, b, valid_to=_PAST)
        svc = KGGraphService(pg_db_session)
        _filter(monkeypatch, False)
        assert len(await svc._load_relations([a.id, b.id])) == 1
        _filter(monkeypatch, True)
        assert await svc._load_relations([a.id, b.id]) == []


# ---------------------------------------------------------------------------
# knowledge_graph_service — list_relations, get_stats, save_relation dedup
# ---------------------------------------------------------------------------
class TestKnowledgeGraphService:
    async def _seed(self, db, name):
        owner = await _user(db, name)
        a = await _entity(db, owner, "A")
        b = await _entity(db, owner, "B")
        c = await _entity(db, owner, "C")
        await _rel(db, owner, a, b, valid_to=_PAST)
        await _rel(db, owner, a, c)
        await _rel(db, owner, b, c, valid_to=_FUTURE)
        return owner, a, b, c

    async def test_list_relations_and_stats_count_live_only(self, pg_db_session, monkeypatch):
        owner, *_ = await self._seed(pg_db_session, "kgsvc875_list")
        svc = KnowledgeGraphService(pg_db_session)
        _filter(monkeypatch, False)
        _, total_off = await svc.list_relations(user_id=owner.id)
        stats_off = await svc.get_stats(user_id=owner.id)
        _filter(monkeypatch, True)
        rels_on, total_on = await svc.list_relations(user_id=owner.id)
        stats_on = await svc.get_stats(user_id=owner.id)
        assert (total_off, total_on) == (3, 2)
        assert len(rels_on) == 2
        assert stats_on["relation_count"] == stats_off["relation_count"] - 1

    async def test_reassertion_of_expired_triple_is_a_new_edge(self, pg_db_session, monkeypatch):
        """"Moved back to Bonn": the expired identical triple must not absorb it."""
        _filter(monkeypatch, True)
        owner = await _user(pg_db_session, "kgsvc875_reassert")
        erika = await _entity(pg_db_session, owner, "Erika")
        bonn = await _entity(pg_db_session, owner, "Bonn")
        old = await _rel(pg_db_session, owner, erika, bonn, valid_to=_PAST)
        new = await KnowledgeGraphService(pg_db_session).save_relation(
            erika.id, "wohnt_in", bonn.id, user_id=owner.id)
        assert new is not None and new.id != old.id
        assert new.valid_to is None

    async def test_reassertion_dedups_into_live_triple(self, pg_db_session, monkeypatch):
        _filter(monkeypatch, True)
        owner = await _user(pg_db_session, "kgsvc875_dedup")
        erika = await _entity(pg_db_session, owner, "Erika")
        bonn = await _entity(pg_db_session, owner, "Bonn")
        live = await _rel(pg_db_session, owner, erika, bonn)
        again = await KnowledgeGraphService(pg_db_session).save_relation(
            erika.id, "wohnt_in", bonn.id, user_id=owner.id)
        assert again is not None and again.id == live.id


# ---------------------------------------------------------------------------
# routes/knowledge_graph — list_merge_proposals relation counts
# ---------------------------------------------------------------------------
class TestMergeProposalCounts:
    async def test_counts_live_edges_only(self, pg_db_session, monkeypatch):
        from api.routes.knowledge_graph import list_merge_proposals

        owner = await _user(pg_db_session, "mp875")
        loser = await _entity(pg_db_session, owner, "Acme GmbH")
        winner = await _entity(pg_db_session, owner, "ACME")
        other = await _entity(pg_db_session, owner, "Bonn")
        await _rel(pg_db_session, owner, loser, other, pred="sitzt_in", valid_to=_PAST)
        await _rel(pg_db_session, owner, loser, other, pred="gehoert_zu")
        pg_db_session.add(KgMergeProposal(
            user_id=owner.id, loser_entity_id=loser.id, winner_entity_id=winner.id,
            similarity=0.9, status=KG_MERGE_PROPOSAL_PENDING))
        await pg_db_session.flush()

        _filter(monkeypatch, False)
        off = await list_merge_proposals(db=pg_db_session, user=None)
        _filter(monkeypatch, True)
        on = await list_merge_proposals(db=pg_db_session, user=None)

        def loser_count(resp):
            return next(p.loser.relation_count for p in resp.proposals if p.loser.id == loser.id)

        assert (loser_count(off), loser_count(on)) == (2, 1)


# ---------------------------------------------------------------------------
# R2 — validity never widens circle visibility
# ---------------------------------------------------------------------------
class TestCircleFilterUnchanged:
    async def test_non_member_sees_no_private_edge_with_filter_on(self, pg_db_session, monkeypatch):
        monkeypatch.setattr(settings, "kg_validity_filter_enabled", True)
        monkeypatch.setattr(settings, "auth_enabled", True)
        monkeypatch.setattr(settings, "graph_expansion_enabled", True)
        owner = await _user(pg_db_session, "r2_owner")
        stranger = await _user(pg_db_session, "r2_stranger")
        a = await _entity(pg_db_session, owner, "Privat-A", tier=0)
        b = await _entity(pg_db_session, owner, "Privat-B", tier=0)
        await _rel(pg_db_session, owner, a, b, valid_to=_FUTURE)  # live, but private
        out = await graph_expansion.expand_fused([_pivot(a)], stranger.id, pg_db_session)
        assert [m for m in out if m.atom.atom_type in ("kg_node", "kg_edge")] == []
