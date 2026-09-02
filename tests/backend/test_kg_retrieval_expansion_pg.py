"""Postgres-only tests for the graph-expansion seam in the KG STRING path (#874).

``KGRetrieval.get_relevant_context`` is the agent's string KG-context path
(backs ``internal.knowledge_search`` + the ReAct loop). #874 routes it through
the SAME ``graph_expansion.expand_fused`` seam that ``PolymorphicAtomStore``
already uses, so the agent benefits from 1-2-hop multi-hop traversal — without
changing the method's signature.

Guarantees under test:
  (a) GRAPH_EXPANSION_ENABLED off  -> flat-1-hop output unchanged (snapshot).
  (b) GRAPH_EXPANSION_ENABLED on   -> a 2-hop fact surfaces that the flat path
      (which only fetches relations touching a *seed* entity) never returns.
  (c) tier-leak guard: a private hop-2 entity/relation must NOT appear in the
      string for an asker who can't see it — relying on expand_fused's per-hop
      ``kg_entities_circles_filter`` + leak-safe edges.

Real PG via ``pg_db_session`` (the ``e.embedding <=> vector`` similarity search +
the circle filter's json::int cast both require Postgres). ``_get_embedding`` and
``_extract_query_entities`` are mocked so no Ollama/LLM call is made; entities get
deterministic pgvector embeddings so a chosen entity is the only seed.
"""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from models.database import (
    EMBEDDING_DIMENSION,
    TIER_PUBLIC,
    KGEntity,
    KGRelation,
    Role,
    User,
)
from services.kg_retrieval import KGRetrieval
from utils.config import settings

pytestmark = [pytest.mark.postgres, pytest.mark.asyncio]


def _unit(i: int) -> list[float]:
    """A unit vector with 1.0 at index ``i`` — orthogonal units have cosine 0."""
    v = [0.0] * EMBEDDING_DIMENSION
    v[i % EMBEDDING_DIMENSION] = 1.0
    return v


async def _make_user(db: AsyncSession, name: str) -> User:
    role = Role(name=f"{name}_role")
    db.add(role)
    await db.flush()
    u = User(username=name, email=f"{name}@ex.test", password_hash="x",
             role_id=role.id, is_active=True)
    db.add(u)
    await db.flush()
    return u


async def _entity(db, owner, name, *, tier=0, etype="person", emb=None) -> KGEntity:
    e = KGEntity(user_id=owner.id, name=name, entity_type=etype, circle_tier=tier,
                 is_active=True, embedding=emb)
    db.add(e)
    await db.flush()
    return e


async def _rel(db, owner, s, o, pred="kennt", *, tier=None) -> KGRelation:
    r = KGRelation(
        user_id=owner.id, subject_id=s.id, predicate=pred, object_id=o.id,
        circle_tier=min(s.circle_tier, o.circle_tier) if tier is None else tier,
        is_active=True,
    )
    db.add(r)
    await db.flush()
    return r


def _retrieval(db, monkeypatch, *, seed_vec: list[float]) -> KGRetrieval:
    """A KGRetrieval whose LLM calls are stubbed: no entity-name extraction (so it
    embeds the full query once) and a fixed query embedding = ``seed_vec``."""
    kg = KGRetrieval(db)
    monkeypatch.setattr(kg, "_extract_query_entities", AsyncMock(return_value=[]))
    monkeypatch.setattr(kg, "_get_embedding", AsyncMock(return_value=seed_vec))
    return kg


def _triple_lines(out: str | None) -> list[str]:
    return [ln for ln in (out or "").splitlines() if ln.startswith("- ")]


class TestGetRelevantContextExpansion:
    async def test_flag_off_flat_output_unchanged(self, pg_db_session, monkeypatch):
        # SNAPSHOT: with expansion OFF, only relations touching a SEED entity are
        # returned (flat 1-hop). The hop-2 entity/fact must be absent — proving
        # the new block is inert and the output is byte-identical to today.
        monkeypatch.setattr(settings, "auth_enabled", False)
        monkeypatch.setattr(settings, "graph_expansion_enabled", False)
        owner = await _make_user(pg_db_session, "gxs_off")
        anna = await _entity(pg_db_session, owner, "Anna", emb=_unit(0))
        bonn = await _entity(pg_db_session, owner, "Bonn", etype="place", emb=_unit(5))
        chur = await _entity(pg_db_session, owner, "Chur", etype="place", emb=_unit(6))
        await _rel(pg_db_session, owner, anna, bonn, pred="wohnt_in")
        await _rel(pg_db_session, owner, bonn, chur, pred="liegt_bei")

        kg = _retrieval(pg_db_session, monkeypatch, seed_vec=_unit(0))
        out = await kg.get_relevant_context("Wo wohnt Anna?", user_id=owner.id)

        lines = _triple_lines(out)
        assert lines == ["- Anna wohnt_in Bonn"]     # exactly the flat seed triple
        assert "Chur" not in (out or "")             # hop-2 fact NOT surfaced

    async def test_flag_on_two_hop_fact_surfaces(self, pg_db_session, monkeypatch):
        # With expansion ON, the same graph now surfaces the hop-2 fact
        # (Bonn liegt_bei Chur) that the flat path never returns.
        monkeypatch.setattr(settings, "auth_enabled", False)
        monkeypatch.setattr(settings, "graph_expansion_enabled", True)
        owner = await _make_user(pg_db_session, "gxs_on")
        anna = await _entity(pg_db_session, owner, "Anna", emb=_unit(0))
        bonn = await _entity(pg_db_session, owner, "Bonn", etype="place", emb=_unit(5))
        chur = await _entity(pg_db_session, owner, "Chur", etype="place", emb=_unit(6))
        await _rel(pg_db_session, owner, anna, bonn, pred="wohnt_in")
        await _rel(pg_db_session, owner, bonn, chur, pred="liegt_bei")

        kg = _retrieval(pg_db_session, monkeypatch, seed_vec=_unit(0))
        out = await kg.get_relevant_context("Wo wohnt Anna?", user_id=owner.id)

        assert "- Anna wohnt_in Bonn" in (out or "")   # seed relation kept
        assert "- Bonn liegt_bei Chur" in (out or "")  # hop-2 fact ADDED
        # no duplicate of the seed (hop-1) relation from the expansion edges
        assert _triple_lines(out).count("- Anna wohnt_in Bonn") == 1

    async def test_flag_on_tier_leak_guard_hop2_entity(self, pg_db_session, monkeypatch):
        # CRITICAL: a private hop-2 entity/relation must NOT leak into the string
        # for an asker who can't see it. Anna(a_user) -- Bonn(public) -- Secret
        # (b_user, self-tier 0). Even with the B->Secret relation forced PUBLIC
        # (so relation-traversal passes), the per-hop ENTITY circle filter drops
        # Secret, and the leak-safe edge never names it.
        monkeypatch.setattr(settings, "auth_enabled", True)
        monkeypatch.setattr(settings, "graph_expansion_enabled", True)
        a_user = await _make_user(pg_db_session, "gxs_a")
        b_user = await _make_user(pg_db_session, "gxs_b")
        anna = await _entity(pg_db_session, a_user, "Anna", tier=0, emb=_unit(0))
        bonn = await _entity(pg_db_session, a_user, "Bonn", tier=TIER_PUBLIC,
                             etype="place", emb=_unit(5))
        secret = await _entity(pg_db_session, b_user, "SecretPlace", tier=0,
                               etype="place", emb=_unit(6))
        await _rel(pg_db_session, a_user, anna, bonn, pred="wohnt_in")
        # relation forced public so ONLY the entity filter can stop the leak
        await _rel(pg_db_session, a_user, bonn, secret, pred="liegt_bei",
                   tier=TIER_PUBLIC)

        kg = _retrieval(pg_db_session, monkeypatch, seed_vec=_unit(0))
        out = await kg.get_relevant_context("Wo wohnt Anna?", user_id=a_user.id)

        assert "- Anna wohnt_in Bonn" in (out or "")   # accessible hop-1 kept
        assert "SecretPlace" not in (out or "")        # private hop-2 entity hidden
        assert "liegt_bei" not in (out or "")          # and its predicate/edge too

    async def test_flag_on_no_seed_returns_none(self, pg_db_session, monkeypatch):
        # No entity clears the similarity threshold -> None, unchanged, no crash.
        monkeypatch.setattr(settings, "auth_enabled", False)
        monkeypatch.setattr(settings, "graph_expansion_enabled", True)
        owner = await _make_user(pg_db_session, "gxs_none")
        await _entity(pg_db_session, owner, "Anna", emb=_unit(5))  # orthogonal to seed

        kg = _retrieval(pg_db_session, monkeypatch, seed_vec=_unit(0))
        out = await kg.get_relevant_context("Wo wohnt Anna?", user_id=owner.id)
        assert out is None
