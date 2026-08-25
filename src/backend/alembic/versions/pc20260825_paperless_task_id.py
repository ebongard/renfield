"""documents.paperless_task_id — persist the Paperless consume task_id for idempotent refile

Revision ID: pc20260825_paperless_task_id
Revises: pc20260824_pf_finalize
Create Date: 2026-08-25

The folder-ingest Paperless leg (folder_ingest_paperless._leg) uploaded a doc,
then polled the consume task, but on an await timeout it DISCARDED the task_id and
left the doc paperless_state='pending'. The reconciler then re-enqueued it and the
leg RE-UPLOADED — creating a brand-new Paperless copy every cycle (one file reached
2289 copies on xidra). Persisting the submitted task_id lets a retry re-POLL the
same consume task (await_consume_result) instead of re-uploading, so a document is
uploaded at most once. Cleared when the doc settles (done/failed) or is reset for a
fresh attempt.

Additive + nullable + unindexed (read only after locating our row). NULL = no
outstanding upload (normal), or a doc that predates this migration. Fully
transactional.
"""
from alembic import op
import sqlalchemy as sa


revision = "pc20260825_paperless_task_id"
down_revision = "pc20260824_pf_finalize"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "documents",
        sa.Column("paperless_task_id", sa.String(length=255), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("documents", "paperless_task_id")
