"""Multilingual FTS on document_chunks via atomic-swap.

Revision ID: pc20260529_dc_multilingual_fts
Revises: pc20260528_cm_search_vector
Create Date: 2026-05-27 00:00:00.000000

Converts ``document_chunks.search_vector`` from an app-populated single-config
``tsvector`` to a GENERATED STORED column whose body unions ``to_tsvector``
across all ``services/fts_languages.FTS_LANGUAGES`` (DE / EN / FR / IT / ES / NL).
After this migration the column maintains itself for every existing AND future
row — the two ``UPDATE ... SET search_vector = to_tsvector(...)`` sites in
``services/rag_service.py`` are removed in the same release because UPDATEs to
a GENERATED column raise.

Strategy — atomic-swap (NOT the simple DROP-then-ADD pattern used for
``conversation_memories`` in pc20260528):

  1. ADD a NEW column ``search_vector_new`` as GENERATED multilingual.
     Postgres populates it from ``content`` for every existing row at column-
     add time. The OLD ``search_vector`` and its GIN index keep serving
     reads in the meantime — zero retrieval downtime.
  2. CREATE INDEX CONCURRENTLY on the new column. No lock against the
     existing reader path.
  3. Single short transaction: DROP old column (auto-drops old GIN),
     RENAME new column → ``search_vector``, RENAME new index →
     ``idx_document_chunks_search_vector_gin``. ACCESS EXCLUSIVE on the
     table for the duration of this transaction only (sub-second on the
     159-row corpus; would scale linearly if the corpus ever grew —
     for a 10M-row case the swap is still fast, the only cost is the
     instant of writer-blocking which is what the atomic-swap pattern
     exists to minimize).
  4. ANALYZE to refresh planner stats after the column type / index
     identity change.

Why atomic-swap on a 159-row table when DROP+ADD would also finish in
sub-second: forward-compatibility. ``document_chunks`` is the corpus that
realistically grows (a user with thousands of imported documents could hit
hundreds of thousands of chunks). Picking the atomic-swap pattern now means
this migration template is reusable for that future scale without rewrite.

GIN index built CONCURRENTLY; the swap transaction uses
``op.get_context().autocommit_block()`` boundaries appropriately, which now
works because ``env.py`` runs with ``transaction_per_migration=True`` (PR #625)
and the bootstrap commit() (PR #626) ensures alembic sees the
``_in_external_transaction`` snapshot clean.

Downstream code coupling (must be released TOGETHER with this migration):
  - ``services/rag_service.py``: remove both ``UPDATE document_chunks SET
    search_vector = to_tsvector(...)`` sites. UPDATEs to the new GENERATED
    column raise ``ERROR: column "search_vector" can only be updated to
    DEFAULT``.
  - ``services/rag_retrieval.py`` + ``services/lexical_retrieval.py``: switch
    chunk-side FTS reads to union ``websearch_to_tsquery`` across
    ``FTS_LANGUAGES`` (same pattern as the memory side).
  - ``POST /api/knowledge/reindex-fts``: repurpose from UPDATE-based reindex
    to ``REINDEX INDEX CONCURRENTLY`` GIN-rebuild operation.
"""
from alembic import op

from services.fts_languages import build_generated_tsvector_expression


revision = "pc20260529_dc_multilingual_fts"
down_revision = "pc20260528_cm_search_vector"
branch_labels = None
depends_on = None


_NEW_COL = "search_vector_new"
_OLD_COL = "search_vector"
_NEW_INDEX = "idx_document_chunks_search_vector_new_gin"
_OLD_INDEX = "idx_document_chunks_search_vector_gin"


def upgrade() -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name if bind is not None else "postgresql"
    if dialect != "postgresql":
        # Sqlite test harness has no tsvector. Skip silently; the
        # lexical retriever's sqlite branch short-circuits chunk lookups
        # to [] anyway.
        return

    # 1. Add the NEW GENERATED column. Postgres populates it for every
    #    existing row at column-add time (the GENERATED expression
    #    references `content`, which is already populated). The OLD
    #    column + its GIN index keep serving reads.
    #
    #    Defensive DROP IF EXISTS before the ADD: a previous mid-migration
    #    kill (between step 1 and step 3) would leave `search_vector_new`
    #    on the table; without this guard, rerunning the migration would
    #    fail at the ADD COLUMN with "column already exists". Same
    #    conservatism as pc20260528 (which always-drops + re-adds the
    #    target column). The column is fully derived from `content`, so
    #    dropping costs nothing.
    tsvector_expr = build_generated_tsvector_expression("content")
    op.execute(f"ALTER TABLE document_chunks DROP COLUMN IF EXISTS {_NEW_COL}")
    op.execute(
        f"ALTER TABLE document_chunks "
        f"ADD COLUMN {_NEW_COL} tsvector "
        f"GENERATED ALWAYS AS ({tsvector_expr}) STORED"
    )

    # 2. Build the new GIN index CONCURRENTLY on the new column. Runs
    #    outside any transaction (Postgres rule); requires the env.py
    #    transaction_per_migration=True foundation.
    with op.get_context().autocommit_block():
        # Defensive DROP IF EXISTS in case a prior partial migration
        # left an INVALID index under this name (same recovery pattern
        # as pc20260528). CONCURRENTLY on the DROP avoids any brief
        # ACCESS EXCLUSIVE on the table.
        op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {_NEW_INDEX}")
        op.execute(
            f"CREATE INDEX CONCURRENTLY {_NEW_INDEX} "
            f"ON document_chunks USING gin ({_NEW_COL})"
        )

    # 3. Atomic swap. Inside a single transaction (the wrapping per-
    #    migration txn from transaction_per_migration=True): DROP the
    #    old column (which cascades to drop the old GIN index), RENAME
    #    the new column into its place, RENAME the new index for
    #    cosmetic consistency. ACCESS EXCLUSIVE on the table for the
    #    duration — sub-second on the current corpus.
    op.execute(f"ALTER TABLE document_chunks DROP COLUMN {_OLD_COL}")
    op.execute(
        f"ALTER TABLE document_chunks RENAME COLUMN {_NEW_COL} TO {_OLD_COL}"
    )
    op.execute(f"ALTER INDEX {_NEW_INDEX} RENAME TO {_OLD_INDEX}")

    # 4. Refresh planner stats. The renamed column is brand-new to
    #    pg_statistic — no scan history exists for it yet, so the
    #    planner has no selectivity estimate and may favor seq-scan
    #    over the GIN index for the first ~minutes after deploy
    #    (until autovacuum's daemon catches up). ANALYZE eliminates
    #    the cold-stats window. Cheap on the current 159-row corpus;
    #    scales linearly with row count if the corpus grows.
    op.execute("ANALYZE document_chunks")


def downgrade() -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name if bind is not None else "postgresql"
    if dialect != "postgresql":
        return

    # Reverse the swap: GENERATED column → plain nullable tsvector.
    # Recreate the GIN index. Note: post-downgrade the column is empty
    # for all existing rows — the application code's reindex path
    # (POST /api/knowledge/reindex-fts in the legacy form, or the
    # _finalize_document_processing UPDATE) must be restored to
    # repopulate. This downgrade is therefore a recovery scaffold,
    # not a fully reversible no-data-loss operation.
    op.execute("ALTER TABLE document_chunks DROP COLUMN search_vector")
    op.execute(
        "ALTER TABLE document_chunks ADD COLUMN search_vector tsvector"
    )
    with op.get_context().autocommit_block():
        op.execute(
            f"DROP INDEX CONCURRENTLY IF EXISTS {_OLD_INDEX}"
        )
        op.execute(
            f"CREATE INDEX CONCURRENTLY {_OLD_INDEX} "
            f"ON document_chunks USING gin (search_vector)"
        )
