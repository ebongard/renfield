"""restore indexes lost to the fresh-install bootstrap

Revision ID: pc20260924_restore_idx
Revises: pc20260922_conv_meeting_atoms
Create Date: 2026-09-24

WHY THIS MIGRATION EXISTS
-------------------------
`services/database.py:_ensure_alembic_baseline()` builds a fresh install's
schema from the SQLAlchemy models (`create_all`) and then STAMPS the Alembic
head. The docstring there says it itself: "the 41-migration history is
skipped". Consequence: everything a migration creates via
`op.execute("CREATE INDEX ...")` NEVER comes into being on a fresh install --
and is never caught up, because the stamp claims the migration was applied.

Established on 2026-09-24, 37 data points without a single exception: every
index created by raw SQL in a migration up to 2026-04-02 is missing on BOTH
instances, every one from 2026-04-25 on is present. The household database was
set up on 2026-04-18. The cut lies exactly there.

The irony: `models/database.py:748` states explicitly "Vector-search index
is created at migration time, NOT by SQLAlchemy `create_all`" and records the
exact DDL below it. The code knows it -- the bootstrap path defeats it.

This migration REPAIRS the existing instances. It does NOT fix the cause: the
next fresh install loses the same thing again. That needs a baseline migration
which creates the base schema itself -- its own undertaking, because the chain
does not run from empty today (the root migration `9a0d8ccea5b0` is an empty
body, and `rooms` is created by no migration at all).

TWO TRAPS THAT WOULD HAVE SPRUNG WHEN COPYING THE OLD DDL
---------------------------------------------------------
1. **`atttypmod - 4` is wrong.** pgvector stores the dimension DIRECTLY in the
   typmod; the `+4` offset is the varlena convention (varchar), not the one
   `vector` uses. Measured live: `vector(2560)` has `atttypmod = 2560`.
   `pc20260402_add_episodic_hnsw_index.py:60` computes `atttypmod - 4` and
   would have built an index on `halfvec(2556)` -- queries cast to
   `halfvec(2560)`, so the index would NEVER have been usable: built,
   maintained on every write, useless. `y8z9a0b1c2d3` does it right and reads
   raw.
2. **`vector_cosine_ops` does not work at 2560 dimensions.** pgvector caps the
   regular `vector` type at 2000 when indexing. The old DDL for
   `idx_intent_corrections_embedding_hnsw` uses `vector_cosine_ops` -- that
   dates from the time of 768-dim embeddings and today is simply not
   executable. So here the MEASURED dimension decides the operator class,
   instead of the class being pinned.

DELIBERATELY NOT RESTORED
-------------------------
`ix_kg_entities_embedding` (from `g7h8i9j0k1l3`) is the PREDECESSOR of
`idx_kg_entities_embedding_hnsw`: `vector_cosine_ops` on a 2560-dim column,
not creatable today and replaced by the halfvec version. Taking it along "for
completeness" would mean building a dead index.

LOCK DURATION
-------------
Measured on the build box at 5 000 rows x 2560 dimensions (production size:
2 847-4 813 rows): **762 ms** per HNSW index. All eight together stay inside
seconds, hence an ordinary `CREATE INDEX` inside the transaction and no
`CONCURRENTLY` (which would not work inside an Alembic transaction anyway).
"""
import logging
from typing import Sequence, Union

from alembic import op
from sqlalchemy import text

logger = logging.getLogger("alembic.runtime.migration")

revision: str = 'pc20260924_restore_idx'
down_revision: Union[str, None] = 'pc20260922_conv_meeting_atoms'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# (index, table, column) -- the HNSW vector indexes.
_VECTOR_INDEXES = [
    ("idx_document_chunks_embedding_hnsw", "document_chunks", "embedding"),
    ("idx_kg_entities_embedding_hnsw", "kg_entities", "embedding"),
    ("ix_conversation_memories_embedding_hnsw", "conversation_memories", "embedding"),
    ("ix_episodic_embedding_hnsw", "episodic_memories", "embedding"),
    ("idx_intent_corrections_embedding_hnsw", "intent_corrections", "embedding"),
]

# The other three, with no dependency on the dimension.
_PLAIN_INDEXES = [
    ("idx_document_chunks_doc_chunk", "document_chunks",
     "CREATE INDEX IF NOT EXISTS idx_document_chunks_doc_chunk "
     "ON document_chunks (document_id, chunk_index)"),
    # NOT `idx_document_chunks_search_vector` -- that is the OLD name. Today
    # the GIN is called `..._gin`: `pc20260529` drops the old column (Postgres
    # removes its index automatically) and renames the new one.
    # `rag_service.py:1207` looks it up BY NAME as `..._gin`. Creating the old
    # name would mean: in the household a second, fully redundant GIN on the
    # same GENERATED column (double write cost, zero read benefit), on xidra an
    # index the service never finds. Found in the adversarial review of #1336
    # -- I had copied the list from the old migrations without reading the
    # rename along with it.
    # On xidra the GIN really is missing; under THIS name that closes the gap.
    ("idx_document_chunks_search_vector_gin", "document_chunks",
     "CREATE INDEX IF NOT EXISTS idx_document_chunks_search_vector_gin "
     "ON document_chunks USING gin(search_vector)"),
    ("ix_conv_memories_cleanup", "conversation_memories",
     "CREATE INDEX IF NOT EXISTS ix_conv_memories_cleanup "
     "ON conversation_memories (category, last_accessed_at) WHERE is_active = true"),
]

# The downgrade does NOT touch everything `upgrade()` creates.
# `idx_document_chunks_search_vector_gin` already exists in the household (from
# the rename in `pc20260529`); there `upgrade()` is a no-op because of
# `IF NOT EXISTS` -- so a `DROP` in the downgrade would remove a FOREIGN index
# that is in production use and pull the ground out from under the lexical
# search (`rag_service.py:1207` looks it up by name). Only on xidra does this
# migration really create it; there it stays standing after a downgrade. That
# is the right direction for the error to point: one index too many costs write
# time, a missing one costs every search.
_KEEP_ON_DOWNGRADE = {"idx_document_chunks_search_vector_gin"}

_ALL_NAMES = [
    n for n, _, _ in _VECTOR_INDEXES + _PLAIN_INDEXES
    if n not in _KEEP_ON_DOWNGRADE
]


def _column_dim(conn, table: str, column: str) -> int | None:
    """Dimension of a `vector(N)` column, or None when there is none.

    RAW, without `- 4`: pgvector stores N directly in the typmod. See the
    header comment.
    """
    row = conn.execute(text("""
        SELECT a.atttypmod
        FROM pg_attribute a
        JOIN pg_class c ON c.oid = a.attrelid
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = current_schema()
          AND c.relname = :t AND a.attname = :col AND a.attnum > 0
          AND NOT a.attisdropped
    """), {"t": table, "col": column}).first()
    if row is None or row[0] is None or row[0] <= 0:
        return None
    return int(row[0])


def _table_exists(conn, table: str) -> bool:
    return conn.execute(text(
        "SELECT to_regclass(current_schema() || '.' || :t) IS NOT NULL"
    ), {"t": table}).scalar() is True


def _configured_embedding_dim() -> int | None:
    """`EMBEDDING_DIMENSION` from the configuration, or None.

    For a warning only. A migration must not fail because the application
    configuration is not importable in this context -- then the warning is what
    falls away, not the repair.
    """
    try:
        from utils.config import settings

        return int(settings.embedding_dimension)
    except Exception:
        return None


def upgrade() -> None:
    conn = op.get_bind()
    _configured_dim = _configured_embedding_dim()

    for name, table, column in _VECTOR_INDEXES:
        # An instance need not have every table (dark features, a different
        # stage of build-out). A missing target is not an error, it is a skip.
        if not _table_exists(conn, table):
            continue
        dim = _column_dim(conn, table, column)
        if dim is None:
            continue
        # The index is built with the dimension of the COLUMN -- that is the
        # truth about what lies there. The services' queries, by contrast, cast
        # to `EMBEDDING_DIMENSION` from the configuration. If the two drift
        # apart, the index is built, maintained and NEVER used: exactly the
        # silent failure mode this migration exists to fix. The setting's
        # default is 768; production runs 2560 only because the ConfigMap sets
        # it -- so an instance without that key drifts immediately. The
        # migration cannot repair that, but it can SAY it instead of letting it
        # happen.
        if _configured_dim is not None and _configured_dim != dim:
            logger.warning(
                "%s: Spalte %s.%s ist vector(%d), die Konfiguration sagt "
                "EMBEDDING_DIMENSION=%d. Der Index wird auf %d gebaut und von "
                "Abfragen, die auf %d casten, NIE benutzt werden.",
                name, table, column, dim, _configured_dim, dim, _configured_dim,
            )
        if dim > 2000:
            # halfvec, because the regular vector type stops at 2000 when indexing.
            expr = f"(({column}::halfvec({dim})) halfvec_cosine_ops)"
        else:
            expr = f"({column} vector_cosine_ops)"
        op.execute(
            f"CREATE INDEX IF NOT EXISTS {name} ON {table} "
            f"USING hnsw {expr} WITH (m = 16, ef_construction = 64)"
        )

    for name, table, ddl in _PLAIN_INDEXES:
        if not _table_exists(conn, table):
            continue
        op.execute(ddl)


def downgrade() -> None:
    # Purely additive, so the way back is a pure removal. `IF EXISTS`, so a
    # downgrade on an instance that never had one of these indexes does not
    # abort -- exactly the situation this migration came out of.
    #
    # 🛑 ONE IMPRECISION, stated here instead of passed over in silence: on an
    # instance that ALREADY HAD one of these indexes, `upgrade()` was a no-op
    # because of `IF NOT EXISTS` -- this `downgrade()` removes it regardless.
    # There the downgrade does not restore the state from before but a worse
    # one. Deliberately accepted: these are pure indexes, no data loss, rebuilt
    # in seconds (measured 762 ms per HNSW at production size), and keeping
    # books on who created which index would be more apparatus than the error
    # is worth. Recovery: run `upgrade` again. On BOTH production instances all
    # eight are missing today, so there the round trip is exact (measured:
    # 326 -> 334 -> 326 -> 334).
    for name in _ALL_NAMES:
        op.execute(f"DROP INDEX IF EXISTS {name}")
