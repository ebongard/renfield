"""Real-Postgres tests for ``pc20261126_kg_contact_points`` (#1240).

Harness as in ``test_pc20261124_kg_edge_tier_cap_migration.py``: a throwaway
schema with the pre-migration shape of the referenced tables, and the real
``upgrade()`` / ``downgrade()`` bound to a MigrationContext.

What must hold:
- upgrade creates the table with the partial unique index on LIVE values only;
- the server defaults fill tier and created_at for a bare INSERT;
- deleting the entity takes its values along (FK CASCADE);
- upgrade -> downgrade -> upgrade is clean, a second upgrade is a no-op.
Fictitious numbers only.
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

NUMBER = "+491701234567"


def _load_migration():
    import services.database as _db

    path = (Path(_db.__file__).resolve().parents[1] / "alembic" / "versions"
            / "pc20261126_kg_contact_points.py")
    spec = importlib.util.spec_from_file_location("pc20261126_under_test", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.fixture
async def schema_conn(pg_url):
    schema = f"pc1240_{uuid.uuid4().hex[:12]}"
    setup = create_async_engine(pg_url, poolclass=NullPool, future=True)
    async with setup.connect() as c:
        await c.exec_driver_sql(f'CREATE SCHEMA "{schema}"')
        await c.commit()
    await setup.dispose()
    # The test schema ALONE on the search_path: the migration's ``_has_table``
    # lists every table VISIBLE through it (``pg_table_is_visible``), and
    # ``public`` holds the harness's full schema with this very table -- with
    # ``public`` on the path the upgrade would skip. Set at connect time so the
    # dialect's default schema is the test schema too.
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


_PRE_DDL = [
    "CREATE TABLE users (id SERIAL PRIMARY KEY)",
    "CREATE TABLE documents (id SERIAL PRIMARY KEY)",
    "CREATE TABLE kg_entities (id SERIAL PRIMARY KEY, circle_tier INTEGER NOT NULL DEFAULT 0)",
]


async def _setup(conn) -> int:
    for stmt in _PRE_DDL:
        await conn.exec_driver_sql(stmt)
    eid = (await conn.execute(text(
        "INSERT INTO kg_entities (circle_tier) VALUES (2) RETURNING id"))).scalar()
    await conn.commit()
    return eid


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
    return bool((await conn.execute(text(
        "SELECT 1 FROM information_schema.tables WHERE table_schema = :s "
        "AND table_name = 'kg_entity_contact_points'"), {"s": schema})).first())


async def _insert(conn, eid: int, *, ended: bool = False) -> int:
    return (await conn.execute(text(
        "INSERT INTO kg_entity_contact_points (entity_id, kind, value, normalized, source_type, valid_to) "
        "VALUES (:e, 'mobile', '0170 1234567', :n, 'manual', CASE WHEN :ended THEN now() END) "
        "RETURNING id"), {"e": eid, "n": NUMBER, "ended": ended})).scalar()


async def test_upgrade_creates_table_defaults_and_live_unique(schema_conn):
    conn, schema = schema_conn
    eid = await _setup(conn)
    await _run(conn, "upgrade")
    assert await _has_table(conn, schema)

    cid = await _insert(conn, eid)
    row = (await conn.execute(text(
        "SELECT source_tier, circle_tier, created_at IS NOT NULL "
        "FROM kg_entity_contact_points WHERE id = :i"), {"i": cid})).one()
    assert tuple(row) == (0, 0, True)
    # An ENDED copy of the same value may coexist; a second LIVE one may not.
    await _insert(conn, eid, ended=True)
    await conn.commit()
    with pytest.raises(IntegrityError, match="uq_contact_points_live"):
        await _insert(conn, eid)
    await conn.rollback()

    await conn.execute(text("DELETE FROM kg_entities WHERE id = :e"), {"e": eid})
    left = (await conn.execute(text("SELECT count(*) FROM kg_entity_contact_points"))).scalar()
    assert left == 0


async def test_round_trip_is_clean_and_idempotent(schema_conn):
    conn, schema = schema_conn
    await _setup(conn)
    await _run(conn, "upgrade")
    await _run(conn, "downgrade")
    assert not await _has_table(conn, schema)
    await _run(conn, "upgrade")
    assert await _has_table(conn, schema)
    # A second upgrade on an existing table is a no-op (the fresh-install case).
    await _run(conn, "upgrade")
    assert await _has_table(conn, schema)
