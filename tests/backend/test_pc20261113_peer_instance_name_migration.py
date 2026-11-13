"""Real-Postgres round trip for ``pc20261113_peer_instance_name``.

Harness as in ``test_pc20261111_push_subscriptions_migration.py``: a throwaway
schema holding the two tables in their PRE-migration shape, and the real
``upgrade()`` / ``downgrade()`` bound to a MigrationContext.

Two additive nullable columns, no backfill. What is checked: the columns and
their shape, that rows which existed before keep everything and read NULL, the
reverse path (forward -> reverse -> forward) with row counts, that the reverse
loses only what the forward path added, the fresh-install case (the baseline
already built the columns from the models), and that migration and model
describe the same columns.
"""

from __future__ import annotations

import importlib.util
import uuid
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

pytestmark = [pytest.mark.database]

PEERS = ("peer_users", "remote_instance_name")
LOG = ("federation_query_log", "peer_instance_name_snapshot")


def _versions_dir() -> Path:
    import services.database as _db

    return Path(_db.__file__).resolve().parents[1] / "alembic" / "versions"


def _load_migration():
    path = _versions_dir() / "pc20261113_peer_instance_name.py"
    spec = importlib.util.spec_from_file_location("pc20261113_peer_instance_name_under_test", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.fixture
async def schema_conn(pg_url):
    engine = create_async_engine(pg_url, poolclass=NullPool, future=True)
    schema = f"pcin_{uuid.uuid4().hex[:12]}"
    conn = await engine.connect()
    try:
        await conn.exec_driver_sql(f'CREATE SCHEMA "{schema}"')
        await conn.exec_driver_sql(f'SET search_path TO "{schema}", public')
        await conn.commit()
        yield conn, schema
    finally:
        try:
            await conn.rollback()
        except Exception:
            pass
        try:
            await conn.exec_driver_sql(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
            await conn.commit()
        except Exception:
            pass
        await conn.close()
        await engine.dispose()


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
    """The two tables as they stood at ``pc20261111_push_subs`` -- reduced to
    the columns that matter here, with one row each."""
    await conn.exec_driver_sql(
        "CREATE TABLE peer_users ("
        " id SERIAL PRIMARY KEY,"
        " remote_pubkey VARCHAR(64) NOT NULL,"
        " remote_display_name VARCHAR(255) NOT NULL,"
        " revoked_at TIMESTAMP)"
    )
    await conn.exec_driver_sql(
        "CREATE TABLE federation_query_log ("
        " id SERIAL PRIMARY KEY,"
        " peer_user_id INTEGER REFERENCES peer_users(id) ON DELETE SET NULL,"
        " peer_pubkey_snapshot VARCHAR(64) NOT NULL,"
        " peer_display_name_snapshot VARCHAR(255) NOT NULL,"
        " query_text TEXT NOT NULL)"
    )
    await conn.execute(text(
        "INSERT INTO peer_users (remote_pubkey, remote_display_name) VALUES (:k, 'evdb')"
    ), {"k": "ab" * 32})
    await conn.execute(text(
        "INSERT INTO federation_query_log"
        " (peer_user_id, peer_pubkey_snapshot, peer_display_name_snapshot, query_text)"
        " VALUES (1, :k, 'evdb', 'eine alte Frage')"
    ), {"k": "ab" * 32})
    await conn.commit()


async def _column(conn, schema, table_column) -> dict | None:
    table, column = table_column
    row = (await conn.execute(text(
        "SELECT data_type, character_maximum_length, is_nullable, column_default"
        " FROM information_schema.columns"
        " WHERE table_schema = :s AND table_name = :t AND column_name = :c"
    ), {"s": schema, "t": table, "c": column})).first()
    return dict(row._mapping) if row else None


async def _count(conn, table) -> int:
    return (await conn.execute(text(f"SELECT count(*) FROM {table}"))).scalar()


async def test_round_trip_keeps_every_row_and_loses_only_what_it_added(schema_conn):
    conn, schema = schema_conn
    await _pre_migration_tables(conn)
    assert await _column(conn, schema, PEERS) is None
    assert await _column(conn, schema, LOG) is None

    # --- forward ---------------------------------------------------------
    await _run(conn, "upgrade")
    for target in (PEERS, LOG):
        assert await _column(conn, schema, target) == {
            "data_type": "character varying",
            "character_maximum_length": 64,
            "is_nullable": "YES",
            "column_default": None,
        }, target
    # No backfill: NULL is the truth for everything that existed before.
    assert (await conn.execute(text(
        "SELECT remote_display_name, remote_instance_name FROM peer_users"
    ))).all() == [("evdb", None)]
    assert (await conn.execute(text(
        "SELECT peer_display_name_snapshot, peer_instance_name_snapshot, query_text"
        " FROM federation_query_log"
    ))).all() == [("evdb", None, "eine alte Frage")]

    # The far side tells us its name; a query is logged with it.
    await conn.execute(text("UPDATE peer_users SET remote_instance_name = 'Instance B'"))
    await conn.execute(text(
        "INSERT INTO federation_query_log"
        " (peer_user_id, peer_pubkey_snapshot, peer_display_name_snapshot,"
        "  peer_instance_name_snapshot, query_text)"
        " VALUES (1, :k, 'evdb', 'Instance B', 'eine neue Frage')"
    ), {"k": "ab" * 32})
    await conn.commit()

    # --- reverse ---------------------------------------------------------
    await _run(conn, "downgrade")
    assert await _column(conn, schema, PEERS) is None
    assert await _column(conn, schema, LOG) is None
    # The rows stay -- both of the log, including the one written in between.
    assert await _count(conn, "peer_users") == 1
    assert await _count(conn, "federation_query_log") == 2
    assert (await conn.execute(text(
        "SELECT remote_pubkey, remote_display_name FROM peer_users"
    ))).all() == [("ab" * 32, "evdb")]
    assert [r[0] for r in (await conn.execute(text(
        "SELECT query_text FROM federation_query_log ORDER BY id"
    ))).all()] == ["eine alte Frage", "eine neue Frage"]

    # --- forward again ---------------------------------------------------
    await _run(conn, "upgrade")
    assert await _column(conn, schema, PEERS) is not None
    assert await _count(conn, "peer_users") == 1
    assert await _count(conn, "federation_query_log") == 2
    # The name learned before the downgrade is gone, and says so by being
    # NULL -- it is not resurrected from anywhere.
    assert (await conn.execute(text(
        "SELECT remote_instance_name FROM peer_users"
    ))).scalar() is None
    assert (await conn.execute(text(
        "SELECT count(*) FROM federation_query_log WHERE peer_instance_name_snapshot IS NOT NULL"
    ))).scalar() == 0


async def test_upgrade_over_existing_columns_is_a_no_op(schema_conn):
    """A fresh install: the baseline built both tables from the current
    models, so the columns are already there when this migration runs."""
    conn, _schema = schema_conn
    await _pre_migration_tables(conn)
    await _run(conn, "upgrade")
    await conn.execute(text("UPDATE peer_users SET remote_instance_name = 'Instance B'"))
    await conn.commit()

    await _run(conn, "upgrade")
    assert (await conn.execute(text(
        "SELECT remote_instance_name FROM peer_users"
    ))).scalar() == "Instance B"


async def test_upgrade_adds_only_the_missing_column(schema_conn):
    """Half-applied state (one column there, one not) heals instead of failing."""
    conn, schema = schema_conn
    await _pre_migration_tables(conn)
    await conn.exec_driver_sql("ALTER TABLE peer_users ADD COLUMN remote_instance_name VARCHAR(64)")
    await conn.commit()
    await _run(conn, "upgrade")
    assert await _column(conn, schema, PEERS) is not None
    assert await _column(conn, schema, LOG) is not None


async def test_downgrade_without_the_columns_is_a_no_op(schema_conn):
    conn, _schema = schema_conn
    await _pre_migration_tables(conn)
    await _run(conn, "downgrade")
    assert await _count(conn, "peer_users") == 1
    assert await _count(conn, "federation_query_log") == 1


async def test_without_the_tables_nothing_is_created(schema_conn):
    conn, schema = schema_conn
    await _run(conn, "upgrade")
    await _run(conn, "downgrade")
    assert (await conn.execute(text(
        "SELECT count(*) FROM information_schema.tables WHERE table_schema = :s"
    ), {"s": schema})).scalar() == 0


async def test_migration_and_model_describe_the_same_columns(schema_conn):
    """The baseline builds the columns from the MODEL on a fresh install, the
    migration on an existing one. Both must yield the same shape."""
    conn, schema = schema_conn
    await _pre_migration_tables(conn)
    await _run(conn, "upgrade")

    from models.database import FederationQueryLog, PeerUser

    for model, target in ((PeerUser, PEERS), (FederationQueryLog, LOG)):
        column = model.__table__.c[target[1]]
        migrated = await _column(conn, schema, target)
        assert column.type.length == migrated["character_maximum_length"] == 64
        assert bool(column.nullable) is (migrated["is_nullable"] == "YES") is True
        assert column.default is None and column.server_default is None


def test_revision_chain():
    mig = _load_migration()
    assert mig.revision == "pc20261113_peer_instance_name"
    assert mig.down_revision == "pc20261111_push_subs"
    assert len(mig.revision) <= 32, "alembic_version.version_num is VARCHAR(32)"


def test_the_chain_still_has_exactly_one_head():
    """A second head would make `alembic upgrade head` refuse on deploy."""
    from alembic.script import ScriptDirectory

    script = ScriptDirectory(str(_versions_dir().parent))
    heads = script.get_heads()
    assert len(heads) == 1, heads
    lineage = {rev.revision for rev in script.iterate_revisions(heads[0], "pc20261111_push_subs")}
    assert "pc20261113_peer_instance_name" in lineage
