"""simba_ingest_proposals: watch-folder PDF → Simba review queue (xidra).

Revision ID: pc20260828b_simba_ingest
Revises: pc20260828_scheduled_task_runs
Create Date: 2026-08-28

A watch-folder-ingested PDF that should go to the Simba tax portal lands here as
a PENDING proposal with a content-classified category/type suggestion; the owner
confirms (→ real, irreversible upload) or rejects on /brain/review. Nothing is
auto-uploaded to the tax accountant.

PG-only for the table (the sqlite test harness builds it via create_all from the
ORM model). Rerunnable + inspector-guarded. Fully transactional.
"""
import sqlalchemy as sa
from alembic import op

revision = "pc20260828b_simba_ingest"
down_revision = "pc20260828_scheduled_task_runs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name if bind is not None else "postgresql"
    inspector = sa.inspect(bind)

    if "documents" not in inspector.get_table_names():
        # Fresh install: create_all owns the whole schema (this table references
        # documents/users and can't precede them).
        return
    if dialect != "postgresql":
        return

    op.execute("""
        CREATE TABLE IF NOT EXISTS simba_ingest_proposals (
            id                  serial PRIMARY KEY,
            document_id         integer NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
            user_id             integer REFERENCES users(id),
            status              varchar(20) NOT NULL DEFAULT 'pending',
            filename            varchar NOT NULL,
            suggested_category  varchar(100),
            suggested_type      varchar(100),
            created_at          timestamp without time zone DEFAULT now(),
            resolved_at         timestamp without time zone,
            resolved_by_user_id integer REFERENCES users(id)
        )
    """)
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_simba_ingest_proposals_document_id "
        "ON simba_ingest_proposals (document_id)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_simba_ingest_proposals_user_status "
        "ON simba_ingest_proposals (user_id, status)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_simba_ingest_proposals_status "
        "ON simba_ingest_proposals (status)"
    )
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_simba_ingest_proposals_pending_doc "
        "ON simba_ingest_proposals (document_id) "
        "WHERE status = 'pending'"
    )


def downgrade() -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name if bind is not None else "postgresql"
    if dialect == "postgresql":
        op.execute("DROP TABLE IF EXISTS simba_ingest_proposals")
