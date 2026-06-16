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


# Items for the per-fact tests: a state/attribute fact about a person (object is
# NOT a named entity → the KG emits no relation) plus a named-entity-object fact.
_PERFACT_ITEMS = [
    # state fact about Anna — would be LOST under the subject-level proxy if Anna
    # already has a relation; the per-fact gate keeps it flat.
    {"content": "Anna ist müde", "category": "fact", "subject": "Anna", "importance": 0.5},
    # named-entity-object fact about Tom — KG captures "Tom --arbeitet_bei--> Siemens".
    {"content": "Tom arbeitet bei Siemens", "category": "fact", "subject": "Tom", "importance": 0.6},
    {"content": "mag Jazz", "category": "preference", "subject": "Ich", "importance": 0.5},
]


def _svc_perfact(db, monkeypatch, *, subsume: bool, require_relation: bool = True):
    """Like _svc but feeds the per-fact item set."""
    svc = _svc(db, monkeypatch, subsume=subsume, require_relation=require_relation)
    monkeypatch.setattr(
        svc, "_parse_extraction_response", lambda raw: [dict(i) for i in _PERFACT_ITEMS]
    )
    return svc


class TestSubsumePerFactGate:
    """Phase 3-subsume PER-FACT fix: subsume a fact only when THIS turn's KG
    extraction actually captured a relation for its subject (``captured_kg_subjects``),
    not on the subject-level proxy. Closes the residual loss the proxy missed:
    a state/attribute fact about an ALREADY-related person.
    """

    async def test_state_fact_about_already_related_person_kept_flat(
        self, pg_db_session, monkeypatch
    ):
        """HEADLINE CASE — the residual loss the subject-level proxy guard missed.

        Anna is ALREADY a related person in the KG (so the OLD proxy guard would
        return True and drop "Anna ist müde" = silent loss). THIS turn the KG
        captured a relation only for Tom (Tom→Siemens), NOT Anna. With the
        per-fact gate, "Anna ist müde" is KEPT FLAT because anna is not in the
        captured set, while "Tom arbeitet bei Siemens" IS subsumed.
        """
        owner = await _make_user(pg_db_session, "pf_anna")
        # Anna already has a prior relation — the proxy guard would say "drop it".
        await _seed_entity_with_relation(pg_db_session, owner.id, "Anna")
        svc = _svc_perfact(pg_db_session, monkeypatch, subsume=True)
        # This turn the KG captured a relation ONLY for Tom (not Anna).
        captured = {"tom"}
        saved = await svc._extract_and_save_v1_impl(
            "u", "a", user_id=owner.id, captured_kg_subjects=captured
        )
        contents = {m.content for m in saved}
        # Residual loss CLOSED: the state fact about the already-related Anna stays flat.
        assert "Anna ist müde" in contents
        # Named-entity-object fact captured this turn IS subsumed (KG owns it).
        assert "Tom arbeitet bei Siemens" not in contents
        # Preference always stays flat.
        assert "mag Jazz" in contents

    async def test_proxy_would_have_lost_it_contrast(self, pg_db_session, monkeypatch):
        """Contrast: with NO per-turn coordination (captured_kg_subjects=None) the
        OLD subject-level proxy runs and DROPS the state fact (the documented
        residual loss) because Anna already has a relation. This is the behavior
        the per-fact fix replaces on the coordinated path."""
        owner = await _make_user(pg_db_session, "pf_proxy")
        await _seed_entity_with_relation(pg_db_session, owner.id, "Anna")
        svc = _svc_perfact(pg_db_session, monkeypatch, subsume=True)
        saved = await svc._extract_and_save_v1_impl(
            "u", "a", user_id=owner.id, captured_kg_subjects=None
        )
        contents = {m.content for m in saved}
        # Proxy fallback: Anna is already-related → the state fact is LOST (dropped).
        assert "Anna ist müde" not in contents

    async def test_named_entity_fact_subsumed_when_captured(
        self, pg_db_session, monkeypatch
    ):
        """A named-entity-object fact whose relation IS captured this turn is
        subsumed (the duplicate-reduction still works)."""
        owner = await _make_user(pg_db_session, "pf_tom")
        svc = _svc_perfact(pg_db_session, monkeypatch, subsume=True)
        captured = {"anna", "tom"}
        saved = await svc._extract_and_save_v1_impl(
            "u", "a", user_id=owner.id, captured_kg_subjects=captured
        )
        contents = {m.content for m in saved}
        # Both facts captured this turn → both subsumed; only preference stays flat.
        assert "Anna ist müde" not in contents
        assert "Tom arbeitet bei Siemens" not in contents
        assert "mag Jazz" in contents

    async def test_empty_captured_set_keeps_all_facts_flat(
        self, pg_db_session, monkeypatch
    ):
        """Coordinated turn where the KG captured NOTHING (empty set, not None):
        every fact is kept flat — no loss. Distinguishes 'coordinated, nothing
        captured' (keep all) from 'uncoordinated' (proxy fallback)."""
        owner = await _make_user(pg_db_session, "pf_empty")
        await _seed_entity_with_relation(pg_db_session, owner.id, "Anna")
        svc = _svc_perfact(pg_db_session, monkeypatch, subsume=True)
        saved = await svc._extract_and_save_v1_impl(
            "u", "a", user_id=owner.id, captured_kg_subjects=set()
        )
        contents = {m.content for m in saved}
        assert "Anna ist müde" in contents
        assert "Tom arbeitet bei Siemens" in contents
        assert "mag Jazz" in contents

    async def test_flag_off_is_legacy_saves_everything(
        self, pg_db_session, monkeypatch
    ):
        """Subsume OFF (default): every item saved flat, captured set ignored —
        byte-identical to legacy behavior."""
        owner = await _make_user(pg_db_session, "pf_off")
        svc = _svc_perfact(pg_db_session, monkeypatch, subsume=False)
        saved = await svc._extract_and_save_v1_impl(
            "u", "a", user_id=owner.id, captured_kg_subjects={"anna", "tom"}
        )
        contents = {m.content for m in saved}
        assert contents == {"Anna ist müde", "Tom arbeitet bei Siemens", "mag Jazz"}

    async def test_should_subsume_fact_helper_direct(self, pg_db_session, monkeypatch):
        """_should_subsume_fact decision matrix."""
        owner = await _make_user(pg_db_session, "pf_helper")
        await _seed_entity_with_relation(pg_db_session, owner.id, "Anna")
        svc = ConversationMemoryService(pg_db_session)
        monkeypatch.setattr(settings, "memory_subsume_require_kg_relation", True)
        # Per-fact signal present (captured set is authoritative):
        assert await svc._should_subsume_fact("Anna", owner.id, {"anna"}) is True
        assert await svc._should_subsume_fact("Anna", owner.id, set()) is False
        # case-insensitive
        assert await svc._should_subsume_fact("ANNA", owner.id, {"anna"}) is True
        # no subject -> never subsume
        assert await svc._should_subsume_fact(None, owner.id, {"anna"}) is False
        # uncoordinated (None) -> proxy fallback: Anna already-related -> True
        assert await svc._should_subsume_fact("Anna", owner.id, None) is True
        assert await svc._should_subsume_fact("Unknown", owner.id, None) is False
        # guard disabled -> always True (legacy unguarded), even with empty set
        monkeypatch.setattr(settings, "memory_subsume_require_kg_relation", False)
        assert await svc._should_subsume_fact("Anything", owner.id, set()) is True

    async def test_same_turn_same_subject_mixed_loses_state_fact(
        self, pg_db_session, monkeypatch
    ):
        """DOCUMENTED RESIDUAL (NOT closed by this gate): a single turn stating
        BOTH an entity-object fact AND a state fact about the SAME subject. The
        entity-object fact's relation is captured (subject "anna" in the set), so
        the per-(subject,turn) gate subsumes BOTH — the co-stated state fact is
        dropped-and-lost. This asserts the limitation explicitly so it can't
        silently change. Truly-per-fact would need a per-(subject,object) signal.
        """
        owner = await _make_user(pg_db_session, "pf_mixed")
        svc = _svc(pg_db_session, monkeypatch, subsume=True, require_relation=True)
        mixed_items = [
            {"content": "Anna wohnt in Berlin", "category": "fact", "subject": "Anna", "importance": 0.6},
            {"content": "Anna ist müde", "category": "fact", "subject": "Anna", "importance": 0.5},
        ]
        monkeypatch.setattr(
            svc, "_parse_extraction_response", lambda raw: [dict(i) for i in mixed_items]
        )
        # "anna" captured this turn (the entity-object fact's relation was saved).
        captured = {"anna"}
        saved = await svc._extract_and_save_v1_impl(
            "u", "a", user_id=owner.id, captured_kg_subjects=captured
        )
        contents = {m.content for m in saved}
        # Both subsumed because the gate keys on the subject name, not the object:
        assert "Anna wohnt in Berlin" not in contents   # legitimately captured
        assert "Anna ist müde" not in contents          # RESIDUAL LOSS (documented)
