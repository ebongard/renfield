"""Unit tests for the lexical (keyword) retrieval path.

Regression for the v2.10.3 brain-quality fix: ``/api/atoms?q=jutta``
returned only 1 of 9 relevant memories because the polymorphic atom
store had only vector retrievers and the user's third-person German
queries embed below the 0.5 cosine threshold. The lexical retriever
gives single-token name queries a deterministic recall floor.

Sqlite test path: chunk lookups short-circuit to [] (no tsvector),
memory lookups use a simplified owner-only ILIKE filter that still
exercises the token-pattern, stop-word, and result-shape logic.
"""
from __future__ import annotations

from datetime import datetime, UTC

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from models.database import ConversationMemory
from services.lexical_retrieval import LexicalRetrieval, _significant_tokens


# --------------------------------------------------------- token splitter
class TestSignificantTokens:
    def test_strips_short_tokens(self):
        assert _significant_tokens("a is the of in on") == []

    def test_keeps_name_tokens(self):
        assert _significant_tokens("Jutta Geburtstag") == ["Jutta", "Geburtstag"]

    def test_keeps_mixed_case_and_german_diacritics(self):
        assert _significant_tokens("Maracujas und Ananas") == ["Maracujas", "Ananas"]

    def test_handles_punctuation(self):
        assert _significant_tokens("Was mag Jutta gerne essen?") == ["Jutta", "gerne", "essen"]

    def test_empty_and_none(self):
        assert _significant_tokens("") == []
        assert _significant_tokens(None) == []


# --------------------------------------------------------- memory ILIKE search
async def _seed_memory(
    db: AsyncSession,
    *,
    user_id: int,
    content: str,
    importance: float = 0.5,
) -> int:
    mem = ConversationMemory(
        user_id=user_id,
        content=content,
        category="fact",
        importance=importance,
        confidence=1.0,
        circle_tier=0,
        created_at=datetime.now(UTC).replace(tzinfo=None),
    )
    db.add(mem)
    await db.commit()
    await db.refresh(mem)
    return mem.id


@pytest.mark.asyncio
class TestSearchMemoriesLexical:
    async def test_finds_single_token_name(self, db_session: AsyncSession):
        await _seed_memory(db_session, user_id=1, content="Jutta mag Maracujas und Ananas")
        await _seed_memory(db_session, user_id=1, content="Anna kocht gerne Pasta am Wochenende")
        retr = LexicalRetrieval(db_session)
        hits = await retr.search_memories_lexical("Jutta", asker_id=1, top_k=10)
        contents = [h["content"] for h in hits]
        assert "Jutta mag Maracujas und Ananas" in contents
        assert "Anna kocht gerne Pasta am Wochenende" not in contents

    async def test_requires_all_tokens(self, db_session: AsyncSession):
        """Memory-side lexical uses AND across tokens — typing both
        narrows correctly."""
        await _seed_memory(db_session, user_id=1, content="Jutta mag Maracujas und Ananas")
        await _seed_memory(db_session, user_id=1, content="Der Geburtstag des Kindes ist morgen")
        retr = LexicalRetrieval(db_session)
        hits = await retr.search_memories_lexical(
            "Jutta Geburtstag", asker_id=1, top_k=10,
        )
        assert hits == []  # no row mentions both

    async def test_returns_memory_shape(self, db_session: AsyncSession):
        await _seed_memory(
            db_session, user_id=1,
            content="Jutta hat am 14.02.1969 Geburtstag",
            importance=0.9,
        )
        retr = LexicalRetrieval(db_session)
        hits = await retr.search_memories_lexical("Geburtstag", asker_id=1, top_k=5)
        assert len(hits) == 1
        h = hits[0]
        for key in ("id", "atom_id", "user_id", "content", "category",
                    "importance", "confidence", "circle_tier", "similarity"):
            assert key in h, f"missing key {key} in memory shape"
        assert h["importance"] == pytest.approx(0.9)

    async def test_owner_scope_isolates_users(self, db_session: AsyncSession):
        """In single-user-mode test harness we restrict to owner-only.
        A memory owned by another user must not leak."""
        await _seed_memory(db_session, user_id=2, content="Jutta mag Maracujas")
        retr = LexicalRetrieval(db_session)
        hits = await retr.search_memories_lexical("Jutta", asker_id=1, top_k=5)
        assert hits == []

    async def test_empty_query_returns_empty(self, db_session: AsyncSession):
        retr = LexicalRetrieval(db_session)
        assert await retr.search_memories_lexical("", asker_id=1, top_k=5) == []
        assert await retr.search_memories_lexical("the of and", asker_id=1, top_k=5) == []

    async def test_no_asker_returns_empty(self, db_session: AsyncSession):
        """A retrieval call without a concrete asker can't be circles-
        filtered safely. Refuse rather than leak."""
        await _seed_memory(db_session, user_id=1, content="Jutta mag Maracujas")
        retr = LexicalRetrieval(db_session)
        assert await retr.search_memories_lexical("Jutta", asker_id=None, top_k=5) == []


# --------------------------------------------------------- chunk path on sqlite
@pytest.mark.asyncio
class TestSearchChunksLexicalSqlite:
    async def test_short_circuits_on_sqlite(self, db_session: AsyncSession):
        """tsvector + websearch_to_tsquery don't exist in sqlite. The
        retriever returns [] silently rather than raising — the
        polymorphic store's RRF tolerates empty source lists."""
        retr = LexicalRetrieval(db_session)
        hits = await retr.search_chunks_lexical("Jutta", asker_id=1, top_k=5)
        assert hits == []
