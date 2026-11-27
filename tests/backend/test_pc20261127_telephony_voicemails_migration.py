"""Real-Postgres tests for ``pc20261127_telephony_voicemails`` (#1669).

Harness as in ``test_pc20261126_kg_contact_points_migration.py``: a throwaway
schema and the real ``upgrade()`` / ``downgrade()`` bound to a MigrationContext.

What must hold:
- upgrade creates the table; a bare INSERT gets status/attempts/timestamps;
- ``call_id`` is unique (a replayed voicemail makes no second job);
- it builds on the single head and fits ``alembic_version`` (VARCHAR(32));
- upgrade -> downgrade -> upgrade is clean, a second upgrade is a no-op.
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

REC = "20261127-101500-1795700000.42"


def _load_migration():
    import services.database as _db

    path = (Path(_db.__file__).resolve().parents[1] / "alembic" / "versions"
            / "pc20261127_telephony_voicemails.py")
    spec = importlib.util.spec_from_file_location("pc20261127_under_test", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module, path.parent.parent


@pytest.fixture
async def schema_conn(pg_url):
    schema = f"pc1669_{uuid.uuid4().hex[:12]}"
    setup = create_async_engine(pg_url, poolclass=NullPool, future=True)
    async with setup.connect() as c:
        await c.exec_driver_sql(f'CREATE SCHEMA "{schema}"')
        await c.commit()
    await setup.dispose()
    # The test schema ALONE on the search_path (``public`` already has the table).
    engine = create_async_engine(
        pg_url, poolclass=NullPool, future=True,
        connect_args={"server_settings": {"search_path": f'"{schema}"'}},
    )
    conn = await engine.connect()
    try:
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

    mig, _ = _load_migration()

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
    try:
        await conn.run_sync(_apply)
    except Exception:
        await conn.rollback()
        raise
    await conn.commit()


async def _has_table(conn, schema) -> bool:
    return bool((await conn.execute(text(
        "SELECT 1 FROM information_schema.tables WHERE table_schema = :s "
        "AND table_name = 'telephony_voicemails'"), {"s": schema})).first())


async def _insert(conn, call_id: str) -> int:
    return (await conn.execute(text(
        "INSERT INTO telephony_voicemails (call_id, recording) VALUES (:c, :r) RETURNING id"),
        {"c": call_id, "r": REC})).scalar()


def test_builds_on_the_single_head():
    from alembic.script import ScriptDirectory

    mig, alembic_dir = _load_migration()
    assert mig.down_revision == "pc20261126b_merge_heads"
    assert len(mig.revision) <= 32, "alembic_version.version_num is VARCHAR(32)"
    assert ScriptDirectory(str(alembic_dir)).get_heads() == [mig.revision]


async def test_upgrade_defaults_and_unique_call(schema_conn):
    conn, schema = schema_conn
    await _run(conn, "upgrade")
    assert await _has_table(conn, schema)
    jid = await _insert(conn, "c1")
    row = (await conn.execute(text(
        "SELECT status, attempts, next_attempt_at IS NOT NULL, created_at IS NOT NULL, updated_at IS NOT NULL "
        "FROM telephony_voicemails WHERE id = :i"), {"i": jid})).one()
    assert tuple(row) == ("pending", 0, True, True, True)
    await conn.commit()
    with pytest.raises(IntegrityError):
        await _insert(conn, "c1")
    await conn.rollback()


async def test_round_trip_is_clean_and_idempotent(schema_conn):
    conn, schema = schema_conn
    await _run(conn, "upgrade")
    await _run(conn, "downgrade")
    assert not await _has_table(conn, schema)
    await _run(conn, "upgrade")
    await _run(conn, "upgrade")  # the fresh-install case: the table is already there
    assert await _has_table(conn, schema)
