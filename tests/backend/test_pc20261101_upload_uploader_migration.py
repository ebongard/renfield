"""Real-Postgres tests for ``pc20261101_upload_uploader`` (#1459).

Harness as in ``test_pc20261101_kb_shares_migration.py``: a throwaway schema
with the pre-migration shape of the tables involved and the real
``upgrade()`` / ``downgrade()`` bound to a MigrationContext.

What must hold:
- the upgrade adds a nullable ``uploaded_by_user_id`` with a foreign key to
  ``users`` (ON DELETE SET NULL) and an index, and backfills NOTHING —
  existing rows stay NULL (nobody is guessed);
- a second upgrade is a no-op;
- the downgrade drops the column and leaves every row in place;
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
            / "pc20261101_chat_upload_uploader.py")
    spec = importlib.util.spec_from_file_location("pc20261101_upload_uploader_under_test", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.fixture
async def schema_conn(pg_url):
    engine = create_async_engine(pg_url, poolclass=NullPool, future=True)
    schema = f"pcupl_{uuid.uuid4().hex[:12]}"
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
    "CREATE TABLE chat_uploads (id INTEGER PRIMARY KEY, session_id VARCHAR(128) NOT NULL,"
    " filename VARCHAR(255) NOT NULL)",
]


async def _setup(conn) -> None:
    for stmt in _DDL:
        await conn.exec_driver_sql(stmt)
    await conn.execute(text("INSERT INTO users (id) VALUES (1), (2)"))
    await conn.execute(text(
        "INSERT INTO chat_uploads (id, session_id, filename) VALUES"
        " (10, 's-a', 'a.txt'), (11, 's-b', 'b.txt')"))
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


async def _has_column(conn, schema) -> bool:
    return bool((await conn.execute(text(
        "SELECT 1 FROM information_schema.columns WHERE table_schema = :s"
        " AND table_name = 'chat_uploads' AND column_name = 'uploaded_by_user_id'"),
        {"s": schema})).scalar())


async def _rows(conn):
    return (await conn.execute(text(
        "SELECT id, session_id, filename FROM chat_uploads ORDER BY id"))).all()


async def test_upgrade_adds_a_nullable_fk_column_and_backfills_nothing(schema_conn):
    conn, schema = schema_conn
    await _setup(conn)
    await _run(conn, "upgrade")

    assert await _has_column(conn, schema)
    assert (await conn.execute(text(
        "SELECT COUNT(*) FROM chat_uploads WHERE uploaded_by_user_id IS NOT NULL"))).scalar() == 0
    nullable = (await conn.execute(text(
        "SELECT is_nullable FROM information_schema.columns WHERE table_schema = :s"
        " AND table_name = 'chat_uploads' AND column_name = 'uploaded_by_user_id'"),
        {"s": schema})).scalar()
    assert nullable == "YES"
    # Foreign key to users, ON DELETE SET NULL ('n'), named as create_all names it.
    fk = (await conn.execute(text(
        "SELECT c.conname, c.confdeltype::text, c.confrelid::regclass::text FROM pg_constraint c"
        " JOIN pg_namespace n ON n.oid = c.connamespace"
        " WHERE n.nspname = :s AND c.contype = 'f'"
        " AND c.conrelid = to_regclass(:t)"),
        {"s": schema, "t": f'"{schema}".chat_uploads'})).all()
    assert len(fk) == 1
    assert fk[0][0] == "chat_uploads_uploaded_by_user_id_fkey"
    assert fk[0][1] == "n"
    assert fk[0][2].endswith("users")
    idx = (await conn.execute(text(
        "SELECT indexname FROM pg_indexes WHERE schemaname = :s AND tablename = 'chat_uploads'"),
        {"s": schema})).scalars().all()
    assert "ix_chat_uploads_uploaded_by_user_id" in idx

    # SET NULL is live: deleting the uploader clears the column, keeps the row.
    await conn.execute(text("UPDATE chat_uploads SET uploaded_by_user_id = 2 WHERE id = 10"))
    await conn.execute(text("DELETE FROM users WHERE id = 2"))
    await conn.commit()
    assert (await conn.execute(text(
        "SELECT uploaded_by_user_id FROM chat_uploads WHERE id = 10"))).scalar() is None


async def test_second_upgrade_is_a_noop(schema_conn):
    conn, schema = schema_conn
    await _setup(conn)
    await _run(conn, "upgrade")
    await _run(conn, "upgrade")
    assert await _has_column(conn, schema)


async def test_round_trip_keeps_every_row(schema_conn):
    conn, schema = schema_conn
    await _setup(conn)
    before = await _rows(conn)

    await _run(conn, "upgrade")
    await conn.execute(text("UPDATE chat_uploads SET uploaded_by_user_id = 1 WHERE id = 11"))
    await conn.commit()

    await _run(conn, "downgrade")
    assert not await _has_column(conn, schema)
    assert await _rows(conn) == before
    idx = (await conn.execute(text(
        "SELECT indexname FROM pg_indexes WHERE schemaname = :s AND tablename = 'chat_uploads'"),
        {"s": schema})).scalars().all()
    assert "ix_chat_uploads_uploaded_by_user_id" not in idx

    await _run(conn, "upgrade")
    assert await _has_column(conn, schema)
    assert await _rows(conn) == before
    # The uploader written before the downgrade is gone, not resurrected.
    assert (await conn.execute(text(
        "SELECT COUNT(*) FROM chat_uploads WHERE uploaded_by_user_id IS NOT NULL"))).scalar() == 0


async def test_downgrade_without_the_column_is_a_noop(schema_conn):
    conn, schema = schema_conn
    await _setup(conn)
    await _run(conn, "downgrade")
    assert not await _has_column(conn, schema)
    assert len(await _rows(conn)) == 2
