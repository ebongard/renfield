"""Tests for chat message branching (edit-and-fork, Phase 1).

Two layers, mirroring test_message_search.py:

  * **sqlite** (``@pytest.mark.database``) — the tree-maintenance write path
    (``save_message`` always chains ``parent_message_id`` + advances
    ``active_leaf_message_id``; an explicit ``parent_message_id`` forks a
    sibling) and the ``set_active_leaf`` ownership gate. The recursive CTEs
    return ``[]`` on sqlite (PG-only), so the read helpers fall back — asserted
    here by inspecting the columns directly.

  * **Postgres** (``@pytest.mark.postgres``) — the recursive CTEs themselves:
    ``active_path_message_ids`` reproduces the historical (timestamp,id) order
    for a linearly-chained conversation (the exact shape the migration backfill
    produces), a fork's history follows the NEW branch not the old, the
    downward abandoned-subtree walk, and deactivate-at-fork flipping
    ``is_active=False`` on the abandoned subtree's memories.

PG tests build trees with direct model inserts + ``flush()`` (NOT
``save_message``, which commits — that would break the ``pg_db_session``
rollback isolation). ``deactivate_memories_for_abandoned_subtree`` does NOT
commit internally, so it composes with the flush-based fixture.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from models.database import (
    Conversation,
    ConversationMemory,
    KGEntity,
    KGRelation,
    Message,
    Role,
    User,
)
from services.conversation_service import ConversationService


async def _is_active(db: AsyncSession, mem_id: int) -> bool:
    return (
        await db.execute(
            select(ConversationMemory.is_active).where(
                ConversationMemory.id == mem_id
            )
        )
    ).scalar_one()


async def _fork_tree(db: AsyncSession, session_id: str, user_id: int):
    """u0 (root) with TWO assistant siblings a_x, a_y (a regenerate-style fork).
    Returns (conv, u0, a_x, a_y). Leaf left unset — the caller points it at the
    branch under test."""
    conv = Conversation(session_id=session_id, user_id=user_id)
    db.add(conv)
    await db.flush()
    base = datetime(2026, 4, 1, 10, 0, 0)
    u0 = Message(conversation_id=conv.id, role="user", content="frage",
                 timestamp=base, parent_message_id=None)
    db.add(u0)
    await db.flush()
    a_x = Message(conversation_id=conv.id, role="assistant", content="antwort x",
                  timestamp=base + timedelta(minutes=1), parent_message_id=u0.id)
    a_y = Message(conversation_id=conv.id, role="assistant", content="antwort y",
                  timestamp=base + timedelta(minutes=2), parent_message_id=u0.id)
    db.add_all([a_x, a_y])
    await db.flush()
    return conv, u0, a_x, a_y


# ===========================================================================
# sqlite — tree maintenance on the write path + set_active_leaf gate
# ===========================================================================
@pytest.mark.backend
@pytest.mark.database
class TestTreeMaintenanceSqlite:
    async def test_save_message_chains_parent_and_advances_leaf(self, db_session: AsyncSession):
        svc = ConversationService(db_session)
        m1 = await svc.save_message("c", "user", "hallo", user_id=1)
        m2 = await svc.save_message("c", "assistant", "hi", user_id=1)
        m3 = await svc.save_message("c", "user", "wie geht's", user_id=1)

        # Parent chain: m1=root, m2→m1, m3→m2.
        assert m1.parent_message_id is None
        assert m2.parent_message_id == m1.id
        assert m3.parent_message_id == m2.id

        # active_leaf advanced to the last message.
        conv = (
            await db_session.execute(
                select(Conversation).where(Conversation.session_id == "c")
            )
        ).scalar_one()
        assert conv.active_leaf_message_id == m3.id

    async def test_explicit_parent_forks_a_sibling(self, db_session: AsyncSession):
        svc = ConversationService(db_session)
        m1 = await svc.save_message("c", "user", "frage A", user_id=1)
        m2 = await svc.save_message("c", "assistant", "antwort A", user_id=1)
        # Fork: a new user message under m1 (sibling of m2's branch).
        fork = await svc.save_message(
            "c", "user", "frage A neu", user_id=1, parent_message_id=m1.id
        )
        assert fork.parent_message_id == m1.id
        assert fork.id != m2.id
        # Leaf advanced to the fork.
        conv = (
            await db_session.execute(
                select(Conversation).where(Conversation.session_id == "c")
            )
        ).scalar_one()
        assert conv.active_leaf_message_id == fork.id
        # The original branch (m2) is preserved (still in the table).
        still = (
            await db_session.execute(select(Message).where(Message.id == m2.id))
        ).scalar_one_or_none()
        assert still is not None

    async def test_set_active_leaf_ownership_gate(self, db_session: AsyncSession):
        svc = ConversationService(db_session)
        m1 = await svc.save_message("c", "user", "x", user_id=7)
        await svc.save_message("c", "assistant", "y", user_id=7)

        # Wrong owner → False (the route maps this to 404).
        assert await svc.set_active_leaf("c", m1.id, user_id=999) is False
        # Foreign message id → False.
        assert await svc.set_active_leaf("c", 424242, user_id=7) is False
        # Correct owner + valid message → True, leaf repointed.
        assert await svc.set_active_leaf("c", m1.id, user_id=7) is True
        conv = (
            await db_session.execute(
                select(Conversation).where(Conversation.session_id == "c")
            )
        ).scalar_one()
        assert conv.active_leaf_message_id == m1.id

    async def test_missing_conversation_returns_false(self, db_session: AsyncSession):
        svc = ConversationService(db_session)
        assert await svc.set_active_leaf("nope", 1, user_id=1) is False


# ===========================================================================
# Postgres — the recursive CTEs
# ===========================================================================
@pytest.fixture
async def branch_user(pg_db_session: AsyncSession) -> int:
    role = Role(name="branch-role", description="role for branching tests")
    pg_db_session.add(role)
    await pg_db_session.flush()
    u = User(
        username="branch-u1", email="branch-u1@example.invalid",
        password_hash="x", is_active=True, role_id=role.id,
    )
    pg_db_session.add(u)
    await pg_db_session.flush()
    return u.id


async def _linear_conv(db: AsyncSession, session_id: str, user_id: int, n: int):
    """Build a linearly-chained conversation (the backfill shape): each message
    parented to the previous, leaf = last, ascending timestamps."""
    conv = Conversation(session_id=session_id, user_id=user_id)
    db.add(conv)
    await db.flush()
    base = datetime(2026, 1, 1, 12, 0, 0)
    prev_id = None
    ids = []
    for i in range(n):
        m = Message(
            conversation_id=conv.id,
            role="user" if i % 2 == 0 else "assistant",
            content=f"msg {i}",
            timestamp=base + timedelta(minutes=i),
            parent_message_id=prev_id,
        )
        db.add(m)
        await db.flush()
        prev_id = m.id
        ids.append(m.id)
    conv.active_leaf_message_id = ids[-1]
    await db.flush()
    return conv, ids


@pytest.mark.postgres
@pytest.mark.asyncio
@pytest.mark.backend
class TestActivePathPostgres:
    async def test_linear_chain_reproduces_history_order(
        self, pg_db_session: AsyncSession, branch_user
    ):
        conv, ids = await _linear_conv(pg_db_session, "pg-lin", branch_user, 5)
        svc = ConversationService(pg_db_session)
        path = await svc.active_path_message_ids(conv)
        # Exact (timestamp ASC, id ASC) order — same as /api/chat/history.
        assert path == ids

    async def test_null_leaf_returns_empty(
        self, pg_db_session: AsyncSession, branch_user
    ):
        conv = Conversation(session_id="pg-empty", user_id=branch_user)
        pg_db_session.add(conv)
        await pg_db_session.flush()
        svc = ConversationService(pg_db_session)
        assert await svc.active_path_message_ids(conv) == []

    async def test_fork_history_follows_new_branch_not_old(
        self, pg_db_session: AsyncSession, branch_user
    ):
        # Linear: u0 → a1 (ids[0], ids[1]).
        conv, ids = await _linear_conv(pg_db_session, "pg-fork", branch_user, 2)
        # Fork: new user message as a sibling of u0 (same parent = root NULL),
        # then an assistant under it. Point the leaf at the new assistant.
        base = datetime(2026, 1, 1, 13, 0, 0)
        new_u = Message(
            conversation_id=conv.id, role="user", content="edited",
            timestamp=base, parent_message_id=None,
        )
        pg_db_session.add(new_u)
        await pg_db_session.flush()
        new_a = Message(
            conversation_id=conv.id, role="assistant", content="new answer",
            timestamp=base + timedelta(minutes=1), parent_message_id=new_u.id,
        )
        pg_db_session.add(new_a)
        await pg_db_session.flush()
        conv.active_leaf_message_id = new_a.id
        await pg_db_session.flush()

        svc = ConversationService(pg_db_session)
        path = await svc.active_path_message_ids(conv)
        # The active path is the NEW branch; the old turns are gone from it.
        assert path == [new_u.id, new_a.id]
        assert ids[0] not in path and ids[1] not in path

    async def test_abandoned_subtree_walks_descendants(
        self, pg_db_session: AsyncSession, branch_user
    ):
        # u0 → a1 → u2 → a3 (a chain). Abandon from a1 → {a1, u2, a3}.
        conv, ids = await _linear_conv(pg_db_session, "pg-sub", branch_user, 4)
        svc = ConversationService(pg_db_session)
        subtree = await svc._abandoned_subtree_message_ids(ids[1])
        assert set(subtree) == {ids[1], ids[2], ids[3]}
        assert ids[0] not in subtree

    async def test_deactivate_memories_for_abandoned_subtree(
        self, pg_db_session: AsyncSession, branch_user
    ):
        conv, ids = await _linear_conv(pg_db_session, "pg-mem", branch_user, 4)
        # A memory sourced from a message in the abandoned subtree (a3) + one
        # from a kept message (u0).
        mem_abandoned = ConversationMemory(
            user_id=branch_user, content="abandoned fact",
            source_message_id=ids[3], is_active=True, circle_tier=0,
        )
        mem_kept = ConversationMemory(
            user_id=branch_user, content="kept fact",
            source_message_id=ids[0], is_active=True, circle_tier=0,
        )
        pg_db_session.add_all([mem_abandoned, mem_kept])
        await pg_db_session.flush()

        svc = ConversationService(pg_db_session)
        # Abandon from a1 → subtree {a1, u2, a3}; mem_abandoned (a3) flips off.
        n = await svc.deactivate_memories_for_abandoned_subtree(ids[1])
        await pg_db_session.flush()
        assert n == 1

        ref_ab = (
            await pg_db_session.execute(
                select(ConversationMemory.is_active).where(
                    ConversationMemory.id == mem_abandoned.id
                )
            )
        ).scalar_one()
        ref_kept = (
            await pg_db_session.execute(
                select(ConversationMemory.is_active).where(
                    ConversationMemory.id == mem_kept.id
                )
            )
        ).scalar_one()
        assert ref_ab is False
        assert ref_kept is True


# ===========================================================================
# Postgres — search restricted to the active branch + branch-local index
# ===========================================================================
@pytest.fixture
async def messages_fts_installed_branch(pg_db_session: AsyncSession) -> None:
    from services.fts_languages import build_generated_tsvector_expression

    expr = build_generated_tsvector_expression("content")
    await pg_db_session.execute(text("ALTER TABLE messages DROP COLUMN IF EXISTS search_vector"))
    await pg_db_session.execute(text(
        f"ALTER TABLE messages ADD COLUMN search_vector tsvector "
        f"GENERATED ALWAYS AS ({expr}) STORED"
    ))
    await pg_db_session.flush()


@pytest.mark.postgres
@pytest.mark.asyncio
@pytest.mark.backend
class TestSearchActiveBranchPostgres:
    async def test_search_excludes_abandoned_branch_and_indexes_within_branch(
        self, pg_db_session: AsyncSession, messages_fts_installed_branch, branch_user
    ):
        # Active branch: u0("apfel alpha") → a1 → u2("apfel beta"). Plus an
        # ABANDONED sibling of u2 ("apfel gamma") that is NOT on the active path.
        conv = Conversation(session_id="pg-search-branch", user_id=branch_user)
        pg_db_session.add(conv)
        await pg_db_session.flush()
        base = datetime(2026, 2, 1, 9, 0, 0)
        u0 = Message(conversation_id=conv.id, role="user", content="apfel alpha",
                     timestamp=base, parent_message_id=None)
        pg_db_session.add(u0)
        await pg_db_session.flush()
        a1 = Message(conversation_id=conv.id, role="assistant", content="ok eins",
                     timestamp=base + timedelta(minutes=1), parent_message_id=u0.id)
        pg_db_session.add(a1)
        await pg_db_session.flush()
        # Abandoned sibling under a1.
        gamma = Message(conversation_id=conv.id, role="user", content="apfel gamma",
                        timestamp=base + timedelta(minutes=2), parent_message_id=a1.id)
        pg_db_session.add(gamma)
        await pg_db_session.flush()
        # Active u2 under a1 (a sibling of gamma), later → active leaf.
        u2 = Message(conversation_id=conv.id, role="user", content="apfel beta",
                     timestamp=base + timedelta(minutes=3), parent_message_id=a1.id)
        pg_db_session.add(u2)
        await pg_db_session.flush()
        conv.active_leaf_message_id = u2.id
        await pg_db_session.flush()

        svc = ConversationService(pg_db_session)
        out = await svc.search_messages("apfel", user_id=branch_user)
        sessions = {r["session_id"] for r in out["results"]}
        assert sessions == {"pg-search-branch"}
        contents = {r["content"] for r in out["results"]}
        # The abandoned "apfel gamma" must NOT appear.
        assert "apfel gamma" not in contents
        assert {"apfel alpha", "apfel beta"} <= contents
        # message_index is branch-local: u0 = 0, u2 = 2 (u0,a1,u2 on the branch).
        by_content = {r["content"]: r["message_index"] for r in out["results"]}
        assert by_content["apfel alpha"] == 0
        assert by_content["apfel beta"] == 2


# ===========================================================================
# P1 #1 — cross-conversation IDOR: the recursive walk must NEVER leave the
# seed conversation, even when a stray parent pointer crosses conversations.
# ===========================================================================
@pytest.fixture
async def two_branch_users(pg_db_session: AsyncSession) -> tuple[int, int]:
    role = Role(name="idor-role", description="role for idor branching tests")
    pg_db_session.add(role)
    await pg_db_session.flush()
    ua = User(
        username="idor-a", email="idor-a@example.invalid",
        password_hash="x", is_active=True, role_id=role.id,
    )
    ub = User(
        username="idor-b", email="idor-b@example.invalid",
        password_hash="x", is_active=True, role_id=role.id,
    )
    pg_db_session.add_all([ua, ub])
    await pg_db_session.flush()
    return ua.id, ub.id


@pytest.mark.postgres
@pytest.mark.asyncio
@pytest.mark.backend
class TestCrossConversationIsolationPostgres:
    """A bad cross-conversation parent pointer must not let either CTE climb
    into another conversation (defense in depth behind the handler's
    session-scoped fork lookup)."""

    async def _build_two_conversations_with_cross_pointer(
        self, db: AsyncSession, user_a: int, user_b: int
    ):
        """Conversation A (user A) + Conversation B (user B). A message in A is
        given a parent_message_id pointing INTO B — the corruption the recursive
        CTE must refuse to follow."""
        # Conversation B (the "secret" one).
        conv_b = Conversation(session_id="idor-b-conv", user_id=user_b)
        db.add(conv_b)
        await db.flush()
        base_b = datetime(2026, 3, 1, 8, 0, 0)
        b_secret = Message(
            conversation_id=conv_b.id, role="user", content="B SECRET banane",
            timestamp=base_b, parent_message_id=None,
        )
        db.add(b_secret)
        await db.flush()
        conv_b.active_leaf_message_id = b_secret.id

        # Conversation A. a_root is the real root; a_leaf is the active leaf,
        # but a_leaf's parent is MISWIRED to point into conversation B.
        conv_a = Conversation(session_id="idor-a-conv", user_id=user_a)
        db.add(conv_a)
        await db.flush()
        base_a = datetime(2026, 3, 1, 9, 0, 0)
        a_root = Message(
            conversation_id=conv_a.id, role="user", content="A banane alpha",
            timestamp=base_a, parent_message_id=None,
        )
        db.add(a_root)
        await db.flush()
        a_leaf = Message(
            conversation_id=conv_a.id, role="assistant", content="A banane beta",
            timestamp=base_a + timedelta(minutes=1),
            parent_message_id=b_secret.id,  # ← cross-conversation corruption
        )
        db.add(a_leaf)
        await db.flush()
        conv_a.active_leaf_message_id = a_leaf.id
        await db.flush()
        return conv_a, conv_b, a_root, a_leaf, b_secret

    async def test_active_path_never_crosses_into_other_conversation(
        self, pg_db_session: AsyncSession, two_branch_users
    ):
        ua, ub = two_branch_users
        conv_a, conv_b, a_root, a_leaf, b_secret = (
            await self._build_two_conversations_with_cross_pointer(
                pg_db_session, ua, ub
            )
        )
        svc = ConversationService(pg_db_session)
        path = await svc.active_path_message_ids(conv_a)
        # The walk stops at a_leaf (its cross-conversation parent is refused),
        # so B's secret message is NEVER pulled into conversation A's path.
        assert b_secret.id not in path
        assert a_leaf.id in path
        # And no returned id belongs to conversation B.
        rows = await pg_db_session.execute(
            select(Message.id, Message.conversation_id).where(Message.id.in_(path))
        )
        for mid, cid in rows.all():
            assert cid == conv_a.id, f"msg {mid} from foreign conversation {cid}"

    async def test_search_active_cte_never_crosses_into_other_conversation(
        self, pg_db_session: AsyncSession, messages_fts_installed_branch, two_branch_users
    ):
        ua, ub = two_branch_users
        conv_a, conv_b, a_root, a_leaf, b_secret = (
            await self._build_two_conversations_with_cross_pointer(
                pg_db_session, ua, ub
            )
        )
        svc = ConversationService(pg_db_session)
        # User A searches the shared term "banane". B's "B SECRET banane" must
        # NOT surface for A — neither via ownership nor by the CTE climbing the
        # bad pointer into conversation B.
        out = await svc.search_messages("banane", user_id=ua)
        contents = {r["content"] for r in out["results"]}
        assert "B SECRET banane" not in contents
        sessions = {r["session_id"] for r in out["results"]}
        assert sessions == {"idor-a-conv"}

    async def test_abandoned_subtree_never_crosses_into_other_conversation(
        self, pg_db_session: AsyncSession, two_branch_users
    ):
        """The downward subtree walk must not pull a foreign child via a stray
        cross-conversation child pointer (would deactivate foreign memories)."""
        ua, ub = two_branch_users
        # Conversation A root.
        conv_a = Conversation(session_id="idor-sub-a", user_id=ua)
        pg_db_session.add(conv_a)
        await pg_db_session.flush()
        base = datetime(2026, 3, 2, 9, 0, 0)
        a_root = Message(conversation_id=conv_a.id, role="user", content="a sub root",
                         timestamp=base, parent_message_id=None)
        pg_db_session.add(a_root)
        await pg_db_session.flush()
        # Conversation B child MISWIRED to parent off A's root.
        conv_b = Conversation(session_id="idor-sub-b", user_id=ub)
        pg_db_session.add(conv_b)
        await pg_db_session.flush()
        b_child = Message(conversation_id=conv_b.id, role="user", content="b foreign child",
                          timestamp=base + timedelta(minutes=1),
                          parent_message_id=a_root.id)  # ← cross-conversation corruption
        pg_db_session.add(b_child)
        await pg_db_session.flush()

        svc = ConversationService(pg_db_session)
        subtree = await svc._abandoned_subtree_message_ids(a_root.id)
        # b_child belongs to conversation B and must be refused.
        assert b_child.id not in subtree
        assert subtree == [a_root.id]


# ===========================================================================
# P1 #2 — conversation deletion must not raise an FK violation on the
# active_leaf_message_id pointer (ON DELETE SET NULL).
# ===========================================================================
@pytest.mark.backend
@pytest.mark.database
class TestDeletionFkSqlite:
    def test_active_leaf_fk_carries_set_null(self):
        """The model FK must declare ondelete='SET NULL' so a conversation
        delete (which cascade-deletes its messages first) doesn't dangle the
        leaf pointer. Sqlite skips FK enforcement by default, so assert the
        declaration directly (robust regardless of PRAGMA)."""
        fk = next(
            iter(Conversation.__table__.c.active_leaf_message_id.foreign_keys)
        )
        assert fk.ondelete == "SET NULL"

    async def test_delete_backfilled_conversation_does_not_raise(
        self, db_session: AsyncSession
    ):
        """Deleting a conversation whose active_leaf_message_id is set must not
        raise. Enable sqlite FK enforcement for this test so the SET NULL action
        is actually exercised (sqlite ignores FKs by default)."""
        await db_session.execute(text("PRAGMA foreign_keys=ON"))
        # With FK enforcement ON, the conversation's user_id FK is also checked,
        # so seed a real user (else the conversation INSERT fails on the users
        # FK, masking what this test is actually about).
        role = Role(name="del-role", description="role for delete test")
        db_session.add(role)
        await db_session.flush()
        u = User(
            username="del-u1", email="del-u1@example.invalid",
            password_hash="x", is_active=True, role_id=role.id,
        )
        db_session.add(u)
        await db_session.flush()
        svc = ConversationService(db_session)
        await svc.save_message("del-conv", "user", "frage", user_id=u.id)
        await svc.save_message("del-conv", "assistant", "antwort", user_id=u.id)
        conv = (
            await db_session.execute(
                select(Conversation).where(Conversation.session_id == "del-conv")
            )
        ).scalar_one()
        assert conv.active_leaf_message_id is not None
        # ORM cascade deletes the messages, then the conversation row. With
        # ondelete=SET NULL the leaf pointer clears as the messages go.
        await db_session.delete(conv)
        await db_session.commit()
        gone = (
            await db_session.execute(
                select(Conversation).where(Conversation.session_id == "del-conv")
            )
        ).scalar_one_or_none()
        assert gone is None


# ===========================================================================
# P1 #2 (Postgres) — the real FK-violation path the sqlite default hides.
# ===========================================================================
@pytest.mark.postgres
@pytest.mark.asyncio
@pytest.mark.backend
class TestDeletionFkPostgres:
    async def test_delete_backfilled_conversation_does_not_raise(
        self, pg_db_session: AsyncSession, branch_user
    ):
        conv, ids = await _linear_conv(pg_db_session, "pg-del", branch_user, 4)
        assert conv.active_leaf_message_id == ids[-1]
        # On Postgres the FK IS enforced — without ON DELETE SET NULL this raises
        # a ForeignKeyViolation. Cascade-delete the messages (mirrors the ORM
        # relationship cascade) then the conversation row.
        await pg_db_session.execute(
            text("DELETE FROM messages WHERE conversation_id = :cid"),
            {"cid": conv.id},
        )
        await pg_db_session.execute(
            text("DELETE FROM conversations WHERE id = :cid"), {"cid": conv.id}
        )
        await pg_db_session.flush()
        remaining = (
            await pg_db_session.execute(
                text("SELECT count(*) FROM conversations WHERE id = :cid"),
                {"cid": conv.id},
            )
        ).scalar()
        assert remaining == 0


# ===========================================================================
# Phase 2 — symmetric memory recompute (deactivate AND reactivate)
# ===========================================================================
@pytest.mark.postgres
@pytest.mark.asyncio
@pytest.mark.backend
class TestRecomputeMemoryActivationPostgres:
    async def test_recompute_is_symmetric_deactivate_then_reactivate(
        self, pg_db_session: AsyncSession, branch_user
    ):
        conv, u0, a_x, a_y = await _fork_tree(pg_db_session, "pg-recompute", branch_user)
        mem_x = ConversationMemory(
            user_id=branch_user, content="fact x",
            source_message_id=a_x.id, is_active=True, circle_tier=0,
        )
        mem_y = ConversationMemory(
            user_id=branch_user, content="fact y",
            source_message_id=a_y.id, is_active=True, circle_tier=0,
        )
        pg_db_session.add_all([mem_x, mem_y])
        await pg_db_session.flush()
        svc = ConversationService(pg_db_session)

        # Branch X active → mem_x stays active, mem_y deactivated (1 flip).
        conv.active_leaf_message_id = a_x.id
        await pg_db_session.flush()
        changed = await svc.recompute_memory_activation(conv)
        await pg_db_session.flush()
        assert changed == 1
        assert await _is_active(pg_db_session, mem_x.id) is True
        assert await _is_active(pg_db_session, mem_y.id) is False

        # Switch to branch Y → mem_y REACTIVATED, mem_x deactivated (2 flips).
        # This is the half Phase 1's one-way deactivate could not do.
        conv.active_leaf_message_id = a_y.id
        await pg_db_session.flush()
        changed2 = await svc.recompute_memory_activation(conv)
        await pg_db_session.flush()
        assert changed2 == 2
        assert await _is_active(pg_db_session, mem_x.id) is False
        assert await _is_active(pg_db_session, mem_y.id) is True

    async def test_recompute_null_leaf_is_noop(
        self, pg_db_session: AsyncSession, branch_user
    ):
        # An uncomputable path (null leaf) must NEVER deactivate everything.
        conv = Conversation(session_id="pg-recompute-null", user_id=branch_user)
        pg_db_session.add(conv)
        await pg_db_session.flush()
        m = Message(conversation_id=conv.id, role="user", content="x",
                    timestamp=datetime(2026, 4, 2, 10, 0, 0), parent_message_id=None)
        pg_db_session.add(m)
        await pg_db_session.flush()
        mem = ConversationMemory(
            user_id=branch_user, content="f",
            source_message_id=m.id, is_active=True, circle_tier=0,
        )
        pg_db_session.add(mem)
        await pg_db_session.flush()
        svc = ConversationService(pg_db_session)
        assert conv.active_leaf_message_id is None
        assert await svc.recompute_memory_activation(conv) == 0
        assert await _is_active(pg_db_session, mem.id) is True


# ===========================================================================
# Phase 2 — deepest-leaf resolution (the switch-to-sibling target)
# ===========================================================================
@pytest.mark.postgres
@pytest.mark.asyncio
@pytest.mark.backend
class TestDeepestLeafPostgres:
    async def test_deepest_leaf_returns_subtree_tip(
        self, pg_db_session: AsyncSession, branch_user
    ):
        conv, ids = await _linear_conv(pg_db_session, "pg-deep", branch_user, 4)
        svc = ConversationService(pg_db_session)
        # Subtree tip of the root = the last message; a leaf resolves to itself.
        assert await svc._deepest_leaf_message_id(ids[0]) == ids[3]
        assert await svc._deepest_leaf_message_id(ids[3]) == ids[3]

    async def test_deepest_leaf_picks_latest_when_subtree_forks(
        self, pg_db_session: AsyncSession, branch_user
    ):
        conv, u0, a_x, a_y = await _fork_tree(pg_db_session, "pg-deep-fork", branch_user)
        svc = ConversationService(pg_db_session)
        # u0's subtree {u0, a_x, a_y}; tip = a_y (latest timestamp).
        assert await svc._deepest_leaf_message_id(u0.id) == a_y.id


# ===========================================================================
# Phase 2 — branch metadata for the ‹n/m› switcher
# ===========================================================================
@pytest.mark.postgres
@pytest.mark.asyncio
@pytest.mark.backend
class TestBranchMetadataPostgres:
    async def test_branch_metadata_reports_siblings(
        self, pg_db_session: AsyncSession, branch_user
    ):
        conv, u0, a_x, a_y = await _fork_tree(pg_db_session, "pg-meta", branch_user)
        conv.active_leaf_message_id = a_y.id
        await pg_db_session.flush()
        svc = ConversationService(pg_db_session)
        active = await svc.active_path_message_ids(conv)  # [u0, a_y]
        meta = await svc.branch_metadata(conv, active)
        # The single root u0 has no siblings → omitted; a_y has sibling a_x.
        assert u0.id not in meta
        assert a_y.id in meta
        assert meta[a_y.id]["count"] == 2
        # (timestamp, id) order → [a_x, a_y]; a_y is index 1.
        assert meta[a_y.id]["sibling_ids"] == [a_x.id, a_y.id]
        assert meta[a_y.id]["index"] == 1

    async def test_branch_metadata_linear_is_empty(
        self, pg_db_session: AsyncSession, branch_user
    ):
        conv, ids = await _linear_conv(pg_db_session, "pg-meta-lin", branch_user, 4)
        svc = ConversationService(pg_db_session)
        active = await svc.active_path_message_ids(conv)
        assert await svc.branch_metadata(conv, active) == {}


# ===========================================================================
# Phase 2 — delete-branch guards (PG; early returns, no commit → isolation-safe)
# ===========================================================================
@pytest.mark.postgres
@pytest.mark.asyncio
@pytest.mark.backend
class TestDeleteBranchGuardsPostgres:
    async def test_delete_active_path_message_refused(
        self, pg_db_session: AsyncSession, branch_user
    ):
        conv, ids = await _linear_conv(pg_db_session, "pg-del-active", branch_user, 4)
        svc = ConversationService(pg_db_session)
        # ids[2] is on the (linear) active path → "active": refused, no commit.
        status = await svc.delete_branch("pg-del-active", ids[2], user_id=branch_user)
        assert status == "active"
        still = (
            await pg_db_session.execute(select(Message.id).where(Message.id == ids[2]))
        ).scalar_one_or_none()
        assert still == ids[2]

    async def test_delete_foreign_or_unowned_not_found(
        self, pg_db_session: AsyncSession, branch_user
    ):
        conv, ids = await _linear_conv(pg_db_session, "pg-del-foreign", branch_user, 2)
        svc = ConversationService(pg_db_session)
        assert await svc.delete_branch("pg-del-foreign", 99999999, user_id=branch_user) == "not_found"
        assert await svc.delete_branch("pg-del-foreign", ids[0], user_id=424242) == "not_found"


# ===========================================================================
# Phase 2 — delete-branch happy path (sqlite; commits + exercises the FK
# hazards: memory delete + KG-relation provenance detach with FK enforcement).
# ===========================================================================
@pytest.mark.backend
@pytest.mark.database
class TestDeleteBranchSqlite:
    async def test_delete_branch_clears_memory_and_detaches_kg_no_fk_block(
        self, db_session: AsyncSession
    ):
        # PRAGMA FK ON so an un-cleaned memory/relation ref to the deleted message
        # would FK-block — a clean delete proves the pre-delete cleanup works.
        await db_session.execute(text("PRAGMA foreign_keys=ON"))
        role = Role(name="delbr-role", description="r")
        db_session.add(role)
        await db_session.flush()
        u = User(username="delbr-u", email="delbr-u@example.invalid",
                 password_hash="x", is_active=True, role_id=role.id)
        db_session.add(u)
        await db_session.flush()
        svc = ConversationService(db_session)
        await svc.save_message("delbr", "user", "frage", user_id=u.id)
        m_a = await svc.save_message("delbr", "assistant", "antwort", user_id=u.id)

        e1 = KGEntity(user_id=u.id, name="E1", entity_type="thing", circle_tier=0)
        e2 = KGEntity(user_id=u.id, name="E2", entity_type="thing", circle_tier=0)
        db_session.add_all([e1, e2])
        await db_session.flush()
        mem = ConversationMemory(
            user_id=u.id, content="branch fact",
            source_message_id=m_a.id, is_active=True, circle_tier=0,
        )
        rel = KGRelation(
            user_id=u.id, subject_id=e1.id, predicate="knows", object_id=e2.id,
            source_message_id=m_a.id, circle_tier=0,
        )
        db_session.add_all([mem, rel])
        await db_session.flush()

        status = await svc.delete_branch("delbr", m_a.id, user_id=u.id)
        assert status == "ok"
        # Message gone, branch-local memory gone, KG provenance detached (row kept).
        assert (await db_session.execute(
            select(Message.id).where(Message.id == m_a.id)
        )).scalar_one_or_none() is None
        assert (await db_session.execute(
            select(ConversationMemory.id).where(ConversationMemory.id == mem.id)
        )).scalar_one_or_none() is None
        assert (await db_session.execute(
            select(KGRelation.source_message_id).where(KGRelation.id == rel.id)
        )).scalar_one() is None

    async def test_delete_branch_ownership_and_missing(self, db_session: AsyncSession):
        svc = ConversationService(db_session)
        m = await svc.save_message("delbr2", "user", "x", user_id=5)
        assert await svc.delete_branch("delbr2", m.id, user_id=999) == "not_found"
        assert await svc.delete_branch("nope", 1, user_id=5) == "not_found"
