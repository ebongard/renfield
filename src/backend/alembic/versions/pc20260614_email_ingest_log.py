"""email_ingest_log — per-attachment provenance + idempotency ledger

Revision ID: pc20260614_email_ingest
Revises: pc20260613_pl_docid
Create Date: 2026-06-09

Backs email-mailbox auto-ingest (Phase 1): the renfield-mcp-email-ingest watcher
pushes each allowlisted attachment to POST /api/email-ingest/document; one row
per (mailbox_id, message_id, attachment_sha256) records provenance (sender,
subject) + the 4-state outcome and gives the watcher idempotency on re-push.

UNIQUE (mailbox_id, message_id, attachment_sha256) keyed by mailbox so two
spheres never cross-dedup. document_id FK is ON DELETE SET NULL (a purged doc
leaves the audit row). Additive; ships dark behind EMAIL_INGEST_ENABLED. Fully
transactional.
"""
from alembic import op
import sqlalchemy as sa


revision = "pc20260614_email_ingest"
down_revision = "pc20260613_pl_docid"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "email_ingest_log",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("mailbox_id", sa.String(length=120), nullable=False),
        sa.Column("message_id", sa.String(length=998), nullable=False),
        sa.Column("attachment_sha256", sa.String(length=64), nullable=False),
        sa.Column(
            "document_id",
            sa.Integer(),
            sa.ForeignKey("documents.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("sender", sa.String(length=320), nullable=True),
        sa.Column("subject", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint(
            "mailbox_id", "message_id", "attachment_sha256",
            name="uq_email_ingest_mailbox_msg_attachment",
        ),
    )
    op.create_index("ix_email_ingest_log_mailbox_id", "email_ingest_log", ["mailbox_id"])
    op.create_index("ix_email_ingest_log_document_id", "email_ingest_log", ["document_id"])


def downgrade() -> None:
    op.drop_index("ix_email_ingest_log_document_id", table_name="email_ingest_log")
    op.drop_index("ix_email_ingest_log_mailbox_id", table_name="email_ingest_log")
    op.drop_table("email_ingest_log")
