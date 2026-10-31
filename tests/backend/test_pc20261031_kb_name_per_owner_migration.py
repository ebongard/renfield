"""Real-Postgres tests for ``pc20261031_kb_name_per_owner``.

Harness as in ``test_pc20260929_kg_validity_migration.py``: a throwaway schema
with the pre-migration shape of ``knowledge_bases`` and the real ``upgrade()`` /
``downgrade()`` bound to a MigrationContext.

What must hold:
- the global UNIQUE(name) is gone -- whatever it is called, constraint or bare
  index -- and (name, owner_id) is unique NULLS NOT DISTINCT;
- existing rows are untouched;
- upgrade -> downgrade -> upgrade is clean, a second upgrade is a no-op;
- the downgrade refuses while two KBs share a name, and changes nothing then.
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


def _mig_path() -> Path:
    import services.database as _db

    return (Path(_db.__file__).resolve().parents[1] / "alembic" / "versions"
            / "pc20261031_kb_name_per_owner.py")


def _load_migration():
    spec = importlib.util.spec_from_file_location("pc20261031_kbname_under_test", _mig_path())
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.fixture
async def schema_conn(pg_url):
    engine = create_async_engine(pg_url, poolclass=NullPool, future=True)
    schema = f"pckbn_{uuid.uuid4().hex[:12]}"
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


_USERS = "CREATE TABLE users (id INTEGER PRIMARY KEY)"
# As b2c3d4e5f6g7 and create_all build it: `name ... UNIQUE` -> knowledge_bases_name_key.
_KB_WITH_CONSTRAINT = (
    "CREATE TABLE knowledge_bases (id SERIAL PRIMARY KEY,"
    " name VARCHAR(255) NOT NULL UNIQUE, owner_id INTEGER REFERENCES users(id))"
)
# A bare unique index instead of a constraint -- the upgrade must not rely on a name.
_KB_WITH_INDEX = [
    ("CREATE TABLE knowledge_bases (id SERIAL PRIMARY KEY,"
     " name VARCHAR(255) NOT NULL, owner_id INTEGER REFERENCES users(id))"),
    "CREATE UNIQUE INDEX some_other_name_idx ON knowledge_bases (name)",
]


async def _setup(conn, kb_ddl=(_KB_WITH_CONSTRAINT,)) -> None:
    await conn.exec_driver_sql(_USERS)
    for stmt in kb_ddl:
        await conn.exec_driver_sql(stmt)
    for uid in (1, 2):
        await conn.execute(text("INSERT INTO users (id) VALUES (:u)"), {"u": uid})
    await conn.execute(text(
        "INSERT INTO knowledge_bases (name, owner_id) VALUES "
        "('Eingang', NULL), ('Privat', 1), ('Chat Uploads', NULL)"))
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


async def _unique_defs(conn, schema) -> list[str]:
    rows = await conn.execute(text(
        "SELECT indexdef FROM pg_indexes WHERE schemaname = :s "
        "AND tablename = 'knowledge_bases' AND indexdef LIKE 'CREATE UNIQUE%' "
        "AND indexname NOT LIKE '%pkey' ORDER BY indexname"), {"s": schema})
    return [r[0] for r in rows]


async def _rows(conn):
    return (await conn.execute(text(
        "SELECT id, name, owner_id FROM knowledge_bases ORDER BY id"))).all()


async def _insert(conn, name, owner):
    await conn.execute(text(
        "INSERT INTO knowledge_bases (name, owner_id) VALUES (:n, :o)"), {"n": name, "o": owner})
    await conn.commit()


@pytest.mark.parametrize("kb_ddl", [(_KB_WITH_CONSTRAINT,), _KB_WITH_INDEX],
                         ids=["constraint", "bare-index"])
async def test_upgrade_makes_the_name_unique_per_owner(schema_conn, kb_ddl):
    conn, schema = schema_conn
    await _setup(conn, kb_ddl)
    before = await _rows(conn)
    await _run(conn, "upgrade")
    defs = await _unique_defs(conn, schema)
    assert len(defs) == 1, defs
    assert "uq_knowledge_bases_name_owner" in defs[0]
    assert "(name, owner_id) NULLS NOT DISTINCT" in defs[0]
    assert await _rows(conn) == before

    # A second person's inbox of the same name is now allowed ...
    await _insert(conn, "Eingang", 1)
    await _insert(conn, "Eingang", 2)
    # ... but not a second one for the same owner, nor a second ownerless one.
    for owner in (1, None):
        with pytest.raises(IntegrityError):
            await _insert(conn, "Eingang", owner)
        await conn.rollback()


async def test_second_upgrade_is_a_noop(schema_conn):
    conn, schema = schema_conn
    await _setup(conn)
    await _run(conn, "upgrade")
    defs = await _unique_defs(conn, schema)
    await _run(conn, "upgrade")
    assert await _unique_defs(conn, schema) == defs


async def test_round_trip(schema_conn):
    conn, schema = schema_conn
    await _setup(conn)
    before_defs = await _unique_defs(conn, schema)
    before_rows = await _rows(conn)
    await _run(conn, "upgrade")
    await _run(conn, "downgrade")
    assert await _unique_defs(conn, schema) == before_defs  # knowledge_bases_name_key is back
    assert await _rows(conn) == before_rows
    with pytest.raises(IntegrityError):
        await _insert(conn, "Privat", 2)  # global uniqueness enforced again
    await conn.rollback()
    await _run(conn, "upgrade")
    assert "uq_knowledge_bases_name_owner" in (await _unique_defs(conn, schema))[0]


async def test_downgrade_refuses_while_a_name_is_shared(schema_conn):
    conn, schema = schema_conn
    await _setup(conn)
    await _run(conn, "upgrade")
    await _insert(conn, "Eingang", 2)  # a per-owner inbox next to the shared one
    defs = await _unique_defs(conn, schema)
    rows = await _rows(conn)
    with pytest.raises(RuntimeError, match="refuses to choose"):
        await _run(conn, "downgrade")
    assert await _unique_defs(conn, schema) == defs
    assert await _rows(conn) == rows
