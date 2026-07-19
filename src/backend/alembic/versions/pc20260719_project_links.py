"""project links — Phase 4A: project_id on meetings + conversations

Revision ID: pc20260719_project_links
Revises: pc20260718_meeting_minutes
Create Date: 2026-07-19

Adds a nullable ``project_id`` FK (ON DELETE SET NULL) to ``meetings`` and
``conversations`` so a meeting / chat can be scoped to a Project and surface on
its ``/projects/{id}`` timeline (documents already reach a project via its 1:1
KB). SET NULL mirrors the owner/KB FK convention — deleting a project never
blocks; the row just de-scopes.

Idempotency: ``Base.metadata.create_all`` (backend boot) already carries these
columns for a fresh install, so every op is inspector-guarded to a no-op re-run
(same pattern as pc20260713_projects). Fully transactional
(transaction_per_migration).
"""
import sqlalchemy as sa
from alembic import op

revision = "pc20260719_project_links"
down_revision = "pc20260718_meeting_minutes"
branch_labels = None
depends_on = None


def _cols(inspector, table: str) -> set[str]:
    return {c["name"] for c in inspector.get_columns(table)}


def _idx(inspector, table: str) -> set[str]:
    return {ix["name"] for ix in inspector.get_indexes(table)}


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())

    for table in ("meetings", "conversations"):
        if table not in tables:
            continue  # create_all will build it with the column already present
        if "project_id" not in _cols(inspector, table):
            op.add_column(
                table,
                sa.Column(
                    "project_id", sa.Integer(),
                    sa.ForeignKey("projects.id", ondelete="SET NULL"), nullable=True,
                ),
            )
        idx = f"ix_{table}_project_id"
        if idx not in _idx(inspector, table):
            op.create_index(idx, table, ["project_id"])


def downgrade() -> None:
    for table in ("conversations", "meetings"):
        op.drop_index(f"ix_{table}_project_id", table_name=table)
        op.drop_column(table, "project_id")
