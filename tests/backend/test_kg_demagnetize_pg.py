"""Postgres-only tests for the person-entity de-magnetize pass.

Verifies that person rows carrying a generic meta-description ("Vollständiger
Name einer Person") are selected, their description NULLed, and the embedding
re-computed from the bare name — while specific-description rows and non-person
rows are left untouched. ``_get_embedding`` is patched so no Ollama call fires.
"""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.database import EMBEDDING_DIMENSION, KGEntity, Role, User
from services import kg_demagnetize

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


async def _entity(db, owner, name, *, etype="person", desc=None, emb=None) -> KGEntity:
    e = KGEntity(user_id=owner.id, name=name, entity_type=etype, circle_tier=0,
                 description=desc, embedding=emb)
    db.add(e)
    await db.flush()
    return e


def _patch_embed(monkeypatch, vec):
    monkeypatch.setattr(
        "services.knowledge_graph_service.KnowledgeGraphService._get_embedding",
        AsyncMock(return_value=vec),
    )


class TestDemagnetize:
    async def test_generic_person_is_repaired(self, pg_db_session, monkeypatch):
        owner = await _make_user(pg_db_session, "dm_generic")
        hub = await _entity(pg_db_session, owner, "Anna Johanna von den Bongard",
                            desc="Vollständiger Name einer Person", emb=_vec(7))
        _patch_embed(monkeypatch, _vec(123))

        rep = await kg_demagnetize.run(pg_db_session, user_id=owner.id)

        assert rep.candidates == 1 and rep.updated == 1 and rep.failed == 0
        refreshed = (await pg_db_session.execute(
            select(KGEntity).where(KGEntity.id == hub.id))).scalar_one()
        assert refreshed.description is None              # generic desc dropped
        assert list(refreshed.embedding) == _vec(123)     # re-embedded (name-only)

    async def test_specific_description_untouched(self, pg_db_session, monkeypatch):
        owner = await _make_user(pg_db_session, "dm_specific")
        keep = await _entity(pg_db_session, owner, "Jutta",
                             desc="Nachbarin aus Bonn", emb=_vec(8))
        _patch_embed(monkeypatch, _vec(124))

        rep = await kg_demagnetize.run(pg_db_session, user_id=owner.id)

        assert rep.candidates == 0 and rep.updated == 0
        refreshed = (await pg_db_session.execute(
            select(KGEntity).where(KGEntity.id == keep.id))).scalar_one()
        assert refreshed.description == "Nachbarin aus Bonn"
        assert list(refreshed.embedding) == _vec(8)       # unchanged

    async def test_nonperson_not_selected(self, pg_db_session, monkeypatch):
        # Even if a place somehow carried the generic phrase, the pass is person-scoped.
        owner = await _make_user(pg_db_session, "dm_place")
        await _entity(pg_db_session, owner, "Bonn", etype="place",
                      desc="Vollständiger Name einer Person", emb=_vec(9))
        _patch_embed(monkeypatch, _vec(125))

        rep = await kg_demagnetize.run(pg_db_session, user_id=owner.id)
        assert rep.candidates == 0

    async def test_idempotent(self, pg_db_session, monkeypatch):
        owner = await _make_user(pg_db_session, "dm_idem")
        await _entity(pg_db_session, owner, "Thomas Meunier",
                      desc="Vollständiger Name einer Person", emb=_vec(10))
        _patch_embed(monkeypatch, _vec(126))

        first = await kg_demagnetize.run(pg_db_session, user_id=owner.id)
        assert first.updated == 1
        second = await kg_demagnetize.dry_run(pg_db_session, user_id=owner.id)
        assert second.candidates == 0                     # nothing left to do
