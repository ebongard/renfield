"""Real-Postgres test for ``pc20260927_voice2fa`` -- the way up AND back.

The migration adds `users.voice_second_factor_enabled`: the per-person consent
that a login may demand their voice. An ECAPA voiceprint is biometric data
(Art. 9 GDPR), which is why this is a column and not a flag.

🛑 Three properties are exercised here, not asserted:

1. **The way up really creates the column**, with `NOT NULL` and
   `server_default false` -- otherwise an existing account would get the login
   hurdle without being asked, or the migration would fail on existing rows.
2. **It is idempotent.** A fresh install already has the column from
   `create_all` (`pc20260926_schema_baseline`); the migration must not fail
   there. Exactly that class of defect cost nine indexes on 2026-09-26.
3. **The way back is EXERCISED.** "Before" cannot be reconstructed from
   "after" -- a `downgrade` that has never run is an assertion.

Gated on ``RENFIELD_TEST_PG_URL`` like the other Postgres tests.
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

_TABLE = "users"
_COLUMN = "voice_second_factor_enabled"


def _backend_root() -> Path:
    import services.database as _db

    return Path(_db.__file__).resolve().parents[1]


def _load_migration():
    path = _backend_root() / "alembic" / "versions" / "pc20260927_voice_second_factor.py"
    spec = importlib.util.spec_from_file_location("pc20260927_mig_under_test", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.fixture
async def schema_conn(pg_url):
    """(AsyncConnection, schema) in a throwaway schema.

    Takes the DSN from the shared `pg_url` fixture rather than reading the env
    var -- see the note there. Reading it raw is what made all seven tests in
    this file ERROR with `ModuleNotFoundError: psycopg2` on 2026-10-30, and
    that includes `TestTheDowngradeIsExercised`: the reverse path was never
    actually exercised, only written down.
    """
    dsn = pg_url
    schema = f"t_{uuid.uuid4().hex[:12]}"
    engine = create_async_engine(dsn, poolclass=NullPool)
    async with engine.connect() as conn:
        await conn.execute(text(f'CREATE SCHEMA "{schema}"'))
        await conn.execute(text(f'SET search_path TO "{schema}", public'))
        await conn.commit()
        try:
            yield conn, schema
        finally:
            await conn.rollback()
            await conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
            await conn.commit()
    await engine.dispose()


async def _minimal_users(conn) -> None:
    """A `users` stub with two rows -- enough for the column operation.

    Deliberately NOT the whole schema: the migration touches one column, and a
    test that builds 200 tables for it checks the environment instead of the
    migration. The two rows matter -- a `NOT NULL` column without a
    `server_default` only fails when rows are already there.
    """
    await conn.execute(text("""
        CREATE TABLE users (
            id serial PRIMARY KEY,
            username varchar(100) NOT NULL
        )
    """))
    await conn.execute(text(
        "INSERT INTO users (username) VALUES ('alt1'), ('alt2')"
    ))
    await conn.commit()


async def _column(conn, schema: str) -> dict | None:
    row = (await conn.execute(text("""
        SELECT is_nullable, column_default, data_type
        FROM information_schema.columns
        WHERE table_schema = :s AND table_name = :t AND column_name = :c
    """), {"s": schema, "t": _TABLE, "c": _COLUMN})).first()
    if row is None:
        return None
    return {"is_nullable": row[0], "default": row[1], "type": row[2]}


class _Op:
    """The slim `op` stand-in the migration needs.

    The migration calls `op.get_bind()`, `op.add_column()` and
    `op.drop_column()`. Alembic's real `op` needs a MigrationContext; for a
    column operation in a throwaway schema that is ceremony. What matters --
    that the migration runs its own idempotence check through `get_bind()` --
    stays real.
    """

    def __init__(self, conn):
        self._conn = conn

    def get_bind(self):
        return self._conn

    def add_column(self, table, column):
        from sqlalchemy.schema import CreateColumn

        ddl = str(CreateColumn(column).compile(dialect=self._conn.engine.dialect))
        self._conn.exec_driver_sql(f'ALTER TABLE "{table}" ADD COLUMN {ddl}')

    def drop_column(self, table, column_name):
        self._conn.exec_driver_sql(
            f'ALTER TABLE "{table}" DROP COLUMN "{column_name}"'
        )


async def _run(conn, direction: str) -> None:
    """Migration im echten Schema fahren (sync-Bridge, wie Alembic selbst)."""
    mig = _load_migration()

    def _sync(sync_conn):
        import alembic.op as alembic_op

        real_get_bind = getattr(alembic_op, "get_bind", None)
        fake = _Op(sync_conn)
        # The migration uses `op.<x>` -- redirect module-wide, then restore.
        patched = {}
        for name in ("get_bind", "add_column", "drop_column"):
            patched[name] = getattr(alembic_op, name, None)
            setattr(alembic_op, name, getattr(fake, name))
        try:
            getattr(mig, direction)()
        finally:
            for name, orig in patched.items():
                if orig is None:
                    delattr(alembic_op, name)
                else:
                    setattr(alembic_op, name, orig)
            assert real_get_bind is None or alembic_op.get_bind is real_get_bind

    await conn.run_sync(_sync)
    await conn.commit()


class TestTheUpgrade:
    async def test_it_adds_the_column_not_null_defaulting_false(self, schema_conn):
        conn, schema = schema_conn
        await _minimal_users(conn)
        assert await _column(conn, schema) is None, "Voraussetzung: Spalte fehlt"

        await _run(conn, "upgrade")

        col = await _column(conn, schema)
        assert col is not None, "der Aufweg hat die Spalte nicht angelegt"
        assert col["is_nullable"] == "NO"
        assert "false" in (col["default"] or "").lower(), (
            "ohne `server_default` scheitert die Migration an bestehenden Zeilen — "
            f"default war {col['default']!r}"
        )
        assert col["type"] == "boolean"

    async def test_existing_rows_get_false_not_null(self, schema_conn):
        """🛑 The actual point: nobody gets the hurdle without being asked."""
        conn, _ = schema_conn
        await _minimal_users(conn)
        await _run(conn, "upgrade")

        vals = [r[0] for r in (await conn.execute(text(
            f"SELECT {_COLUMN} FROM users ORDER BY id"
        ))).all()]
        assert vals == [False, False], f"bestehende Zeilen sind nicht false: {vals}"

    async def test_running_it_twice_is_harmless(self, schema_conn):
        """A fresh install already has the column from `create_all`. If the
        migration fails there, the schema stays standing half-way -- exactly the
        trap that cost nine indexes on 2026-09-26."""
        conn, schema = schema_conn
        await _minimal_users(conn)
        await _run(conn, "upgrade")
        await _run(conn, "upgrade")   # must not raise
        assert await _column(conn, schema) is not None

    async def test_it_is_a_no_op_when_create_all_already_made_the_column(self, schema_conn):
        conn, schema = schema_conn
        await conn.execute(text(f"""
            CREATE TABLE users (
                id serial PRIMARY KEY,
                username varchar(100) NOT NULL,
                {_COLUMN} boolean NOT NULL DEFAULT false
            )
        """))
        await conn.commit()
        await _run(conn, "upgrade")   # must not raise
        assert await _column(conn, schema) is not None


class TestTheDowngradeIsExercised:
    """🛑 The way back is EXERCISED, not asserted.

    "Before" cannot be reconstructed from "after". A `downgrade` that has never
    run is a line of code with a hope attached to it.
    """

    async def test_it_removes_the_column(self, schema_conn):
        conn, schema = schema_conn
        await _minimal_users(conn)
        await _run(conn, "upgrade")
        assert await _column(conn, schema) is not None

        await _run(conn, "downgrade")
        assert await _column(conn, schema) is None, "der Rückweg hat nichts entfernt"

    async def test_downgrade_twice_is_harmless(self, schema_conn):
        conn, schema = schema_conn
        await _minimal_users(conn)
        await _run(conn, "upgrade")
        await _run(conn, "downgrade")
        await _run(conn, "downgrade")   # must not raise
        assert await _column(conn, schema) is None

    async def test_up_down_up_leaves_the_same_shape(self, schema_conn):
        """The round trip: up twice, down once, and the result is identical."""
        conn, schema = schema_conn
        await _minimal_users(conn)
        await _run(conn, "upgrade")
        before = await _column(conn, schema)
        await _run(conn, "downgrade")
        await _run(conn, "upgrade")
        assert await _column(conn, schema) == before


class TestTheChain:
    def test_it_hangs_off_the_live_head(self):
        """🛑 On 2026-09-27 the LIVE head of both instances stood on
        `pc20260926_baseline` (queried through `alembic_version`, not guessed
        from files). If this migration hangs off anywhere else, the chain forks
        at deploy time."""
        mig = _load_migration()
        assert mig.revision == "pc20260927_voice2fa"
        assert mig.down_revision == "pc20260926_baseline"
