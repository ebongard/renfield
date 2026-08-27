"""scheduled_tasks — DB-defined, UI-managed recurring jobs (#1137)

Revision ID: pc20260827_scheduled_tasks
Revises: pc20260825_paperless_task_id
Create Date: 2026-08-27

The Scheduled Tasks subsystem (docs/design/scheduled-tasks.md): task definitions
run by a single engine loop that spawns each due task as its own asyncio.Task.
Each row is a schedule (interval OR cron, optional start/end window, enable
toggle) bound to a ``handler_key`` resolved against the code registry.

Idempotency: every DDL op is guarded — Base.metadata.create_all (backend boot)
creates this table for a new model, so re-running this migration must be a
no-op (same pattern as pc20260714_meetings). Fully transactional
(transaction_per_migration).
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "pc20260827_scheduled_tasks"
down_revision = "pc20260825_paperless_task_id"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing_tables = set(inspector.get_table_names())

    json_type = (
        postgresql.JSONB(astext_type=sa.Text())
        if bind.dialect.name == "postgresql"
        else sa.JSON()
    )

    if "scheduled_tasks" not in existing_tables:
        op.create_table(
            "scheduled_tasks",
            sa.Column("id", sa.Integer(), primary_key=True),
            # UNIQUE — the seed/idempotency key for ensure_builtin_tasks
            # (INSERT ... ON CONFLICT (name) DO NOTHING).
            sa.Column("name", sa.String(255), nullable=False, unique=True),
            sa.Column("handler_key", sa.String(100), nullable=False),
            # "interval" | "cron"
            sa.Column("schedule_kind", sa.String(20), nullable=False, server_default="interval"),
            sa.Column("interval_seconds", sa.Integer(), nullable=True),
            sa.Column("cron_expr", sa.String(120), nullable=True),
            # handler arguments (validated per-handler on write)
            sa.Column("params", json_type, nullable=False, server_default="{}"),
            sa.Column("enabled", sa.Boolean(), nullable=False, server_default="true"),
            sa.Column("run_at_boot", sa.Boolean(), nullable=False, server_default="false"),
            sa.Column("start_at", sa.DateTime(), nullable=True),
            sa.Column("end_at", sa.DateTime(), nullable=True),
            # engine-computed; the due-selection index target
            sa.Column("next_run_at", sa.DateTime(), nullable=True),
            sa.Column("last_run_at", sa.DateTime(), nullable=True),
            sa.Column("last_status", sa.String(20), nullable=True),
            sa.Column("last_error", sa.Text(), nullable=True),
            sa.Column("last_duration_ms", sa.Integer(), nullable=True),
            # built-ins are edit-not-delete; custom tasks are deletable
            sa.Column("is_builtin", sa.Boolean(), nullable=False, server_default="false"),
            sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        )

    def _has_idx(idx_name: str) -> bool:
        if "scheduled_tasks" not in existing_tables:
            return False
        return idx_name in {ix["name"] for ix in inspector.get_indexes("scheduled_tasks")}

    # Due-selection scans enabled rows by next_run_at every tick.
    if not _has_idx("ix_scheduled_tasks_next_run_at"):
        op.create_index("ix_scheduled_tasks_next_run_at", "scheduled_tasks", ["next_run_at"])


def downgrade() -> None:
    op.drop_index("ix_scheduled_tasks_next_run_at", table_name="scheduled_tasks")
    op.drop_table("scheduled_tasks")
