"""Real-Postgres tests for ``pc20261101_kb_shares``.

Harness as in ``test_pc20261031_kb_name_per_owner_migration.py``: a throwaway
schema with the pre-migration shape of the tables involved and the real
``upgrade()`` / ``downgrade()`` bound to a MigrationContext.

What must hold:
- the upgrade creates ``kb_shares`` and changes NO data (legacy grants stay);
- a second upgrade is a no-op;
- the downgrade writes every share back as per-document grants (the old
  representation) at the share's level, then drops the table; a share of an
  empty KB cannot be represented and is dropped;
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

pytestmark = [pytest.mark.database]


def _load_migration():
    import services.database as _db

    path = (Path(_db.__file__).resolve().parents[1] / "alembic" / "versions"
            / "pc20261101_kb_shares.py")
    spec = importlib.util.spec_from_file_location("pc20261101_kb_shares_under_test", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.fixture
async def schema_conn(pg_url):
    engine = create_async_engine(pg_url, poolclass=NullPool, future=True)
    schema = f"pckbs_{uuid.uuid4().hex[:12]}"
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
    "CREATE TABLE documents (id INTEGER PRIMARY KEY,"
    " knowledge_base_id INTEGER REFERENCES knowledge_bases(id) ON DELETE CASCADE)",
    "CREATE TABLE atoms (atom_id VARCHAR(36) PRIMARY KEY, source_table VARCHAR(64),"
    " source_id VARCHAR(64))",
    "CREATE TABLE atom_explicit_grants (atom_id VARCHAR(36) REFERENCES atoms(atom_id)"
    " ON DELETE CASCADE, granted_to_user_id INTEGER REFERENCES users(id),"
    " permission_level VARCHAR(16) NOT NULL, granted_by INTEGER NOT NULL REFERENCES users(id),"
    " granted_at TIMESTAMP, PRIMARY KEY (atom_id, granted_to_user_id))",
]


async def _setup(conn) -> None:
    for stmt in _DDL:
        await conn.exec_driver_sql(stmt)
    await conn.execute(text("INSERT INTO users (id) VALUES (1), (2), (3)"))
    # KB 10 (two documents), KB 20 (empty) — both owned by user 1.
    await conn.execute(text("INSERT INTO knowledge_bases (id, owner_id) VALUES (10, 1), (20, 1)"))
    await conn.execute(text("INSERT INTO documents (id, knowledge_base_id) VALUES (100, 10), (101, 10)"))
    await conn.execute(text(
        "INSERT INTO atoms (atom_id, source_table, source_id) VALUES "
        "('a100', 'documents', '100'), ('a101', 'documents', '101')"))
    # One legacy grant (user 2 on document 100, read).
    await conn.execute(text(
        "INSERT INTO atom_explicit_grants VALUES ('a100', 2, 'read', 1, '2026-10-01')"))
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


async def _exists(conn, schema) -> bool:
    return bool((await conn.execute(
        text("SELECT to_regclass(:t) IS NOT NULL"), {"t": f'"{schema}".kb_shares'})).scalar())


async def _grants(conn):
    return (await conn.execute(text(
        "SELECT atom_id, granted_to_user_id, permission_level, granted_by "
        "FROM atom_explicit_grants ORDER BY 1, 2"))).all()


async def test_upgrade_creates_the_table_and_changes_no_data(schema_conn):
    conn, schema = schema_conn
    await _setup(conn)
    before = await _grants(conn)
    await _run(conn, "upgrade")
    assert await _exists(conn, schema)
    assert (await conn.execute(text("SELECT COUNT(*) FROM kb_shares"))).scalar() == 0
    assert await _grants(conn) == before
    idx = (await conn.execute(text(
        "SELECT indexname FROM pg_indexes WHERE schemaname = :s AND tablename = 'kb_shares' "
        "ORDER BY 1"), {"s": schema})).scalars().all()
    assert "idx_kb_shares_user" in idx and "kb_shares_pkey" in idx


async def test_second_upgrade_is_a_noop(schema_conn):
    conn, schema = schema_conn
    await _setup(conn)
    await _run(conn, "upgrade")
    await _run(conn, "upgrade")
    assert await _exists(conn, schema)


async def test_round_trip_writes_shares_back_as_grants(schema_conn):
    conn, schema = schema_conn
    await _setup(conn)
    await _run(conn, "upgrade")
    # User 2 shares KB 10 at write (has a legacy read grant on doc 100);
    # user 3 shares KB 10 without a granter (auth off); user 2 shares empty KB 20.
    await conn.execute(text(
        "INSERT INTO kb_shares VALUES (10, 2, 'write', 1, '2026-11-01'),"
        " (10, 3, 'read', NULL, '2026-11-01'), (20, 2, 'read', 1, '2026-11-01')"))
    await conn.commit()

    await _run(conn, "downgrade")

    assert not await _exists(conn, schema)
    assert await _grants(conn) == [
        ("a100", 2, "write", 1),   # legacy grant raised to the share's level
        ("a100", 3, "read", 1),    # no granter -> the KB owner
        ("a101", 2, "write", 1),
        ("a101", 3, "read", 1),
    ]  # the empty-KB share (20, 2) has no document to land on: dropped

    await _run(conn, "upgrade")
    assert await _exists(conn, schema)
    assert (await conn.execute(text("SELECT COUNT(*) FROM kb_shares"))).scalar() == 0


async def test_downgrade_of_an_empty_table_leaves_grants_alone(schema_conn):
    conn, schema = schema_conn
    await _setup(conn)
    before = await _grants(conn)
    await _run(conn, "upgrade")
    await _run(conn, "downgrade")
    assert not await _exists(conn, schema)
    assert await _grants(conn) == before
