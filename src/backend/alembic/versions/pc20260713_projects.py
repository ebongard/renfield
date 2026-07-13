"""projects — business-instance Phase 1 (minimal Project model + 1:1 KB)

Revision ID: pc20260713_projects
Revises: pc20260712_federation_user_links
Create Date: 2026-07-13

A lightweight ``projects`` table: each row owns exactly ONE KnowledgeBase via
``knowledge_base_id`` (1:1, enforced in services/project_service.py). Gated by
``settings.projects_enabled``; the household instance leaves the feature off.
Design: the business-instance plan (§3 + §7.1).

Idempotency: every DDL op is guarded — Base.metadata.create_all (backend boot)
creates this table for a new model, so re-running this migration must be a no-op
(same pattern as pc20260712_federation_user_links). Fully transactional
(transaction_per_migration).
"""
import sqlalchemy as sa
from alembic import op

revision = "pc20260713_projects"
down_revision = "pc20260712_federation_user_links"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing_tables = set(inspector.get_table_names())

    if "projects" not in existing_tables:
        op.create_table(
            "projects",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("name", sa.String(255), nullable=False),
            sa.Column("description", sa.Text(), nullable=True),
            # SET NULL so deleting a user demotes the project to unowned rather
            # than blocking the delete (mirrors knowledge_bases.owner_id semantics).
            sa.Column(
                "owner_id", sa.Integer(),
                sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True,
            ),
            # The project's dedicated KnowledgeBase (1:1). SET NULL so dropping a
            # KB out of band never blocks; the service always links a fresh KB.
            sa.Column(
                "knowledge_base_id", sa.Integer(),
                sa.ForeignKey("knowledge_bases.id", ondelete="SET NULL"), nullable=True,
            ),
            sa.Column("circle_tier", sa.Integer(), nullable=False, server_default="2"),
            sa.Column("status", sa.String(50), nullable=False, server_default="active"),
            sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        )

    def _has_idx(idx_name: str) -> bool:
        if "projects" not in existing_tables:
            return False
        return idx_name in {ix["name"] for ix in inspector.get_indexes("projects")}

    if not _has_idx("ix_projects_owner_id"):
        op.create_index("ix_projects_owner_id", "projects", ["owner_id"])
    if not _has_idx("ix_projects_knowledge_base_id"):
        op.create_index("ix_projects_knowledge_base_id", "projects", ["knowledge_base_id"])


def downgrade() -> None:
    op.drop_index("ix_projects_knowledge_base_id", table_name="projects")
    op.drop_index("ix_projects_owner_id", table_name="projects")
    op.drop_table("projects")
