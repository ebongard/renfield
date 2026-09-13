"""scheduled_tasks: consecutive-error counter + alert marker (A2).

A task that fails every run used to be perfectly recorded (``ScheduledTaskRun``,
``last_status``) and perfectly silent — the Paperless dedupe task failed 50 times
in a row over a day and a half without anyone being told. These two additive
columns give the engine the minimum durable state it needs to alert ONCE per
failure streak instead of once per run:

* ``consecutive_error_count`` — reset to 0 on any non-error run, so it measures
  a *streak*, not a lifetime total.
* ``error_alerted_at`` — when the owner admin was last told about THIS streak.
  Durable on purpose: the in-process ledger in ``services/ops_alert.py`` is a
  rate limiter that a pod restart legitimately re-arms, but "we already told
  them about this streak" must survive a restart or a crash-looping pod would
  re-alert on every boot.

Additive + fully transactional: no backfill, both columns carry server defaults
so rows written by an older pod during a rolling deploy are valid.

Revision ID: pc20260912_taskalert
Revises: pc20260908b_cred_sphere
"""
from alembic import op
import sqlalchemy as sa

revision = "pc20260912_taskalert"
down_revision = "pc20260908b_cred_sphere"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "scheduled_tasks",
        sa.Column(
            "consecutive_error_count",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )
    op.add_column(
        "scheduled_tasks",
        sa.Column("error_alerted_at", sa.DateTime(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("scheduled_tasks", "error_alerted_at")
    op.drop_column("scheduled_tasks", "consecutive_error_count")
