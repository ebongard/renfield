"""Paperless metadata extraction — cold-start counter + pending confirms + examples

Revision ID: pc20260424_paperless_metadata_tables
Revises: pc20260423_atoms_per_document
Create Date: 2026-04-24

Design rationale: docs/design/paperless-llm-metadata.md

PR 2a of the LLM-driven Paperless metadata feature. Creates three small
pieces of state the extractor + confirm flow depend on. PR 2b wires the
state machine that reads/writes them.

Tables created:
  paperless_extraction_examples — corrections captured at confirm time
    (primary signal) + Paperless-UI-edit sweeps (secondary, PR 4).
    Read in PR 3 by the prompt-augmentation path to prepend in-context
    examples from real user corrections.

  paperless_pending_confirms — transient state holding a completed
    extraction between the first tool call (``forward_attachment_to_paperless``
    returns ``action_required=paperless_confirm``) and the second
    (``paperless_commit_upload`` receives the user's "ja"/"nein"/edit).
    Wiped after commit or via PR 4's abandoned-confirm sweeper (24 h).

Column added:
  users.paperless_confirms_used — cold-start counter (N = 10 by default).
    Increments only on successful upload; not on user-said-nein or on
    Paperless-upload-failed paths. Once >= N, the confirm step is skipped
    and extraction runs silently.
"""
import sqlalchemy as sa
from alembic import op


# revision identifiers
revision = "pc20260424_paperless_metadata_tables"
down_revision = "pc20260423_atoms_per_document"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ---------------------------------------------------------------------
    # 1. users.paperless_confirms_used — cold-start counter
    # ---------------------------------------------------------------------
    op.add_column(
        "users",
        sa.Column(
            "paperless_confirms_used",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )

    # ---------------------------------------------------------------------
    # 2. paperless_extraction_examples — correction-feedback source
    # ---------------------------------------------------------------------
    #
    # llm_output     what the LLM emitted (post-fuzzy, post-validation)
    # user_approved  what the user actually committed (after any edits)
    # source         'confirm_diff' | 'paperless_ui_sweep' | 'seed'
    # superseded     set by PR 4's no-re-edit filter when a ui_sweep row
    #                turns out to be taxonomy drift, not an extraction
    #                correction — the prompt-augmentation reader ignores
    #                superseded rows.
    #
    op.create_table(
        "paperless_extraction_examples",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("doc_text", sa.Text(), nullable=False),
        sa.Column("llm_output", sa.JSON(), nullable=False),
        sa.Column("user_approved", sa.JSON(), nullable=False),
        sa.Column("source", sa.String(32), nullable=False),
        sa.Column(
            "superseded",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index(
        "ix_paperless_extraction_examples_source",
        "paperless_extraction_examples",
        ["source", "superseded", "created_at"],
    )

    # ---------------------------------------------------------------------
    # 3. paperless_pending_confirms — transient state between two tool calls
    # ---------------------------------------------------------------------
    #
    # confirm_token  uuid4 in str form — agent receives this back from the
    #                first call and passes it to paperless_commit_upload.
    # attachment_id  ChatUpload pk; FK with ON DELETE CASCADE so that
    #                deleting a stale upload drops the pending-confirm row.
    # session_id     scope check for cross-session commit-token attempts
    #                (same guard as #442).
    # llm_output     raw LLM response (for diff computation in PR 2b)
    # post_fuzzy_output  post-fuzzy, post-validation result the user
    #                confirms against.
    # proposals      new_entry_proposals[] to surface in the confirm
    #                message.
    # edit_rounds    prevents infinite confirm ↔ edit loops. Capped at 3
    #                in PR 2b.
    #
    op.create_table(
        "paperless_pending_confirms",
        sa.Column("confirm_token", sa.String(36), primary_key=True),
        sa.Column(
            "attachment_id",
            sa.Integer(),
            sa.ForeignKey("chat_uploads.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("session_id", sa.String(64), nullable=False, index=True),
        sa.Column(
            "user_id",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("llm_output", sa.JSON(), nullable=False),
        sa.Column("post_fuzzy_output", sa.JSON(), nullable=False),
        sa.Column("proposals", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column(
            "edit_rounds",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    # Composite index for session-scoped lookups + oldest-first sweep.
    op.create_index(
        "ix_paperless_pending_confirms_session_created",
        "paperless_pending_confirms",
        ["session_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_paperless_pending_confirms_session_created",
        table_name="paperless_pending_confirms",
    )
    op.drop_table("paperless_pending_confirms")
    op.drop_index(
        "ix_paperless_extraction_examples_source",
        table_name="paperless_extraction_examples",
    )
    op.drop_table("paperless_extraction_examples")
    op.drop_column("users", "paperless_confirms_used")
