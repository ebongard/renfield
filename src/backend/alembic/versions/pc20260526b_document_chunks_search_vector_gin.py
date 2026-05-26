"""GIN index on document_chunks.search_vector — brain-quality fix.

Revision ID: pc20260526b_dc_search_gin
Revises: pc20260527_skill_approval_status
Create Date: 2026-05-26 19:00:00.000000

``document_chunks.search_vector`` has been populated since the BM25
retriever shipped, but the table never had a GIN index on it. Every
lexical retrieval issued a sequential scan with per-row
``websearch_to_tsquery @@`` evaluation. Fine on the 159-row production
corpus today; will become the dominant cost the moment Paperless
ingestion picks up.

GIN is the right index for tsvector by every Postgres-side benchmark;
``CREATE INDEX CONCURRENTLY`` so the index build doesn't lock the
table on production deploy. Cannot run inside a transaction — Alembic
detects the ``with_concurrency`` op below and runs it outside the
implicit migration transaction.
"""
from alembic import op


revision = "pc20260526b_dc_search_gin"
down_revision = "pc20260527_skill_approval_status"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name if bind is not None else "postgresql"
    if dialect != "postgresql":
        return

    # CONCURRENTLY must run outside a transaction. Alembic wraps
    # upgrade() in a transaction by default; the safe pattern is
    # autocommit_block.
    with op.get_context().autocommit_block():
        op.execute(
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS "
            "idx_document_chunks_search_vector_gin "
            "ON document_chunks USING gin (search_vector)"
        )


def downgrade() -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name if bind is not None else "postgresql"
    if dialect != "postgresql":
        return
    with op.get_context().autocommit_block():
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS idx_document_chunks_search_vector_gin")
