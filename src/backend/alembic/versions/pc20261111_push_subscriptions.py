"""push_subscriptions -- one row per browser Web Push subscription (#79).

Written by ``POST /api/push/subscribe`` while Web Push is active
(``services.push_preconditions``); read by the sender in
``services.push_service``. Bound to the user, unique per endpoint (the
endpoint is the device). New table, no backfill.

Revision ID: pc20261111_push_subs
Revises: pc20261110_routing_traces
Create Date: 2026-11-11
"""
import sqlalchemy as sa
from alembic import op

revision: str = "pc20261111_push_subs"
down_revision: str | None = "pc20261110_routing_traces"
branch_labels: None = None
depends_on: None = None

TABLE = "push_subscriptions"


def _has_table() -> bool:
    """A fresh install's baseline runs ``create_all`` from the CURRENT models and
    already built it. Qualified with current_schema(), as in
    ``pc20261110_routing_traces``."""
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
            nullable=False,
        ),
        sa.Column("endpoint", sa.Text, nullable=False),
        sa.Column("p256dh", sa.String(255), nullable=False),
        sa.Column("auth", sa.String(255), nullable=False),
        sa.Column("device_label", sa.String(255), nullable=True),
        sa.Column("created_at", sa.DateTime, nullable=False),
    )
    op.create_index("uq_push_subscriptions_endpoint", TABLE, ["endpoint"], unique=True)
    op.create_index("idx_push_subscriptions_user", TABLE, ["user_id"])


def downgrade() -> None:
    # The indexes go with the table. The rows are not reconstructible: after a
    # downgrade every browser has to subscribe again.
    if _has_table():
        op.drop_table(TABLE)
