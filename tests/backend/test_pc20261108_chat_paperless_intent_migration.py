"""Real-Postgres round trip for ``pc20261108_chat_pl_intent``.

Harness as in ``test_pc20261107_chat_fk_delete_rules_migration.py``: a throwaway
schema with the pre-migration ``chat_uploads`` shape and the real ``upgrade()`` /
``downgrade()`` bound to a MigrationContext. An existing row keeps its data and
reads NULL in the new column; upgrade -> downgrade -> upgrade is clean.
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

    path = Path(_db.__file__).resolve().parents[1] / "alembic" / "versions" / "pc20261108_chat_paperless_intent.py"
    spec = importlib.util.spec_from_file_location("pc20261108_chat_pl_intent_under_test", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.fixture
async def schema_conn(pg_url):
    engine = create_async_engine(pg_url, poolclass=NullPool, future=True)
    schema = f"pcpli_{uuid.uuid4().hex[:12]}"
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


async def _has_column(conn, schema) -> bool:
    return bool(
        (
            await conn.execute(
                text(
                    "SELECT 1 FROM information_schema.columns WHERE table_schema = :s"
                    " AND table_name = 'chat_uploads' AND column_name = 'paperless_intent'"
                ),
                {"s": schema},
            )
        ).first()
    )


async def test_round_trip_keeps_rows_and_reads_null(schema_conn):
    conn, schema = schema_conn
    await conn.exec_driver_sql(
        "CREATE TABLE chat_uploads (id INTEGER PRIMARY KEY, session_id VARCHAR(128) NOT NULL,"
        " filename VARCHAR(255) NOT NULL)"
    )
    await conn.execute(text("INSERT INTO chat_uploads VALUES (1, 's', 'a.pdf')"))
    await conn.commit()

    await _run(conn, "upgrade")
    assert await _has_column(conn, schema)
    assert (await conn.execute(text("SELECT filename, paperless_intent FROM chat_uploads"))).all() == [("a.pdf", None)]
    await conn.execute(text("UPDATE chat_uploads SET paperless_intent = 'requested'"))
    await conn.commit()

    await _run(conn, "downgrade")
    assert not await _has_column(conn, schema)
    assert (await conn.execute(text("SELECT count(*) FROM chat_uploads"))).scalar() == 1

    await _run(conn, "upgrade")
    assert await _has_column(conn, schema)


def test_revision_chain():
    mig = _load_migration()
    assert mig.revision == "pc20261108_chat_pl_intent"
    assert mig.down_revision == "pc20261107_chat_fk_rules"
