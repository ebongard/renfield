"""scheduled_task_runs — per-run history for scheduled tasks (#1137)

Revision ID: pc20260828_scheduled_task_runs
Revises: pc20260827_scheduled_tasks
Create Date: 2026-08-28

A bounded per-run audit trail for the Scheduled Tasks engine so the admin UI can
show the log of each scheduled run (start/finish, status, duration, the handler's
output detail, and any error). The engine writes one row per run and prunes to
the newest ``scheduled_tasks_run_history_limit`` per task.

Idempotent create-table (Base.metadata.create_all creates it for a new model, so
re-running must be a no-op — same pattern as pc20260827_scheduled_tasks). Fully
transactional (transaction_per_migration).
"""
import sqlalchemy as sa
from alembic import op

revision = "pc20260828_scheduled_task_runs"
down_revision = "pc20260827_scheduled_tasks"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing_tables = set(inspector.get_table_names())

    if "scheduled_task_runs" not in existing_tables:
        op.create_table(
            "scheduled_task_runs",
            sa.Column("id", sa.Integer(), primary_key=True),
            # CASCADE: a task's runs are meaningless without it (custom tasks are
            # deletable; built-ins aren't, but keep the FK honest either way).
            sa.Column(
                "task_id", sa.Integer(),
                sa.ForeignKey("scheduled_tasks.id", ondelete="CASCADE"), nullable=False,
            ),
            sa.Column("started_at", sa.DateTime(), nullable=False),
            sa.Column("finished_at", sa.DateTime(), nullable=True),
            sa.Column("status", sa.String(20), nullable=False),  # ok | error | skipped
            sa.Column("duration_ms", sa.Integer(), nullable=True),
            # the handler's return string (e.g. "deleted=200 remaining=1852" or a
            # "skipped: ..." reason)
            sa.Column("detail", sa.Text(), nullable=True),
            sa.Column("error", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        )

    def _has_idx(idx_name: str) -> bool:
        if "scheduled_task_runs" not in existing_tables:
            return False
        return idx_name in {ix["name"] for ix in inspector.get_indexes("scheduled_task_runs")}

    # History query: newest runs for a task.
    if not _has_idx("ix_scheduled_task_runs_task_started"):
        op.create_index(
            "ix_scheduled_task_runs_task_started",
            "scheduled_task_runs",
            ["task_id", "started_at"],
        )


def downgrade() -> None:
    op.drop_index("ix_scheduled_task_runs_task_started", table_name="scheduled_task_runs")
    op.drop_table("scheduled_task_runs")
