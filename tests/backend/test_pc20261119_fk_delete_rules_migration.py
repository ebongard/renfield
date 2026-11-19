"""Real-Postgres round trip for ``pc20261119_fk_delete_rules`` (#1585).

Harness as in ``test_pc20261114_fed_log_user_null_migration.py``: a throwaway
schema that is the ONLY one on the search path, holding the tables in their
pre-migration shape, and the real ``upgrade()`` / ``downgrade()`` bound to a
MigrationContext.

What is checked: the delete rule of each key before and after, that the
hourly cleanup's DELETE now goes through and leaves the reminder and the
suppression in place, the reverse path (forward -> reverse -> forward) with
row counts, that a key already carrying the rule is left alone (fresh
install; the household's procedural_skills key), and that model and migration
agree.
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

REVISION = "pc20261119_fk_delete_rules"
PREVIOUS = "pc20261114_fed_log_user_null"


def _versions_dir() -> Path:
    import services.database as _db

    return Path(_db.__file__).resolve().parents[1] / "alembic" / "versions"


def _load_migration():
    path = _versions_dir() / f"{REVISION}.py"
    spec = importlib.util.spec_from_file_location(f"{REVISION}_under_test", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.fixture
async def schema_conn(pg_url):
    """A connection that sees ONE schema: a throwaway one."""
    schema = f"pcfk_{uuid.uuid4().hex[:12]}"
    admin = create_async_engine(pg_url, poolclass=NullPool, future=True)
    async with admin.connect() as setup:
        await setup.exec_driver_sql(f'CREATE SCHEMA "{schema}"')
        await setup.commit()
    engine = create_async_engine(
        pg_url, poolclass=NullPool, future=True,
        connect_args={"server_settings": {"search_path": f'"{schema}"'}},
    )
    conn = await engine.connect()
    try:
        assert (await conn.execute(text("SELECT current_schema()"))).scalar() == schema
        await conn.commit()
        yield conn, schema
    finally:
        try:
            await conn.rollback()
        except Exception:
            pass
        await conn.close()
        await engine.dispose()
        try:
            async with admin.connect() as teardown:
                await teardown.exec_driver_sql(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
                await teardown.commit()
        finally:
            await admin.dispose()


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


async def _pre_migration_tables(conn, *, skills_rule: str = "") -> None:
    """The five tables as they stood at ``pc20261114``, reduced to the columns
    that matter: one expired notification referenced by a reminder AND a
    suppression, one conversation a skill was learned from. ``skills_rule``
    is xidra's state ('') or the household's (' ON DELETE SET NULL')."""
    for ddl in (
        "CREATE TABLE notifications (id SERIAL PRIMARY KEY, title TEXT, expires_at TIMESTAMP)",
        "CREATE TABLE reminders (id SERIAL PRIMARY KEY, message TEXT NOT NULL,"
        " notification_id INTEGER REFERENCES notifications(id))",
        "CREATE TABLE notification_suppressions (id SERIAL PRIMARY KEY, event_pattern TEXT NOT NULL,"
        " source_notification_id INTEGER REFERENCES notifications(id))",
        "CREATE TABLE conversations (id SERIAL PRIMARY KEY, session_id TEXT)",
        "CREATE TABLE procedural_skills (id SERIAL PRIMARY KEY, name TEXT,"
        f" learned_from_conversation_id INTEGER REFERENCES conversations(id){skills_rule})",
        "INSERT INTO notifications (title, expires_at) VALUES ('expired', now() - interval '1 hour')",
        "INSERT INTO reminders (message, notification_id) VALUES ('r', 1)",
        "INSERT INTO notification_suppressions (event_pattern, source_notification_id) VALUES ('p', 1)",
        "INSERT INTO conversations (session_id) VALUES ('s')",
        "INSERT INTO procedural_skills (name, learned_from_conversation_id) VALUES ('k', 1)",
    ):
        await conn.exec_driver_sql(ddl)
    await conn.commit()


async def _rules(conn, schema) -> dict[str, tuple[str, str]]:
    """table -> (constraint name, confdeltype) of its one foreign key."""
    rows = (await conn.execute(text(
        "SELECT t.relname, c.conname, c.confdeltype::text FROM pg_constraint c"
        " JOIN pg_class t ON t.oid = c.conrelid"
        " JOIN pg_namespace n ON n.oid = t.relnamespace"
        " WHERE c.contype = 'f' AND n.nspname = :s"
    ), {"s": schema})).all()
    return {r[0]: (r[1], r[2]) for r in rows}


def _kinds(rules: dict[str, tuple[str, str]]) -> dict[str, str]:
    return {t: k for t, (_n, k) in rules.items()}


async def _counts(conn) -> dict[str, int]:
    out = {}
    for t in ("notifications", "reminders", "notification_suppressions", "conversations", "procedural_skills"):
        out[t] = (await conn.execute(text(f"SELECT count(*) FROM {t}"))).scalar()
    return out


_CLEANUP = "DELETE FROM notifications WHERE expires_at IS NOT NULL AND expires_at < now()"


async def test_round_trip_lets_the_cleanup_through_and_keeps_every_reference_holder(schema_conn):
    conn, schema = schema_conn
    await _pre_migration_tables(conn)
    before = await _rules(conn, schema)
    assert _kinds(before) == {"reminders": "a", "notification_suppressions": "a", "procedural_skills": "a"}
    # The bug: the hourly cleanup is refused as a whole.
    with pytest.raises(IntegrityError, match="reminders_notification_id_fkey"):
        await conn.exec_driver_sql(_CLEANUP)
    await conn.rollback()
    counts = await _counts(conn)

    # --- forward ---------------------------------------------------------
    await _run(conn, "upgrade")
    after = await _rules(conn, schema)
    assert _kinds(after) == {"reminders": "n", "notification_suppressions": "n", "procedural_skills": "n"}
    # Same constraint names as before: rebuilt, not renamed.
    assert {t: n for t, (n, _k) in after.items()} == {t: n for t, (n, _k) in before.items()}
    assert await _counts(conn) == counts

    # --- reverse ---------------------------------------------------------
    await _run(conn, "downgrade")
    assert _kinds(await _rules(conn, schema)) == {
        "reminders": "a", "notification_suppressions": "a",
        # The model says SET NULL on both sides of the revision; whether a
        # database had it before is not recorded (the household did).
        "procedural_skills": "n",
    }
    assert await _counts(conn) == counts

    # --- forward again, then the cleanup ---------------------------------
    await _run(conn, "upgrade")
    assert set(_kinds(await _rules(conn, schema)).values()) == {"n"}
    await conn.exec_driver_sql(_CLEANUP)
    await conn.exec_driver_sql("DELETE FROM conversations")
    await conn.commit()
    assert await _counts(conn) == {
        "notifications": 0, "reminders": 1, "notification_suppressions": 1,
        "conversations": 0, "procedural_skills": 1,
    }
    refs = (await conn.execute(text(
        "SELECT (SELECT notification_id FROM reminders),"
        " (SELECT source_notification_id FROM notification_suppressions),"
        " (SELECT learned_from_conversation_id FROM procedural_skills)"
    ))).one()
    assert tuple(refs) == (None, None, None)


async def test_a_key_that_already_has_the_rule_is_left_alone(schema_conn):
    """The household's procedural_skills key, and a fresh install built from
    the model: nothing to rebuild."""
    conn, schema = schema_conn
    await _pre_migration_tables(conn, skills_rule=" ON DELETE SET NULL")
    skills_before = (await _oids(conn, schema))["procedural_skills"]
    await _run(conn, "upgrade")
    after_first = await _oids(conn, schema)
    assert after_first["procedural_skills"] == skills_before, "a key with the right rule was rebuilt"
    await _run(conn, "upgrade")
    assert await _oids(conn, schema) == after_first, "a second upgrade rebuilt a key"
    assert set(_kinds(await _rules(conn, schema)).values()) == {"n"}


async def _oids(conn, schema) -> dict[str, int]:
    rows = (await conn.execute(text(
        "SELECT t.relname, c.oid FROM pg_constraint c JOIN pg_class t ON t.oid = c.conrelid"
        " JOIN pg_namespace n ON n.oid = t.relnamespace"
        " WHERE c.contype = 'f' AND n.nspname = :s"
    ), {"s": schema})).all()
    return {r[0]: r[1] for r in rows}


async def test_a_duplicate_key_on_a_column_is_refused_before_anything_changes(schema_conn):
    """Fixing one of two keys would leave the other blocking the delete."""
    conn, schema = schema_conn
    await _pre_migration_tables(conn)
    await conn.exec_driver_sql(
        "ALTER TABLE notification_suppressions ADD CONSTRAINT dup_source_fk"
        " FOREIGN KEY (source_notification_id) REFERENCES notifications(id)"
    )
    await conn.commit()
    with pytest.raises(RuntimeError, match="2 foreign keys notification_suppressions"):
        await _run(conn, "upgrade")
    await conn.rollback()
    # The reminders key came first and was rebuilt inside the same
    # transaction -- the refusal rolled that back as well.
    assert (await _rules(conn, schema))["reminders"][1] == "a"


async def test_without_the_keys_the_upgrade_refuses_and_changes_nothing(schema_conn):
    conn, schema = schema_conn
    with pytest.raises(RuntimeError, match=r"no foreign key reminders\.notification_id"):
        await _run(conn, "upgrade")
    await conn.rollback()
    await _run(conn, "downgrade")
    assert (await conn.execute(text(
        "SELECT count(*) FROM information_schema.tables WHERE table_schema = :s"
    ), {"s": schema})).scalar() == 0


def test_migration_and_model_agree():
    """The baseline builds the keys from the MODEL on a fresh install."""
    from models.database import NotificationSuppression, ProceduralSkill, Reminder

    mig = _load_migration()
    models = {
        "reminders": Reminder, "notification_suppressions": NotificationSuppression,
        "procedural_skills": ProceduralSkill,
    }
    for table, column, ref, _revert in mig.KEYS:
        col = models[table].__table__.c[column]
        (fk,) = col.foreign_keys
        assert fk.column.table.name == ref
        assert (fk.ondelete or "").upper() == "SET NULL", f"{table}.{column}"
        assert col.nullable is True, f"SET NULL needs a nullable {table}.{column}"


def test_revision_chain():
    mig = _load_migration()
    assert mig.revision == REVISION
    assert mig.down_revision == PREVIOUS
    assert len(mig.revision) <= 32, "alembic_version.version_num is VARCHAR(32)"


def test_the_chain_still_has_exactly_one_head():
    from alembic.script import ScriptDirectory

    script = ScriptDirectory(str(_versions_dir().parent))
    heads = script.get_heads()
    # One head, not THIS revision: a later one must not break this test.
    assert len(heads) == 1, heads
