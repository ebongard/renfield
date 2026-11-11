"""Real-Postgres tests for ``pc20261111_tombstone_idx`` (#1529).

Harness as in ``test_pc20261106_conversation_tombstones_migration.py``: a
throwaway schema with the pre-migration shape of the table and the real
``upgrade()`` / ``downgrade()`` bound to a MigrationContext.

What must hold:
- the revision sits on the head it was written against and fits the
  ``alembic_version`` column;
- the upgrade builds the index on ``deleted_at`` in the schema it runs in and
  touches no row;
- a second upgrade is a no-op, also when ``create_all`` built the index first;
- the downgrade removes the index from ITS schema only -- the same-named index
  further down the search_path (``public``, built by this harness) stays --
  and touches no row; without the index the downgrade is a no-op;
- upgrade -> downgrade -> upgrade is clean.
"""
from __future__ import annotations

import importlib.util
import uuid
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from services.conversation_tombstones import DELETED_AT_INDEX

pytestmark = [pytest.mark.database]


def _load_migration():
    import services.database as _db

    path = (Path(_db.__file__).resolve().parents[1] / "alembic" / "versions"
            / "pc20261111_tombstone_deleted_at_index.py")
    spec = importlib.util.spec_from_file_location("pc20261111_tombstone_idx_under_test", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.fixture
async def schema_conn(pg_url):
    engine = create_async_engine(pg_url, poolclass=NullPool, future=True)
    schema = f"pctidx_{uuid.uuid4().hex[:12]}"
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


async def _setup(conn, schema) -> None:
    """The table as ``pc20261106_conv_tombstones`` left it, with three rows."""
    await conn.exec_driver_sql(
        f'CREATE TABLE "{schema}".deleted_conversation_sessions ('
        " session_id VARCHAR PRIMARY KEY,"
        " deleted_at TIMESTAMP NOT NULL DEFAULT timezone('utc', now()))")
    await conn.execute(text(
        "INSERT INTO deleted_conversation_sessions (session_id, deleted_at) VALUES"
        " ('s-a', '2026-01-01 00:00:00'), ('s-b', '2026-06-01 12:00:00'),"
        " ('s-c', '2026-11-01 23:59:59')"))
    await conn.commit()


async def _run(conn, direction: str) -> None:
    from alembic.operations import Operations
    from alembic.runtime.migration import MigrationContext

    mig = _load_migration()

    def _apply(sync_conn):
        ctx = MigrationContext.configure(
            connection=sync_conn, opts={"transaction_per_migration": True}
        )
        prev = mig.op
        mig.op = Operations(ctx)
        try:
            with ctx.begin_transaction(_per_migration=True):
                getattr(mig, direction)()
        finally:
            mig.op = prev

    await conn.commit()
    try:
        await conn.run_sync(_apply)
    except Exception:
        await conn.rollback()
        raise
    await conn.commit()


async def _indexes(conn, schema) -> dict[str, str]:
    rows = (await conn.execute(text(
        "SELECT indexname, indexdef FROM pg_indexes"
        " WHERE schemaname = :s AND tablename = 'deleted_conversation_sessions'"),
        {"s": schema})).all()
    return {name: definition for name, definition in rows}


async def _rows(conn) -> list[tuple]:
    return (await conn.execute(text(
        "SELECT session_id, deleted_at FROM deleted_conversation_sessions ORDER BY session_id"
    ))).all()


def test_the_revision_sits_on_the_routing_traces_head():
    mig = _load_migration()
    assert mig.revision == "pc20261111_tombstone_idx"
    assert mig.down_revision == "pc20261110_routing_traces"
    assert len(mig.revision) <= 32  # alembic_version.version_num is varchar(32)


async def test_upgrade_builds_the_index_and_touches_no_row(schema_conn):
    conn, schema = schema_conn
    await _setup(conn, schema)
    before = await _rows(conn)
    assert DELETED_AT_INDEX not in await _indexes(conn, schema)

    await _run(conn, "upgrade")

    indexes = await _indexes(conn, schema)
    assert DELETED_AT_INDEX in indexes
    assert indexes[DELETED_AT_INDEX].endswith("USING btree (deleted_at)")
    assert f"ON {schema}.deleted_conversation_sessions" in indexes[DELETED_AT_INDEX]
    assert await _rows(conn) == before


async def test_second_upgrade_is_a_noop(schema_conn):
    conn, schema = schema_conn
    await _setup(conn, schema)
    await _run(conn, "upgrade")
    first = await _indexes(conn, schema)

    await _run(conn, "upgrade")

    assert await _indexes(conn, schema) == first


async def test_upgrade_on_an_index_create_all_built(schema_conn):
    """A fresh install's baseline (and a development checkout) built the index
    from the model. The upgrade keeps it and adds no second one."""
    conn, schema = schema_conn
    await _setup(conn, schema)
    await conn.exec_driver_sql(
        f'CREATE INDEX {DELETED_AT_INDEX} ON "{schema}".deleted_conversation_sessions (deleted_at)')
    await conn.commit()
    first = await _indexes(conn, schema)

    await _run(conn, "upgrade")

    assert await _indexes(conn, schema) == first


async def test_downgrade_removes_only_this_schemas_index(schema_conn):
    conn, schema = schema_conn
    await _setup(conn, schema)
    before = await _rows(conn)
    # The harness built `public` with `create_all`, so the same-named index
    # exists further down this connection's search_path.
    assert DELETED_AT_INDEX in await _indexes(conn, "public")

    await _run(conn, "upgrade")
    await _run(conn, "downgrade")

    assert DELETED_AT_INDEX not in await _indexes(conn, schema)
    assert await _rows(conn) == before
    assert DELETED_AT_INDEX in await _indexes(conn, "public")

    # Nothing left to drop: a second downgrade must not reach into `public`.
    await _run(conn, "downgrade")
    assert DELETED_AT_INDEX in await _indexes(conn, "public")


async def test_round_trip_is_clean(schema_conn):
    conn, schema = schema_conn
    await _setup(conn, schema)
    before_rows = await _rows(conn)
    before_indexes = await _indexes(conn, schema)

    await _run(conn, "upgrade")
    after_up = await _indexes(conn, schema)
    await _run(conn, "downgrade")
    assert await _indexes(conn, schema) == before_indexes
    await _run(conn, "upgrade")

    assert await _indexes(conn, schema) == after_up
    assert await _rows(conn) == before_rows
