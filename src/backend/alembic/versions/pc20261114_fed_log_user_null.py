"""federation_query_log.user_id becomes nullable -- for revocation rows only.

A revocation record (``direction='revoke'``: a pairing or a person link was
revoked) carries no ``user_id``. Those rows are never pruned, and the column's
foreign key onto ``users.id`` has no delete rule -- a row filed under an
account would make that account undeletable for good. The accounts concerned
are named in the row's ``query_text`` (JSON of ids) instead.

The NOT NULL goes, and a CHECK takes its place for every other row:
``direction = 'revoke' OR user_id IS NOT NULL``. A query row without an
account stays impossible in the database, not merely unwritten by the code.

The foreign key and its (absent) delete rule stay: query rows keep their
account, and an account that still has query rows is refused on delete like
any other that holds data.

No backfill, nothing derived: every existing row keeps its ``user_id`` and
satisfies the CHECK as it stands.

The way back is NOT always open, and says so. ``SET NOT NULL`` cannot hold
while rows without a ``user_id`` exist, and which account such a row "belonged
to" is not something the column ever recorded. The downgrade therefore refuses
while there are any -- before it changes anything -- naming the count;
deleting an audit trail is a decision for a person, not for a migration.

Revision ID: pc20261114_fed_log_user_null
Revises: pc20261113_peer_instance_name
Create Date: 2026-11-14
"""
import sqlalchemy as sa
from alembic import op

revision: str = "pc20261114_fed_log_user_null"
down_revision: str | None = "pc20261113_peer_instance_name"
branch_labels: None = None
depends_on: None = None

TABLE = "federation_query_log"
COLUMN = "user_id"
#: Same name and expression as on the model (`FederationQueryLog`): a fresh
#: install gets the constraint from there, an existing one from here.
CHECK_NAME = "ck_fed_query_log_user_or_revoke"
CHECK_SQL = "direction = 'revoke' OR user_id IS NOT NULL"


def _is_nullable() -> bool | None:
    """Nullability of the column in THIS schema; ``None`` when table or column
    is not there. Qualified with ``current_schema()``, as in
    ``pc20261113_peer_instance_name``: the unqualified DDL below follows the
    search path, and the check has to look at the same table."""
    value = op.get_bind().execute(sa.text(
        "SELECT is_nullable FROM information_schema.columns"
        " WHERE table_schema = current_schema() AND table_name = :t AND column_name = :c"
    ), {"t": TABLE, "c": COLUMN}).scalar()
    return None if value is None else value == "YES"


def _has_check() -> bool:
    """Is the CHECK on the table of THIS schema?"""
    return bool(op.get_bind().execute(sa.text(
        "SELECT EXISTS (SELECT 1 FROM pg_constraint c"
        " JOIN pg_class t ON t.oid = c.conrelid"
        " JOIN pg_namespace n ON n.oid = t.relnamespace"
        " WHERE c.contype = 'c' AND c.conname = :name"
        "   AND t.relname = :t AND n.nspname = current_schema())"
    ), {"name": CHECK_NAME, "t": TABLE}).scalar())


def upgrade() -> None:
    nullable = _is_nullable()
    if nullable is None:
        # The table exists on every database at `down_revision`. Without it in
        # this schema, an unqualified ALTER could only reach a table of the
        # same name further down the search path -- not ours.
        raise RuntimeError(
            f"{revision}: {TABLE}.{COLUMN} is missing in the current schema; "
            "refusing to alter a table of that name elsewhere on the search path"
        )
    # The CHECK first: at no point is the column nullable without it. Both
    # steps are skipped on a fresh install, where the baseline built the table
    # from the current model.
    if not _has_check():
        op.create_check_constraint(CHECK_NAME, TABLE, CHECK_SQL)
    if not nullable:
        op.alter_column(TABLE, COLUMN, existing_type=sa.Integer(), nullable=True)


def downgrade() -> None:
    nullable = _is_nullable()
    if nullable is None:
        # Not ours: nothing to take back.
        return
    if nullable:
        orphans = op.get_bind().execute(sa.text(
            f"SELECT count(*) FROM {TABLE} WHERE {COLUMN} IS NULL"
        )).scalar()
        if orphans:
            raise RuntimeError(
                f"{revision}: {orphans} row(s) in {TABLE} carry no {COLUMN} "
                "(revocation records). NOT NULL cannot be restored while they exist, "
                "and no account can be derived for them. Export and delete them "
                f"deliberately (DELETE FROM {TABLE} WHERE {COLUMN} IS NULL), then "
                "run the downgrade again."
            )
        # NOT NULL back first, the CHECK after: never nullable without one.
        op.alter_column(TABLE, COLUMN, existing_type=sa.Integer(), nullable=False)
    if _has_check():
        op.drop_constraint(CHECK_NAME, TABLE, type_="check")
