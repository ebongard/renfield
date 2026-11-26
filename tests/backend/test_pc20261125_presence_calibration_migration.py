"""Real-Postgres round trip for ``pc20261125_presence_calibration`` (#1551).

Harness as in ``test_pc20261111_push_subscriptions_migration.py``: a throwaway
schema with minimal ``users`` and ``rooms`` tables and the real ``upgrade()`` /
``downgrade()`` bound to a MigrationContext. Checks the three tables, the
bound-above-cutoff CHECK, a room in at most one zone, the delete rules (room
and zone CASCADE, the saving user SET NULL), the reverse path (forward ->
reverse -> forward), and that an upgrade over tables ``create_all`` already
built is a no-op.
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

TABLES = ("presence_receivers", "presence_zones", "presence_zone_rooms")


def _load_migration():
    import services.database as _db

    path = (Path(_db.__file__).resolve().parents[1] / "alembic" / "versions"
            / "pc20261125_presence_calibration.py")
    spec = importlib.util.spec_from_file_location("pc20261125_presence_calibration_under_test", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.fixture
async def schema_conn(pg_url):
    engine = create_async_engine(pg_url, poolclass=NullPool, future=True)
    schema = f"pcpc_{uuid.uuid4().hex[:12]}"
    conn = await engine.connect()
    try:
        await conn.exec_driver_sql(f'CREATE SCHEMA "{schema}"')
        await conn.exec_driver_sql(f'SET search_path TO "{schema}", public')
        await conn.exec_driver_sql("CREATE TABLE users (id INTEGER PRIMARY KEY)")
        await conn.exec_driver_sql("CREATE TABLE rooms (id INTEGER PRIMARY KEY)")
        await conn.execute(text("INSERT INTO users VALUES (1)"))
        await conn.execute(text("INSERT INTO rooms VALUES (10), (11), (12)"))
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


async def _tables(conn, schema) -> set[str]:
    rows = await conn.execute(
        text("SELECT tablename FROM pg_tables WHERE schemaname = :s"), {"s": schema}
    )
    return {r[0] for r in rows} & set(TABLES)


async def _exec(conn, sql: str, **params) -> None:
    await conn.execute(text(sql), params)
    await conn.commit()


async def _scalar(conn, sql: str):
    return (await conn.execute(text(sql))).scalar()


async def test_upgrade_builds_the_tables_and_their_rules(schema_conn):
    conn, schema = schema_conn
    await _run(conn, "upgrade")
    assert await _tables(conn, schema) == set(TABLES)

    await _exec(conn, "INSERT INTO presence_receivers (satellite_id, offset_db, in_room_dbm, cutoff_dbm,"
                      " updated_by_user_id) VALUES ('sat-a', 0, -60, -80, 1)")
    # The bound must lie above the cutoff (equal is refused too).
    with pytest.raises(IntegrityError):
        await _exec(conn, "INSERT INTO presence_receivers (satellite_id, offset_db, in_room_dbm, cutoff_dbm)"
                          " VALUES ('sat-b', 0, -80, -80)")
    await conn.rollback()

    await _exec(conn, "INSERT INTO presence_zones (id, name) VALUES (1, 'open-plan'), (2, 'other')")
    await _exec(conn, "INSERT INTO presence_zone_rooms (room_id, zone_id) VALUES (10, 1), (11, 1)")
    # A room is in at most one zone.
    with pytest.raises(IntegrityError):
        await _exec(conn, "INSERT INTO presence_zone_rooms (room_id, zone_id) VALUES (10, 2)")
    await conn.rollback()
    with pytest.raises(IntegrityError):
        await _exec(conn, "INSERT INTO presence_zones (id, name) VALUES (3, 'open-plan')")
    await conn.rollback()

    # Delete rules: room and zone CASCADE their membership; the user SET NULL.
    await _exec(conn, "DELETE FROM rooms WHERE id = 11")
    assert await _scalar(conn, "SELECT count(*) FROM presence_zone_rooms") == 1
    await _exec(conn, "DELETE FROM presence_zones WHERE id = 1")
    assert await _scalar(conn, "SELECT count(*) FROM presence_zone_rooms") == 0
    await _exec(conn, "DELETE FROM users WHERE id = 1")
    assert await _scalar(conn, "SELECT updated_by_user_id FROM presence_receivers") is None
    assert await _scalar(conn, "SELECT updated_at IS NOT NULL FROM presence_receivers") is True


async def test_reverse_path_and_upgrade_again(schema_conn):
    conn, schema = schema_conn
    await _run(conn, "upgrade")
    await _run(conn, "downgrade")
    assert await _tables(conn, schema) == set()
    await _run(conn, "upgrade")
    assert await _tables(conn, schema) == set(TABLES)


async def test_upgrade_over_existing_tables_is_a_no_op(schema_conn):
    """A fresh install's baseline (`create_all`) may already have built the
    tables from the models; the upgrade then must not fail or touch data."""
    conn, _schema = schema_conn
    await _run(conn, "upgrade")
    await _exec(conn, "INSERT INTO presence_zones (id, name) VALUES (1, 'kept')")
    await _run(conn, "upgrade")
    assert await _scalar(conn, "SELECT name FROM presence_zones") == "kept"
