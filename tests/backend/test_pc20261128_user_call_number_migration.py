"""Real-Postgres tests for ``pc20261128_user_call_number`` (#1683).

Harness as in ``test_pc20261127b_telephony_faxes_migration.py`` (a throwaway schema
with a bare ``users`` table, the real ``upgrade()`` / ``downgrade()``).

What must hold:
- upgrade adds the two nullable columns (an existing user row stays valid, NULL);
- it lies on the path to the single head and fits ``alembic_version`` (VARCHAR(32));
- upgrade -> downgrade -> upgrade is clean, a second upgrade is a no-op.
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
            / "pc20261128_user_call_number.py")
    spec = importlib.util.spec_from_file_location("pc20261128_under_test", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module, path.parent.parent


@pytest.fixture
async def schema_conn(pg_url):
    schema = f"pc1683_{uuid.uuid4().hex[:12]}"
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
    await conn.exec_driver_sql("CREATE TABLE users (id SERIAL PRIMARY KEY)")
    await conn.commit()
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


async def _columns(conn, schema) -> set[str]:
    return {r[0] for r in (await conn.execute(text(
        "SELECT column_name FROM information_schema.columns WHERE table_schema = :s "
        "AND table_name = 'users'"), {"s": schema})).all()}


def test_builds_on_the_single_head():
    from alembic.script import ScriptDirectory

    mig, alembic_dir = _load_migration()
    assert mig.down_revision == "pc20261127b_telephony_faxes"
    assert len(mig.revision) <= 32, "alembic_version.version_num is VARCHAR(32)"
    script = ScriptDirectory(str(alembic_dir))
    heads = script.get_heads()
    assert len(heads) == 1, heads
    assert mig.revision in {r.revision for r in script.iterate_revisions(heads[0], "base")}


async def test_upgrade_adds_nullable_columns(schema_conn):
    conn, schema = schema_conn
    await conn.execute(text("INSERT INTO users DEFAULT VALUES"))
    await conn.commit()
    await _run(conn, "upgrade")
    assert {"call_number", "call_number_verified_at"} <= await _columns(conn, schema)
    row = (await conn.execute(text("SELECT call_number, call_number_verified_at FROM users"))).one()
    assert tuple(row) == (None, None)


async def test_round_trip_is_clean_and_idempotent(schema_conn):
    conn, schema = schema_conn
    await _run(conn, "upgrade")
    await _run(conn, "downgrade")
    assert "call_number" not in await _columns(conn, schema)
    await _run(conn, "upgrade")
    await _run(conn, "upgrade")  # fresh install: the columns are already there
    assert "call_number_verified_at" in await _columns(conn, schema)
