"""A fresh install gets GENERATED `search_vector` columns (`pc20261031_sv_generated`).

Measured 2026-10-31 on a brand-new instance: all six `search_vector` columns were
plain `tsvector`, every chunk `search_vector IS NULL`, BM25 returned nothing.
The baseline builds a fresh install with `create_all`; the GENERATED shape only
ever lived in migrations that run before it.

The database tests replay what `init_db()` does on an empty database -- every
migration from the baseline's parent up to the head, walked from the real
script directory -- inside a throwaway schema on the real test Postgres:

* `schema_translate_map={None: schema}` so `create_all` creates INTO the
  schema (a bare one skips every table visible through `public`), asserted
  with `to_regclass`;
* `search_path` = schema first, so raw-SQL DDL lands there too;
* the dialect's default schema pointed at it, so migrations that use
  `sa.inspect()` look at the schema and not at the shared `public`.

Negative control: on main (no `pc20261031` in the chain) the fresh-install
test fails on every one of the six columns.
"""
from __future__ import annotations

import importlib.util
import uuid
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

import services.database as _db
from services.fts_generated_columns import (
    GENERATED_SEARCH_VECTORS,
    generated_kind,
    has_valid_gin,
    model_fetched_value_columns,
    not_generated,
)

BACKEND = Path(_db.__file__).resolve().parents[1]
VERSIONS = BACKEND / "alembic" / "versions"
REVISION = "pc20261031_sv_generated"
TABLES = sorted(s.table for s in GENERATED_SEARCH_VECTORS)


def _script():
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    cfg = Config(str(BACKEND / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND / "alembic"))
    return ScriptDirectory.from_config(cfg)


def _chain(stop_after: str | None = None) -> list:
    """Revisions a fresh install runs, oldest first: everything after
    `PRE_BASELINE_REVISION` up to the head (or up to `stop_after`)."""
    revs = list(_script().iterate_revisions("heads", _db.PRE_BASELINE_REVISION))
    revs.reverse()
    if stop_after is not None:
        idx = [r.revision for r in revs].index(stop_after)
        revs = revs[: idx + 1]
    return revs


def _load(filename: str):
    spec = importlib.util.spec_from_file_location(filename[:-3] + "_under_test", VERSIONS / filename)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# Unit: the map, the models, the original migrations and the chain agree
# ---------------------------------------------------------------------------
@pytest.mark.unit
def test_the_map_covers_exactly_the_models_fetched_value_columns():
    import ha_glue.models.database  # noqa: F401 -- fills Base.metadata
    from models.database import Base

    assert model_fetched_value_columns(Base.metadata) == {
        (s.table, "search_vector") for s in GENERATED_SEARCH_VECTORS
    }


@pytest.mark.unit
@pytest.mark.parametrize("filename,attr,table", [
    ("pc20260602_document_facts_fts.py", "_CONTENT_EXPR", "document_facts"),
    ("pc20260720_notes.py", "_CONTENT_EXPR", "notes"),
    ("pc20260829_documents_fts.py", "_CONTENT", "documents"),
])
def test_content_expression_is_the_one_production_got(filename, attr, table):
    spec = {s.table: s for s in GENERATED_SEARCH_VECTORS}[table]
    original = _load(filename)
    assert getattr(original, attr) == spec.content_expr
    assert original.revision == spec.origin


@pytest.mark.unit
def test_origins_are_real_revisions_and_content_tables_use_content():
    script = _script()
    for spec in GENERATED_SEARCH_VECTORS:
        assert script.get_revision(spec.origin) is not None, spec.origin
    by_table = {s.table: s.content_expr for s in GENERATED_SEARCH_VECTORS}
    for t in ("conversation_memories", "document_chunks", "messages"):
        assert by_table[t] == "content"


@pytest.mark.unit
def test_the_repair_is_on_the_fresh_install_path_and_the_chain_is_single_headed():
    assert len(_script().get_heads()) == 1
    chain = [r.revision for r in _chain()]
    assert chain[0] == "pc20260926_baseline"
    assert REVISION in chain


# ---------------------------------------------------------------------------
# Real Postgres: a throwaway schema, the real chain
# ---------------------------------------------------------------------------
@pytest.fixture
async def schema_conn(pg_url):
    engine = create_async_engine(pg_url, poolclass=NullPool, future=True)
    schema = f"sv_fresh_{uuid.uuid4().hex[:12]}"
    conn = await engine.connect()
    try:
        await conn.exec_driver_sql(f'CREATE SCHEMA "{schema}"')
        await conn.exec_driver_sql(f'SET search_path TO "{schema}", public')
        await conn.commit()
        # Own NullPool engine: only this connection's dialect is repointed.
        await conn.run_sync(lambda c: setattr(c.dialect, "default_schema_name", schema))
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


async def _run(conn, schema, revisions, direction: str = "upgrade") -> None:
    from alembic.operations import Operations
    from alembic.runtime.migration import MigrationContext

    def _apply(sync_conn):
        ctx = MigrationContext.configure(
            connection=sync_conn.execution_options(schema_translate_map={None: schema}),
            opts={"transaction_per_migration": True},
        )
        for rev in revisions:
            with ctx.begin_transaction(_per_migration=True):
                with Operations.context(ctx):
                    getattr(rev.module, direction)()

    await conn.commit()  # alembic opens its own transaction per migration
    try:
        await conn.run_sync(_apply)
    except Exception:
        await conn.rollback()
        raise
    await conn.commit()


async def _fresh_install(conn, schema, stop_after: str | None = None) -> None:
    await _run(conn, schema, _chain(stop_after))
    in_schema = (await conn.execute(
        text("SELECT to_regclass(:t) IS NOT NULL"), {"t": f'"{schema}".users'}
    )).scalar()
    assert in_schema, "the baseline did not create its tables in the throwaway schema"


async def _state(conn) -> dict[str, tuple]:
    """table -> (attgenerated, information_schema.is_generated, valid GIN, relfilenode)."""
    def _read(c):
        out = {}
        for t in TABLES:
            is_gen = c.execute(text(
                "SELECT is_generated FROM information_schema.columns "
                "WHERE table_schema = current_schema() AND table_name = :t "
                "AND column_name = 'search_vector'"), {"t": t}).scalar()
            node = c.execute(text(
                "SELECT relfilenode FROM pg_class WHERE oid = "
                "(quote_ident(current_schema()) || '.' || :t)::regclass"), {"t": t}).scalar()
            out[t] = (generated_kind(c, t), is_gen, has_valid_gin(c, t), node)
        return out
    return await conn.run_sync(_read)


async def _insert_chunk(conn) -> int:
    doc = (await conn.execute(text(
        "INSERT INTO documents (filename, file_path, circle_tier) "
        "VALUES ('Mietvertrag.pdf', '/x/Mietvertrag.pdf', 0) RETURNING id"))).scalar()
    chunk = (await conn.execute(text(
        "INSERT INTO document_chunks (document_id, content, circle_tier) "
        "VALUES (:d, 'Die Kündigungsfrist beträgt drei Monate', 0) RETURNING id"),
        {"d": doc})).scalar()
    await conn.commit()
    return chunk


async def _chunk_vector(conn, chunk_id: int):
    return (await conn.execute(text(
        "SELECT search_vector IS NOT NULL, "
        "search_vector @@ websearch_to_tsquery('german', 'Kündigungsfristen') "
        "FROM document_chunks WHERE id = :i"), {"i": chunk_id})).first()


@pytest.mark.database
async def test_a_fresh_install_has_every_search_vector_generated(schema_conn):
    conn, schema = schema_conn
    await _fresh_install(conn, schema)

    state = await _state(conn)
    print("fresh install:", {t: s[:3] for t, s in state.items()})
    for table, (kind, is_gen, gin, _node) in state.items():
        assert kind == "s", f"{table}.search_vector is not GENERATED (attgenerated={kind!r})"
        assert is_gen == "ALWAYS", table
        assert gin, f"{table}.search_vector has no valid GIN index"

    # The "same as production" check, by column state: every column the model
    # says the database computes IS computed. This is the class the baseline's
    # table+index comparison could not see.
    import ha_glue.models.database  # noqa: F401
    from models.database import Base

    assert await conn.run_sync(lambda c: not_generated(c, Base.metadata)) == []

    chunk = await _insert_chunk(conn)
    filled, matches = await _chunk_vector(conn, chunk)
    assert filled, "a new chunk has search_vector NULL -- BM25 would find nothing"
    assert matches, "the German stemmer does not match: wrong expression"


@pytest.mark.database
async def test_the_baseline_alone_leaves_them_plain_and_the_migration_repairs_rows(schema_conn):
    """The defect, reproduced, then repaired on a row that already exists."""
    conn, schema = schema_conn
    await _fresh_install(conn, schema, stop_after="pc20261029_audit_basis")

    before = await _state(conn)
    print("before pc20261031:", {t: s[:3] for t, s in before.items()})
    assert all(s[0] == "" and s[1] == "NEVER" for s in before.values()), before
    chunk = await _insert_chunk(conn)
    assert (await _chunk_vector(conn, chunk))[0] is False  # the measured defect

    mig = _script().get_revision(REVISION)
    await _run(conn, schema, [mig])
    after = await _state(conn)
    print("after pc20261031:", {t: s[:3] for t, s in after.items()})
    assert all(s[0] == "s" and s[1] == "ALWAYS" and s[2] for s in after.values()), after
    filled, matches = await _chunk_vector(conn, chunk)
    assert filled and matches, "the existing chunk was not recomputed"

    # Reverse path, EXERCISED: downgrade is the identity, upgrade again a no-op.
    await _run(conn, schema, [mig], "downgrade")
    assert await _state(conn) == after
    await _run(conn, schema, [mig])
    assert await _state(conn) == after
    assert (await _chunk_vector(conn, chunk))[0] is True


@pytest.mark.database
async def test_on_a_production_shaped_schema_the_migration_is_a_no_op(schema_conn):
    """Columns already GENERATED (every live instance): no rewrite, no index
    rebuild -- the relfilenode of each table and the index OIDs stay put."""
    conn, schema = schema_conn
    await _fresh_install(conn, schema)
    await _insert_chunk(conn)

    async def _index_oids():
        rows = await conn.execute(text(
            "SELECT indexrelid::bigint FROM pg_index i JOIN pg_class t ON t.oid = i.indrelid "
            "JOIN pg_namespace n ON n.oid = t.relnamespace "
            "WHERE n.nspname = current_schema() AND t.relname = ANY(:ts) ORDER BY 1"),
            {"ts": TABLES})
        return [r[0] for r in rows]

    before, idx_before = await _state(conn), await _index_oids()
    await _run(conn, schema, [_script().get_revision(REVISION)])
    assert await _state(conn) == before
    assert await _index_oids() == idx_before
