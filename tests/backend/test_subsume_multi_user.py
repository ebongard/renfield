"""Subsume is per-ASKER now (auth-on cutover §8.2, P0 Nr. 7).

Subsume DROPS a flat fact memory on the argument that the knowledge graph
already holds it. Memories are the one corpus with no second copy, so the
argument has to be about the ASKER's graph — and with several users it was not:

* the subject resolver matched ``user_id == asker OR user_id IS NULL``. The
  ownerless half is everyone's: a relation on a legacy auth-off entity made the
  guard say "represented" for a user whose fact it was not, and the fact went.
* the per-turn captured set was keyed by NAME. "Anna" from one graph and "Anna"
  from another are the same string; the KG extractor resolved ITS Anna under its
  own rules, and this side compared text.
* a housemate's reachable tier-2 entity resolved to nothing at all — the same
  name finding no node where the asker would in fact retrieve one.

The rule now: resolution follows REACH (which node does this asker mean), the
proof of representation stays OWNER-bound (only the asker's own relation lets
their memory be dropped). A housemate narrowing their tier must not turn into
this user's data loss.

Real Postgres: every branch of the circle filter is Postgres SQL.
"""
from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from models.database import KGEntity, KGRelation
from services.conversation_memory_service import ConversationMemoryService
from tests.backend.dbrows import ensure_role, ensure_user
from utils.config import settings

pytestmark = [pytest.mark.backend, pytest.mark.database]


async def _member_of(db: AsyncSession, owner_id: int, member_id: int, tier: int) -> None:
    await db.execute(
        text(
            "INSERT INTO circle_memberships "
            "(circle_owner_id, member_user_id, dimension, value, granted_by, granted_at) "
            "VALUES (:o, :m, 'tier', :v, :o, NOW())"
        ),
        {"o": owner_id, "m": member_id, "v": str(tier)},
    )
    await db.commit()


async def _person(
    db: AsyncSession, user_id: int | None, name: str, *, tier: int = 0
) -> KGEntity:
    ent = KGEntity(user_id=user_id, name=name, entity_type="person", circle_tier=tier)
    db.add(ent)
    await db.flush()
    return ent


async def _relation(db: AsyncSession, user_id: int, subject: KGEntity) -> None:
    obj = await _person(db, user_id, f"{subject.name}-ort")
    obj.entity_type = "place"
    await db.flush()
    db.add(KGRelation(
        user_id=user_id, subject_id=subject.id, predicate="wohnt_in",
        object_id=obj.id, confidence=0.9,
    ))
    await db.flush()


@pytest.fixture
async def two_users(db_session: AsyncSession, monkeypatch):
    """Anna and Bert, Bert reaching Anna's tier-2 circle. Auth ON."""
    monkeypatch.setattr(settings, "auth_enabled", True)
    monkeypatch.setattr(settings, "memory_subsume_require_kg_relation", True)
    await ensure_role(db_session)
    anna = await ensure_user(db_session, 1, username="anna")
    bert = await ensure_user(db_session, 2, username="bert")
    await db_session.commit()
    await _member_of(db_session, anna.id, bert.id, 2)
    return anna, bert


class TestSubjectResolutionIsPerAsker:
    async def test_an_ownerless_legacy_entity_is_not_everyones(
        self, db_session, two_users
    ):
        """THE defect. The auth-off era left entities with `user_id IS NULL`.
        Under the old predicate they matched every asker, so one user's relation
        on such a node made the guard drop ANOTHER user's fact about the same
        name — silently, and memories have no second copy."""
        anna, bert = two_users
        ownerless = await _person(db_session, None, "Mama")
        await _relation(db_session, anna.id, ownerless)
        await db_session.commit()

        svc = ConversationMemoryService(db_session)
        # Bert has no reachable Mama of his own: his fact must stay flat.
        assert await svc._subject_is_kg_representable("Mama", bert.id) is False

    async def test_a_reachable_housemates_entity_resolves(self, db_session, two_users):
        """The other half: a tier-2 entity of Anna's that Bert reaches used to
        resolve to nothing, so the same name found no node where Bert would in
        fact retrieve one."""
        anna, bert = two_users
        annas_anna = await _person(db_session, anna.id, "Oma", tier=2)
        await db_session.commit()

        svc = ConversationMemoryService(db_session)
        assert await svc._resolve_subject_entity_id("Oma", bert.id) == annas_anna.id

    async def test_an_unreachable_entity_does_not(self, db_session, two_users):
        """Anna's tier-0 entity is hers alone — Bert resolves nothing."""
        anna, bert = two_users
        await _person(db_session, anna.id, "Tagebuch-Person", tier=0)
        await db_session.commit()

        svc = ConversationMemoryService(db_session)
        assert await svc._resolve_subject_entity_id("Tagebuch-Person", bert.id) is None

    async def test_reach_resolves_but_the_relation_must_be_the_askers(
        self, db_session, two_users
    ):
        """The deliberate asymmetry. Bert REACHES Anna's tier-2 Oma, so that is
        the node he means. But the relation proving representation is Anna's,
        and Anna can narrow her tier tomorrow — dropping Bert's memory on her
        graph would make her setting his data loss. Owner-bound: keep it flat."""
        anna, bert = two_users
        oma = await _person(db_session, anna.id, "Oma", tier=2)
        await _relation(db_session, anna.id, oma)
        await db_session.commit()

        svc = ConversationMemoryService(db_session)
        assert await svc._resolve_subject_entity_id("Oma", bert.id) == oma.id
        assert await svc._subject_is_kg_representable("Oma", bert.id) is False
        # Anna's own fact about her own Oma IS representable.
        assert await svc._subject_is_kg_representable("Oma", anna.id) is True

    async def test_auth_off_keeps_the_legacy_predicate(self, db_session, monkeypatch):
        """One trust domain: the ownerless entity is everyone's, as before."""
        monkeypatch.setattr(settings, "auth_enabled", False)
        monkeypatch.setattr(settings, "memory_subsume_require_kg_relation", True)
        await ensure_role(db_session)
        user = await ensure_user(db_session, 1, username="allein")
        await db_session.commit()
        ownerless = await _person(db_session, None, "Mama")
        await _relation(db_session, user.id, ownerless)
        await db_session.commit()

        svc = ConversationMemoryService(db_session)
        assert await svc._subject_is_kg_representable("Mama", user.id) is True


class TestTheCapturedSetIsKeyedByEntity:
    async def test_a_same_named_entity_of_another_user_does_not_match(
        self, db_session, two_users
    ):
        """Two Annas, one name. The KG captured a relation about Anna-the-user's
        Anna; Bert's fact is about HIS Anna. Keyed by name the gate dropped
        Bert's fact against a node that is not his."""
        anna, bert = two_users
        annas_anna = await _person(db_session, anna.id, "Anna", tier=0)
        berts_anna = await _person(db_session, bert.id, "Anna", tier=0)
        await db_session.commit()

        svc = ConversationMemoryService(db_session)
        captured = {("anna", annas_anna.id, anna.id)}
        # Bert resolves HIS Anna; the captured id is somebody else's.
        assert await svc._resolve_subject_entity_id("Anna", bert.id) == berts_anna.id
        assert await svc._should_subsume_fact("Anna", bert.id, captured) is False
        # Anna's own fact about the captured node IS subsumed.
        assert await svc._should_subsume_fact("Anna", anna.id, captured) is True

    async def test_no_resolvable_subject_keeps_the_fact_flat(
        self, db_session, two_users
    ):
        """Fail-safe: a subject this asker cannot resolve is never subsumed,
        whatever the captured set says."""
        anna, bert = two_users
        ghost = await _person(db_session, anna.id, "Niemand", tier=0)
        await db_session.commit()

        svc = ConversationMemoryService(db_session)
        captured = {("niemand", ghost.id, anna.id)}
        assert await svc._should_subsume_fact("Niemand", bert.id, captured) is False

    async def test_an_empty_captured_set_never_subsumes(self, db_session, two_users):
        anna, _ = two_users
        ent = await _person(db_session, anna.id, "Anna")
        await db_session.commit()
        svc = ConversationMemoryService(db_session)
        assert await svc._should_subsume_fact("Anna", anna.id, set()) is False
        assert ent.id is not None


class TestTheV2PathHasTheSameGate:
    """BL-0421: v2 bypassed the subsume gate entirely, so flipping
    `memory_extraction_v2_authoritative` silently changed what gets stored."""

    async def test_the_op_carries_a_subject(self):
        from services.memory_ops import MemoryOp, OpType

        op = MemoryOp(
            op=OpType.ADD, content="Anna arbeitet bei Arkadon",
            category="fact", subject="Anna",
        )
        assert op.subject == "Anna"

    async def test_a_missing_subject_is_allowed_and_means_keep_flat(self):
        """Optional on purpose: a model that omits it costs a duplicate, never a
        lost memory. Requiring it would turn a model slip into data loss."""
        from services.memory_ops import MemoryOp, OpType

        op = MemoryOp(op=OpType.ADD, content="mag Jazz", category="preference")
        assert op.subject is None

    async def test_the_v2_insert_records_the_subject(self, db_session, two_users):
        """Subject attribution (D9) reaches v2 too — retrieval tags the injected
        context per subject, and a v2 row without it conflates people."""
        anna, _ = two_users
        svc = ConversationMemoryService(db_session)
        memory = await svc._apply_add_v2(
            content="Anna arbeitet bei Arkadon",
            category="fact",
            importance=0.7,
            user_id=anna.id,
            session_id="s",
            subject="Anna",
        )
        await db_session.commit()
        assert memory is not None
        assert memory.subject_name == "Anna"


class TestTheStartupGate:
    """Subsume stays single-user until the cutover says otherwise (D-7). The
    household must turn it OFF when it turns auth ON — an automatic 'it is
    fixed, carry on' is the silent widening the cutover exists to avoid."""

    def test_subsume_with_auth_on_refuses_to_boot(self):
        from utils.config import Settings

        with pytest.raises(ValueError, match="MEMORY_SUBSUME_TO_KG"):
            Settings(auth_enabled=True, memory_subsume_to_kg=True)

    def test_subsume_with_auth_off_is_the_household_today(self):
        from utils.config import Settings

        s = Settings(auth_enabled=False, memory_subsume_to_kg=True)
        assert s.memory_subsume_to_kg is True

    def test_auth_on_without_subsume_boots(self):
        from utils.config import Settings

        s = Settings(auth_enabled=True, memory_subsume_to_kg=False)
        assert s.auth_enabled is True

    def test_the_message_says_what_to_do(self):
        from utils.config import Settings

        with pytest.raises(ValueError) as exc:
            Settings(auth_enabled=True, memory_subsume_to_kg=True)
        message = str(exc.value)
        assert "MEMORY_SUBSUME_TO_KG=" in message and "false" in message
