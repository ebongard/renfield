"""The test harness's schema reset: independent of what other branches left behind.

The shared `renfield_test` database outlives every branch. On 2026-11-01 a
`kb_shares` table that another branch's model had created (FK to
`knowledge_bases`) made `Base.metadata.drop_all` fail at setup -- every run,
until it was dropped by hand. The harness now drops and recreates the whole
`public` schema, and because that is far more destructive than `drop_all`, it
refuses any database whose name does not mark it as a test database.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import text

from tests.backend.conftest import (
    _build_schema,
    _refuse_the_application_database,
    _reset_public_schema,
)

_BASE = "postgresql+asyncpg://u:secret@db:5432/"


# --------------------------------------------------------------------------
# The guard: only a database named as a test database
# --------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize("name", ["renfield_test", "renfield_test2", "renfield_test_agent_7"])
def test_guard_accepts_test_databases(name, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", _BASE + "renfield")
    _refuse_the_application_database(_BASE + name)   # no exit


@pytest.mark.unit
@pytest.mark.parametrize(
    "name",
    ["renfield", "renfield_prod", "postgres", "renfield_testing", "renfield_test-x",
     "test", "x_renfield_test", ""],
)
def test_guard_refuses_any_other_database(name, monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with pytest.raises(pytest.exit.Exception) as exc:
        _refuse_the_application_database(_BASE + name)
    # The message names the database, never the credential.
    assert "secret" not in str(exc.value)


@pytest.mark.unit
@pytest.mark.parametrize("tail", ["renfield?x=/renfield_test", "renfield?application_name=renfield_test"])
def test_guard_reads_the_database_the_driver_connects_to(tail, monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with pytest.raises(pytest.exit.Exception):
        _refuse_the_application_database(_BASE + tail)


@pytest.mark.unit
def test_guard_refuses_the_application_database_even_when_named_like_a_test(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", _BASE + "renfield_test")
    with pytest.raises(pytest.exit.Exception):
        _refuse_the_application_database(_BASE + "renfield_test")


@pytest.mark.unit
async def test_reset_checks_the_connection_it_is_handed_before_any_statement():
    """The reset re-checks at the destructive point itself, on the connection's
    OWN url -- not on whatever the caller validated earlier."""
    from sqlalchemy.engine import make_url

    conn = MagicMock()
    conn.engine.url = make_url(_BASE + "renfield")
    conn.execute = AsyncMock()

    with pytest.raises(pytest.exit.Exception):
        await _reset_public_schema(conn)
    conn.execute.assert_not_called()


# --------------------------------------------------------------------------
# The reset itself, on the real test database
# --------------------------------------------------------------------------


@pytest.mark.database
async def test_rebuild_survives_a_foreign_table_that_references_ours(async_engine):
    """A table this branch does not know, with an FK into one it does, plus a
    stale enum type: the rebuild removes both and leaves this branch's schema,
    the GENERATED columns and `vector` in place."""
    async with async_engine.begin() as conn:
        await conn.execute(text(
            "CREATE TABLE foreign_branch_probe ("
            " id serial PRIMARY KEY,"
            " kb_id integer REFERENCES knowledge_bases(id))"
        ))
        await conn.execute(text("CREATE TYPE foreign_branch_enum AS ENUM ('a')"))

    async with async_engine.begin() as conn:
        await _build_schema(conn)

    async with async_engine.connect() as conn:
        tables = {r[0] for r in (await conn.execute(text(
            "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
        ))).all()}
        assert "foreign_branch_probe" not in tables
        assert "knowledge_bases" in tables

        stale_enum = (await conn.execute(text(
            "SELECT 1 FROM pg_type WHERE typname = 'foreign_branch_enum'"
        ))).scalar()
        assert stale_enum is None

        owner, acl = (await conn.execute(text(
            "SELECT nspowner::regrole::text, nspacl::text FROM pg_namespace"
            " WHERE nspname = 'public'"
        ))).one()
        assert owner == "pg_database_owner"
        assert "=U/pg_database_owner" in acl    # USAGE for PUBLIC, as on a fresh DB

        vector_schema = (await conn.execute(text(
            "SELECT extnamespace::regnamespace::text FROM pg_extension"
            " WHERE extname = 'vector'"
        ))).scalar()
        assert vector_schema == "public"

        generated = (await conn.execute(text(
            "SELECT attgenerated::text FROM pg_attribute"
            " WHERE attrelid = 'public.document_chunks'::regclass"
            "   AND attname = 'search_vector'"
        ))).scalar()
        assert generated == "s"
