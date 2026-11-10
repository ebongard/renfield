"""routing_traces -- one row per agent-router decision (#1524).

Backs ``GET /api/admin/routing-traces`` and ``/routing-stats``. Filled only
while ``ROUTING_TRACES_ENABLED`` is on; rows hold the full user message and are
purged after ``ROUTING_TRACES_RETENTION_DAYS``. New table, no backfill.

Revision ID: pc20261110_routing_traces
Revises: pc20261108_chat_pl_intent
Create Date: 2026-11-10
"""
import sqlalchemy as sa
from alembic import op

revision: str = "pc20261110_routing_traces"
down_revision: str | None = "pc20261108_chat_pl_intent"
branch_labels: None = None
depends_on: None = None

TABLE = "routing_traces"


def _has_table() -> bool:
    """A fresh install's baseline runs ``create_all`` from the CURRENT models and
    already built it. Qualified with current_schema(), as in
    ``pc20261106_conversation_tombstones``."""
    return bool(op.get_bind().execute(sa.text(
        "SELECT to_regclass(quote_ident(current_schema()) || '.' || :t) IS NOT NULL"
    ), {"t": TABLE}).scalar())


def upgrade() -> None:
    if _has_table():
        return
    op.create_table(
        TABLE,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column(
            "user_id",
            sa.Integer,
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("message", sa.Text, nullable=False),
        sa.Column("domain", sa.String(64), nullable=False),
        sa.Column("sub_intent", sa.String(64), nullable=True),
        sa.Column("layer", sa.String(20), nullable=False),
        sa.Column("confidence", sa.Float, nullable=True),
        sa.Column("entity_matches", sa.JSON, nullable=True),
        sa.Column("created_at", sa.DateTime, nullable=False),
    )
    op.create_index("idx_routing_traces_created", TABLE, ["created_at"])
    op.create_index("idx_routing_traces_domain_created", TABLE, ["domain", "created_at"])


def downgrade() -> None:
    # The indexes go with the table.
    if _has_table():
        op.drop_table(TABLE)
