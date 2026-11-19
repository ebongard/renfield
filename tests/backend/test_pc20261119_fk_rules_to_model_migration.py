"""Real-Postgres round trip for ``pc20261119_fk_rules_to_model`` (#1588).

Harness as in ``test_pc20261119_fk_delete_rules_migration.py``: a throwaway
schema that is the ONLY one on the search path, holding the tables in their
pre-migration shape, and the real ``upgrade()`` / ``downgrade()`` bound to a
MigrationContext.

The two live starting points are both built: the household (every key SET
NULL) and xidra (already at the target). What is checked: the rules before and
after, what a delete does afterwards (the atom takes its document, the
credential outlives its creator, a skill owner cannot be deleted), constraint
names and row counts across forward -> reverse -> forward, that a key already
at its rule is not rebuilt, the refusals, and model agreement.
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

REVISION = "pc20261119_fk_rules_to_model"
PREVIOUS = "pc20261119_fk_delete_rules"

_SQL = {"a": "", "n": " ON DELETE SET NULL", "c": " ON DELETE CASCADE"}
HOUSEHOLD = {"documents": "n", "ingest_credentials": "n", "procedural_skills": "n"}
XIDRA = {"documents": "c", "ingest_credentials": "n", "procedural_skills": "a"}
TARGET = {"documents": "c", "ingest_credentials": "n", "procedural_skills": "a"}


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
    schema = f"pcfm_{uuid.uuid4().hex[:12]}"
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


async def _pre_migration_tables(conn, rules: dict[str, str]) -> None:
    """The tables as they stood at ``pc20261119_fk_delete_rules``, reduced to
    the columns that matter. Two users: 1 owns a skill and minted a
    credential; 2 minted a credential only. One atom owns one document."""
    for ddl in (
        "CREATE TABLE users (id SERIAL PRIMARY KEY, username TEXT)",
        "CREATE TABLE atoms (atom_id VARCHAR(36) PRIMARY KEY)",
        "CREATE TABLE documents (id SERIAL PRIMARY KEY, filename TEXT,"
        f" atom_id VARCHAR(36) REFERENCES atoms(atom_id){_SQL[rules['documents']]})",
        "CREATE TABLE ingest_credentials (id SERIAL PRIMARY KEY, client_id TEXT,"
        f" created_by_user_id INTEGER REFERENCES users(id){_SQL[rules['ingest_credentials']]})",
        "CREATE TABLE procedural_skills (id SERIAL PRIMARY KEY, title TEXT,"
        f" user_id INTEGER REFERENCES users(id){_SQL[rules['procedural_skills']]})",
        "INSERT INTO users (username) VALUES ('owner'), ('minter')",
        "INSERT INTO atoms (atom_id) VALUES ('a1')",
        "INSERT INTO documents (filename, atom_id) VALUES ('d', 'a1')",
        "INSERT INTO ingest_credentials (client_id, created_by_user_id) VALUES ('c1', 1), ('c2', 2)",
        "INSERT INTO procedural_skills (title, user_id) VALUES ('k', 1)",
    ):
        await conn.exec_driver_sql(ddl)
    await conn.commit()


async def _constraints(conn, schema) -> dict[str, tuple[str, str, int]]:
    """table -> (constraint name, confdeltype, oid) of its one foreign key."""
    rows = (await conn.execute(text(
        "SELECT t.relname, c.conname, c.confdeltype::text, c.oid::bigint FROM pg_constraint c"
        " JOIN pg_class t ON t.oid = c.conrelid"
        " JOIN pg_namespace n ON n.oid = t.relnamespace"
        " WHERE c.contype = 'f' AND n.nspname = :s"
    ), {"s": schema})).all()
    return {r[0]: (r[1], r[2], r[3]) for r in rows}


def _kinds(found) -> dict[str, str]:
    return {t: k for t, (_n, k, _o) in found.items()}


def _names(found) -> dict[str, str]:
    return {t: n for t, (n, _k, _o) in found.items()}


async def _counts(conn) -> dict[str, int]:
    out = {}
    for t in ("users", "atoms", "documents", "ingest_credentials", "procedural_skills"):
        out[t] = (await conn.execute(text(f"SELECT count(*) FROM {t}"))).scalar()
    return out


async def test_household_round_trip_and_what_a_delete_does_afterwards(schema_conn):
    conn, schema = schema_conn
    await _pre_migration_tables(conn, HOUSEHOLD)
    before = await _constraints(conn, schema)
    assert _kinds(before) == HOUSEHOLD
    counts = await _counts(conn)

    # --- forward ---------------------------------------------------------
    await _run(conn, "upgrade")
    after = await _constraints(conn, schema)
    assert _kinds(after) == TARGET
    assert _names(after) == _names(before), "rebuilt under another name"
    assert after["ingest_credentials"][2] == before["ingest_credentials"][2], (
        "a key already at its rule was rebuilt"
    )
    assert await _counts(conn) == counts

    # --- reverse: nothing to restore, nothing changes ---------------------
    await _run(conn, "downgrade")
    assert await _constraints(conn, schema) == after
    assert await _counts(conn) == counts

    # --- forward again: idempotent ----------------------------------------
    await _run(conn, "upgrade")
    assert await _constraints(conn, schema) == after

    # --- what a delete does now -------------------------------------------
    # A skill owner cannot be deleted (delete_user answers 409) ...
    with pytest.raises(IntegrityError, match="procedural_skills"):
        await conn.exec_driver_sql("DELETE FROM users WHERE id = 1")
    await conn.rollback()
    # ... a user who only minted a credential can, and the credential stays.
    await conn.exec_driver_sql("DELETE FROM users WHERE id = 2")
    # The atom takes its document with it.
    await conn.exec_driver_sql("DELETE FROM atoms WHERE atom_id = 'a1'")
    await conn.commit()
    assert await _counts(conn) == {
        "users": 1, "atoms": 0, "documents": 0, "ingest_credentials": 2, "procedural_skills": 1,
    }
    creators = (await conn.execute(text(
        "SELECT client_id, created_by_user_id FROM ingest_credentials ORDER BY client_id"
    ))).all()
    assert [tuple(r) for r in creators] == [("c1", 1), ("c2", None)]


async def test_xidra_is_already_at_the_target_and_nothing_is_rebuilt(schema_conn):
    conn, schema = schema_conn
    await _pre_migration_tables(conn, XIDRA)
    before = await _constraints(conn, schema)
    await _run(conn, "upgrade")
    assert await _constraints(conn, schema) == before


async def test_the_old_model_rule_on_ingest_credentials_is_brought_to_set_null(schema_conn):
    """A database built from the model before this revision had NO ACTION."""
    conn, schema = schema_conn
    await _pre_migration_tables(conn, {**XIDRA, "ingest_credentials": "a"})
    await _run(conn, "upgrade")
    assert _kinds(await _constraints(conn, schema)) == TARGET


async def test_a_duplicate_key_on_a_column_is_refused_before_anything_changes(schema_conn):
    conn, schema = schema_conn
    await _pre_migration_tables(conn, HOUSEHOLD)
    await conn.exec_driver_sql(
        "ALTER TABLE procedural_skills ADD CONSTRAINT dup_user_fk"
        " FOREIGN KEY (user_id) REFERENCES users(id)"
    )
    await conn.commit()
    with pytest.raises(RuntimeError, match="found 2"):
        await _run(conn, "upgrade")
    await conn.rollback()
    # documents came first and was rebuilt inside the same transaction --
    # the refusal rolled that back as well.
    assert (await _constraints(conn, schema))["documents"][1] == "n"


async def test_without_the_keys_the_upgrade_refuses_and_changes_nothing(schema_conn):
    conn, schema = schema_conn
    with pytest.raises(RuntimeError, match=r"documents\.atom_id -> atoms, found 0"):
        await _run(conn, "upgrade")
    await conn.rollback()
    assert (await conn.execute(text(
        "SELECT count(*) FROM information_schema.tables WHERE table_schema = :s"
    ), {"s": schema})).scalar() == 0


def test_migration_and_model_agree():
    """The baseline builds the keys from the MODEL on a fresh install."""
    from models.database import Document, IngestCredential, ProceduralSkill

    mig = _load_migration()
    models = {"documents": Document, "ingest_credentials": IngestCredential, "procedural_skills": ProceduralSkill}
    model_rule = {None: "a", "SET NULL": "n", "CASCADE": "c", "RESTRICT": "r"}
    for table, column, ref, ref_column, rule in mig.KEYS:
        (fk,) = models[table].__table__.c[column].foreign_keys
        assert (fk.column.table.name, fk.column.name) == (ref, ref_column), f"{table}.{column}"
        ondelete = fk.ondelete.upper() if fk.ondelete else None
        assert model_rule[ondelete] == rule, f"{table}.{column}: model {ondelete}, migration {rule}"


def test_revision_chain():
    mig = _load_migration()
    assert mig.revision == REVISION
    assert mig.down_revision == PREVIOUS
    assert len(mig.revision) <= 32, "alembic_version.version_num is VARCHAR(32)"


def test_the_chain_still_has_exactly_one_head():
    from alembic.script import ScriptDirectory

    heads = ScriptDirectory(str(_versions_dir().parent)).get_heads()
    assert len(heads) == 1, heads
