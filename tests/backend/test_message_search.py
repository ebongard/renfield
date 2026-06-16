"""Tests for chat message search (chat-UI roadmap item 3).

Two layers:

  * **Postgres FTS** (``@pytest.mark.postgres``) — the real path:
    ``ConversationService.search_messages`` over the GENERATED multilingual
    ``messages.search_vector`` column. Verifies ranking, jump-to-message
    metadata (``session_id`` + ``message_index`` + snippet), in-conversation
    scoping, single-user mode, AND — the load-bearing security property — that
    a user CANNOT find another user's messages (ownership scoping, NOT
    circle_sql).

  * **sqlite fallback** (``@pytest.mark.database``) — the test-harness branch
    + the pure token helper. Confirms the ownership filter and the API shape
    hold without a real tsvector.

The schema swap (plain TSVECTOR → GENERATED) mirrors the real pc20260617
migration, replicated in a fixture exactly as test_fts_multilingual_pg.py does
for pc20260528/pc20260529 (Base.metadata.create_all lays down a plain column;
the migration ADDs the GENERATED one).
"""
from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from models.database import Conversation, Message, Role, User
from services.conversation_service import (
    ConversationService,
    _significant_message_tokens,
)
from services.fts_languages import build_generated_tsvector_expression


# ===========================================================================
# Pure helper (no DB)
# ===========================================================================
@pytest.mark.backend
@pytest.mark.unit
class TestSignificantMessageTokens:
    def test_drops_short_and_punctuation(self):
        assert _significant_message_tokens("a, b!! Licht?") == ["Licht"]

    def test_keeps_word_chars_and_digits(self):
        assert _significant_message_tokens("Termin 2026 Bonn") == ["Termin", "2026", "Bonn"]

    def test_empty_query(self):
        assert _significant_message_tokens("") == []
        assert _significant_message_tokens("  ") == []

    def test_german_umlauts(self):
        assert _significant_message_tokens("Müll Tür") == ["Müll", "Tür"]


# ===========================================================================
# sqlite fallback path — ownership scope + shape without a real tsvector
# ===========================================================================
@pytest.mark.backend
@pytest.mark.database
class TestSearchMessagesSqlite:
    async def _seed(self, db: AsyncSession):
        """Two conversations, each owned by a different user, plus one orphan."""
        svc = ConversationService(db)
        # user 1
        await svc.save_message("u1-conv", "user", "Schalte das Licht im Wohnzimmer ein", user_id=1)
        await svc.save_message("u1-conv", "assistant", "Erledigt, Licht ist an", user_id=1)
        # user 2
        await svc.save_message("u2-conv", "user", "Wie wird das Wetter morgen", user_id=2)
        await svc.save_message("u2-conv", "assistant", "Das Licht der Sonne scheint", user_id=2)
        # orphan (no owner) — single-user mode visible only
        await svc.save_message("orphan-conv", "user", "Licht ohne Besitzer", user_id=None)
        return svc

    async def test_thin_query_returns_empty(self, db_session: AsyncSession):
        svc = await self._seed(db_session)
        out = await svc.search_messages("a", user_id=1)
        assert out == {"results": [], "count": 0, "has_more": False}

    async def test_owner_finds_own_messages(self, db_session: AsyncSession):
        svc = await self._seed(db_session)
        out = await svc.search_messages("Licht", user_id=1)
        sessions = {r["session_id"] for r in out["results"]}
        assert sessions == {"u1-conv"}
        # jump metadata present
        for r in out["results"]:
            assert r["session_id"] == "u1-conv"
            assert isinstance(r["message_index"], int)
            assert "snippet" in r

    async def test_cross_user_isolation_negative(self, db_session: AsyncSession):
        """SECURITY: user 1 must NOT find user 2's 'Licht der Sonne' message."""
        svc = await self._seed(db_session)
        out = await svc.search_messages("Sonne", user_id=1)
        assert out["results"] == []
        # And user 2 finds it.
        out2 = await svc.search_messages("Sonne", user_id=2)
        assert {r["session_id"] for r in out2["results"]} == {"u2-conv"}

    async def test_in_conversation_scope(self, db_session: AsyncSession):
        svc = await self._seed(db_session)
        # 'Licht' is in both u1-conv and (user 2's) u2-conv; scope to u1-conv.
        out = await svc.search_messages("Licht", user_id=None, session_id="u1-conv")
        assert {r["session_id"] for r in out["results"]} == {"u1-conv"}

    async def test_single_user_mode_sees_all(self, db_session: AsyncSession):
        svc = await self._seed(db_session)
        out = await svc.search_messages("Licht", user_id=None)
        sessions = {r["session_id"] for r in out["results"]}
        # u1-conv, u2-conv (Licht der Sonne), orphan-conv all contain "Licht"
        assert sessions == {"u1-conv", "u2-conv", "orphan-conv"}

    async def test_message_index_is_zero_based_position(self, db_session: AsyncSession):
        svc = ConversationService(db_session)
        await svc.save_message("idx-conv", "user", "erste nachricht alpha", user_id=1)
        await svc.save_message("idx-conv", "assistant", "zweite nachricht", user_id=1)
        await svc.save_message("idx-conv", "user", "dritte nachricht beta", user_id=1)
        out = await svc.search_messages("beta", user_id=1)
        assert len(out["results"]) == 1
        # 'beta' is the 3rd message → index 2
        assert out["results"][0]["message_index"] == 2


# ===========================================================================
# Postgres FTS path — the real ranking + scoping
# ===========================================================================
@pytest.fixture
async def two_users(pg_db_session: AsyncSession) -> tuple[int, int]:
    role = Role(name="msgsearch-role", description="role for message-search tests")
    pg_db_session.add(role)
    await pg_db_session.flush()
    u1 = User(
        username="msgsearch-u1", email="ms-u1@example.invalid",
        password_hash="x", is_active=True, role_id=role.id,
    )
    u2 = User(
        username="msgsearch-u2", email="ms-u2@example.invalid",
        password_hash="x", is_active=True, role_id=role.id,
    )
    pg_db_session.add_all([u1, u2])
    await pg_db_session.flush()
    return u1.id, u2.id


@pytest.fixture
async def messages_fts_installed(pg_db_session: AsyncSession) -> None:
    """Replace messages.search_vector with the GENERATED multilingual column
    (replicates the pc20260617 migration; create_all only made a plain one)."""
    expr = build_generated_tsvector_expression("content")
    await pg_db_session.execute(text("ALTER TABLE messages DROP COLUMN IF EXISTS search_vector"))
    await pg_db_session.execute(text(
        f"ALTER TABLE messages ADD COLUMN search_vector tsvector "
        f"GENERATED ALWAYS AS ({expr}) STORED"
    ))
    await pg_db_session.flush()


async def _add_conv(db: AsyncSession, session_id: str, user_id: int, msgs: list[tuple[str, str]]):
    conv = Conversation(session_id=session_id, user_id=user_id)
    db.add(conv)
    await db.flush()
    for role, content in msgs:
        db.add(Message(conversation_id=conv.id, role=role, content=content))
    await db.flush()
    return conv


@pytest.mark.postgres
@pytest.mark.asyncio
@pytest.mark.backend
class TestSearchMessagesPostgres:
    async def test_generated_column_self_populates(
        self, pg_db_session: AsyncSession, messages_fts_installed, two_users
    ):
        u1, _ = two_users
        await _add_conv(pg_db_session, "pg-pop", u1, [("user", "Steuererklärung abgeben")])
        row = await pg_db_session.execute(text(
            "SELECT search_vector IS NOT NULL FROM messages "
            "WHERE content = 'Steuererklärung abgeben'"
        ))
        assert row.scalar() is True

    async def test_owner_finds_ranked_match_with_snippet(
        self, pg_db_session: AsyncSession, messages_fts_installed, two_users
    ):
        u1, _ = two_users
        await _add_conv(pg_db_session, "pg-u1", u1, [
            ("user", "Schalte das Licht im Wohnzimmer ein"),
            ("assistant", "Alles klar"),
        ])
        svc = ConversationService(pg_db_session)
        out = await svc.search_messages("Licht", user_id=u1)
        assert out["count"] >= 1
        hit = out["results"][0]
        assert hit["session_id"] == "pg-u1"
        assert hit["message_index"] == 0
        assert hit["rank"] > 0
        # ts_headline wraps the match in the STX/ETX sentinels (not HTML).
        assert "\x02" in hit["snippet"] and "\x03" in hit["snippet"]

    async def test_cross_user_isolation_negative(
        self, pg_db_session: AsyncSession, messages_fts_installed, two_users
    ):
        """SECURITY: the asker must never match another user's messages."""
        u1, u2 = two_users
        await _add_conv(pg_db_session, "pg-secret", u2, [
            ("user", "Mein geheimes Passwort lautet Hunter2"),
        ])
        svc = ConversationService(pg_db_session)
        # u1 searches for u2's content → nothing.
        out = await svc.search_messages("geheimes", user_id=u1)
        assert out["results"] == []
        assert out["count"] == 0
        # u2 (the owner) finds it.
        out_owner = await svc.search_messages("geheimes", user_id=u2)
        assert {r["session_id"] for r in out_owner["results"]} == {"pg-secret"}

    async def test_in_conversation_scope(
        self, pg_db_session: AsyncSession, messages_fts_installed, two_users
    ):
        u1, _ = two_users
        await _add_conv(pg_db_session, "pg-a", u1, [("user", "Rechnung vom Finanzamt")])
        await _add_conv(pg_db_session, "pg-b", u1, [("user", "Rechnung vom Stromanbieter")])
        svc = ConversationService(pg_db_session)
        # Global: both match "Rechnung".
        all_out = await svc.search_messages("Rechnung", user_id=u1)
        assert {r["session_id"] for r in all_out["results"]} == {"pg-a", "pg-b"}
        # Scoped to pg-a only.
        scoped = await svc.search_messages("Rechnung", user_id=u1, session_id="pg-a")
        assert {r["session_id"] for r in scoped["results"]} == {"pg-a"}

    async def test_single_user_mode_sees_all_owners(
        self, pg_db_session: AsyncSession, messages_fts_installed, two_users
    ):
        u1, u2 = two_users
        await _add_conv(pg_db_session, "su-1", u1, [("user", "Kalender Eintrag")])
        await _add_conv(pg_db_session, "su-2", u2, [("user", "Kalender Termin")])
        svc = ConversationService(pg_db_session)
        out = await svc.search_messages("Kalender", user_id=None)
        assert {r["session_id"] for r in out["results"]} == {"su-1", "su-2"}

    async def test_pagination_has_more(
        self, pg_db_session: AsyncSession, messages_fts_installed, two_users
    ):
        u1, _ = two_users
        msgs = [("user", f"Notiz Nummer {i} apfel") for i in range(5)]
        await _add_conv(pg_db_session, "pg-page", u1, msgs)
        svc = ConversationService(pg_db_session)
        page1 = await svc.search_messages("apfel", user_id=u1, limit=2, offset=0)
        assert page1["count"] == 2
        assert page1["has_more"] is True
        page3 = await svc.search_messages("apfel", user_id=u1, limit=2, offset=4)
        assert page3["count"] == 1
        assert page3["has_more"] is False

    async def test_multilingual_match(
        self, pg_db_session: AsyncSession, messages_fts_installed, two_users
    ):
        """A query in one language stems against content in another via the
        FTS_LANGUAGES union (same property as the chunk/memory paths)."""
        u1, _ = two_users
        await _add_conv(pg_db_session, "pg-fr", u1, [("user", "J'ai bu un café ce matin")])
        svc = ConversationService(pg_db_session)
        out = await svc.search_messages("café", user_id=u1)
        assert {r["session_id"] for r in out["results"]} == {"pg-fr"}
