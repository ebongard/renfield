"""Paperless upload tracking — record every successful upload for the UI-edit sweeper

Revision ID: pc20260426_paperless_upload_tracking
Revises: pc20260425_paperless_examples_embedding
Create Date: 2026-04-26

Design rationale: docs/design/paperless-llm-metadata.md (PR 4 —
Paperless-UI-edit sweeper + abandoned-confirm cleanup).

The sweeper needs to know, for every document we uploaded:
  - which Paperless document_id it landed as
  - what metadata we sent (so we can diff against the current Paperless
    state to detect user edits)
  - when the upload happened (so we can apply the 1 h edit window)
  - which user did it (so the correction row lands on the right owner)

We keep this out of ``chat_uploads`` because ``chat_uploads`` is a
generic attachment table used for far more than Paperless. Bolting
paperless-specific columns onto it would couple the two subsystems
in the wrong direction.

``swept_at`` is set when the sweeper has processed the row. Rows with
``swept_at IS NULL`` are sweep candidates; post-sweep we don't touch
them again. Keeps the query fast even once the table grows.
"""
import sqlalchemy as sa
from alembic import op


# revision identifiers
revision = "pc20260426_paperless_upload_tracking"
down_revision = "pc20260425_paperless_examples_embedding"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "paperless_upload_tracking",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "chat_upload_id",
            sa.Integer(),
            sa.ForeignKey("chat_uploads.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("paperless_document_id", sa.Integer(), nullable=False),
        sa.Column(
            "user_id",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=True,
            index=True,
        ),
        sa.Column(
            "uploaded_at",
            sa.DateTime(),
            nullable=False,
            # Explicit UTC cast: the sweeper compares ``uploaded_at``
            # against Python's ``datetime.utcnow()`` (naive UTC). A bare
            # ``now()`` would produce a naive timestamp in the DB
            # session's timezone — if the container drifts off UTC the
            # window filter misfires silently.
            server_default=sa.text("(now() AT TIME ZONE 'UTC')"),
        ),
        # Exactly what we sent to Paperless — the field set we compare
        # against when the sweeper runs. JSON shape mirrors
        # ``PaperlessMetadata.model_dump`` minus confidence + proposals.
        sa.Column("original_metadata", sa.JSON(), nullable=False),
        # doc_text is the OCR extract the extractor saw. Needed so the
        # ui_sweep row we eventually write has the same embeddable
        # document text as a confirm_diff row would. Nullable because
        # the silent-past-cap path doesn't always retain it.
        sa.Column("doc_text", sa.Text(), nullable=True),
        # NULL until the sweeper processes this row. After sweep, set
        # to now() so subsequent sweeps skip it.
        sa.Column("swept_at", sa.DateTime(), nullable=True, index=True),
    )
    # Composite index for the sweeper's main query: find unswept rows
    # uploaded in the last window.
    op.create_index(
        "ix_paperless_upload_tracking_sweep_candidates",
        "paperless_upload_tracking",
        ["swept_at", "uploaded_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_paperless_upload_tracking_sweep_candidates",
        table_name="paperless_upload_tracking",
    )
    op.drop_table("paperless_upload_tracking")
