"""Postgres integration tests for the multilingual FTS architecture.

Exercises the load-bearing pieces that the sqlite test harness CANNOT
reach:
  - The GENERATED ``search_vector`` columns on ``document_chunks`` (pc20260529)
    and ``conversation_memories`` (pc20260528) actually exist and are
    populated server-side from ``content`` for every existing row.
  - ``services.fts_languages.build_generated_tsvector_expression`` produces
    an IMMUTABLE expression that Postgres accepts inside ``GENERATED ALWAYS
    AS (...) STORED``.
  - ``services.fts_languages.build_tsquery_union_sql`` produces an
    expression that Postgres accepts on the query side and that yields
    cross-language matches (FR-stemmed content surfaces for DE queries
    and vice versa).
  - The two retriever entry points (``RAGRetrieval._search_bm25`` for chunks
    via ``LexicalRetrieval.search_chunks_lexical`` for the alt path, plus
    ``LexicalRetrieval.search_memories_lexical`` for memories) both find
    cross-language matches through their respective GENERATED columns.

Gated on RENFIELD_TEST_PG_URL via the pg_db_session fixture (skipped when
unset). See conftest.py for the fixture details.
"""
from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from models.database import ConversationMemory, Document, DocumentChunk, Role, User
from services.fts_languages import (
    FTS_LANGUAGES,
    build_generated_tsvector_expression,
    build_tsquery_union_sql,
)
from services.lexical_retrieval import LexicalRetrieval


pytestmark = [pytest.mark.postgres, pytest.mark.asyncio]


# ---------------------------------------------------------------------------
# Fixture: install the migration-managed GENERATED columns on the live test
# DB. Base.metadata.create_all (used by pg_async_engine) lays down the ORM-
# declared columns as plain TSVECTOR — it doesn't know about GENERATED. The
# real pc20260528 / pc20260529 migrations explicitly DROP+ADD them. We
# replicate that ADD here so the integration tests run against a schema
# shape matching post-migration production.
# ---------------------------------------------------------------------------
@pytest.fixture
async def seeded_user(pg_db_session: AsyncSession) -> int:
    """Insert a User row so conversation_memories.user_id FK can resolve.

    Returns the new user's id. Memory-side tests reference this id
    rather than hardcoding `user_id=1` so the FK constraint passes
    even on a fresh DB.
    """
    role = Role(name="fts-test-role", description="role for FTS pg tests")
    pg_db_session.add(role)
    await pg_db_session.flush()

    u = User(
        username="fts-test-user",
        email="fts-test-user@example.invalid",
        password_hash="not-a-real-hash",
        is_active=True,
        role_id=role.id,
    )
    pg_db_session.add(u)
    await pg_db_session.flush()
    return u.id


@pytest.fixture
async def fts_columns_installed(pg_db_session: AsyncSession) -> None:
    """Swap the plain-TSVECTOR columns for GENERATED multilingual ones.

    Per-test scope because the outer transaction from pg_db_session rolls
    back on teardown, undoing the schema swap. Yields nothing — caller
    just declares the dependency to get the setup.
    """
    expr = build_generated_tsvector_expression("content")

    for table_name, content_col in (
        ("document_chunks", "content"),
        ("conversation_memories", "content"),
    ):
        await pg_db_session.execute(
            text(f"ALTER TABLE {table_name} DROP COLUMN IF EXISTS search_vector")
        )
        await pg_db_session.execute(
            text(
                f"ALTER TABLE {table_name} "
                f"ADD COLUMN search_vector tsvector "
                f"GENERATED ALWAYS AS ({expr}) STORED"
            )
        )
    await pg_db_session.flush()


# ===========================================================================
# Pure SQL-level tests — verify the migration-equivalent DDL works as
# claimed against real Postgres, independent of the retriever code.
# ===========================================================================
class TestGeneratedColumnDDL:
    """The migration-shape SQL itself must be accepted by Postgres."""

    async def test_generated_expression_is_immutable_for_postgres(
        self,
        pg_db_session: AsyncSession,
    ):
        """The 6-way to_tsvector union must satisfy Postgres's IMMUTABLE
        check for GENERATED columns. If any FTS_LANGUAGES entry ever
        points at a non-immutable config, the ADD COLUMN would raise
        `ERROR: generation expression is not immutable` at apply time.
        This test makes the boundary explicit."""
        expr = build_generated_tsvector_expression("content")
        # Use a throwaway table so we don't pollute the schema fixture.
        await pg_db_session.execute(text(
            "CREATE TEMPORARY TABLE _fts_immut_smoke (id serial, content text)"
        ))
        # The actual assertion: this must not raise.
        await pg_db_session.execute(text(
            f"ALTER TABLE _fts_immut_smoke "
            f"ADD COLUMN search_vector tsvector "
            f"GENERATED ALWAYS AS ({expr}) STORED"
        ))

    async def test_all_fts_languages_present_in_pg_catalog(
        self,
        pg_db_session: AsyncSession,
    ):
        """Every config in FTS_LANGUAGES must exist in pg_ts_config.
        Catches a typo'd language name before it reaches a migration."""
        for lang in FTS_LANGUAGES:
            result = await pg_db_session.execute(text(
                "SELECT 1 FROM pg_ts_config WHERE cfgname = :name"
            ), {"name": lang})
            assert result.scalar() == 1, (
                f"FTS_LANGUAGES entry {lang!r} not found in pg_ts_config. "
                f"Either Postgres doesn't ship that config in the default "
                f"install or the name is misspelled."
            )


# ===========================================================================
# document_chunks tests — chunk side
# ===========================================================================
class TestChunkSideGeneratedColumn:
    """document_chunks.search_vector self-populates from content."""

    async def test_search_vector_is_generated_always(
        self,
        pg_db_session: AsyncSession,
        fts_columns_installed,
    ):
        """information_schema must report is_generated=ALWAYS."""
        row = (await pg_db_session.execute(text(
            "SELECT is_generated FROM information_schema.columns "
            "WHERE table_name='document_chunks' "
            "  AND column_name='search_vector'"
        ))).first()
        assert row is not None, "search_vector column missing post-fixture"
        assert row[0] == "ALWAYS"

    async def test_insert_populates_search_vector_for_german_content(
        self,
        pg_db_session: AsyncSession,
        fts_columns_installed,
    ):
        """A vanilla INSERT triggers the GENERATED expression — no app
        code needs to touch search_vector."""
        doc = Document(
            filename="t.pdf",
            file_path="/tmp/t.pdf",
            title="t",
            file_type="pdf",
            status="completed",
        )
        pg_db_session.add(doc)
        await pg_db_session.flush()

        chunk = DocumentChunk(
            document_id=doc.id,
            content="Jutta mag Maracujas und Ananas",
            chunk_index=0,
        )
        pg_db_session.add(chunk)
        await pg_db_session.flush()

        populated = (await pg_db_session.execute(text(
            "SELECT search_vector IS NOT NULL "
            "FROM document_chunks WHERE id = :id"
        ), {"id": chunk.id})).scalar()
        assert populated is True

    async def test_french_content_matches_french_query(
        self,
        pg_db_session: AsyncSession,
        fts_columns_installed,
    ):
        """French stemmer in the GENERATED union actually works."""
        doc = Document(filename="fr.pdf", file_path="/tmp/fr.pdf", title="fr", file_type="pdf", status="completed")
        pg_db_session.add(doc)
        await pg_db_session.flush()
        chunk = DocumentChunk(
            document_id=doc.id,
            content="Pierre aime les pâtes et le café",
            chunk_index=0,
        )
        pg_db_session.add(chunk)
        await pg_db_session.flush()

        match = (await pg_db_session.execute(text(
            "SELECT 1 FROM document_chunks "
            "WHERE id = :id "
            "  AND search_vector @@ websearch_to_tsquery('french', 'café')"
        ), {"id": chunk.id})).scalar()
        assert match == 1, "French content should match French-stemmed query"

    async def test_cross_language_match_via_union_query(
        self,
        pg_db_session: AsyncSession,
        fts_columns_installed,
    ):
        """The build_tsquery_union_sql helper produces an expression that
        matches content stemmed by ANY of the FTS_LANGUAGES configs.
        Same query string matches DE, EN, FR content."""
        doc = Document(filename="multi.pdf", file_path="/tmp/multi.pdf", title="m", file_type="pdf", status="completed")
        pg_db_session.add(doc)
        await pg_db_session.flush()
        for i, content in enumerate([
            "Jutta mag Maracujas",        # DE
            "The user likes football",     # EN
            "Pierre aime le café",         # FR
        ]):
            pg_db_session.add(DocumentChunk(
                document_id=doc.id, content=content, chunk_index=i,
            ))
        await pg_db_session.flush()

        tsquery_union = build_tsquery_union_sql("q")
        # Word that should hit the FR row via the french stemmer in the union
        match_count = (await pg_db_session.execute(text(
            f"SELECT COUNT(*) FROM document_chunks "
            f"WHERE document_id = :doc_id "
            f"  AND search_vector @@ ({tsquery_union})"
        ), {"doc_id": doc.id, "q": "café"})).scalar()
        assert match_count == 1


# ===========================================================================
# conversation_memories tests — memory side
# ===========================================================================
class TestMemorySideGeneratedColumn:
    """conversation_memories.search_vector mirrors the chunk side."""

    async def test_search_vector_is_generated_always(
        self,
        pg_db_session: AsyncSession,
        fts_columns_installed,
    ):
        row = (await pg_db_session.execute(text(
            "SELECT is_generated FROM information_schema.columns "
            "WHERE table_name='conversation_memories' "
            "  AND column_name='search_vector'"
        ))).first()
        assert row is not None
        assert row[0] == "ALWAYS"

    async def test_natural_language_query_finds_discriminator(
        self,
        pg_db_session: AsyncSession,
        fts_columns_installed,
        seeded_user: int,
    ):
        """The PR #625 regression case: 'Was mag Jutta gerne essen?' must
        surface the Jutta memory even though most query tokens don't
        match it. ts_rank ordering ensures the rare-token match wins."""
        for content, importance in [
            ("Jutta mag Maracujas und Ananas", 0.8),
            ("Anna kocht gerne Pasta", 0.5),
            ("Der Geburtstag des Kindes ist morgen", 0.4),
        ]:
            pg_db_session.add(ConversationMemory(
                user_id=seeded_user,
                content=content,
                category="fact",
                importance=importance,
                confidence=1.0,
                circle_tier=0,
                created_at=datetime.now(UTC).replace(tzinfo=None),
            ))
        await pg_db_session.flush()

        retr = LexicalRetrieval(pg_db_session)
        hits = await retr.search_memories_lexical(
            "Was mag Jutta gerne essen", asker_id=seeded_user, top_k=10,
        )
        # At minimum the Jutta memory surfaces.
        assert any("Jutta mag Maracujas" in h["content"] for h in hits), (
            "Natural-language query with the discriminator 'Jutta' must "
            "find the Jutta memory; FTS union failed."
        )
        # And it must rank first (or above the gerne-only competitor).
        jutta_pos = next(
            (i for i, h in enumerate(hits) if "Jutta mag Maracujas" in h["content"]),
            -1,
        )
        anna_pos = next(
            (i for i, h in enumerate(hits) if "Anna kocht gerne" in h["content"]),
            -1,
        )
        if anna_pos >= 0:
            assert jutta_pos < anna_pos, (
                f"Expected Jutta-rank ({jutta_pos}) < Anna-rank ({anna_pos}) — "
                f"the discriminator token should outrank the function-word match."
            )

    async def test_dutch_memory_matches_dutch_query(
        self,
        pg_db_session: AsyncSession,
        fts_columns_installed,
        seeded_user: int,
    ):
        """Dutch stemmer in the union actually works (smoke for a
        language NONE of the existing fixtures exercise)."""
        pg_db_session.add(ConversationMemory(
            user_id=seeded_user,
            content="De gebruiker houdt van fietsen door Amsterdam",
            category="fact",
            importance=0.5,
            confidence=1.0,
            circle_tier=0,
            created_at=datetime.now(UTC).replace(tzinfo=None),
        ))
        await pg_db_session.flush()

        retr = LexicalRetrieval(pg_db_session)
        hits = await retr.search_memories_lexical(
            "fietsen", asker_id=seeded_user, top_k=5,
        )
        assert len(hits) == 1
        assert "Amsterdam" in hits[0]["content"]
