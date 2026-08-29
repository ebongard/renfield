"""Multilingual FTS column on documents — document search by name.

Revision ID: pc20260829_documents_fts
Revises: pc20260828b_simba_ingest
Create Date: 2026-08-29 00:00:00.000000

Adds ``documents.search_vector`` so the document search
(``GET /api/knowledge/documents?q=`` → ``services/document_search.py``) can rank
documents by NAME with Postgres FTS (``ts_rank``) over the display fields —
``generated_title`` (Schicht A), ``title``, ``filename``. Until now the document
list had no text index at all (unlike messages / document_chunks / document_facts),
so a document could only be reached in the 100-newest recency window — an old
document (e.g. rank 368 of 401) was unreachable by name.

Pattern — identical to ``pc20260617`` (messages): a STORED GENERATED column
unioning ``to_tsvector`` across all ``services/fts_languages.FTS_LANGUAGES``
(DE / EN / FR / IT / ES / NL), plus a GIN index built CONCURRENTLY. The tsvector
content is the concatenation of the three name columns, so a term in the
generated title OR the filename matches.

GENERATED STORED (not trigger + backfill): the column auto-populates for EVERY
existing row the instant it is added — Postgres computes it from the already-
populated name columns at ALTER time; no backfill window. The ORM column is
``FetchedValue()`` so it stays out of INSERTs (writing a GENERATED column raises).

Transaction model: ``env.py`` runs ``transaction_per_migration=True``; the GIN
index is built inside ``op.get_context().autocommit_block()`` (CONCURRENTLY
cannot run in a transaction). A defensive ``DROP INDEX CONCURRENTLY IF EXISTS``
precedes the CREATE so a prior INVALID index is rebuilt, not silently skipped.
"""
from alembic import op

from services.fts_languages import build_generated_tsvector_expression

revision = "pc20260829_documents_fts"
down_revision = "pc20260828b_simba_ingest"
branch_labels = None
depends_on = None

_INDEX = "idx_documents_search_vector_gin"

# tsvector over generated_title + title + filename (concatenated). The helper
# wraps this in a further coalesce(..., '') — harmless; the whole expression is a
# concat of columns + literals, so it stays IMMUTABLE for the GENERATED check.
_CONTENT = (
    "(coalesce(generated_title, '') || ' ' || coalesce(title, '') || ' ' "
    "|| coalesce(filename, ''))"
)


def upgrade() -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name if bind is not None else "postgresql"
    if dialect != "postgresql":
        # Sqlite test harness has no tsvector; document_search has a sqlite
        # ILIKE fallback branch.
        return

    tsvector_expr = build_generated_tsvector_expression(_CONTENT)
    # Unconditional DROP-then-ADD: a create_all-bootstrapped dev DB gets a plain
    # nullable TSVECTOR that would never populate; ADD COLUMN IF NOT EXISTS
    # matches on name only. The column is fully derived, so re-adding repopulates
    # every existing row instantly.
    op.execute("ALTER TABLE documents DROP COLUMN IF EXISTS search_vector")
    op.execute(
        "ALTER TABLE documents "
        "ADD COLUMN search_vector tsvector "
        f"GENERATED ALWAYS AS ({tsvector_expr}) STORED"
    )

    with op.get_context().autocommit_block():
        op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {_INDEX}")
        op.execute(
            f"CREATE INDEX CONCURRENTLY {_INDEX} ON documents USING gin (search_vector)"
        )

    # Refresh planner stats so the fresh GIN index isn't ignored for a seq scan
    # in the cold-stats window right after deploy.
    op.execute("ANALYZE documents")


def downgrade() -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name if bind is not None else "postgresql"
    if dialect != "postgresql":
        return
    with op.get_context().autocommit_block():
        op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {_INDEX}")
    op.execute("ALTER TABLE documents DROP COLUMN IF EXISTS search_vector")
