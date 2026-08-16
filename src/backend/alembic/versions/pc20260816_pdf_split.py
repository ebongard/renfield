"""PDF-split: split lineage columns on documents + pdf_split_proposals queue.

Revision ID: pc20260816_pdf_split
Revises: pc20260726_audit_review
Create Date: 2026-08-16

Multi-document PDF detection + splitting at ingest (docs/design/pdf-split.md):

- ``documents.split_from_document_id`` — child → archived combined original
  (ON DELETE SET NULL so deleting the archive never cascades into children).
- ``documents.split_heartbeat_at`` — row-level in-flight claim for the slow
  split lane (mirrors ``meetings.heartbeat_at``; split jobs are
  unbounded-duration, reclaim keys on heartbeat staleness).
- ``pdf_split_proposals`` — uncertain boundary proposals awaiting owner review
  (precedent: ``kg_merge_proposals``), with a partial unique index enforcing
  at most one PENDING proposal per document.

PG-only for the table (the sqlite test harness builds it via create_all from
the ORM model); columns are inspector-guarded + rerunnable. Fully transactional.
"""
import sqlalchemy as sa
from alembic import op

revision = "pc20260816_pdf_split"
down_revision = "pc20260726_audit_review"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name if bind is not None else "postgresql"
    inspector = sa.inspect(bind)

    if "documents" not in inspector.get_table_names():
        # Fresh install: Base.metadata.create_all owns the whole schema
        # (documents columns AND pdf_split_proposals, which references
        # documents/users and cannot be created before them).
        return

    cols = {c["name"] for c in inspector.get_columns("documents")}
    if "split_from_document_id" not in cols:
        op.add_column(
            "documents",
            sa.Column("split_from_document_id", sa.Integer(), nullable=True),
        )
        if dialect == "postgresql":
            op.execute(
                "ALTER TABLE documents "
                "ADD CONSTRAINT fk_documents_split_from_document_id "
                "FOREIGN KEY (split_from_document_id) "
                "REFERENCES documents(id) ON DELETE SET NULL"
            )
        op.execute(
            "CREATE INDEX IF NOT EXISTS ix_documents_split_from_document_id "
            "ON documents (split_from_document_id)"
        )
    if "split_heartbeat_at" not in cols:
        op.add_column(
            "documents",
            sa.Column("split_heartbeat_at", sa.DateTime(), nullable=True),
        )

    if dialect != "postgresql":
        return

    op.execute("""
        CREATE TABLE IF NOT EXISTS pdf_split_proposals (
            id                  serial PRIMARY KEY,
            document_id         integer NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
            user_id             integer REFERENCES users(id),
            status              varchar(20) NOT NULL DEFAULT 'pending',
            proposal            jsonb NOT NULL,
            page_signals        jsonb,
            page_count          integer NOT NULL,
            overall_confidence  double precision NOT NULL DEFAULT 0.0,
            created_at          timestamp without time zone DEFAULT now(),
            resolved_at         timestamp without time zone,
            resolved_by_user_id integer REFERENCES users(id)
        )
    """)
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_pdf_split_proposals_document_id "
        "ON pdf_split_proposals (document_id)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_pdf_split_proposals_user_status "
        "ON pdf_split_proposals (user_id, status)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_pdf_split_proposals_status "
        "ON pdf_split_proposals (status)"
    )
    # At most one PENDING proposal per document — approve/reject must resolve
    # the existing one before the detector may file another.
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_pdf_split_proposals_pending_doc "
        "ON pdf_split_proposals (document_id) "
        "WHERE status = 'pending'"
    )


def downgrade() -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name if bind is not None else "postgresql"
    inspector = sa.inspect(bind)

    if dialect == "postgresql":
        op.execute("DROP TABLE IF EXISTS pdf_split_proposals")

    if "documents" in inspector.get_table_names():
        cols = {c["name"] for c in inspector.get_columns("documents")}
        if "split_heartbeat_at" in cols:
            op.drop_column("documents", "split_heartbeat_at")
        if "split_from_document_id" in cols:
            if dialect == "postgresql":
                op.execute(
                    "ALTER TABLE documents DROP CONSTRAINT IF EXISTS "
                    "fk_documents_split_from_document_id"
                )
            op.drop_column("documents", "split_from_document_id")
