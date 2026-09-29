"""Real-Postgres tests for ``pc20260929_kg_validity`` (#875 Stufe 1).

Harness as in ``test_pc20260423_migration.py``: a throwaway schema, the
pre-migration shape of the one table the migration touches, and the real
``upgrade()`` / ``downgrade()`` bound to a MigrationContext. The migration uses
``autocommit_block()`` for ``CREATE INDEX CONCURRENTLY``; the tests prove that
path works on this harness too.

What must hold (docs/design/kg-bitemporal-edges.md §5, §12.1):
- NO BACKFILL — existing rows keep valid_from/valid_to NULL.
- the partial index exists, is VALID, and carries the live predicate.
- invalidated_by_relation_id is ON DELETE SET NULL.
- upgrade → downgrade → upgrade is clean; a second upgrade is a no-op.
"""
from __future__ import annotations

import importlib.util
import os
import uuid
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

pytestmark = [pytest.mark.database]


def _mig_path() -> Path:
    import services.database as _db

    return (Path(_db.__file__).resolve().parents[1] / "alembic" / "versions"
            / "pc20260929_kg_relation_validity.py")


def _load_migration():
    spec = importlib.util.spec_from_file_location("pc20260929_under_test", _mig_path())
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.fixture
async def schema_conn():
    dsn = os.environ.get("RENFIELD_TEST_PG_URL")
    if dsn is None:
        pytest.skip("RENFIELD_TEST_PG_URL not set — Postgres tests disabled")
    if dsn.startswith("postgresql://"):
        dsn = dsn.replace("postgresql://", "postgresql+asyncpg://", 1)
    engine = create_async_engine(dsn, poolclass=NullPool, future=True)
    schema = f"pc875_{uuid.uuid4().hex[:12]}"
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


# Pre-migration shape of kg_relations (only what the migration and the checks touch).
_PRE_DDL = [
    "CREATE TABLE kg_entities (id SERIAL PRIMARY KEY, name VARCHAR(255))",
    (
        "CREATE TABLE kg_relations ("
        " id SERIAL PRIMARY KEY,"
        " subject_id INTEGER NOT NULL REFERENCES kg_entities(id),"
        " predicate VARCHAR(100) NOT NULL,"
        " object_id INTEGER NOT NULL REFERENCES kg_entities(id),"
        " is_active BOOLEAN DEFAULT true,"
        " created_at TIMESTAMP DEFAULT NOW())"
    ),
]


async def _setup(conn) -> tuple[int, int]:
    for stmt in _PRE_DDL:
        await conn.exec_driver_sql(stmt)
    a = (await conn.execute(text("INSERT INTO kg_entities (name) VALUES ('Erika') RETURNING id"))).scalar()
    b = (await conn.execute(text("INSERT INTO kg_entities (name) VALUES ('Bonn') RETURNING id"))).scalar()
    await conn.execute(text(
        "INSERT INTO kg_relations (subject_id, predicate, object_id, created_at) "
        "VALUES (:a, 'wohnt_in', :b, '2025-01-15 10:00')"), {"a": a, "b": b})
    await conn.commit()
    return a, b


async def _run(conn, direction: str) -> None:
    from alembic.operations import Operations
    from alembic.runtime.migration import MigrationContext

    mig = _load_migration()

    def _apply(sync_conn):
        # Mirror alembic/env.py: transaction_per_migration + a logical transaction
        # opened by alembic itself. autocommit_block() asserts that transaction
        # exists — the older harness (pc20260423) skips it because its migration
        # never needed an autocommit block.
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

    await conn.commit()  # alembic must open its own transaction, not inherit one
    try:
        await conn.run_sync(_apply)
    except Exception:
        await conn.rollback()
        raise
    await conn.commit()


async def _columns(conn, schema) -> set[str]:
    rows = await conn.execute(text(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema = :s AND table_name = 'kg_relations'"), {"s": schema})
    return {r[0] for r in rows}


async def _index(conn, schema):
    return (await conn.execute(text(
        "SELECT pg_get_indexdef(i.indexrelid), i.indisvalid "
        "FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid "
        "JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = :s AND c.relname = 'idx_kg_relations_live'"), {"s": schema})).first()


_NEW = {"valid_from", "valid_to", "invalidated_by_relation_id"}


async def test_upgrade_adds_columns_without_backfill(schema_conn):
    conn, schema = schema_conn
    await _setup(conn)
    await _run(conn, "upgrade")
    assert _NEW <= await _columns(conn, schema)
    row = (await conn.execute(text(
        "SELECT valid_from, valid_to, invalidated_by_relation_id FROM kg_relations"))).one()
    # 🛑 The design's central rule: the extraction time is NOT the validity start.
    assert tuple(row) == (None, None, None)


async def test_partial_index_is_valid_and_live(schema_conn):
    conn, schema = schema_conn
    await _setup(conn)
    await _run(conn, "upgrade")
    idx = await _index(conn, schema)
    assert idx is not None, "idx_kg_relations_live missing"
    definition, valid = idx
    assert valid is True
    assert "(subject_id, predicate)" in definition
    assert "is_active = true" in definition and "valid_to IS NULL" in definition


async def test_invalidated_by_is_set_null_on_delete(schema_conn):
    conn, _ = schema_conn
    a, b = await _setup(conn)
    await _run(conn, "upgrade")
    old_id = (await conn.execute(text("SELECT id FROM kg_relations"))).scalar()
    new_id = (await conn.execute(text(
        "INSERT INTO kg_relations (subject_id, predicate, object_id) "
        "VALUES (:a, 'wohnt_in', :b) RETURNING id"), {"a": a, "b": b})).scalar()
    await conn.execute(text(
        "UPDATE kg_relations SET valid_to = NOW(), invalidated_by_relation_id = :n "
        "WHERE id = :o"), {"n": new_id, "o": old_id})
    await conn.execute(text("DELETE FROM kg_relations WHERE id = :n"), {"n": new_id})
    await conn.commit()
    row = (await conn.execute(text(
        "SELECT valid_to IS NOT NULL, invalidated_by_relation_id FROM kg_relations "
        "WHERE id = :o"), {"o": old_id})).one()
    # The expired edge survives its successor's deletion, just loses the pointer.
    assert tuple(row) == (True, None)


async def test_upgrade_downgrade_upgrade_cycle(schema_conn):
    conn, schema = schema_conn
    await _setup(conn)
    await _run(conn, "upgrade")
    await _run(conn, "downgrade")
    assert not (_NEW & await _columns(conn, schema))
    assert await _index(conn, schema) is None
    await _run(conn, "upgrade")
    assert _NEW <= await _columns(conn, schema)
    assert (await _index(conn, schema))[1] is True


async def test_second_upgrade_is_a_noop(schema_conn):
    """create_all databases already carry the columns — the migration must not
    fail or add a second FK."""
    conn, schema = schema_conn
    await _setup(conn)
    await _run(conn, "upgrade")
    await _run(conn, "upgrade")
    fks = (await conn.execute(text(
        "SELECT count(*) FROM information_schema.table_constraints tc "
        "JOIN information_schema.key_column_usage k "
        "  ON k.constraint_name = tc.constraint_name AND k.table_schema = tc.table_schema "
        "WHERE tc.table_schema = :s AND tc.table_name = 'kg_relations' "
        "AND tc.constraint_type = 'FOREIGN KEY' "
        "AND k.column_name = 'invalidated_by_relation_id'"), {"s": schema})).scalar()
    assert fks == 1
