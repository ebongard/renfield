"""Real-Postgres round trip for ``pc20261111_push_subs`` (#79).

Harness as in ``test_pc20261110_routing_traces_migration.py``: a throwaway
schema with a minimal ``users`` table and the real ``upgrade()`` /
``downgrade()`` bound to a MigrationContext. Checks the table, the unique
endpoint index, the NOT NULL owner, the CASCADE on the user, the reverse path
(forward -> reverse -> forward), and that an upgrade over a table the
baseline's ``create_all`` already built is a no-op.
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

TABLE = "push_subscriptions"


def _load_migration():
    import services.database as _db

    path = Path(_db.__file__).resolve().parents[1] / "alembic" / "versions" / "pc20261111_push_subscriptions.py"
    spec = importlib.util.spec_from_file_location("pc20261111_push_subscriptions_under_test", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.fixture
async def schema_conn(pg_url):
    engine = create_async_engine(pg_url, poolclass=NullPool, future=True)
    schema = f"pcps_{uuid.uuid4().hex[:12]}"
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
                text(f"SELECT to_regclass(quote_ident(:s) || '.{TABLE}') IS NOT NULL"),
                {"s": schema},
            )
        ).scalar()
    )


async def _indexes(conn, schema) -> dict[str, str]:
    rows = await conn.execute(
        text("SELECT indexname, indexdef FROM pg_indexes WHERE schemaname = :s AND tablename = :t"),
        {"s": schema, "t": TABLE},
    )
    return {r[0]: r[1] for r in rows}


async def _count(conn) -> int:
    return (await conn.execute(text(f"SELECT count(*) FROM {TABLE}"))).scalar()


async def _add(conn, user_id, endpoint) -> None:
    await conn.execute(
        text(
            f"INSERT INTO {TABLE} (user_id, endpoint, p256dh, auth, created_at)"
            " VALUES (:u, :e, 'p', 'a', now())"
        ),
        {"u": user_id, "e": endpoint},
    )
    await conn.commit()


async def _users(conn) -> None:
    await conn.exec_driver_sql("CREATE TABLE users (id INTEGER PRIMARY KEY)")
    await conn.execute(text("INSERT INTO users VALUES (1), (2)"))
    await conn.commit()


async def test_round_trip_constraints_and_user_cascade(schema_conn):
    conn, schema = schema_conn
    await _users(conn)

    await _run(conn, "upgrade")
    assert await _has_table(conn, schema)
    indexes = await _indexes(conn, schema)
    assert {"uq_push_subscriptions_endpoint", "idx_push_subscriptions_user"} <= set(indexes)
    assert "UNIQUE" in indexes["uq_push_subscriptions_endpoint"]

    await _add(conn, 1, "https://fcm.googleapis.com/fcm/send/a")
    await _add(conn, 2, "https://fcm.googleapis.com/fcm/send/b")

    # One row per endpoint: the endpoint IS the device.
    with pytest.raises(IntegrityError):
        await _add(conn, 2, "https://fcm.googleapis.com/fcm/send/a")
    await conn.rollback()
    # A subscription without an owner cannot exist.
    with pytest.raises(IntegrityError):
        await _add(conn, None, "https://fcm.googleapis.com/fcm/send/c")
    await conn.rollback()

    await conn.execute(text("DELETE FROM users WHERE id = 1"))
    await conn.commit()
    assert await _count(conn) == 1

    await _run(conn, "downgrade")
    assert not await _has_table(conn, schema)
    # The rest of the schema is untouched by the reverse path.
    assert (await conn.execute(text("SELECT count(*) FROM users"))).scalar() == 1

    await _run(conn, "upgrade")
    assert await _has_table(conn, schema)
    assert await _count(conn) == 0


async def test_upgrade_over_an_existing_table_is_a_no_op(schema_conn):
    """A fresh install's baseline already built the table from the models."""
    conn, _schema = schema_conn
    await _users(conn)
    await _run(conn, "upgrade")
    await _add(conn, 1, "https://fcm.googleapis.com/fcm/send/a")

    await _run(conn, "upgrade")
    assert await _count(conn) == 1


async def test_downgrade_without_the_table_is_a_no_op(schema_conn):
    conn, schema = schema_conn
    await _users(conn)
    await _run(conn, "downgrade")
    assert not await _has_table(conn, schema)


async def test_migration_and_model_describe_the_same_table(schema_conn):
    """The baseline builds the table from the MODEL on a fresh install, the
    migration on an existing one. Both must yield the same columns."""
    conn, schema = schema_conn
    await _users(conn)
    await _run(conn, "upgrade")
    rows = await conn.execute(
        text(
            "SELECT column_name, is_nullable FROM information_schema.columns"
            " WHERE table_schema = :s AND table_name = :t"
        ),
        {"s": schema, "t": TABLE},
    )
    migrated = {r[0]: r[1] == "YES" for r in rows}

    from models.database import PushSubscription

    modelled = {c.name: bool(c.nullable) for c in PushSubscription.__table__.columns}
    assert migrated == modelled
    assert {i.name for i in PushSubscription.__table__.indexes} == set(await _indexes(conn, schema)) - {
        "push_subscriptions_pkey"
    }


def test_revision_chain():
    mig = _load_migration()
    assert mig.revision == "pc20261111_push_subs"
    assert mig.down_revision == "pc20261111_tombstone_idx"
