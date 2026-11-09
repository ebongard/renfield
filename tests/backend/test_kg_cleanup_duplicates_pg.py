"""Postgres: the admin duplicate scan reads the rows the reconciler would (#1499).

The unit tests feed rows by hand; the filters that decide WHICH rows the scan
reads live in its SQL, so they are proven here against real ``kg_entities``:
per owner and tier, no notes (they resolve by exact title only), no merge
tombstones.
"""
from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from models.database import KGEntity, Role, User
from services.kg_cleanup_service import KGCleanupService

pytestmark = [pytest.mark.postgres, pytest.mark.asyncio]


async def _make_user(db: AsyncSession, name: str) -> User:
    role = Role(name=f"{name}_role")
    db.add(role)
    await db.flush()
    user = User(username=name, email=f"{name}@ex.test", password_hash="x",
                role_id=role.id, is_active=True)
    db.add(user)
    await db.flush()
    return user


async def _entity(db, owner, name, *, tier=0, etype="organization", **kw) -> KGEntity:
    e = KGEntity(user_id=owner.id, name=name, entity_type=etype, circle_tier=tier, **kw)
    db.add(e)
    await db.flush()
    return e


def _id_sets(clusters) -> list[set[int]]:
    return sorted(({c["canonical"]["id"], *(d["id"] for d in c["duplicates"])} for c in clusters), key=min)


async def test_scan_clusters_per_owner_and_tier(pg_db_session):
    db = pg_db_session
    alice, bob = await _make_user(db, "alice1499"), await _make_user(db, "bob1499")
    a1 = await _entity(db, alice, "Stadtwerke Bonn", mention_count=5)
    a2 = await _entity(db, alice, "Stadtwerke Bnn")
    await _entity(db, bob, "Stadtwerke Bon")              # other owner
    await _entity(db, alice, "Stadtwerk Bonn", tier=2)    # other tier

    clusters = await KGCleanupService(db).find_duplicate_clusters()

    assert _id_sets(clusters) == [{a1.id, a2.id}]


async def test_scan_skips_notes_and_tombstones(pg_db_session):
    db = pg_db_session
    alice = await _make_user(db, "carol1499")
    await _entity(db, alice, "Urlaubsplanung", etype="note")
    await _entity(db, alice, "Urlaubsplanungen", etype="note")
    live = await _entity(db, alice, "Stadtwerke Bonn", mention_count=5)
    # A tombstone that is still active must not be offered as a duplicate again.
    await _entity(db, alice, "Stadtwerke Bnn", canonical_id=live.id)

    assert await KGCleanupService(db).find_duplicate_clusters() == []
    assert await KGCleanupService(db).find_duplicate_clusters(entity_type="note") == []
