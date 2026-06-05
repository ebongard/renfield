"""Postgres-only tests for the Phase 1 resolve_entity cascade (T3).

Cascade: exact-name -> surface-form (jsonb @>) -> embedding (same-tier + high
threshold, name+desc) -> create new. Real PG via ``pg_db_session`` (the @>
operator + halfvec cosine are PG-only). ``_get_embedding`` is mocked so the
embedding step is deterministic and never calls Ollama.
"""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.database import EMBEDDING_DIMENSION, KGEntity, Role, User
from services.knowledge_graph_service import KnowledgeGraphService

pytestmark = [pytest.mark.postgres, pytest.mark.asyncio]


def _vec(seed: int) -> list[float]:
    v = [0.0] * EMBEDDING_DIMENSION
    v[seed % EMBEDDING_DIMENSION] = 1.0
    return v


async def _make_user(db: AsyncSession, name: str) -> User:
    role = Role(name=f"{name}_role")
    db.add(role)
    await db.flush()
    user = User(username=name, email=f"{name}@ex.test", password_hash="x",
                role_id=role.id, is_active=True)
    db.add(user)
    await db.flush()
    return user


async def _entity(db, owner, name, *, tier=0, etype="person", **kw) -> KGEntity:
    e = KGEntity(user_id=owner.id, name=name, entity_type=etype, circle_tier=tier, **kw)
    db.add(e)
    await db.flush()
    return e


def _svc(db, monkeypatch, *, embed=None) -> KnowledgeGraphService:
    svc = KnowledgeGraphService(db)
    monkeypatch.setattr(svc, "_get_embedding",
                        AsyncMock(return_value=embed if embed is not None else _vec(1)))
    return svc


class TestExactAndSurfaceForm:
    async def test_exact_name_bumps_existing(self, pg_db_session, monkeypatch):
        owner = await _make_user(pg_db_session, "rc_exact")
        ent = await _entity(pg_db_session, owner, "Alice", mention_count=1)
        svc = _svc(pg_db_session, monkeypatch)

        got = await svc.resolve_entity("Alice", "person", owner.id)

        assert got.id == ent.id
        assert got.mention_count == 2  # bumped, no new row

    async def test_surface_form_match(self, pg_db_session, monkeypatch):
        owner = await _make_user(pg_db_session, "rc_sf")
        canon = await _entity(pg_db_session, owner, "Alice",
                              surface_forms=["Alice Brown"], mention_count=4)
        svc = _svc(pg_db_session, monkeypatch)

        # "Alice Brown" has no exact-name row, but it's a known surface form.
        got = await svc.resolve_entity("Alice Brown", "person", owner.id)

        assert got.id == canon.id
        assert got.mention_count == 5

    async def test_pointer_chase_skips_tombstone(self, pg_db_session, monkeypatch):
        owner = await _make_user(pg_db_session, "rc_ptr")
        live = await _entity(pg_db_session, owner, "Alice", mention_count=2)
        # a merge tombstone with the SAME name must never be returned
        await _entity(pg_db_session, owner, "Alice", mention_count=9,
                      is_active=False, canonical_id=live.id)
        svc = _svc(pg_db_session, monkeypatch)

        got = await svc.resolve_entity("Alice", "person", owner.id)
        assert got.id == live.id


class TestEmbeddingStep:
    async def test_same_tier_attaches(self, pg_db_session, monkeypatch):
        owner = await _make_user(pg_db_session, "rc_emb_same")
        bonn = await _entity(pg_db_session, owner, "Bonn", tier=0, etype="place",
                             embedding=_vec(7))
        svc = _svc(pg_db_session, monkeypatch, embed=_vec(7))  # cosine 1.0 >= 0.85

        # different surface name, no exact/surface hit -> embedding match (same tier)
        got = await svc.resolve_entity("Bnn", "place", owner.id)
        assert got.id == bonn.id

    async def test_cross_tier_creates_new(self, pg_db_session, monkeypatch):
        owner = await _make_user(pg_db_session, "rc_emb_cross")
        bonn = await _entity(pg_db_session, owner, "Bonn", tier=2, etype="place",
                             embedding=_vec(8))
        svc = _svc(pg_db_session, monkeypatch, embed=_vec(8))

        # embedding is identical, but the candidate is tier 2 and a fresh
        # extraction defaults to tier 0 -> same-tier guard refuses the fold,
        # a NEW tier-0 entity is created (reconciler proposes the cross-tier merge).
        got = await svc.resolve_entity("Bnn", "place", owner.id)
        assert got.id != bonn.id
        assert got.circle_tier == 0


class TestCreateNew:
    async def test_new_entity_gets_multitype_and_is_canonical(self, pg_db_session, monkeypatch):
        owner = await _make_user(pg_db_session, "rc_new")
        svc = _svc(pg_db_session, monkeypatch, embed=_vec(99))

        got = await svc.resolve_entity("Wikipedia", "organization", owner.id)

        assert got.id is not None
        assert got.entity_type == "organization"
        assert got.entity_types == ["organization"]  # multi-type seeded from scalar
        assert got.canonical_id is None               # canonical/live
        assert got.circle_tier == 0

    async def test_unknown_type_coerced_to_thing(self, pg_db_session, monkeypatch):
        owner = await _make_user(pg_db_session, "rc_new2")
        svc = _svc(pg_db_session, monkeypatch, embed=_vec(98))

        got = await svc.resolve_entity("Quux", "spaceship", owner.id)
        assert got.entity_type == "thing"
        assert got.entity_types == ["thing"]


class TestPhase3CreateTier:
    async def test_create_tier_honored(self, pg_db_session, monkeypatch):
        # Phase 3: a backfilled household (tier 2) fact must mint a tier-2 entity,
        # not the legacy hardcoded tier-0.
        owner = await _make_user(pg_db_session, "rc_ct")
        svc = _svc(pg_db_session, monkeypatch, embed=_vec(40))
        got = await svc.resolve_entity("Hausmeister", "person", owner.id, create_tier=2)
        assert got.id is not None and got.circle_tier == 2

    async def test_create_tier_clamped(self, pg_db_session, monkeypatch):
        owner = await _make_user(pg_db_session, "rc_clamp")
        svc = _svc(pg_db_session, monkeypatch, embed=_vec(41))
        got = await svc.resolve_entity("Out", "person", owner.id, create_tier=99)
        assert got.circle_tier == 4  # clamped to the top of the 0..4 ladder

    async def test_create_tier_scopes_embedding_search(self, pg_db_session, monkeypatch):
        # An identical-embedding candidate at tier 0 must NOT be folded into when
        # create_tier=2 (same-tier guard now keyed on create_tier) -> new tier-2 row.
        owner = await _make_user(pg_db_session, "rc_ct_emb")
        t0 = await _entity(pg_db_session, owner, "Bonn", tier=0, etype="place", embedding=_vec(42))
        svc = _svc(pg_db_session, monkeypatch, embed=_vec(42))  # cosine 1.0
        got = await svc.resolve_entity("Bnn", "place", owner.id, create_tier=2)
        assert got.id != t0.id and got.circle_tier == 2


class TestPhase3MatchEntityType:
    async def test_exact_name_wrong_type_skipped_when_scoped(self, pg_db_session, monkeypatch):
        # A "Bella" PLACE must not absorb a "Bella" PERSON resolve when type-scoped.
        owner = await _make_user(pg_db_session, "rc_mt_exact")
        place = await _entity(pg_db_session, owner, "Bella", tier=0, etype="place")
        svc = _svc(pg_db_session, monkeypatch, embed=_vec(50))

        scoped = await svc.resolve_entity("Bella", "person", owner.id, match_entity_type=True)
        assert scoped.id != place.id and scoped.entity_type == "person"

    async def test_exact_name_matches_cross_type_by_default(self, pg_db_session, monkeypatch):
        # Control: without the flag, resolution stays type-blind (legacy behavior).
        owner = await _make_user(pg_db_session, "rc_mt_default")
        place = await _entity(pg_db_session, owner, "Bella", tier=0, etype="place")
        svc = _svc(pg_db_session, monkeypatch, embed=_vec(51))

        got = await svc.resolve_entity("Bella", "person", owner.id)  # no flag
        assert got.id == place.id and got.entity_type == "place"  # matched the place

    async def test_surface_form_wrong_type_skipped_when_scoped(self, pg_db_session, monkeypatch):
        owner = await _make_user(pg_db_session, "rc_mt_sf")
        place = await _entity(pg_db_session, owner, "Bonn", tier=0, etype="place",
                              surface_forms=["Beuel"])
        svc = _svc(pg_db_session, monkeypatch, embed=_vec(52))

        scoped = await svc.resolve_entity("Beuel", "person", owner.id, match_entity_type=True)
        assert scoped.id != place.id and scoped.entity_type == "person"

    async def test_embedding_wrong_type_skipped_when_scoped(self, pg_db_session, monkeypatch):
        owner = await _make_user(pg_db_session, "rc_mt_emb")
        place = await _entity(pg_db_session, owner, "Bella", tier=0, etype="place", embedding=_vec(53))
        svc = _svc(pg_db_session, monkeypatch, embed=_vec(53))  # cosine 1.0, same tier

        scoped = await svc.resolve_entity("Bella2", "person", owner.id, match_entity_type=True)
        assert scoped.id != place.id and scoped.entity_type == "person"


class TestUseEmbeddingGuard:
    async def test_use_embedding_false_never_folds_a_different_name(self, pg_db_session, monkeypatch):
        # The bridge path (use_embedding=False) skips the embedding match for ANY
        # type, not just person — a non-person sanity check that the explicit opt-out
        # still works independently of the person rule.
        owner = await _make_user(pg_db_session, "rc_ue_false")
        bonn = await _entity(pg_db_session, owner, "Bonn", tier=0, etype="place",
                             embedding=_vec(60))
        svc = _svc(pg_db_session, monkeypatch, embed=_vec(60))  # 'Beuel' embeds == Bonn

        got = await svc.resolve_entity("Beuel", "place", owner.id, create_tier=0,
                                       use_embedding=False)
        assert got.id != bonn.id and got.name == "Beuel"  # own entity despite the opt-out

    async def test_person_never_embedding_folds_even_when_enabled(self, pg_db_session, monkeypatch):
        # The live extraction-path fix: PERSON entities skip the embedding match
        # UNCONDITIONALLY (use_embedding default True), because a bare name embeds
        # onto a generic-person centroid and folded different people into one magnet
        # hub (entity #11). Even with embedding enabled, Jutta must get her own row.
        owner = await _make_user(pg_db_session, "rc_ue_true")
        anna = await _entity(pg_db_session, owner, "Anna Johanna von den Bongard",
                             tier=0, etype="person", embedding=_vec(61))
        svc = _svc(pg_db_session, monkeypatch, embed=_vec(61))  # 'Jutta' embeds == Anna

        got = await svc.resolve_entity("Jutta", "person", owner.id, create_tier=0,
                                       match_entity_type=True)  # use_embedding default True
        assert got.id != anna.id and got.name == "Jutta"  # own entity, NOT the Anna hub

    async def test_nonperson_still_embedding_folds_when_enabled(self, pg_db_session, monkeypatch):
        # Guard against over-disabling: NON-person types keep the embedding match on
        # the live path (it salvages OCR/typo variants like "Bnn"→"Bonn").
        owner = await _make_user(pg_db_session, "rc_ue_place")
        bonn = await _entity(pg_db_session, owner, "Bonn", tier=0, etype="place",
                             embedding=_vec(62))
        svc = _svc(pg_db_session, monkeypatch, embed=_vec(62))  # cosine 1.0

        got = await svc.resolve_entity("Bnn", "place", owner.id, create_tier=0)
        assert got.id == bonn.id  # place still folds via embedding
