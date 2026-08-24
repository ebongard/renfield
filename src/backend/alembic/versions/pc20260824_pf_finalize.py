"""paperless_pending_finalize — durable intent for the async commit finalize (#658)

Revision ID: pc20260824_pf_finalize
Revises: pc20260816_pdf_split
Create Date: 2026-08-24

The interactive Paperless commit uploads async (wait_for_consume=False) and
finishes in a fire-and-forget background task (_finalize_paperless_commit): poll
the consume task <=300s → apply the deferred metadata PATCH → write the
PaperlessUploadTracking row. If the consume exceeds the poll window OR the pod
restarts mid-poll, that in-memory task is lost and the PATCH/tracking silently
never land (#658). This table persists the finalize intent BEFORE the task
spawns so paperless_finalize_reconciler can re-run a lost finalize.

Fresh installs get the table via Base.metadata.create_all (see CLAUDE.md — the
alembic job is not run on an empty DB). This migration is for existing DBs;
inspector-guarded so it is rerunnable. Fully transactional.
"""
import sqlalchemy as sa
from alembic import op

revision = "pc20260824_pf_finalize"
down_revision = "pc20260816_pdf_split"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    # (#658) UNIQUE backstop for the finalize idempotency: one ChatUpload → one
    # paperless_upload_tracking row. Self-healing so it can never break the
    # deploy — dedupe any pre-existing re-forward duplicates (keep the oldest
    # per chat_upload_id) BEFORE adding the constraint. Postgres-only; fresh
    # (sqlite) installs get it from the model via create_all.
    if bind.dialect.name == "postgresql" and "paperless_upload_tracking" in inspector.get_table_names():
        uq_names = {c["name"] for c in inspector.get_unique_constraints("paperless_upload_tracking")}
        if "uq_paperless_upload_tracking_chat_upload_id" not in uq_names:
            op.execute(
                "DELETE FROM paperless_upload_tracking a "
                "USING paperless_upload_tracking b "
                "WHERE a.chat_upload_id = b.chat_upload_id AND a.id > b.id"
            )
            op.create_unique_constraint(
                "uq_paperless_upload_tracking_chat_upload_id",
                "paperless_upload_tracking",
                ["chat_upload_id"],
            )

    if "paperless_pending_finalize" in inspector.get_table_names():
        return  # already present (rerun / fresh-install create_all)

    op.create_table(
        "paperless_pending_finalize",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("task_id", sa.String(255), nullable=False),
        sa.Column(
            "chat_upload_id",
            sa.Integer,
            sa.ForeignKey("chat_uploads.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "user_id",
            sa.Integer,
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("session_id", sa.String(255), nullable=True),
        sa.Column("filename", sa.String(1024), nullable=False),
        sa.Column("deferred_patch", sa.JSON, nullable=False),
        sa.Column("original_metadata", sa.JSON, nullable=False),
        sa.Column("created_note", sa.Text, nullable=True),
        sa.Column("doc_text", sa.Text, nullable=True),
        sa.Column("created_at", sa.DateTime, nullable=False, server_default=sa.func.now()),
        sa.Column("finalized_at", sa.DateTime, nullable=True),
        sa.Column("attempts", sa.Integer, nullable=False, server_default="0"),
    )
    op.create_index(
        "ix_paperless_pending_finalize_chat_upload_id",
        "paperless_pending_finalize", ["chat_upload_id"],
    )
    op.create_index(
        "ix_paperless_pending_finalize_user_id",
        "paperless_pending_finalize", ["user_id"],
    )
    # The reconciler scans finalized_at IS NULL ordered by created_at, so index
    # both (partial index on the NULL rows keeps the working set tiny).
    op.create_index(
        "ix_pf_finalize_unfinalized",
        "paperless_pending_finalize", ["created_at"],
        postgresql_where=sa.text("finalized_at IS NULL"),
    )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if "paperless_pending_finalize" in inspector.get_table_names():
        # Guarded drops: a create_all-first DB may have the table without the
        # migration's named indexes, so drop only what exists.
        idx_names = {i["name"] for i in inspector.get_indexes("paperless_pending_finalize")}
        for idx in (
            "ix_pf_finalize_unfinalized",
            "ix_paperless_pending_finalize_user_id",
            "ix_paperless_pending_finalize_chat_upload_id",
        ):
            if idx in idx_names:
                op.drop_index(idx, table_name="paperless_pending_finalize")
        op.drop_table("paperless_pending_finalize")

    if bind.dialect.name == "postgresql":
        uq_names = {c["name"] for c in inspector.get_unique_constraints("paperless_upload_tracking")}
        if "uq_paperless_upload_tracking_chat_upload_id" in uq_names:
            op.drop_constraint(
                "uq_paperless_upload_tracking_chat_upload_id",
                "paperless_upload_tracking",
                type_="unique",
            )
