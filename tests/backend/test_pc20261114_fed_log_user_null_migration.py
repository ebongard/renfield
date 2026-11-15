"""Real-Postgres round trip for ``pc20261114_fed_log_user_null``.

Harness as in ``test_pc20261113_peer_instance_name_migration.py``: a throwaway
schema that is the ONLY one on the search path, holding the tables in their
pre-migration shape, and the real ``upgrade()`` / ``downgrade()`` bound to a
MigrationContext.

One column loses its NOT NULL. What is checked: the shape before and after,
that the foreign key and its delete rule are untouched, that existing rows keep
their account, the reverse path (forward -> reverse -> forward) with row
counts, and that the reverse REFUSES while rows without an account exist --
it cannot restore NOT NULL over them and must not delete an audit trail to get
there.
"""

from __future__ import annotations

import importlib.util
import uuid
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

pytestmark = [pytest.mark.database]

REVISION = "pc20261114_fed_log_user_null"


def _versions_dir() -> Path:
    import services.database as _db

    return Path(_db.__file__).resolve().parents[1] / "alembic" / "versions"


def _load_migration():
    path = _versions_dir() / f"{REVISION}.py"
    spec = importlib.util.spec_from_file_location(f"{REVISION}_under_test", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.fixture
async def schema_conn(pg_url):
    """A connection that sees ONE schema: a throwaway one. `public` is not on
    the search path, so nothing here can reflect or alter the shared tables."""
    schema = f"pcun_{uuid.uuid4().hex[:12]}"
    admin = create_async_engine(pg_url, poolclass=NullPool, future=True)
    async with admin.connect() as setup:
        await setup.exec_driver_sql(f'CREATE SCHEMA "{schema}"')
        await setup.commit()
    engine = create_async_engine(
        pg_url, poolclass=NullPool, future=True,
        connect_args={"server_settings": {"search_path": f'"{schema}"'}},
    )
    conn = await engine.connect()
    try:
        assert (await conn.execute(text("SELECT current_schema()"))).scalar() == schema
        await conn.commit()
        yield conn, schema
    finally:
        try:
            await conn.rollback()
        except Exception:
            pass
        await conn.close()
        await engine.dispose()
        try:
            async with admin.connect() as teardown:
                await teardown.exec_driver_sql(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
                await teardown.commit()
        finally:
            await admin.dispose()


async def _run(conn, direction: str) -> None:
    from alembic.operations import Operations
    from alembic.runtime.migration import MigrationContext

    mig = _load_migration()

    def _apply(sync_conn):
        ctx = MigrationContext.configure(connection=sync_conn, opts={"transaction_per_migration": True})
        prev = mig.op
        mig.op = Operations(ctx)
        try:
            with ctx.begin_transaction(_per_migration=True):
                getattr(mig, direction)()
        finally:
            mig.op = prev

    await conn.commit()
    await conn.run_sync(_apply)
    await conn.commit()


async def _pre_migration_tables(conn) -> None:
    """`users` and the log as they stood at ``pc20261113_peer_instance_name``,
    reduced to the columns that matter here: two accounts, one query row."""
    await conn.exec_driver_sql("CREATE TABLE users (id SERIAL PRIMARY KEY, username VARCHAR(64))")
    await conn.exec_driver_sql(
        "CREATE TABLE federation_query_log ("
        " id SERIAL PRIMARY KEY,"
        " user_id INTEGER NOT NULL REFERENCES users(id),"
        " direction VARCHAR(8) NOT NULL DEFAULT 'asked',"
        " query_text TEXT NOT NULL)"
    )
    await conn.exec_driver_sql("INSERT INTO users (username) VALUES ('first'), ('second')")
    await conn.exec_driver_sql(
        "INSERT INTO federation_query_log (user_id, query_text) VALUES (1, 'an old question')"
    )
    await conn.commit()


async def _nullable(conn, schema) -> str | None:
    return (await conn.execute(text(
        "SELECT is_nullable FROM information_schema.columns"
        " WHERE table_schema = :s AND table_name = 'federation_query_log'"
        "   AND column_name = 'user_id'"
    ), {"s": schema})).scalar()


async def _delete_rules(conn, schema) -> list[str]:
    """`confdeltype` of every foreign key on the log: 'a' = NO ACTION."""
    rows = (await conn.execute(text(
        "SELECT c.confdeltype::text FROM pg_constraint c"
        " JOIN pg_class t ON t.oid = c.conrelid"
        " JOIN pg_namespace n ON n.oid = t.relnamespace"
        " WHERE c.contype = 'f' AND t.relname = 'federation_query_log' AND n.nspname = :s"
    ), {"s": schema})).all()
    return [r[0] for r in rows]


async def _check(conn, schema) -> str | None:
    """The definition of the CHECK on the log in this schema, if it is there."""
    return (await conn.execute(text(
        "SELECT pg_get_constraintdef(c.oid) FROM pg_constraint c"
        " JOIN pg_class t ON t.oid = c.conrelid"
        " JOIN pg_namespace n ON n.oid = t.relnamespace"
        " WHERE c.contype = 'c' AND c.conname = 'ck_fed_query_log_user_or_revoke'"
        "   AND t.relname = 'federation_query_log' AND n.nspname = :s"
    ), {"s": schema})).scalar()


async def _insert_query_without_account(conn) -> None:
    await conn.exec_driver_sql(
        "INSERT INTO federation_query_log (user_id, direction, query_text)"
        " VALUES (NULL, 'asked', 'nobody asked this')"
    )
    await conn.commit()


async def _rows(conn) -> list[tuple]:
    return [tuple(r) for r in (await conn.execute(text(
        "SELECT user_id, direction, query_text FROM federation_query_log ORDER BY id"
    ))).all()]


async def _insert_revocation(conn) -> None:
    await conn.exec_driver_sql(
        "INSERT INTO federation_query_log (user_id, direction, query_text)"
        " VALUES (NULL, 'revoke', '{}')"
    )
    await conn.commit()


async def test_round_trip_keeps_every_row_and_the_foreign_key(schema_conn):
    conn, schema = schema_conn
    await _pre_migration_tables(conn)
    assert await _nullable(conn, schema) == "NO"
    assert await _check(conn, schema) is None
    assert await _delete_rules(conn, schema) == ["a"]
    with pytest.raises(IntegrityError):
        await _insert_revocation(conn)
    await conn.rollback()

    # --- forward ---------------------------------------------------------
    await _run(conn, "upgrade")
    assert await _nullable(conn, schema) == "YES"
    definition = await _check(conn, schema)
    assert definition is not None and "revoke" in definition and "user_id IS NOT NULL" in definition
    # The CHECK took the place of the NOT NULL for every row that is a query.
    with pytest.raises(IntegrityError, match="ck_fed_query_log_user_or_revoke"):
        await _insert_query_without_account(conn)
    await conn.rollback()
    # Only the NOT NULL went: same foreign key, same (absent) delete rule.
    assert await _delete_rules(conn, schema) == ["a"]
    assert await _rows(conn) == [(1, "asked", "an old question")]
    with pytest.raises(IntegrityError):
        await conn.exec_driver_sql("DELETE FROM users WHERE id = 1")   # still holds a query row
    await conn.rollback()

    # --- reverse, with nothing in the way ---------------------------------
    await _run(conn, "downgrade")
    assert await _nullable(conn, schema) == "NO"
    assert await _check(conn, schema) is None
    assert await _delete_rules(conn, schema) == ["a"]
    assert await _rows(conn) == [(1, "asked", "an old question")]

    # --- forward again ---------------------------------------------------
    await _run(conn, "upgrade")
    assert await _nullable(conn, schema) == "YES"
    assert await _check(conn, schema) is not None
    assert await _rows(conn) == [(1, "asked", "an old question")]
    await _insert_revocation(conn)
    assert await _rows(conn) == [(1, "asked", "an old question"), (None, "revoke", "{}")]
    # An account named only inside such a row is not pinned by it.
    await conn.exec_driver_sql("DELETE FROM users WHERE id = 2")
    await conn.commit()


async def test_downgrade_refuses_while_rows_without_an_account_exist(schema_conn):
    """NOT NULL cannot hold over them, no account can be derived for them, and
    deleting an audit trail is not a migration's call."""
    conn, schema = schema_conn
    await _pre_migration_tables(conn)
    await _run(conn, "upgrade")
    await _insert_revocation(conn)

    with pytest.raises(RuntimeError, match=r"1 row\(s\).*carry no user_id"):
        await _run(conn, "downgrade")
    await conn.rollback()
    # Refused BEFORE anything changed: nullable and CHECK both still there.
    assert await _nullable(conn, schema) == "YES"
    assert await _check(conn, schema) is not None
    assert len(await _rows(conn)) == 2

    # The way the message names: remove them deliberately, then go back.
    await conn.exec_driver_sql("DELETE FROM federation_query_log WHERE user_id IS NULL")
    await conn.commit()
    await _run(conn, "downgrade")
    assert await _nullable(conn, schema) == "NO"
    assert await _check(conn, schema) is None
    assert await _rows(conn) == [(1, "asked", "an old question")]


async def test_upgrade_over_a_nullable_column_is_a_no_op(schema_conn):
    """A fresh install: the baseline built the table from the current model."""
    conn, schema = schema_conn
    await _pre_migration_tables(conn)
    await _run(conn, "upgrade")
    await _insert_revocation(conn)
    await _run(conn, "upgrade")
    assert await _nullable(conn, schema) == "YES"
    assert await _check(conn, schema) is not None
    assert len(await _rows(conn)) == 2


async def test_a_half_applied_state_heals(schema_conn):
    """Nullable already, CHECK missing: the upgrade adds what is missing; and
    a leftover CHECK on a NOT NULL column is taken back by the downgrade."""
    conn, schema = schema_conn
    await _pre_migration_tables(conn)
    await conn.exec_driver_sql("ALTER TABLE federation_query_log ALTER COLUMN user_id DROP NOT NULL")
    await conn.commit()
    await _run(conn, "upgrade")
    assert await _check(conn, schema) is not None

    await conn.exec_driver_sql("ALTER TABLE federation_query_log ALTER COLUMN user_id SET NOT NULL")
    await conn.commit()
    await _run(conn, "downgrade")
    assert await _check(conn, schema) is None
    assert await _nullable(conn, schema) == "NO"


async def test_downgrade_over_a_not_null_column_is_a_no_op(schema_conn):
    conn, schema = schema_conn
    await _pre_migration_tables(conn)
    await _run(conn, "downgrade")
    assert await _nullable(conn, schema) == "NO"
    assert await _rows(conn) == [(1, "asked", "an old question")]


async def test_without_the_table_the_upgrade_refuses_and_creates_nothing(schema_conn):
    conn, schema = schema_conn
    with pytest.raises(RuntimeError, match="missing in the current schema"):
        await _run(conn, "upgrade")
    await conn.rollback()
    await _run(conn, "downgrade")
    assert (await conn.execute(text(
        "SELECT count(*) FROM information_schema.tables WHERE table_schema = :s"
    ), {"s": schema})).scalar() == 0


async def test_migration_and_model_agree(schema_conn):
    """The baseline builds the column from the MODEL on a fresh install."""
    conn, schema = schema_conn
    await _pre_migration_tables(conn)
    await _run(conn, "upgrade")

    from models.database import FederationQueryLog

    column = FederationQueryLog.__table__.c.user_id
    assert column.nullable is True
    assert await _nullable(conn, schema) == "YES"
    (fk,) = column.foreign_keys
    assert fk.ondelete is None

    # Same CHECK, by name and by what the database makes of the expression.
    from sqlalchemy import CheckConstraint
    from sqlalchemy.schema import CreateTable

    mig = _load_migration()
    (check,) = [c for c in FederationQueryLog.__table__.constraints if isinstance(c, CheckConstraint)]
    assert check.name == mig.CHECK_NAME
    assert str(check.sqltext) == mig.CHECK_SQL
    assert mig.CHECK_NAME in str(CreateTable(FederationQueryLog.__table__))


def test_revision_chain():
    mig = _load_migration()
    assert mig.revision == REVISION
    assert mig.down_revision == "pc20261113_peer_instance_name"
    assert len(mig.revision) <= 32, "alembic_version.version_num is VARCHAR(32)"


def test_the_chain_still_has_exactly_one_head():
    """A second head would make `alembic upgrade head` refuse on deploy."""
    from alembic.script import ScriptDirectory

    script = ScriptDirectory(str(_versions_dir().parent))
    heads = script.get_heads()
    assert len(heads) == 1, heads
    lineage = {rev.revision for rev in script.iterate_revisions(heads[0], "pc20261113_peer_instance_name")}
    assert REVISION in lineage
