"""documents.paperless_state — folder-ingest Paperless leg marker

Revision ID: pc20260612_pl_state
Revises: pc20260611_gen_title
Create Date: 2026-06-08

Adds ``documents.paperless_state``: a small string marker the folder-ingest
bridge (services/folder_ingest.py) uses to record whether a document has been
filed into Paperless, so a re-pushed already-completed document runs ONLY the
still-missing Paperless step rather than re-ingesting the whole pipeline (D2),
and so a Paperless duplicate-marker reject counts as terminal success (D10).

Values (see models.database.PAPERLESS_STATE_*): NULL = never attempted (all
existing rows + normal non-folder uploads stay NULL), "pending"/"failed" =
retryable, "done" = terminal (filed / duplicate / skipped). Additive + nullable
+ unindexed (the bridge reads it only after locating the row by file_hash).
Fully transactional.
"""
from alembic import op
import sqlalchemy as sa


revision = "pc20260612_pl_state"
down_revision = "pc20260611_gen_title"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "documents",
        sa.Column("paperless_state", sa.String(length=20), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("documents", "paperless_state")
