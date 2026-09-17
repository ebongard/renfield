"""Ownership guard for extraction-driven writes on EXISTING memory rows.

The v2 apply path (`_apply_update_v2` / `_apply_delete_v2`) and the v1
contradiction-resolution path both take a `target_id` straight out of an LLM
response. The only membership check on that id —
`services/memory_ops.validate_against_candidates` — deliberately does NOT
validate ownership; its own docstring demands an integration test where the
candidate set spans two users and where the OWNERSHIP recheck (not the
membership check) is what rejects the foreign row. That test is this file.

`user_id` reaches the extraction as `int | None`:
  * auth OFF household turn      -> None  (single trust domain, legitimate)
  * auth ON device/satellite turn -> None  (no identity, NOT legitimate)
`MemoryRetrieval.retrieve` degrades its circle filter for None to `TRUE`
(auth off) resp. public-tier-only (auth on), so the candidate set can span
other users either way. The guard must therefore key on `auth_enabled`, not
merely on "is user_id set".
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.database import ConversationMemory, Role, User
from services.conversation_memory_service import ConversationMemoryService
from services.memory_ops import MemoryOp, MemoryOpsList, OpType, validate_against_candidates
from utils.config import settings


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


async def _make_user(db_session: AsyncSession, username: str) -> User:
    result = await db_session.execute(select(Role).where(Role.name == "OwnershipGuardRole"))
    role = result.scalar_one_or_none()
    if role is None:
        role = Role(name="OwnershipGuardRole", permissions=["kb.own"], is_system=False)
        db_session.add(role)
        await db_session.commit()
        await db_session.refresh(role)

    user = User(
        username=username,
        email=f"{username}@example.com",
        password_hash="fakehash",
        is_active=True,
        role_id=role.id,
    )
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user


async def _make_memory(
    db_session: AsyncSession,
    *,
    user_id: int | None,
    content: str = "Alice trinkt Kaffee schwarz",
) -> ConversationMemory:
    memory = ConversationMemory(
        content=content,
        category="fact",
        user_id=user_id,
        importance=0.5,
        is_active=True,
        access_count=0,
        circle_tier=0,
    )
    db_session.add(memory)
    await db_session.commit()
    await db_session.refresh(memory)
    return memory


async def _content_of(db_session: AsyncSession, memory_id: int) -> str:
    result = await db_session.execute(
        select(ConversationMemory.content).where(ConversationMemory.id == memory_id)
    )
    return result.scalar_one()


async def _is_active(db_session: AsyncSession, memory_id: int) -> bool:
    result = await db_session.execute(
        select(ConversationMemory.is_active).where(ConversationMemory.id == memory_id)
    )
    return bool(result.scalar_one())


def _service(db_session: AsyncSession) -> ConversationMemoryService:
    service = ConversationMemoryService(db_session)
    # Never call the embedding backend from a unit test; None simply omits
    # the embedding from the UPDATE values.
    service._get_embedding = AsyncMock(return_value=None)
    return service


# ---------------------------------------------------------------------------
# v2 apply path — the candidate set spans two users
# ---------------------------------------------------------------------------


class TestV2ForeignTargetRejected:
    """The candidate set deliberately spans two users (auth ON).

    These assert the OWNERSHIP recheck rejects the foreign target_id. The
    membership check cannot: the id IS a member of the candidate set, which
    each test proves explicitly by calling `validate_against_candidates`.
    """

    @pytest.mark.database
    async def test_membership_check_cannot_catch_a_foreign_but_present_target(
        self, db_session: AsyncSession
    ):
        owner = await _make_user(db_session, "guard_owner_a")
        foreign_memory = await _make_memory(db_session, user_id=owner.id)

        ops = MemoryOpsList(
            root=[
                MemoryOp(
                    op=OpType.UPDATE,
                    target_id=foreign_memory.id,
                    content="übernommen",
                )
            ]
        )
        # The asker is a DIFFERENT user, yet the row is in the candidate set
        # (over-reaching circle filter / poisoned retrieval). The schema-level
        # check passes — proving it is NOT what protects the row.
        assert validate_against_candidates(ops, {foreign_memory.id}) is None

    @pytest.mark.database
    async def test_update_rejects_target_owned_by_another_user(
        self, db_session: AsyncSession
    ):
        owner = await _make_user(db_session, "guard_owner_b")
        asker = await _make_user(db_session, "guard_asker_b")
        memory = await _make_memory(db_session, user_id=owner.id)
        service = _service(db_session)

        with patch.object(settings, "auth_enabled", True):
            applied = await service._apply_update_v2(
                target_id=memory.id,
                content="übernommen",
                category=None,
                importance=None,
                user_id=asker.id,
            )

        assert applied is False
        assert await _content_of(db_session, memory.id) == "Alice trinkt Kaffee schwarz"

    @pytest.mark.database
    async def test_delete_rejects_target_owned_by_another_user(
        self, db_session: AsyncSession
    ):
        owner = await _make_user(db_session, "guard_owner_c")
        asker = await _make_user(db_session, "guard_asker_c")
        memory = await _make_memory(db_session, user_id=owner.id)
        service = _service(db_session)

        with patch.object(settings, "auth_enabled", True):
            applied = await service._apply_delete_v2(
                target_id=memory.id, user_id=asker.id
            )

        assert applied is False
        assert await _is_active(db_session, memory.id) is True

    @pytest.mark.database
    async def test_update_still_applies_to_the_askers_own_row(
        self, db_session: AsyncSession
    ):
        owner = await _make_user(db_session, "guard_owner_d")
        memory = await _make_memory(db_session, user_id=owner.id)
        service = _service(db_session)

        with patch.object(settings, "auth_enabled", True):
            applied = await service._apply_update_v2(
                target_id=memory.id,
                content="Alice trinkt Kaffee mit Milch",
                category=None,
                importance=None,
                user_id=owner.id,
            )

        assert applied is True
        assert await _content_of(db_session, memory.id) == "Alice trinkt Kaffee mit Milch"


# ---------------------------------------------------------------------------
# v2 apply path — unidentified turn (device / satellite) on an auth-ON instance
# ---------------------------------------------------------------------------


class TestV2UnidentifiedTurnAuthOn:

    @pytest.mark.database
    async def test_update_refused_without_identity(self, db_session: AsyncSession):
        owner = await _make_user(db_session, "guard_owner_e")
        memory = await _make_memory(db_session, user_id=owner.id)
        service = _service(db_session)

        # The membership check cannot save the row here either: it IS a
        # member of the candidate set a public-tier retrieval returns.
        ops = MemoryOpsList(
            root=[
                MemoryOp(
                    op=OpType.UPDATE,
                    target_id=memory.id,
                    content="vom Gerät überschrieben",
                )
            ]
        )
        assert validate_against_candidates(ops, {memory.id}) is None

        with patch.object(settings, "auth_enabled", True):
            applied = await service._apply_update_v2(
                target_id=memory.id,
                content="vom Gerät überschrieben",
                category=None,
                importance=None,
                user_id=None,
            )

        assert applied is False
        assert await _content_of(db_session, memory.id) == "Alice trinkt Kaffee schwarz"
        # Refused BEFORE any work is done.
        service._get_embedding.assert_not_called()

    @pytest.mark.database
    async def test_delete_refused_without_identity(self, db_session: AsyncSession):
        owner = await _make_user(db_session, "guard_owner_f")
        memory = await _make_memory(db_session, user_id=owner.id)
        service = _service(db_session)

        with patch.object(settings, "auth_enabled", True):
            applied = await service._apply_delete_v2(target_id=memory.id, user_id=None)

        assert applied is False
        assert await _is_active(db_session, memory.id) is True


# ---------------------------------------------------------------------------
# v2 apply path — auth-OFF household must keep working
# ---------------------------------------------------------------------------


class TestV2AuthOffHousehold:
    """Auth off: every household row is attributed to the fallback owner by
    `_resolve_owner_user_id`, while the turn itself carries user_id=None.
    Refusing there would silently stop household memory maintenance."""

    @pytest.mark.database
    async def test_update_applies_without_identity(self, db_session: AsyncSession):
        owner = await _make_user(db_session, "guard_household_a")
        memory = await _make_memory(db_session, user_id=owner.id)
        service = _service(db_session)

        with patch.object(settings, "auth_enabled", False):
            applied = await service._apply_update_v2(
                target_id=memory.id,
                content="Alice trinkt Tee",
                category=None,
                importance=None,
                user_id=None,
            )

        assert applied is True
        assert await _content_of(db_session, memory.id) == "Alice trinkt Tee"

    @pytest.mark.database
    async def test_delete_applies_without_identity(self, db_session: AsyncSession):
        owner = await _make_user(db_session, "guard_household_b")
        memory = await _make_memory(db_session, user_id=owner.id)
        service = _service(db_session)

        with patch.object(settings, "auth_enabled", False):
            applied = await service._apply_delete_v2(target_id=memory.id, user_id=None)

        assert applied is True
        assert await _is_active(db_session, memory.id) is False


# ---------------------------------------------------------------------------
# v1 contradiction-resolution path — the same defect, different code
# ---------------------------------------------------------------------------


class TestV1CandidateSetFailsClosed:
    """`_find_similar_memories` / `_find_duplicate` are the v1 equivalent of the
    v2 candidate set — raw SQL whose user filter is dropped for user_id=None."""

    @pytest.mark.unit
    async def test_find_similar_memories_returns_nothing_without_identity(self):
        db = MagicMock()
        db.execute = AsyncMock()
        service = ConversationMemoryService(db)

        with patch.object(settings, "auth_enabled", True):
            result = await service._find_similar_memories([0.1, 0.2], user_id=None)

        assert result == []
        db.execute.assert_not_called()

    @pytest.mark.unit
    async def test_find_duplicate_returns_nothing_without_identity(self):
        db = MagicMock()
        db.execute = AsyncMock()
        service = ConversationMemoryService(db)

        with patch.object(settings, "auth_enabled", True):
            result = await service._find_duplicate([0.1, 0.2], user_id=None)

        assert result is None
        db.execute.assert_not_called()


class TestV1ContradictionForeignTarget:

    async def _resolution_service(
        self, db_session: AsyncSession, target_memory: ConversationMemory, action: str
    ) -> ConversationMemoryService:
        service = ConversationMemoryService(db_session)
        service._get_embedding = AsyncMock(return_value=[0.1, 0.2])
        service._find_duplicate = AsyncMock(return_value=None)
        service._find_similar_memories = AsyncMock(
            return_value=[
                {
                    "id": target_memory.id,
                    "content": target_memory.content,
                    "category": target_memory.category,
                    "importance": target_memory.importance,
                    "similarity": 0.8,
                }
            ]
        )
        service._resolve_contradiction = AsyncMock(
            return_value={
                "action": action,
                "target_memory_id": target_memory.id,
                "updated_content": "übernommen",
                "reason": "test",
            }
        )
        # The refusal falls back to ADD. `save()` persists a real embedding,
        # which the sqlite test shim cannot bind — and the ADD itself is not
        # what this test is about, so stub it and assert it was reached.
        service.save = AsyncMock(return_value=None)
        return service

    @pytest.mark.database
    async def test_update_of_foreign_row_refused(self, db_session: AsyncSession):
        owner = await _make_user(db_session, "guard_v1_owner_a")
        asker = await _make_user(db_session, "guard_v1_asker_a")
        memory = await _make_memory(db_session, user_id=owner.id)
        service = await self._resolution_service(db_session, memory, "UPDATE")

        with patch.object(settings, "auth_enabled", True):
            await service._apply_contradiction_resolution(
                content="Alice trinkt Tee",
                category="fact",
                importance=0.5,
                user_id=asker.id,
                session_id=None,
                lang="de",
            )

        assert await _content_of(db_session, memory.id) == "Alice trinkt Kaffee schwarz"
        # Refused, not silently dropped: the fact is saved as a new row.
        assert service.save.await_count == 1

    @pytest.mark.database
    async def test_delete_of_foreign_row_refused(self, db_session: AsyncSession):
        owner = await _make_user(db_session, "guard_v1_owner_b")
        asker = await _make_user(db_session, "guard_v1_asker_b")
        memory = await _make_memory(db_session, user_id=owner.id)
        service = await self._resolution_service(db_session, memory, "DELETE")

        with patch.object(settings, "auth_enabled", True):
            await service._apply_contradiction_resolution(
                content="Alice trinkt Tee",
                category="fact",
                importance=0.5,
                user_id=asker.id,
                session_id=None,
                lang="de",
            )

        assert await _is_active(db_session, memory.id) is True
        assert service.save.await_count == 1
