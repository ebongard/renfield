"""kg_relations validity interval — #875 Stufe 1

Adds ``valid_from`` / ``valid_to`` and ``invalidated_by_relation_id`` to
``kg_relations`` plus the partial index ``idx_kg_relations_live``
(docs/design/kg-bitemporal-edges.md §5, §5.1, §12.1).

🛑 NO BACKFILL. Existing edges keep ``valid_from = NULL`` ("holds, start
unknown"). Filling it from ``created_at`` would invent a fact: the extraction
time is not the time the fact began to hold, and that distinction is the whole
point of #875. No ``NOT NULL``, no ``DEFAULT now()`` — for the same reason.

Purely additive: nothing reads the columns unless
``KG_VALIDITY_FILTER_ENABLED`` is on, and nothing writes ``valid_to`` in
Stufe 1, so behaviour is unchanged either way.

Idempotent: columns and FK are only added when missing — a database
bootstrapped via ``Base.metadata.create_all`` already has them from the model.
The index is (re)built CONCURRENTLY inside ``autocommit_block`` after a
defensive ``DROP INDEX CONCURRENTLY IF EXISTS``: ``IF NOT EXISTS`` matches an
INVALID index left by a failed concurrent build and would keep it forever
(same reasoning as pc20260528).

Revision ID: pc20260929_kg_validity
Revises: pc20260927_voice2fa
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "pc20260929_kg_validity"
down_revision: str | None = "pc20260927_voice2fa"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "kg_relations"
_FK = "fk_kg_relations_invalidated_by"
_INDEX = "idx_kg_relations_live"


# information_schema against current_schema(), not sa.inspect(): the inspector
# resolves an unqualified table in the dialect's DEFAULT schema, not the session's
# search_path, and would inspect the wrong table wherever the two differ.
def _columns(conn) -> set[str]:
    rows = conn.execute(sa.text(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema = current_schema() AND table_name = :t"), {"t": _TABLE})
    return {r[0] for r in rows}


def _fks_on(conn, column: str) -> set[str]:
    """Names of the foreign keys constraining exactly ``column``."""
    rows = conn.execute(sa.text(
        "SELECT tc.constraint_name FROM information_schema.table_constraints tc "
        "JOIN information_schema.key_column_usage k "
        "  ON k.constraint_name = tc.constraint_name AND k.table_schema = tc.table_schema "
        "WHERE tc.table_schema = current_schema() AND tc.table_name = :t "
        "AND tc.constraint_type = 'FOREIGN KEY' AND k.column_name = :c"),
        {"t": _TABLE, "c": column})
    return {r[0] for r in rows}


def upgrade() -> None:
    conn = op.get_bind()
    if conn.dialect.name != "postgresql":
        # The sqlite test harness builds its schema from the models.
        return

    cols = _columns(conn)
    if "valid_from" not in cols:
        op.add_column(_TABLE, sa.Column("valid_from", sa.DateTime(), nullable=True))
    if "valid_to" not in cols:
        op.add_column(_TABLE, sa.Column("valid_to", sa.DateTime(), nullable=True))
    if "invalidated_by_relation_id" not in cols:
        op.add_column(
            _TABLE, sa.Column("invalidated_by_relation_id", sa.Integer(), nullable=True)
        )
    # create_all names the self-FK after its own convention; only add ours when
    # NO foreign key covers the column yet, so a create_all database does not
    # end up with two.
    if not _fks_on(conn, "invalidated_by_relation_id"):
        op.create_foreign_key(
            _FK, _TABLE, _TABLE,
            ["invalidated_by_relation_id"], ["id"], ondelete="SET NULL",
        )

    with op.get_context().autocommit_block():
        op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {_INDEX}")
        op.execute(
            f"CREATE INDEX CONCURRENTLY {_INDEX} ON {_TABLE} (subject_id, predicate) "
            "WHERE is_active = true AND valid_to IS NULL"
        )


def downgrade() -> None:
    conn = op.get_bind()
    if conn.dialect.name != "postgresql":
        return

    with op.get_context().autocommit_block():
        op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {_INDEX}")

    if _FK in _fks_on(conn, "invalidated_by_relation_id"):
        op.drop_constraint(_FK, _TABLE, type_="foreignkey")
    cols = _columns(conn)
    # Dropping a column drops any FK constraint on it (the create_all-named one
    # included), so the order above only matters for our own named FK.
    for col in ("invalidated_by_relation_id", "valid_to", "valid_from"):
        if col in cols:
            op.drop_column(_TABLE, col)
