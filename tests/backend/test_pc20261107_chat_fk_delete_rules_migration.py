"""Real-Postgres tests for ``pc20261107_chat_fk_rules``.

Harness as in ``test_pc20261106_conversation_tombstones_migration.py``: a
throwaway schema with the pre-migration shape (foreign keys without a delete
rule) and the real ``upgrade()`` / ``downgrade()`` bound to a MigrationContext.

What must hold:
- after the upgrade, deleting a conversation removes its messages, and the
  memory and the relation that point at one of those messages stay with the
  pointer cleared;
- no row is touched by the upgrade itself;
- the constraint is found by its column, whatever its name, and a duplicate
  constraint on the same column goes as well;
- a missing table is skipped; a second upgrade is a no-op;
- the downgrade restores the rule-less constraints; upgrade -> downgrade ->
  upgrade is clean.
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
            / "pc20261107_chat_fk_delete_rules.py")
    spec = importlib.util.spec_from_file_location("pc20261107_chat_fk_rules_under_test", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.fixture
async def schema_conn(pg_url):
    engine = create_async_engine(pg_url, poolclass=NullPool, future=True)
    schema = f"pcfkr_{uuid.uuid4().hex[:12]}"
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


async def _setup(conn, *, kg_relations: bool = True) -> None:
    await conn.exec_driver_sql("CREATE TABLE conversations (id INTEGER PRIMARY KEY)")
    # A migration-given name on one install, the create_all name on another.
    await conn.exec_driver_sql(
        "CREATE TABLE messages (id INTEGER PRIMARY KEY, conversation_id INTEGER,"
        " CONSTRAINT legacy_msg_conv FOREIGN KEY (conversation_id) REFERENCES conversations (id))")
    await conn.exec_driver_sql(
        "CREATE TABLE conversation_memories (id INTEGER PRIMARY KEY,"
        " source_message_id INTEGER REFERENCES messages (id))")
    if kg_relations:
        await conn.exec_driver_sql(
            "CREATE TABLE kg_relations (id INTEGER PRIMARY KEY,"
            " source_message_id INTEGER REFERENCES messages (id))")
    await conn.execute(text("INSERT INTO conversations (id) VALUES (1), (2)"))
    await conn.execute(text("INSERT INTO messages (id, conversation_id) VALUES (10, 1), (11, 1), (20, 2)"))
    await conn.execute(text("INSERT INTO conversation_memories (id, source_message_id) VALUES (100, 10), (200, 20)"))
    if kg_relations:
        await conn.execute(text("INSERT INTO kg_relations (id, source_message_id) VALUES (300, 11), (400, 20)"))
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


async def _rules(conn, schema) -> dict[tuple[str, str], list[str]]:
    rows = (await conn.execute(text(
        "SELECT c.relname, a.attname, k.confdeltype::text FROM pg_constraint k"
        " JOIN pg_class c ON c.oid = k.conrelid"
        " JOIN pg_namespace n ON n.oid = c.relnamespace"
        " JOIN pg_attribute a ON a.attrelid = k.conrelid AND a.attnum = ANY (k.conkey)"
        " WHERE n.nspname = :s AND k.contype = 'f' ORDER BY 1, 2, 3"), {"s": schema})).all()
    out: dict[tuple[str, str], list[str]] = {}
    for table, column, rule in rows:
        out.setdefault((table, column), []).append(rule)
    return out


async def _rows(conn, table: str) -> list[tuple]:
    return (await conn.execute(text(f"SELECT * FROM {table} ORDER BY id"))).all()


AFTER = {
    ("conversation_memories", "source_message_id"): ["n"],
    ("kg_relations", "source_message_id"): ["n"],
    ("messages", "conversation_id"): ["c"],
}
BEFORE = {key: ["a"] for key in AFTER}


async def test_upgrade_lets_a_conversation_go_with_its_messages(schema_conn):
    conn, schema = schema_conn
    await _setup(conn)
    with pytest.raises(Exception, match="foreign key"):
        await conn.execute(text("DELETE FROM conversations WHERE id = 1"))
    await conn.rollback()

    await _run(conn, "upgrade")

    assert await _rules(conn, schema) == AFTER
    assert len(await _rows(conn, "messages")) == 3  # the upgrade touches no row
    await conn.execute(text("DELETE FROM conversations WHERE id = 1"))
    await conn.commit()
    assert await _rows(conn, "messages") == [(20, 2)]
    assert await _rows(conn, "conversation_memories") == [(100, None), (200, 20)]
    assert await _rows(conn, "kg_relations") == [(300, None), (400, 20)]


async def test_a_duplicate_constraint_on_the_column_goes_too(schema_conn):
    conn, schema = schema_conn
    await _setup(conn)
    await conn.exec_driver_sql(
        "ALTER TABLE messages ADD CONSTRAINT messages_conversation_id_fkey"
        " FOREIGN KEY (conversation_id) REFERENCES conversations (id)")
    await conn.commit()

    await _run(conn, "upgrade")

    assert (await _rules(conn, schema))[("messages", "conversation_id")] == ["c"]


async def test_a_missing_table_is_skipped(schema_conn):
    conn, schema = schema_conn
    await _setup(conn, kg_relations=False)

    await _run(conn, "upgrade")

    assert await _rules(conn, schema) == {
        k: v for k, v in AFTER.items() if k[0] != "kg_relations"
    }


async def test_second_upgrade_is_a_noop(schema_conn):
    conn, schema = schema_conn
    await _setup(conn)
    await _run(conn, "upgrade")

    await _run(conn, "upgrade")

    assert await _rules(conn, schema) == AFTER


async def test_round_trip(schema_conn):
    conn, schema = schema_conn
    await _setup(conn)
    before = {t: await _rows(conn, t) for t in ("messages", "conversation_memories", "kg_relations")}

    await _run(conn, "upgrade")
    await _run(conn, "downgrade")

    assert await _rules(conn, schema) == BEFORE
    assert {t: await _rows(conn, t) for t in before} == before

    await _run(conn, "upgrade")
    assert await _rules(conn, schema) == AFTER
