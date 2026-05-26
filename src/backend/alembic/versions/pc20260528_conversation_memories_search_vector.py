"""Lexical FTS column on conversation_memories — brain-quality fix.

Revision ID: pc20260528_cm_search_vector
Revises: pc20260526b_dc_search_gin
Create Date: 2026-05-26 21:00:00.000000

Adds ``conversation_memories.search_vector`` so the lexical retriever in
``services/lexical_retrieval.py`` can use Postgres FTS (``ts_rank``)
instead of token-OR'd ILIKE. ts_rank applies IDF-style term weighting
automatically, so frequent function-words like "gerne" or "mag" rank
lower than rare proper nouns like "Jutta" — exactly what a natural-
language query needs.

Design: STORED generated column. The expression ``to_tsvector('german',
coalesce(content, ''))`` is IMMUTABLE for any literal config string, so
Postgres accepts it as a generated column. Two consequences:

  1. The column is automatically populated for every existing row when
     it's added — no separate backfill UPDATE needed.
  2. Every INSERT/UPDATE recomputes it DB-side. App code (the two
     ``ConversationMemory(...)`` write sites in
     ``services/conversation_memory_service.py``) does not need to set
     the column. Future write paths inherit the invariant.

The hardcoded ``'german'`` matches ``settings.rag_hybrid_fts_config``'s
project default. Deployments that need a different FTS config must
issue a follow-up migration that drops + re-adds the generated column
with a different expression.

GIN index built CONCURRENTLY — works now that pc20260526b's
``autocommit_block`` foundation (env.py: ``transaction_per_migration=True``)
is in place.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import TSVECTOR


revision = "pc20260528_cm_search_vector"
down_revision = "pc20260526b_dc_search_gin"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name if bind is not None else "postgresql"
    if dialect != "postgresql":
        # Sqlite test harness has no tsvector. Skip silently; the
        # lexical retriever's sqlite branch uses LIKE instead.
        return

    # Generated column — Postgres computes search_vector for every row
    # (existing AND future inserts) without app-side maintenance.
    op.execute(
        "ALTER TABLE conversation_memories "
        "ADD COLUMN IF NOT EXISTS search_vector tsvector "
        "GENERATED ALWAYS AS (to_tsvector('german', coalesce(content, ''))) STORED"
    )

    with op.get_context().autocommit_block():
        # Defensive DROP before CREATE: a prior failed
        # CREATE INDEX CONCURRENTLY can leave the index in an INVALID
        # state. `CREATE INDEX ... IF NOT EXISTS` checks name+namespace
        # only, NOT validity — so it matches the INVALID index and
        # skips, leaving us with a perpetual seq-scan and no error to
        # point at root cause. Unconditional DROP IF EXISTS is cheap on
        # a healthy/absent index and cleans up an invalid one.
        # Requires transaction_per_migration=True in env.py (set in the
        # same release); otherwise autocommit_block asserts.
        op.execute(
            "DROP INDEX IF EXISTS idx_conversation_memories_search_vector_gin"
        )
        op.execute(
            "CREATE INDEX CONCURRENTLY "
            "idx_conversation_memories_search_vector_gin "
            "ON conversation_memories USING gin (search_vector)"
        )

    # Refresh planner stats: the new GENERATED column has zero rows in
    # pg_statistic until the next autovacuum cycle, so the GIN index
    # may be ignored in favor of seq-scan for the first ~minutes of
    # queries after deploy. ANALYZE eliminates the cold-stats window.
    op.execute("ANALYZE conversation_memories")


def downgrade() -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name if bind is not None else "postgresql"
    if dialect != "postgresql":
        return
    with op.get_context().autocommit_block():
        op.execute(
            "DROP INDEX CONCURRENTLY IF EXISTS "
            "idx_conversation_memories_search_vector_gin"
        )
    op.execute(
        "ALTER TABLE conversation_memories DROP COLUMN IF EXISTS search_vector"
    )
