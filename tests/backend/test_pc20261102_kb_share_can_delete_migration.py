"""Real-Postgres tests for ``pc20261102_kb_share_can_delete``.

Harness as in ``test_pc20261101_kb_shares_migration.py``: a throwaway schema
with the pre-migration ``kb_shares`` and the real ``upgrade()`` / ``downgrade()``.

What must hold:
- the upgrade adds ``can_delete`` NOT NULL DEFAULT false; every existing share
  (an admin share included) gets false — no backfill;
- a second upgrade is a no-op;
- upgrade -> downgrade -> upgrade is clean and keeps every share row.
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


def _load_migration():
    import services.database as _db

    path = (Path(_db.__file__).resolve().parents[1] / "alembic" / "versions"
            / "pc20261102_kb_share_can_delete.py")
    spec = importlib.util.spec_from_file_location("pc20261102_under_test", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.fixture
async def schema_conn(pg_url):
    engine = create_async_engine(pg_url, poolclass=NullPool, future=True)
    schema = f"pckbd_{uuid.uuid4().hex[:12]}"
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


_DDL = [
    "CREATE TABLE users (id INTEGER PRIMARY KEY)",
    "CREATE TABLE knowledge_bases (id INTEGER PRIMARY KEY, owner_id INTEGER REFERENCES users(id))",
    # kb_shares exactly as pc20261101_kb_shares creates it.
    "CREATE TABLE kb_shares (kb_id INTEGER REFERENCES knowledge_bases(id) ON DELETE CASCADE,"
    " user_id INTEGER REFERENCES users(id), permission_level VARCHAR(16) NOT NULL,"
    " granted_by INTEGER REFERENCES users(id), granted_at TIMESTAMP NOT NULL,"
    " PRIMARY KEY (kb_id, user_id))",
]


async def _setup(conn) -> None:
    for stmt in _DDL:
        await conn.exec_driver_sql(stmt)
    await conn.execute(text("INSERT INTO users (id) VALUES (1), (2), (3)"))
    await conn.execute(text("INSERT INTO knowledge_bases (id, owner_id) VALUES (10, 1)"))
    await conn.execute(text(
        "INSERT INTO kb_shares VALUES (10, 2, 'admin', 1, '2026-11-01'),"
        " (10, 3, 'read', 1, '2026-11-01')"))
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


async def _column(conn, schema):
    return (await conn.execute(text(
        "SELECT is_nullable, column_default FROM information_schema.columns "
        "WHERE table_schema = :s AND table_name = 'kb_shares' AND column_name = 'can_delete'"),
        {"s": schema})).first()


async def _shares(conn):
    return (await conn.execute(text(
        "SELECT kb_id, user_id, permission_level FROM kb_shares ORDER BY 1, 2"))).all()


async def test_upgrade_adds_the_switch_off_for_every_share(schema_conn):
    conn, schema = schema_conn
    await _setup(conn)

    await _run(conn, "upgrade")

    col = await _column(conn, schema)
    assert col is not None and col[0] == "NO" and "false" in col[1]
    rows = (await conn.execute(text(
        "SELECT user_id, permission_level, can_delete FROM kb_shares ORDER BY 1"))).all()
    # No backfill: the admin grantee loses delete on purpose.
    assert rows == [(2, "admin", False), (3, "read", False)]
    # A row written by an older pod (no column named) gets the default.
    await conn.execute(text("INSERT INTO users (id) VALUES (4)"))
    await conn.execute(text(
        "INSERT INTO kb_shares (kb_id, user_id, permission_level, granted_at) "
        "VALUES (10, 4, 'write', '2026-11-02')"))
    assert (await conn.execute(text(
        "SELECT can_delete FROM kb_shares WHERE user_id = 4"))).scalar() is False


async def test_second_upgrade_is_a_noop(schema_conn):
    conn, schema = schema_conn
    await _setup(conn)
    await _run(conn, "upgrade")
    await _run(conn, "upgrade")
    assert await _column(conn, schema) is not None


async def test_round_trip_keeps_every_share(schema_conn):
    conn, schema = schema_conn
    await _setup(conn)
    before = await _shares(conn)
    await _run(conn, "upgrade")
    await conn.execute(text("UPDATE kb_shares SET can_delete = TRUE WHERE user_id = 3"))
    await conn.commit()

    await _run(conn, "downgrade")
    assert await _column(conn, schema) is None
    assert await _shares(conn) == before

    await _run(conn, "upgrade")
    assert await _column(conn, schema) is not None
    assert await _shares(conn) == before
    # The switch is not restorable (the old schema has none) — back to false.
    assert (await conn.execute(text(
        "SELECT COUNT(*) FROM kb_shares WHERE can_delete"))).scalar() == 0
