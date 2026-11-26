"""kg_entity_contact_points: phone/mobile/fax as attributes of KG entities (#1240).

A number is an attribute of a person or organization, never a node
(docs/design/kg-contact-points.md §3). New table, no backfill: stage 1 only
stores what an owner enters by hand.

Revision ID: pc20261126_kg_contact_points
Revises: pc20261124_kg_edge_tier_cap
Create Date: 2026-11-26
"""
import sqlalchemy as sa
from alembic import op

revision: str = "pc20261126_kg_contact_points"
down_revision: str | None = "pc20261124_kg_edge_tier_cap"
branch_labels: None = None
depends_on: None = None

TABLE = "kg_entity_contact_points"


def _has_table(name: str) -> bool:
    """A fresh install's baseline runs ``create_all`` and already has the table."""
    return name in sa.inspect(op.get_bind()).get_table_names()


def upgrade() -> None:
    if _has_table(TABLE):
        return
    op.create_table(
        TABLE,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("entity_id", sa.Integer, sa.ForeignKey("kg_entities.id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", sa.Integer, sa.ForeignKey("users.id"), nullable=True),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("label", sa.String(16), nullable=True),
        sa.Column("value", sa.Text, nullable=False),
        sa.Column("normalized", sa.Text, nullable=False),
        sa.Column("source_type", sa.String(16), nullable=False),
        sa.Column("source_ref", sa.Text, nullable=True),
        sa.Column("document_id", sa.Integer, sa.ForeignKey("documents.id", ondelete="CASCADE"), nullable=True),
        sa.Column("confidence", sa.Float, nullable=True),
        sa.Column("source_tier", sa.Integer, nullable=False, server_default="0"),
        sa.Column("circle_tier", sa.Integer, nullable=False, server_default="0"),
        sa.Column("valid_from", sa.DateTime, nullable=True),
        sa.Column("valid_to", sa.DateTime, nullable=True),
        sa.Column(
            "superseded_by_id", sa.Integer,
            sa.ForeignKey(f"{TABLE}.id", ondelete="SET NULL"), nullable=True,
        ),
        sa.Column("created_by_user_id", sa.Integer, sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_at", sa.DateTime, nullable=False, server_default=sa.text("now()")),
    )
    op.create_index("ix_kg_entity_contact_points_entity_id", TABLE, ["entity_id"])
    op.create_index("ix_kg_entity_contact_points_user_id", TABLE, ["user_id"])
    op.create_index("ix_kg_entity_contact_points_document_id", TABLE, ["document_id"])
    op.create_index("idx_contact_points_normalized", TABLE, ["normalized"])
    op.create_index(
        "uq_contact_points_live", TABLE, ["entity_id", "kind", "normalized"],
        unique=True, postgresql_where=sa.text("valid_to IS NULL"),
    )


def downgrade() -> None:
    op.drop_table(TABLE)
