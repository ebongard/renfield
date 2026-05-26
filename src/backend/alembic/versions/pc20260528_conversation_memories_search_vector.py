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

Multilingual by design: the expression unions ``to_tsvector`` across
DE / EN / FR / IT / ES / NL so a memory written in one language and a
query expressed in another both stem correctly. Single source of truth
for the language list lives in ``services/fts_languages.FTS_LANGUAGES``
— this migration reads from there at *generation time* (when the file
was written), the literal expression is then baked into the column for
all future inserts.

Adding a 7th language later requires a follow-up migration that
DROPs + re-ADDs the GENERATED column with the new expression
(Postgres does not allow ALTER on a generated column's body). The
migration body is a copy of this one with the new FTS_LANGUAGES tuple
substituted in.

Design: STORED generated column. The ``to_tsvector('<lang>', text)``
expression is IMMUTABLE for any literal config string, so Postgres
accepts the union as a generated column. Two consequences:

  1. The column is automatically populated for every existing row when
     it's added — no separate backfill UPDATE needed.
  2. Every INSERT/UPDATE recomputes it DB-side. App code (the two
     ``ConversationMemory(...)`` write sites in
     ``services/conversation_memory_service.py``) does not need to set
     the column. Future write paths inherit the invariant.

GIN index built CONCURRENTLY — works now that pc20260526b's
``autocommit_block`` foundation (env.py: ``transaction_per_migration=True``)
is in place.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import TSVECTOR

from services.fts_languages import build_generated_tsvector_expression


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

    # Defensive DROP before ADD: ADD COLUMN IF NOT EXISTS is satisfied
    # by name alone, so a pre-existing column with the WRONG shape
    # silently survives. Two real paths into that state: (a) dev DBs
    # bootstrapped via Base.metadata.create_all get a plain nullable
    # TSVECTOR (see ConversationMemory.search_vector in
    # models/database.py), (b) any prior partial deploy that shipped
    # a different generated expression body. Both leave a non-
    # populating column and the lexical retriever returns 0 hits with
    # no error log. Unconditional DROP-then-ADD is cheap because
    # search_vector is fully derived from content — Postgres
    # repopulates it for every existing row as soon as the GENERATED
    # column is re-added.
    tsvector_expr = build_generated_tsvector_expression("content")
    op.execute(
        "ALTER TABLE conversation_memories "
        "DROP COLUMN IF EXISTS search_vector"
    )
    op.execute(
        "ALTER TABLE conversation_memories "
        f"ADD COLUMN search_vector tsvector "
        f"GENERATED ALWAYS AS ({tsvector_expr}) STORED"
    )

    with op.get_context().autocommit_block():
        # Defensive DROP before CREATE: a prior failed
        # CREATE INDEX CONCURRENTLY can leave the index in an INVALID
        # state. `CREATE INDEX ... IF NOT EXISTS` checks name+namespace
        # only, NOT validity — so it matches the INVALID index and
        # skips, leaving us with a perpetual seq-scan and no error to
        # point at root cause. Unconditional DROP IF EXISTS is cheap on
        # a healthy/absent index and cleans up an invalid one.
        # CONCURRENTLY on the DROP avoids the ACCESS EXCLUSIVE table
        # lock that a plain DROP INDEX would briefly take — important
        # if a long-running query is using the GIN index when the
        # migration runs.
        # Requires transaction_per_migration=True in env.py (set in the
        # same release); otherwise autocommit_block asserts.
        op.execute(
            "DROP INDEX CONCURRENTLY IF EXISTS "
            "idx_conversation_memories_search_vector_gin"
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
