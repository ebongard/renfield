"""telephony_faxes: one filing job per received fax (#1677, Telefonie Phase 3).

New table, no backfill. The fax itself is not stored here: the PDF goes through
the document pipeline (folder_ingest.ingest_document), only the job state lives here.

Revision ID: pc20261127b_telephony_faxes
Revises: pc20261127_telephony_voicemails
Create Date: 2026-11-27
"""
import sqlalchemy as sa
from alembic import op

revision: str = "pc20261127b_telephony_faxes"
down_revision: str | None = "pc20261127_telephony_voicemails"
branch_labels: None = None
depends_on: None = None

TABLE = "telephony_faxes"


def _has_table(name: str) -> bool:
    """A fresh install's baseline runs ``create_all`` and already has the table."""
    return name in sa.inspect(op.get_bind()).get_table_names()


def upgrade() -> None:
    if _has_table(TABLE):
        return
    op.create_table(
        TABLE,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("call_id", sa.String(64), nullable=False),
        sa.Column("fax", sa.String(64), nullable=False),
        sa.Column("caller", sa.String(32), nullable=True),
        sa.Column("did", sa.String(32), nullable=True),
        sa.Column("pages", sa.Integer, nullable=False, server_default="0"),
        sa.Column("document_id", sa.Integer, nullable=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("attempts", sa.Integer, nullable=False, server_default="0"),
        sa.Column("next_attempt_at", sa.DateTime, nullable=False, server_default=sa.text("now()")),
        sa.Column("created_at", sa.DateTime, nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime, nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint("call_id", name="telephony_faxes_call_id_key"),
    )
    op.create_index("ix_telephony_faxes_due", TABLE, ["status", "next_attempt_at"])


def downgrade() -> None:
    op.drop_table(TABLE)
