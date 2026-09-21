"""tool_outcome_stats: the SYSTEM bucket for anonymous turns (BL-0233).

Most household voice turns carry no identified user (satellite/device turns with
``user_id=None``), and ``ToolOutcomeService.record`` deliberately no-op'd on
them because ``UNIQUE (user_id, tool_name)`` treats NULLs as distinct — a
NULL-keyed upsert would insert a fresh row on every call. Result: the kiosk
tool-health and the admin console stayed near-empty on the household.

This adds a PARTIAL unique index ``(tool_name) WHERE user_id IS NULL`` so the
NULL rows form one bucket per tool that an ``INSERT … ON CONFLICT`` can target
(the same pattern Reva uses: tool health is about the tool, not the user).

Additive, fully transactional (a small table; no CONCURRENTLY needed), so an
interrupted run rolls back completely. No backfill: the service never wrote a
NULL row (measured 2026-09-21 on both instances: 0 NULL rows, 0 duplicate
groups). Should a stray pair of NULL rows sharing a tool_name ever exist, the
CREATE UNIQUE INDEX fails loudly and the alembic job aborts BEFORE the rollout
— merge the duplicates (SUM counters, MAX timestamps) and rerun. No
``IF NOT EXISTS``: a same-named hand-made index with a different definition
(non-unique, no predicate) must surface here, not as a runtime ON CONFLICT
error on every anonymous turn.

Revision ID: pc20260921_tool_outcome_system_bucket
Revises: pc20260920_output_tts_eq
"""
from alembic import op
import sqlalchemy as sa

revision = "pc20260921_tool_outcome_system_bucket"
down_revision = "pc20260920_output_tts_eq"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "uq_tool_outcome_system_tool",
        "tool_outcome_stats",
        ["tool_name"],
        unique=True,
        postgresql_where=sa.text("user_id IS NULL"),
        sqlite_where=sa.text("user_id IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_tool_outcome_system_tool", table_name="tool_outcome_stats")
