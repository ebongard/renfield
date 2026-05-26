"""Skill approval status — self-learning admin console (v2.10).

Revision ID: pc20260527_skill_approval_status
Revises: pc20260526_curator
Create Date: 2026-05-26 12:00:00.000000

Replaces ``procedural_skills.is_active`` + ``pinned`` (two parallel booleans)
with a single ``status`` enum lifecycle. Adds two new tables wired by the
admin console: ``skill_curator_runs`` (audit row per curator invocation) and
``skill_would_have_injected_log`` (shadow log: rows the retrieval *would*
have returned if the draft-gate were not in place — for measuring recall
delta during rollout).

State machine
-------------
::

                    create (auto)              create (seed/manual)
                          |                            |
                          v                            v
                       [draft] -- approve -----> [approved]
                          |                          |
                          | reject               archive (manual or auto)
                          v                          v
                      [rejected]                 [archived]
                          ^                          ^
                          |                          |
                          +--------- reopen ---------+
                                  (allowed)

Only ``approved`` skills participate in agent retrieval (``find_similar``).
``draft`` rows are surfaced in the admin Skills Inbox for human review.
``rejected`` and ``archived`` are excluded from retrieval but kept for
audit + the would-have-injected shadow query.

Backfill rules (6 in total)
---------------------------
The pre-existing ``is_active``/``pinned`` flags map onto the new lifecycle
as follows. Rules are applied in declaration order; later rules never
overwrite earlier results because the WHERE clauses are mutually exclusive
once status has been touched.

1. ``status='archived'`` WHERE ``is_active = FALSE`` — soft-deleted rows.
2. ``status='approved'`` WHERE ``is_active = TRUE`` AND ``source = 'seed'``.
3. ``status='approved'`` WHERE ``is_active = TRUE`` AND ``source = 'user_created'``.
4. ``status='approved'`` WHERE ``is_active = TRUE`` AND ``source = 'auto_extracted'``
   AND ``pinned = TRUE`` (owner explicitly pinned).
5. ``status='approved'`` WHERE ``is_active = TRUE`` AND ``source = 'auto_extracted'``
   AND ``updated_at - created_at > 60 seconds`` (owner edited after capture —
   treat the edit as implicit approval).
6. *Implicit default* ``status='draft'`` for remaining ``auto_extracted`` rows
   left untouched by rules 1-5 (the server_default takes effect during the
   ``ADD COLUMN``).

Index changes
-------------
Drops ``idx_procedural_skills_active_user`` + ``idx_procedural_skills_tier_active``
(both reference ``is_active``) and recreates them against the new ``status``
column. The single-column ``is_active`` index (auto-created from
``index=True`` in the model) is also dropped.
"""
from alembic import op
import sqlalchemy as sa


revision = "pc20260527_skill_approval_status"
down_revision = "pc20260526_curator"
branch_labels = None
depends_on = None


SKILL_STATUS_VALUES = ("draft", "approved", "rejected", "archived")


def upgrade() -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name if bind is not None else "postgresql"

    # ---- 1. Add the status column (default 'draft') ----------------------
    op.add_column(
        "procedural_skills",
        sa.Column(
            "status",
            sa.String(20),
            nullable=False,
            server_default="draft",
        ),
    )

    # CHECK constraint enforcing the enum. Skipped on sqlite (test harness)
    # because SQLAlchemy's batch mode would force a table rebuild; the
    # constraint is documentational there.
    if dialect == "postgresql":
        op.create_check_constraint(
            "ck_procedural_skills_status",
            "procedural_skills",
            "status IN ('draft', 'approved', 'rejected', 'archived')",
        )

    # ---- 2. Backfill (6 rules — see module docstring) --------------------
    # Order matters only insofar as we want rule 1 (archived) to win for
    # rows that were soft-deleted; for is_active=TRUE rows the per-source
    # WHERE clauses don't overlap, so order is irrelevant within 2-5.
    op.execute(
        "UPDATE procedural_skills SET status = 'archived' "
        "WHERE is_active = FALSE"
    )
    op.execute(
        "UPDATE procedural_skills SET status = 'approved' "
        "WHERE is_active = TRUE AND source = 'seed'"
    )
    op.execute(
        "UPDATE procedural_skills SET status = 'approved' "
        "WHERE is_active = TRUE AND source = 'user_created'"
    )
    op.execute(
        "UPDATE procedural_skills SET status = 'approved' "
        "WHERE is_active = TRUE AND source = 'auto_extracted' AND pinned = TRUE"
    )
    if dialect == "postgresql":
        op.execute(
            "UPDATE procedural_skills SET status = 'approved' "
            "WHERE is_active = TRUE AND source = 'auto_extracted' "
            "AND (updated_at - created_at) > INTERVAL '60 seconds'"
        )
    else:
        # sqlite: julianday-difference in days; 60 s = 60/86400 days.
        op.execute(
            "UPDATE procedural_skills SET status = 'approved' "
            "WHERE is_active = TRUE AND source = 'auto_extracted' "
            "AND (julianday(updated_at) - julianday(created_at)) > "
            "(60.0 / 86400.0)"
        )

    # ---- 3. Drop old indexes that referenced is_active -------------------
    op.drop_index(
        "idx_procedural_skills_active_user",
        table_name="procedural_skills",
    )
    op.drop_index(
        "idx_procedural_skills_tier_active",
        table_name="procedural_skills",
    )
    # Single-column index auto-created by `index=True` on the model column.
    # Wrapped in IF EXISTS because sqlite never auto-creates it.
    if dialect == "postgresql":
        op.execute("DROP INDEX IF EXISTS ix_procedural_skills_is_active")

    # ---- 4. Drop the legacy is_active column -----------------------------
    op.drop_column("procedural_skills", "is_active")

    # ---- 5. Recreate composite indexes against status --------------------
    op.create_index(
        "idx_procedural_skills_status_user",
        "procedural_skills",
        ["status", "user_id"],
    )
    op.create_index(
        "idx_procedural_skills_tier_status",
        "procedural_skills",
        ["circle_tier", "status"],
    )

    # ---- 6. skill_curator_runs table (audit row per curator invocation) --
    op.create_table(
        "skill_curator_runs",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column(
            "started_at",
            sa.DateTime,
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("finished_at", sa.DateTime, nullable=True),
        sa.Column(
            "run_type",
            sa.String(20),
            nullable=False,
            server_default="scheduled",
        ),
        sa.Column(
            "triggered_by_user_id",
            sa.Integer,
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "status",
            sa.String(20),
            nullable=False,
            server_default="running",
        ),
        sa.Column("skills_examined", sa.Integer, nullable=False, server_default="0"),
        sa.Column("duplicate_pairs_found", sa.Integer, nullable=False, server_default="0"),
        sa.Column("duplicate_pairs_merged", sa.Integer, nullable=False, server_default="0"),
        sa.Column("stale_skills_archived", sa.Integer, nullable=False, server_default="0"),
        sa.Column("duration_seconds", sa.Float, nullable=True),
        sa.Column("error_message", sa.Text, nullable=True),
    )
    op.create_index(
        "idx_skill_curator_runs_started",
        "skill_curator_runs",
        ["started_at"],
    )
    if dialect == "postgresql":
        op.create_check_constraint(
            "ck_skill_curator_runs_run_type",
            "skill_curator_runs",
            "run_type IN ('scheduled', 'manual')",
        )
        op.create_check_constraint(
            "ck_skill_curator_runs_status",
            "skill_curator_runs",
            "status IN ('running', 'success', 'partial', 'failed')",
        )

    # ---- 7. skill_would_have_injected_log (recall-delta shadow log) ------
    # During the rollout we run a dual query: the production retrieval uses
    # status='approved' only; the shadow query relaxes the filter to also
    # include 'draft'/'rejected'/'archived' rows that would have been
    # candidates. We log the shadow-only matches so we can measure how much
    # recall the draft-gate costs in practice. After rollout (~30 days) the
    # table can be archived or truncated.
    op.create_table(
        "skill_would_have_injected_log",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column(
            "skill_id",
            sa.Integer,
            sa.ForeignKey("procedural_skills.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "user_id",
            sa.Integer,
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "conversation_id",
            sa.Integer,
            sa.ForeignKey("conversations.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("similarity_score", sa.Float, nullable=False),
        sa.Column("status_at_query", sa.String(20), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime,
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index(
        "idx_skill_would_have_skill",
        "skill_would_have_injected_log",
        ["skill_id"],
    )
    op.create_index(
        "idx_skill_would_have_created",
        "skill_would_have_injected_log",
        ["created_at"],
    )


def downgrade() -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name if bind is not None else "postgresql"

    # Drop new tables first (no FK in from anything else yet).
    op.drop_index(
        "idx_skill_would_have_created",
        table_name="skill_would_have_injected_log",
    )
    op.drop_index(
        "idx_skill_would_have_skill",
        table_name="skill_would_have_injected_log",
    )
    op.drop_table("skill_would_have_injected_log")

    op.drop_index(
        "idx_skill_curator_runs_started",
        table_name="skill_curator_runs",
    )
    op.drop_table("skill_curator_runs")

    # Drop new composite indexes.
    op.drop_index(
        "idx_procedural_skills_tier_status",
        table_name="procedural_skills",
    )
    op.drop_index(
        "idx_procedural_skills_status_user",
        table_name="procedural_skills",
    )

    # Re-add is_active column and reconstruct it from status.
    op.add_column(
        "procedural_skills",
        sa.Column(
            "is_active",
            sa.Boolean,
            nullable=False,
            server_default=sa.true(),
        ),
    )
    # archived/rejected → is_active = FALSE; draft/approved → TRUE.
    op.execute(
        "UPDATE procedural_skills SET is_active = FALSE "
        "WHERE status IN ('archived', 'rejected')"
    )
    op.execute(
        "UPDATE procedural_skills SET is_active = TRUE "
        "WHERE status IN ('draft', 'approved')"
    )

    # Recreate the legacy indexes.
    if dialect == "postgresql":
        op.execute(
            "CREATE INDEX ix_procedural_skills_is_active "
            "ON procedural_skills (is_active)"
        )
    op.create_index(
        "idx_procedural_skills_active_user",
        "procedural_skills",
        ["is_active", "user_id"],
    )
    op.create_index(
        "idx_procedural_skills_tier_active",
        "procedural_skills",
        ["circle_tier", "is_active"],
    )

    # Drop status column + its CHECK constraint.
    if dialect == "postgresql":
        op.drop_constraint(
            "ck_procedural_skills_status",
            "procedural_skills",
            type_="check",
        )
    op.drop_column("procedural_skills", "status")
