"""Real-Postgres round trip for ``pc20261110_routing_traces`` (#1524).

Harness as in ``test_pc20261108_chat_paperless_intent_migration.py``: a throwaway
schema with a minimal ``users`` table and the real ``upgrade()`` / ``downgrade()``
bound to a MigrationContext. Checks the table, both indexes and the CASCADE on
the user (the row holds the user's full message), the reverse path, and that an
upgrade over a table the baseline's ``create_all`` already built is a no-op.
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

    path = Path(_db.__file__).resolve().parents[1] / "alembic" / "versions" / "pc20261110_routing_traces.py"
    spec = importlib.util.spec_from_file_location("pc20261110_routing_traces_under_test", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.fixture
async def schema_conn(pg_url):
    engine = create_async_engine(pg_url, poolclass=NullPool, future=True)
    schema = f"pcrt_{uuid.uuid4().hex[:12]}"
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


async def _has_table(conn, schema) -> bool:
    return bool(
        (
            await conn.execute(
                text("SELECT to_regclass(quote_ident(:s) || '.routing_traces') IS NOT NULL"),
                {"s": schema},
            )
        ).scalar()
    )


async def _indexes(conn, schema) -> set[str]:
    rows = await conn.execute(
        text("SELECT indexname FROM pg_indexes WHERE schemaname = :s AND tablename = 'routing_traces'"),
        {"s": schema},
    )
    return {r[0] for r in rows}


async def _add_trace(conn, user_id) -> None:
    await conn.execute(
        text(
            "INSERT INTO routing_traces (user_id, message, domain, layer, created_at)"
            " VALUES (:u, 'Licht an', 'smart_home', 'semantic', now())"
        ),
        {"u": user_id},
    )
    await conn.commit()


async def test_round_trip_and_user_cascade(schema_conn):
    conn, schema = schema_conn
    await conn.exec_driver_sql("CREATE TABLE users (id INTEGER PRIMARY KEY)")
    await conn.execute(text("INSERT INTO users VALUES (1), (2)"))
    await conn.commit()

    await _run(conn, "upgrade")
    assert await _has_table(conn, schema)
    assert {"idx_routing_traces_created", "idx_routing_traces_domain_created"} <= await _indexes(conn, schema)

    await _add_trace(conn, 1)
    await _add_trace(conn, 2)
    await _add_trace(conn, None)
    await conn.execute(text("DELETE FROM users WHERE id = 1"))
    await conn.commit()
    assert (await conn.execute(text("SELECT count(*) FROM routing_traces"))).scalar() == 2

    await _run(conn, "downgrade")
    assert not await _has_table(conn, schema)

    await _run(conn, "upgrade")
    assert await _has_table(conn, schema)


async def test_upgrade_over_an_existing_table_is_a_no_op(schema_conn):
    """A fresh install's baseline already built the table from the models."""
    conn, _schema = schema_conn
    await conn.exec_driver_sql("CREATE TABLE users (id INTEGER PRIMARY KEY)")
    await conn.commit()
    await _run(conn, "upgrade")
    await _add_trace(conn, None)

    await _run(conn, "upgrade")
    assert (await conn.execute(text("SELECT count(*) FROM routing_traces"))).scalar() == 1


def test_revision_chain():
    mig = _load_migration()
    assert mig.revision == "pc20261110_routing_traces"
    assert mig.down_revision == "pc20261108_chat_pl_intent"
