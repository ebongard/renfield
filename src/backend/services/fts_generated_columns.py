"""The GENERATED `search_vector` columns: what production has, and how to restore it.

Six tables carry a multilingual full-text column that Postgres computes itself
(`GENERATED ALWAYS AS (...) STORED`). The ORM cannot say so: the models declare
a plain `TSVECTOR` with a `FetchedValue()` marker, and `create_all` therefore
builds a PLAIN column that nothing ever fills. Only a migration turns it into
the GENERATED one.

🛑 Measured 2026-10-31 on a brand-new instance: the baseline
(`pc20260926_baseline`) builds a fresh install with `create_all` and brought
the GIN indexes along, but not the GENERATED columns. All six came out plain,
every chunk had `search_vector IS NULL`, and BM25 -- the lexical half of hybrid
search -- returned nothing. `pc20261031_sv_generated` repairs it.

This module is the single place that knows the six columns and their exact
production expression. It is used by that migration, by the test harness
(`tests/backend/conftest.py`), and by the startup check in
`services/database.py`. The content expressions are copied from the migrations
that created the columns; `tests/backend/test_pc20261031_search_vector_generated.py`
pins them against those migrations and against the models.

The functions take a SYNC connection (Alembic hands one to a migration; async
callers use `run_sync`) and act on `current_schema()` only, so a throwaway test
schema next to `public` is never confused with the shared one.
"""
from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import text

from services.fts_languages import build_generated_tsvector_expression

COLUMN = "search_vector"


@dataclass(frozen=True)
class GeneratedSearchVector:
    table: str
    #: The text expression the tsvector is built over, verbatim from the
    #: migration that created the column.
    content_expr: str
    #: The production name of its GIN index.
    gin_index: str
    #: The revision that created the column in production.
    origin: str

    @property
    def generation_expr(self) -> str:
        return build_generated_tsvector_expression(self.content_expr)


GENERATED_SEARCH_VECTORS: tuple[GeneratedSearchVector, ...] = (
    GeneratedSearchVector(
        "conversation_memories", "content",
        "idx_conversation_memories_search_vector_gin", "pc20260528_cm_search_vector"),
    GeneratedSearchVector(
        "document_chunks", "content",
        "idx_document_chunks_search_vector_gin", "pc20260529_dc_multilingual_fts"),
    GeneratedSearchVector(
        "document_facts",
        "value || ' ' || coalesce(normalized_value, '') || ' ' || "
        "coalesce(excerpt, '') || ' ' || kind",
        "idx_document_facts_search_vector_gin", "pc20260602_df_fts"),
    GeneratedSearchVector(
        "messages", "content",
        "idx_messages_search_vector_gin", "pc20260617_msg_search"),
    GeneratedSearchVector(
        "notes", "title || ' ' || coalesce(body, '')",
        "idx_notes_search_vector_gin", "pc20260720_notes"),
    GeneratedSearchVector(
        "documents",
        "(coalesce(generated_title, '') || ' ' || coalesce(title, '') || ' ' "
        "|| coalesce(filename, ''))",
        "idx_documents_search_vector_gin", "pc20260829_documents_fts"),
)


def table_exists(conn, table: str) -> bool:
    return conn.execute(text(
        "SELECT to_regclass(quote_ident(current_schema()) || '.' || quote_ident(:t)) IS NOT NULL"
    ), {"t": table}).scalar() is True


def generated_kind(conn, table: str, column: str = COLUMN) -> str | None:
    """`pg_attribute.attgenerated` of the column: 's' = GENERATED STORED,
    '' = a plain column, None = no such column."""
    return conn.execute(text("""
        SELECT a.attgenerated::text FROM pg_attribute a
        JOIN pg_class c ON c.oid = a.attrelid
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = current_schema() AND c.relname = :t
          AND a.attname = :col AND a.attnum > 0 AND NOT a.attisdropped
    """), {"t": table, "col": column}).scalar()


def has_valid_gin(conn, table: str, column: str = COLUMN) -> bool:
    """A VALID single-column GIN index on the column -- by table and column,
    not by name (production carries both `idx_` and `ix_` spellings)."""
    return conn.execute(text("""
        SELECT EXISTS (
          SELECT 1 FROM pg_index i
          JOIN pg_class t ON t.oid = i.indrelid
          JOIN pg_namespace n ON n.oid = t.relnamespace
          JOIN pg_class ic ON ic.oid = i.indexrelid
          JOIN pg_am am ON am.oid = ic.relam
          JOIN pg_attribute a ON a.attrelid = t.oid AND a.attnum = i.indkey[0]
          WHERE n.nspname = current_schema() AND t.relname = :t
            AND a.attname = :col AND am.amname = 'gin'
            AND i.indnatts = 1 AND i.indisvalid)
    """), {"t": table, "col": column}).scalar() is True


def ensure_generated_search_vectors(conn) -> list[str]:
    """Bring every `search_vector` to the production shape; return what changed.

    Decided by the STATE of each column, not by a revision or a name:

    * GENERATED already (`attgenerated = 's'`)  -> the column is left alone.
      This is every production database: no DDL, no lock beyond catalog reads.
    * plain or missing  -> DROP and re-ADD as GENERATED. Postgres computes the
      value for every existing row at ADD time (a table rewrite under ACCESS
      EXCLUSIVE, which is what a repair costs; on a fresh install the tables
      are empty). The DROP takes the old GIN index with it.
    * no valid GIN index on the column  -> create it under its production name.

    Plain transactional DDL, no `CONCURRENTLY`: a table that needs the index is
    either empty or was just rewritten under ACCESS EXCLUSIVE in the same
    transaction anyway. So the caller's transaction is all-or-nothing.
    A table that does not exist (yet) is skipped -- the migration that creates
    it owns it.
    """
    changed: list[str] = []
    for spec in GENERATED_SEARCH_VECTORS:
        if not table_exists(conn, spec.table):
            continue
        acted = False
        if generated_kind(conn, spec.table) != "s":
            conn.execute(text(f"ALTER TABLE {spec.table} DROP COLUMN IF EXISTS {COLUMN}"))
            conn.execute(text(
                f"ALTER TABLE {spec.table} ADD COLUMN {COLUMN} tsvector "
                f"GENERATED ALWAYS AS ({spec.generation_expr}) STORED"
            ))
            acted = True
        if not has_valid_gin(conn, spec.table):
            # An index under this name that is not a valid GIN on the column
            # (e.g. left INVALID by a broken CONCURRENTLY build) would make
            # `IF NOT EXISTS` skip silently.
            conn.execute(text(f"DROP INDEX IF EXISTS {spec.gin_index}"))
            conn.execute(text(
                f"CREATE INDEX {spec.gin_index} ON {spec.table} USING gin ({COLUMN})"
            ))
            acted = True
        if acted:
            conn.execute(text(f"ANALYZE {spec.table}"))
            changed.append(spec.table)
    return changed


def model_fetched_value_columns(metadata) -> set[tuple[str, str]]:
    """(table, column) of every model column the DATABASE computes -- a bare
    `FetchedValue()` server default. In this schema every such column is a
    GENERATED one, and a fresh install must make it so."""
    from sqlalchemy.schema import DefaultClause, FetchedValue

    out: set[tuple[str, str]] = set()
    for table in metadata.tables.values():
        for col in table.columns:
            sd = col.server_default
            if isinstance(sd, FetchedValue) and not isinstance(sd, DefaultClause):
                out.add((table.name, col.name))
    return out


def not_generated(conn, metadata) -> list[str]:
    """`table.column` for every model FetchedValue column that the database
    holds as a plain column. Missing tables are not reported here."""
    return sorted(
        f"{t}.{c}" for t, c in model_fetched_value_columns(metadata)
        if table_exists(conn, t) and generated_kind(conn, t, c) != "s"
    )
