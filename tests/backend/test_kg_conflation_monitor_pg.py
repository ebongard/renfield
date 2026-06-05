"""Postgres-only tests for the KG conflation tripwire (read-only).

The monitor flags DISTINCT-name, SAME-type, SAME-tier **non-person** entity pairs
whose cosine similarity is >= the threshold — a forming generic-centroid magnet in
a type where resolve still embedding-matches. It must NOT flag same-name pairs
(reconciler's job), cross-type, cross-tier, far-apart pairs, or ANY person pair
(persons skip embedding-match in resolve — their names inherently cluster, so a
close pair can't fold and would be permanent noise). It must never mutate.
"""
from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.database import EMBEDDING_DIMENSION, KGEntity, Role, User
from services.kg_conflation_monitor import KgConflationMonitor

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


async def _entity(db, owner, name, *, etype="organization", tier=0, emb=None,
                  etypes=None) -> KGEntity:
    e = KGEntity(user_id=owner.id, name=name, entity_type=etype, circle_tier=tier,
                 embedding=emb, entity_types=etypes or [etype])
    db.add(e)
    await db.flush()
    return e


class TestConflationMonitor:
    async def test_distinct_name_close_nonperson_pair_flagged(self, pg_db_session):
        # Two organizations, distinct names, identical embedding (cosine 1.0).
        owner = await _make_user(pg_db_session, "cm_flag")
        a = await _entity(pg_db_session, owner, "Acme GmbH", etype="organization", emb=_vec(5))
        b = await _entity(pg_db_session, owner, "Globex AG", etype="organization", emb=_vec(5))
        pairs = await KgConflationMonitor(pg_db_session).scan_for_user(owner.id)
        assert len(pairs) == 1
        assert {pairs[0].id_a, pairs[0].id_b} == {a.id, b.id} and pairs[0].similarity >= 0.85

    async def test_person_pair_never_flagged(self, pg_db_session):
        # Persons skip embedding-match in resolve → a close person pair can't fold;
        # the tripwire must not flag it (would be permanent noise — names cluster).
        owner = await _make_user(pg_db_session, "cm_person")
        await _entity(pg_db_session, owner, "Jutta", etype="person", emb=_vec(6))
        await _entity(pg_db_session, owner, "Anna", etype="person", emb=_vec(6))
        pairs = await KgConflationMonitor(pg_db_session).scan_for_user(owner.id)
        assert pairs == []

    async def test_multitype_person_pair_not_flagged(self, pg_db_session):
        # Primary 'organization' but person in the multi-type set → resolve skips
        # embedding-match (gate on seed_types), so the tripwire excludes it too.
        owner = await _make_user(pg_db_session, "cm_multi")
        await _entity(pg_db_session, owner, "Die Schmidts", etype="organization",
                      etypes=["organization", "person"], emb=_vec(7))
        await _entity(pg_db_session, owner, "Die Müllers", etype="organization",
                      etypes=["organization", "person"], emb=_vec(7))
        pairs = await KgConflationMonitor(pg_db_session).scan_for_user(owner.id)
        assert pairs == []

    async def test_same_name_not_flagged(self, pg_db_session):
        owner = await _make_user(pg_db_session, "cm_samename")
        await _entity(pg_db_session, owner, "Acme GmbH", etype="organization", emb=_vec(8))
        await _entity(pg_db_session, owner, "acme gmbh", etype="organization", emb=_vec(8))
        pairs = await KgConflationMonitor(pg_db_session).scan_for_user(owner.id)
        assert pairs == []

    async def test_cross_type_not_flagged(self, pg_db_session):
        owner = await _make_user(pg_db_session, "cm_xtype")
        await _entity(pg_db_session, owner, "Acme", etype="organization", emb=_vec(9))
        await _entity(pg_db_session, owner, "Bonn", etype="place", emb=_vec(9))
        pairs = await KgConflationMonitor(pg_db_session).scan_for_user(owner.id)
        assert pairs == []

    async def test_cross_tier_not_flagged(self, pg_db_session):
        owner = await _make_user(pg_db_session, "cm_xtier")
        await _entity(pg_db_session, owner, "Acme", etype="organization", tier=0, emb=_vec(10))
        await _entity(pg_db_session, owner, "Globex", etype="organization", tier=2, emb=_vec(10))
        pairs = await KgConflationMonitor(pg_db_session).scan_for_user(owner.id)
        assert pairs == []

    async def test_far_pair_not_flagged(self, pg_db_session):
        owner = await _make_user(pg_db_session, "cm_far")
        await _entity(pg_db_session, owner, "Acme", etype="organization", emb=_vec(11))
        await _entity(pg_db_session, owner, "Globex", etype="organization", emb=_vec(900))
        pairs = await KgConflationMonitor(pg_db_session).scan_for_user(owner.id)
        assert pairs == []

    async def test_scan_all_reports_and_does_not_mutate(self, pg_db_session):
        owner = await _make_user(pg_db_session, "cm_all")
        a = await _entity(pg_db_session, owner, "Acme GmbH", etype="organization", emb=_vec(13))
        b = await _entity(pg_db_session, owner, "Globex AG", etype="organization", emb=_vec(13))
        rep = await KgConflationMonitor(pg_db_session).scan_all(user_id=owner.id)
        assert rep.scanned_users == 1 and len(rep.pairs) == 1
        for ent, seed in ((a, 13), (b, 13)):
            r = (await pg_db_session.execute(
                select(KGEntity).where(KGEntity.id == ent.id))).scalar_one()
            assert r.is_active and list(r.embedding) == _vec(seed)
