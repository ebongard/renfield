"""document_processing_history table + parent_chunk_id CASCADE

Revision ID: pc20260530_dph
Revises: pc20260529_dc_multilingual_fts
Create Date: 2026-05-27

Adds the ``document_processing_history`` audit table that records every
ingestion attempt against a Document. Owned by ``DocumentProcessingHistoryService``;
written by ``RAGService.ingest_document`` and ``reindex_document`` via the
``history.track()`` async context manager.

Concurrent design:

  documents (1) ────────── (N) document_processing_history
                              status: pending|processing|completed|failed
                              trigger: initial_ingest|user_reindex|
                                       script_purge|startup_sweep
                              force_ocr: bool (NOT NULL)
                              ocr_engine: nullable str (easyocr|
                                                        docling_full_page_ocr)

The partial unique index ``uq_dph_initial_ingest_per_doc`` lets the
backfill (`INSERT...SELECT ... ON CONFLICT DO NOTHING`) be safely
re-runnable: at most one ``initial_ingest`` row per document. Per-doc
re-ingests (user_reindex/script_purge/startup_sweep) are unconstrained.

The partial index ``idx_dph_document_force_ocr_status`` accelerates the
script's ``has_force_ocr_succeeded(doc_id)`` query
(``WHERE force_ocr=true AND status='completed'``).

Also fixes ``document_chunks.parent_chunk_id`` self-FK to ``ON DELETE
CASCADE``. Previously RESTRICT, which doesn't fire for ORM-mediated
deletes (Python-side cascade rewrites them) but would fire for any raw
SQL DELETE on a parent chunk — defensive fix while we're touching the
schema.
"""
from alembic import op
import sqlalchemy as sa


revision = "pc20260530_dph"
down_revision = "pc20260529_dc_multilingual_fts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "document_processing_history",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column(
            "document_id",
            sa.Integer,
            sa.ForeignKey("documents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("force_ocr", sa.Boolean, nullable=False),
        sa.Column("ocr_engine", sa.String(50), nullable=True),
        sa.Column("chunks_produced", sa.Integer, nullable=True),
        sa.Column("chunks_dropped_low_quality", sa.Integer, nullable=True),
        sa.Column("trigger", sa.String(30), nullable=False),
        sa.Column("error_message", sa.Text, nullable=True),
        sa.Column(
            "extra",
            sa.dialects.postgresql.JSONB,
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('pending','processing','completed','failed')",
            name="chk_dph_status",
        ),
        sa.CheckConstraint(
            "trigger IN ('initial_ingest','user_reindex','script_purge','startup_sweep')",
            name="chk_dph_trigger",
        ),
    )

    op.create_index(
        "idx_dph_document_id",
        "document_processing_history",
        ["document_id"],
    )

    # Partial index: hot path is the script's has_force_ocr_succeeded query.
    op.create_index(
        "idx_dph_document_force_ocr_status",
        "document_processing_history",
        ["document_id", "force_ocr", "status"],
        postgresql_where=sa.text("force_ocr = true AND status = 'completed'"),
    )

    # Partial unique index: at most one initial_ingest row per document.
    # Lets the backfill below (and any future re-run) stay idempotent.
    op.create_index(
        "uq_dph_initial_ingest_per_doc",
        "document_processing_history",
        ["document_id"],
        unique=True,
        postgresql_where=sa.text("trigger = 'initial_ingest'"),
    )

    # Backfill: one initial_ingest row per existing Document. force_ocr=false
    # because initial_ingest by definition didn't pass a force flag.
    # ON CONFLICT keeps the migration safely re-runnable after a partial fail.
    op.execute(
        """
        INSERT INTO document_processing_history
            (document_id, started_at, finished_at, status, force_ocr, trigger)
        SELECT
            id,
            COALESCE(created_at, now()),
            processed_at,
            COALESCE(status, 'pending'),
            false,
            'initial_ingest'
        FROM documents
        ON CONFLICT ON CONSTRAINT uq_dph_initial_ingest_per_doc DO NOTHING
        """
    )

    # document_chunks.parent_chunk_id: drop the implicit-RESTRICT FK and
    # re-create it with ON DELETE CASCADE. ORM-mediated deletes never trip
    # this constraint because SA rewrites parent-chunk deletes Python-side,
    # but raw-SQL deletes (in scripts or in future tooling) would currently
    # fail with NO ACTION. Fix it now while the schema is open.
    with op.batch_alter_table("document_chunks") as batch:
        batch.drop_constraint(
            "document_chunks_parent_chunk_id_fkey",
            type_="foreignkey",
        )
        batch.create_foreign_key(
            "document_chunks_parent_chunk_id_fkey",
            "document_chunks",
            ["parent_chunk_id"],
            ["id"],
            ondelete="CASCADE",
        )


def downgrade() -> None:
    with op.batch_alter_table("document_chunks") as batch:
        batch.drop_constraint(
            "document_chunks_parent_chunk_id_fkey",
            type_="foreignkey",
        )
        batch.create_foreign_key(
            "document_chunks_parent_chunk_id_fkey",
            "document_chunks",
            ["parent_chunk_id"],
            ["id"],
        )

    op.drop_index(
        "uq_dph_initial_ingest_per_doc",
        table_name="document_processing_history",
    )
    op.drop_index(
        "idx_dph_document_force_ocr_status",
        table_name="document_processing_history",
    )
    op.drop_index(
        "idx_dph_document_id",
        table_name="document_processing_history",
    )
    op.drop_table("document_processing_history")
