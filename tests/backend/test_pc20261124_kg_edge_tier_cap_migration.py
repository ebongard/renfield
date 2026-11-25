"""Real-Postgres tests for ``pc20261124_kg_edge_tier_cap`` (#1625).

Harness as in ``test_pc20260929_kg_validity_migration.py``: a throwaway schema,
the pre-migration shape of the tables the migration touches, and the real
``upgrade()`` / ``downgrade()`` bound to a MigrationContext.

What must hold:
- a document edge created AFTER the cutover is capped at its document's tier
  and its atom policy follows;
- a document edge from BEFORE the cutover stays uncapped (decision D-2a);
- an edge without a document source stays uncapped;
- upgrade -> downgrade -> upgrade is clean; the downgrade keeps lowered tiers.
"""
from __future__ import annotations

import importlib.util
import json
import uuid
from datetime import datetime
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

pytestmark = [pytest.mark.database]


def _mig_path() -> Path:
    import services.database as _db

    return (Path(_db.__file__).resolve().parents[1] / "alembic" / "versions"
            / "pc20261124_kg_edge_tier_cap.py")


def _load_migration():
    spec = importlib.util.spec_from_file_location("pc20261124_under_test", _mig_path())
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.fixture
async def schema_conn(pg_url):
    engine = create_async_engine(pg_url, poolclass=NullPool, future=True)
    schema = f"pc1625_{uuid.uuid4().hex[:12]}"
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


_PRE_DDL = [
    "CREATE TABLE documents (id SERIAL PRIMARY KEY, circle_tier INTEGER NOT NULL DEFAULT 0)",
    "CREATE TABLE kg_entities (id SERIAL PRIMARY KEY, circle_tier INTEGER NOT NULL DEFAULT 0)",
    "CREATE TABLE atoms (atom_id VARCHAR(36) PRIMARY KEY, atom_type VARCHAR(32),"
    " source_id VARCHAR(64), policy JSON, updated_at TIMESTAMP)",
    (
        "CREATE TABLE kg_relations ("
        " id SERIAL PRIMARY KEY,"
        " subject_id INTEGER NOT NULL REFERENCES kg_entities(id),"
        " predicate VARCHAR(100) NOT NULL,"
        " object_id INTEGER NOT NULL REFERENCES kg_entities(id),"
        " source_session_id VARCHAR(255),"
        " circle_tier INTEGER NOT NULL DEFAULT 0,"
        " created_at TIMESTAMP)"
    ),
]


async def _setup(conn) -> dict[str, int]:
    for stmt in _PRE_DDL:
        await conn.exec_driver_sql(stmt)
    doc = (await conn.execute(text(
        "INSERT INTO documents (circle_tier) VALUES (0) RETURNING id"))).scalar()
    a = (await conn.execute(text(
        "INSERT INTO kg_entities (circle_tier) VALUES (2) RETURNING id"))).scalar()
    b = (await conn.execute(text(
        "INSERT INTO kg_entities (circle_tier) VALUES (2) RETURNING id"))).scalar()
    ids: dict[str, int] = {}
    for key, source, created in (
        ("after", f"doc:{doc}", datetime(2026, 10, 11, 14, 15)),
        ("before", f"doc:{doc}", datetime(2026, 9, 8, 20, 0)),
        ("chat", None, datetime(2026, 10, 12, 9, 0)),
    ):
        rid = (await conn.execute(text(
            "INSERT INTO kg_relations (subject_id, predicate, object_id, source_session_id,"
            " circle_tier, created_at) VALUES (:a, 'kennt', :b, :src, 2, :c)"
            " RETURNING id"), {"a": a, "b": b, "src": source, "c": created})).scalar()
        await conn.execute(text(
            "INSERT INTO atoms (atom_id, atom_type, source_id, policy) "
            "VALUES (:aid, 'kg_edge', :sid, CAST(:p AS JSON))"),
            {"aid": f"edge-{key}", "sid": str(rid), "p": json.dumps({"tier": 2})})
        ids[key] = rid
    await conn.commit()
    return ids


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


async def _edge(conn, rid: int) -> tuple:
    return (await conn.execute(text(
        "SELECT r.circle_tier, r.tier_cap, (a.policy->>'tier')::int "
        "FROM kg_relations r JOIN atoms a ON a.source_id = r.id::text WHERE r.id = :r"),
        {"r": rid})).one()


async def _has_cap_column(conn, schema) -> bool:
    return bool((await conn.execute(text(
        "SELECT 1 FROM information_schema.columns WHERE table_schema = :s "
        "AND table_name = 'kg_relations' AND column_name = 'tier_cap'"), {"s": schema})).first())


async def test_upgrade_caps_only_post_cutover_document_edges(schema_conn):
    conn, schema = schema_conn
    ids = await _setup(conn)
    await _run(conn, "upgrade")

    assert await _has_cap_column(conn, schema)
    # After the cutover, from a tier-0 document: capped, tier and policy down.
    assert await _edge(conn, ids["after"]) == (0, 0, 0)
    # Before the cutover: D-2a, untouched and uncapped.
    assert await _edge(conn, ids["before"]) == (2, None, 2)
    # No document source: nothing to cap by.
    assert await _edge(conn, ids["chat"]) == (2, None, 2)


async def test_round_trip_keeps_lowered_tier_and_is_idempotent(schema_conn):
    conn, schema = schema_conn
    ids = await _setup(conn)
    await _run(conn, "upgrade")
    await _run(conn, "downgrade")
    assert not await _has_cap_column(conn, schema)
    # The lowered tier stays lowered: the previous one is not recorded anywhere.
    row = (await conn.execute(text(
        "SELECT circle_tier FROM kg_relations WHERE id = :r"), {"r": ids["after"]})).scalar()
    assert row == 0

    await _run(conn, "upgrade")
    assert await _edge(conn, ids["after"]) == (0, 0, 0)
    assert await _edge(conn, ids["before"]) == (2, None, 2)
    # A second upgrade on an already-migrated table is a no-op.
    await _run(conn, "upgrade")
    assert await _edge(conn, ids["after"]) == (0, 0, 0)
