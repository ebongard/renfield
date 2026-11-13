"""peer_users.remote_instance_name + its snapshot on federation_query_log.

A pairing carried one name: ``remote_display_name``, the PERSON who paired on
the far side. With one person owning both instances, each side showed the other
under that person's name. The far instance now sends a name of its own in a
separately signed attachment (``services/federation_instance_name.py``); this
is where the receiving side keeps it.

``federation_query_log.peer_instance_name_snapshot`` is the audit counterpart:
the instance part of the label AT THE TIME of the query, next to the existing
``peer_display_name_snapshot`` and with the same snapshot semantics.

Both columns are nullable with no default and no backfill. NULL is the truth
for every existing row -- the far side has not told us yet, and a log row from
before today never recorded it. Nothing is derived, so the way back is exact
for everything that existed before: the columns go, the rows stay.

Revision ID: pc20261113_peer_instance_name
Revises: pc20261111_push_subs
Create Date: 2026-11-13
"""
import sqlalchemy as sa
from alembic import op

revision: str = "pc20261113_peer_instance_name"
down_revision: str | None = "pc20261111_push_subs"
branch_labels: None = None
depends_on: None = None

#: (table, column) -- in the order the upgrade adds them.
COLUMNS: tuple[tuple[str, str], ...] = (
    ("peer_users", "remote_instance_name"),
    ("federation_query_log", "peer_instance_name_snapshot"),
)


def _has_column(table: str, column: str) -> bool:
    """A fresh install already has both: the baseline builds the tables from
    the current models, and this migration then runs on top of that."""
    inspector = sa.inspect(op.get_bind())
    if table not in inspector.get_table_names():
        return False
    return column in {c["name"] for c in inspector.get_columns(table)}


def upgrade() -> None:
    for table, column in COLUMNS:
        if not _has_column(table, column):
            op.add_column(table, sa.Column(column, sa.String(64), nullable=True))


def downgrade() -> None:
    # The names learned since the upgrade are lost; they are held nowhere else.
    # The far side sends its name again with the next query or ping after a
    # later upgrade, but the log snapshots of the meantime do not come back.
    for table, column in reversed(COLUMNS):
        if _has_column(table, column):
            op.drop_column(table, column)
