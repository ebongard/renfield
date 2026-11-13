"""Index deleted_conversation_sessions.deleted_at for the retention prune (#1529).

``CONVERSATION_TOMBSTONE_RETENTION_DAYS`` lets a daily task delete the records
older than the window (``services/conversation_tombstones.py``); that is a
range delete over ``deleted_at``, which had no index.

The model declares the same index (``index=True``), so a fresh install's
baseline (``create_all`` from the current models) and a development checkout
already have it -- hence ``IF NOT EXISTS``. No data is touched in either
direction.

Revision ID: pc20261111_tombstone_idx
Revises: pc20261110_routing_traces
Create Date: 2026-11-11
"""
import sqlalchemy as sa
from alembic import op

from services.conversation_tombstones import DELETED_AT_INDEX, TABLE

revision: str = "pc20261111_tombstone_idx"
down_revision: str | None = "pc20261110_routing_traces"
branch_labels: None = None
depends_on: None = None


def _schema() -> str:
    """The schema this migration runs in, quoted.

    Both statements are qualified with it: an unqualified ``DROP INDEX`` would
    remove a same-named index further down the search_path when the first
    schema has none (see ``pc20261106_conversation_tombstones._has_table``).
    """
    return op.get_bind().execute(sa.text("SELECT quote_ident(current_schema())")).scalar()


def upgrade() -> None:
    op.execute(
        f"CREATE INDEX IF NOT EXISTS {DELETED_AT_INDEX} ON {_schema()}.{TABLE} (deleted_at)"
    )


def downgrade() -> None:
    op.execute(f"DROP INDEX IF EXISTS {_schema()}.{DELETED_AT_INDEX}")
