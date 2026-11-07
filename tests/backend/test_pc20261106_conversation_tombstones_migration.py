"""Real-Postgres tests for ``pc20261106_conv_tombstones`` (#1485).

Harness as in ``test_pc20261101_upload_uploader_migration.py``: a throwaway
schema with the pre-migration shape of the table involved and the real
``upgrade()`` / ``downgrade()`` bound to a MigrationContext.

What must hold:
- the upgrade creates ``deleted_conversation_sessions`` and the trigger, and
  backfills nothing;
- from then on every deleted conversation's session id is recorded (UTC);
- a second upgrade is a no-op, also when ``create_all`` built the table first;
- the downgrade removes trigger, function and table and leaves every
  conversation in place; deleting afterwards works;
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
            / "pc20261106_conversation_tombstones.py")
    spec = importlib.util.spec_from_file_location("pc20261106_conv_tombstones_under_test", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.fixture
async def schema_conn(pg_url):
    engine = create_async_engine(pg_url, poolclass=NullPool, future=True)
    schema = f"pctomb_{uuid.uuid4().hex[:12]}"
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


async def _setup(conn) -> None:
    await conn.exec_driver_sql(
        "CREATE TABLE conversations (id INTEGER PRIMARY KEY, session_id VARCHAR UNIQUE)")
    await conn.execute(text(
        "INSERT INTO conversations (id, session_id) VALUES (1, 's-a'), (2, 's-b'), (3, 's-c')"))
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


async def _has_table(conn, schema) -> bool:
    return (await conn.execute(text("SELECT to_regclass(:t) IS NOT NULL"),
                               {"t": f'"{schema}".deleted_conversation_sessions'})).scalar()


async def _triggers(conn, schema) -> list[str]:
    return (await conn.execute(text(
        "SELECT t.tgname FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid"
        " JOIN pg_namespace n ON n.oid = c.relnamespace"
        " WHERE n.nspname = :s AND c.relname = 'conversations' AND NOT t.tgisinternal"),
        {"s": schema})).scalars().all()


async def _functions(conn, schema) -> list[str]:
    return (await conn.execute(text(
        "SELECT p.proname FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace"
        " WHERE n.nspname = :s"), {"s": schema})).scalars().all()


async def _recorded(conn) -> list[str]:
    return (await conn.execute(text(
        "SELECT session_id FROM deleted_conversation_sessions ORDER BY session_id"
    ))).scalars().all()


async def _conversations(conn) -> list[tuple]:
    return (await conn.execute(text(
        "SELECT id, session_id FROM conversations ORDER BY id"))).all()


async def test_upgrade_records_every_delete_from_then_on(schema_conn):
    conn, schema = schema_conn
    await _setup(conn)

    await _run(conn, "upgrade")

    assert await _has_table(conn, schema)
    assert await _triggers(conn, schema) == ["trg_conversations_record_deleted_session"]
    assert await _recorded(conn) == []  # no backfill
    assert len(await _conversations(conn)) == 3

    await conn.execute(text("DELETE FROM conversations WHERE id IN (1, 2)"))
    await conn.commit()
    assert await _recorded(conn) == ["s-a", "s-b"]
    # UTC without a zone, like `_utcnow`.
    off = (await conn.execute(text(
        "SELECT max(abs(extract(epoch FROM deleted_at - timezone('utc', now()))))"
        " FROM deleted_conversation_sessions"))).scalar()
    assert off < 300


async def test_second_upgrade_is_a_noop(schema_conn):
    conn, schema = schema_conn
    await _setup(conn)
    await _run(conn, "upgrade")
    await conn.execute(text("DELETE FROM conversations WHERE id = 1"))
    await conn.commit()

    await _run(conn, "upgrade")

    assert await _triggers(conn, schema) == ["trg_conversations_record_deleted_session"]
    assert await _recorded(conn) == ["s-a"]


async def test_upgrade_on_a_table_create_all_built(schema_conn):
    """A development checkout ran `create_all`: the table is there, the
    trigger is not. The upgrade adds only the trigger."""
    conn, schema = schema_conn
    await _setup(conn)
    await conn.exec_driver_sql(
        "CREATE TABLE deleted_conversation_sessions (session_id VARCHAR PRIMARY KEY,"
        " deleted_at TIMESTAMP NOT NULL DEFAULT timezone('utc', now()))")
    await conn.commit()

    await _run(conn, "upgrade")

    assert await _triggers(conn, schema) == ["trg_conversations_record_deleted_session"]
    await conn.execute(text("DELETE FROM conversations WHERE id = 3"))
    await conn.commit()
    assert await _recorded(conn) == ["s-c"]


async def test_round_trip_keeps_every_conversation(schema_conn):
    conn, schema = schema_conn
    await _setup(conn)
    before = await _conversations(conn)

    await _run(conn, "upgrade")
    await _run(conn, "downgrade")

    assert not await _has_table(conn, schema)
    assert await _triggers(conn, schema) == []
    assert "record_deleted_conversation_session" not in await _functions(conn, schema)
    assert await _conversations(conn) == before
    # Without the trigger a delete still works (and records nothing).
    await conn.execute(text("DELETE FROM conversations WHERE id = 3"))
    await conn.commit()

    await _run(conn, "upgrade")
    assert await _triggers(conn, schema) == ["trg_conversations_record_deleted_session"]
    assert await _recorded(conn) == []
    await conn.execute(text("DELETE FROM conversations WHERE id = 2"))
    await conn.commit()
    assert await _recorded(conn) == ["s-b"]
