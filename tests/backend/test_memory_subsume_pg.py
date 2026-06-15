"""Postgres-only tests for Phase 3-subsume (MEMORY_SUBSUME_TO_KG).

When on, decomposable facts (category=fact + subject) are NOT stored as flat
memories (they live in the KG); preferences / subject-less facts stay flat.
Off = every extracted item is saved (unchanged). Real PG via ``pg_db_session``;
the LLM call + parse are mocked so the loop runs deterministically.
"""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from models.database import EMBEDDING_DIMENSION, KGEntity, KGRelation, Role, User
from services.conversation_memory_service import ConversationMemoryService
from utils.config import settings

pytestmark = [pytest.mark.postgres, pytest.mark.asyncio]

_ITEMS = [
    {"content": "Anna wohnt in Bonn", "category": "fact", "subject": "Anna", "importance": 0.6},
    {"content": "mag Jazz", "category": "preference", "subject": "Ich", "importance": 0.6},
    {"content": "es regnet draussen", "category": "fact", "subject": None, "importance": 0.4},
]


async def _make_user(db: AsyncSession, name: str) -> User:
    role = Role(name=f"{name}_role")
    db.add(role)
    await db.flush()
    u = User(username=name, email=f"{name}@ex.test", password_hash="x", role_id=role.id, is_active=True)
    db.add(u)
    await db.flush()
    return u


def _svc(db, monkeypatch, *, subsume: bool, require_relation: bool = False) -> ConversationMemoryService:
    monkeypatch.setattr(db, "commit", db.flush)
    monkeypatch.setattr(db, "rollback", db.flush)
    svc = ConversationMemoryService(db)
    # distinct one-hot per call so the save() dedup (cosine >= threshold) doesn't
    # collapse the test memories into one.
    _n = {"i": 0}

    def _emb(_content: str) -> list[float]:
        v = [0.0] * EMBEDDING_DIMENSION
        v[_n["i"] % EMBEDDING_DIMENSION] = 1.0
        _n["i"] += 1
        return v

    monkeypatch.setattr(svc, "_get_embedding", AsyncMock(side_effect=_emb))
    monkeypatch.setattr(svc, "should_extract_memories", lambda *a, **k: True)
    chat = AsyncMock()
    chat.chat = AsyncMock(return_value=object())  # content ignored — see below
    monkeypatch.setattr(svc, "_get_chat_client", AsyncMock(return_value=chat))
    # the module does a local `from utils.llm_client import extract_response_content`,
    # which resolves the attribute at call time — patch it there.
    import utils.llm_client as _llm
    monkeypatch.setattr(_llm, "extract_response_content", lambda r: "[]")
    monkeypatch.setattr(svc, "_parse_extraction_response", lambda raw: [dict(i) for i in _ITEMS])
    monkeypatch.setattr(settings, "memory_subsume_to_kg", subsume)
    monkeypatch.setattr(settings, "memory_subsume_require_kg_relation", require_relation)
    monkeypatch.setattr(settings, "memory_kg_bridge_enabled", False)
    monkeypatch.setattr(settings, "memory_contradiction_resolution", False)
    return svc


async def _seed_entity_with_relation(db: AsyncSession, user_id: int, name: str) -> KGEntity:
    """Create a person entity for ``name`` with one outgoing relation, so the
    recall-loss guard sees the subject as KG-representable."""
    subj = KGEntity(user_id=user_id, name=name, entity_type="person")
    obj = KGEntity(user_id=user_id, name=f"{name}-place", entity_type="place")
    db.add_all([subj, obj])
    await db.flush()
    db.add(KGRelation(
        user_id=user_id, subject_id=subj.id, predicate="wohnt_in", object_id=obj.id,
        confidence=0.9,
    ))
    await db.flush()
    return subj


class TestSubsume:
    async def test_off_saves_everything(self, pg_db_session, monkeypatch):
        owner = await _make_user(pg_db_session, "sub_off")
        svc = _svc(pg_db_session, monkeypatch, subsume=False)
        saved = await svc._extract_and_save_v1_impl("u", "a", user_id=owner.id)
        contents = {m.content for m in saved}
        assert contents == {"Anna wohnt in Bonn", "mag Jazz", "es regnet draussen"}

    async def test_on_subsumes_fact_with_subject_only(self, pg_db_session, monkeypatch):
        owner = await _make_user(pg_db_session, "sub_on")
        # guard OFF here: exercise the pure subsume logic (legacy unguarded path)
        svc = _svc(pg_db_session, monkeypatch, subsume=True, require_relation=False)
        saved = await svc._extract_and_save_v1_impl("u", "a", user_id=owner.id)
        contents = {m.content for m in saved}
        assert "Anna wohnt in Bonn" not in contents   # fact + subject -> KG only
        assert "mag Jazz" in contents                  # preference stays flat
        assert "es regnet draussen" in contents        # fact without subject stays flat


class TestSubsumeRecallLossGuard:
    """memory_subsume_require_kg_relation (default ON): a fact+subject is only
    dropped flat when the KG demonstrably represents the subject (its entity has
    >=1 relation). A subject the KG can't represent (no relation — its object was
    a state/feeling) stays flat = recoverable, no silent loss."""

    async def test_guard_keeps_fact_flat_when_no_kg_relation(self, pg_db_session, monkeypatch):
        owner = await _make_user(pg_db_session, "guard_noloss")
        # subsume ON + guard ON, but NO KG entity/relation for "Anna" exists.
        svc = _svc(pg_db_session, monkeypatch, subsume=True, require_relation=True)
        saved = await svc._extract_and_save_v1_impl("u", "a", user_id=owner.id)
        contents = {m.content for m in saved}
        # The guard prevents the silent loss: the fact stays flat.
        assert "Anna wohnt in Bonn" in contents
        assert "mag Jazz" in contents
        assert "es regnet draussen" in contents

    async def test_guard_allows_subsume_when_subject_has_kg_relation(self, pg_db_session, monkeypatch):
        owner = await _make_user(pg_db_session, "guard_repr")
        await _seed_entity_with_relation(pg_db_session, owner.id, "Anna")
        svc = _svc(pg_db_session, monkeypatch, subsume=True, require_relation=True)
        saved = await svc._extract_and_save_v1_impl("u", "a", user_id=owner.id)
        contents = {m.content for m in saved}
        # KG represents Anna (entity + relation) -> safe to drop the flat fact.
        assert "Anna wohnt in Bonn" not in contents
        assert "mag Jazz" in contents               # preference still flat
        assert "es regnet draussen" in contents     # subject-less fact still flat

    async def test_guard_skips_tombstone_only_match_keeps_flat(self, pg_db_session, monkeypatch):
        """Mirroring resolve_entity, the probe's exact-name step filters to
        canonical rows (canonical_id IS NULL). A subject whose ONLY same-name row
        is a merge tombstone is therefore not matched by name (the survivor is
        named differently, reachable only via a surface-form the probe
        deliberately does not chase) -> no canonical match -> fail-safe KEEP
        FLAT, never a wrong-entity subsume."""
        owner = await _make_user(pg_db_session, "guard_tomb")
        survivor = KGEntity(user_id=owner.id, name="Anna Survivor", entity_type="person")
        place = KGEntity(user_id=owner.id, name="Bonn", entity_type="place")
        pg_db_session.add_all([survivor, place])
        await pg_db_session.flush()
        pg_db_session.add(KGRelation(
            user_id=owner.id, subject_id=survivor.id, predicate="wohnt_in",
            object_id=place.id, confidence=0.9,
        ))
        # "Anna" exists ONLY as a tombstone pointing at the differently-named survivor
        tomb = KGEntity(
            user_id=owner.id, name="Anna", entity_type="person",
            canonical_id=survivor.id, is_active=False,
        )
        pg_db_session.add(tomb)
        await pg_db_session.flush()
        svc = _svc(pg_db_session, monkeypatch, subsume=True, require_relation=True)
        saved = await svc._extract_and_save_v1_impl("u", "a", user_id=owner.id)
        contents = {m.content for m in saved}
        assert "Anna wohnt in Bonn" in contents   # tombstone-only -> kept flat (fail-safe)

    async def test_guard_ignores_non_person_homonym(self, pg_db_session, monkeypatch):
        """A same-name NON-person entity with a relation must NOT make the guard
        subsume a person-fact (a wrong-entity false-positive = loss). 'Anna' the
        place has a relation; there is no 'Anna' person -> keep the fact flat."""
        owner = await _make_user(pg_db_session, "guard_homonym")
        anna_place = KGEntity(user_id=owner.id, name="Anna", entity_type="place")
        region = KGEntity(user_id=owner.id, name="Region", entity_type="place")
        pg_db_session.add_all([anna_place, region])
        await pg_db_session.flush()
        pg_db_session.add(KGRelation(
            user_id=owner.id, subject_id=anna_place.id, predicate="liegt_in",
            object_id=region.id, confidence=0.9,
        ))
        await pg_db_session.flush()
        svc = _svc(pg_db_session, monkeypatch, subsume=True, require_relation=True)
        saved = await svc._extract_and_save_v1_impl("u", "a", user_id=owner.id)
        contents = {m.content for m in saved}
        assert "Anna wohnt in Bonn" in contents   # person 'Anna' absent -> kept flat

    async def test_guard_disabled_reverts_to_unguarded(self, pg_db_session, monkeypatch):
        owner = await _make_user(pg_db_session, "guard_off")
        # guard OFF -> legacy: subsume regardless of KG state (no entity exists)
        svc = _svc(pg_db_session, monkeypatch, subsume=True, require_relation=False)
        saved = await svc._extract_and_save_v1_impl("u", "a", user_id=owner.id)
        contents = {m.content for m in saved}
        assert "Anna wohnt in Bonn" not in contents   # unguarded subsume

    async def test_probe_helper_direct(self, pg_db_session, monkeypatch):
        owner = await _make_user(pg_db_session, "guard_probe")
        await _seed_entity_with_relation(pg_db_session, owner.id, "Bella")
        svc = ConversationMemoryService(pg_db_session)
        monkeypatch.setattr(settings, "memory_subsume_require_kg_relation", True)
        assert await svc._subject_is_kg_representable("Bella", owner.id) is True
        assert await svc._subject_is_kg_representable("Unknown", owner.id) is False
        # case-insensitive match
        assert await svc._subject_is_kg_representable("bella", owner.id) is True
        # no user / no subject -> fail-safe False
        assert await svc._subject_is_kg_representable("Bella", None) is False
        assert await svc._subject_is_kg_representable(None, owner.id) is False
        # a same-name NON-person entity with a relation is ignored (person-typed only)
        place = KGEntity(user_id=owner.id, name="Cologne", entity_type="place")
        region = KGEntity(user_id=owner.id, name="NRW", entity_type="place")
        pg_db_session.add_all([place, region])
        await pg_db_session.flush()
        pg_db_session.add(KGRelation(
            user_id=owner.id, subject_id=place.id, predicate="in",
            object_id=region.id, confidence=0.9,
        ))
        await pg_db_session.flush()
        assert await svc._subject_is_kg_representable("Cologne", owner.id) is False
        # an inactive / tombstone same-name person row is excluded (canonical-only)
        tomb = KGEntity(
            user_id=owner.id, name="Ghost", entity_type="person",
            is_active=False, canonical_id=None,
        )
        pg_db_session.add(tomb)
        await pg_db_session.flush()
        assert await svc._subject_is_kg_representable("Ghost", owner.id) is False
        # guard disabled -> always True (legacy)
        monkeypatch.setattr(settings, "memory_subsume_require_kg_relation", False)
        assert await svc._subject_is_kg_representable("Unknown", owner.id) is True
