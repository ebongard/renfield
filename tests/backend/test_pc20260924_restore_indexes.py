"""Real-Postgres tests for ``pc20260924_restore_idx`` -- the dimension trap.

The migration restores nine (eight buildable) indexes that a fresh install
loses, because `create_all` plus the Alembic stamp skips the migration history.
It was exercised against a real schema dump of production
(326 -> 334 -> 326 -> 334 indexes).

What this test guards is something else and smaller: the one line the old DDL
failed on, and the one you copy over unchecked.

🛑 **pgvector stores the dimension DIRECTLY in the typmod.** The `+4` offset is
the varlena convention (`varchar`), not `vector`'s. A `vector(2560)` column has
`atttypmod = 2560`, not 2564. `pc20260402_add_episodic_hnsw_index.py:60`
computes `atttypmod - 4` and would have built an index on `halfvec(2556)` --
queries cast to `halfvec(2560)`, so the index would NEVER have been usable:
built, maintained on every write, useless. A defect like that breaks nothing,
it only makes everything slow while looking healthy.

Second: at 2560 dimensions `vector_cosine_ops` is not indexable at all
(pgvector caps the regular type at 2000). So the operator class MUST follow the
measured dimension instead of being pinned.

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


def _backend_root() -> Path:
    import services.database as _db

    return Path(_db.__file__).resolve().parents[1]


_MIG_PATH = _backend_root() / "alembic" / "versions" / "pc20260924_restore_lost_indexes.py"


def _load_migration():
    spec = importlib.util.spec_from_file_location("pc20260924_mig_under_test", _MIG_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.fixture
async def schema_conn(pg_url):
    """(AsyncConnection, schema) in a throwaway schema, as in #447.

    Takes the DSN from the shared `pg_url` fixture rather than reading the env
    var. This file read it raw and handed it to `create_async_engine` -- with a
    sync `postgresql://` that resolves to the psycopg2 dialect, which is not
    installed in the backend image, and all six tests here ERROR with
    `ModuleNotFoundError`. Measured on 2026-10-30.
    """
    dsn = pg_url
    schema = f"t_{uuid.uuid4().hex[:12]}"
    engine = create_async_engine(dsn, poolclass=NullPool)
    async with engine.connect() as conn:
        await conn.execute(text(f'CREATE SCHEMA "{schema}"'))
        # `public` MUST stay in the path: the `vector` extension lives there,
        # and without it the type is not resolvable in the throwaway schema
        # ("type \"vector\" does not exist"). New objects are still created in
        # the throwaway schema, because it comes first.
        await conn.execute(text(f'SET search_path TO "{schema}", public'))
        await conn.commit()
        try:
            yield conn, schema
        finally:
            await conn.rollback()
            await conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
            await conn.commit()
    await engine.dispose()


class TestDimensionIsReadRaw:
    """The dimension is read RAW -- no `- 4`."""

    @pytest.mark.parametrize("dim", [768, 1536, 2560])
    async def test_column_dim_matches_the_declared_dimension(self, schema_conn, dim):
        conn, _ = schema_conn
        mig = _load_migration()
        await conn.execute(text(f"CREATE TABLE probe (id serial primary key, embedding vector({dim}))"))
        await conn.commit()

        got = await conn.run_sync(lambda sync_conn: mig._column_dim(sync_conn, "probe", "embedding"))

        # The actual point: EXACTLY the declared number. `atttypmod - 4`
        # would give dim-4 here and the test would be red -- as it should.
        assert got == dim

    async def test_missing_table_and_column_yield_none(self, schema_conn):
        conn, _ = schema_conn
        mig = _load_migration()
        await conn.execute(text("CREATE TABLE ohne_vektor (id serial primary key)"))
        await conn.commit()

        # An instance need not have every table -- that is a skip, not an
        # error. Otherwise the migration would break on the leaner instance.
        no_table = await conn.run_sync(
            lambda c: mig._column_dim(c, "gibt_es_nicht", "embedding"))
        no_column = await conn.run_sync(
            lambda c: mig._column_dim(c, "ohne_vektor", "embedding"))

        assert no_table is None
        assert no_column is None
        assert await conn.run_sync(lambda c: mig._table_exists(c, "gibt_es_nicht")) is False
        assert await conn.run_sync(lambda c: mig._table_exists(c, "ohne_vektor")) is True


class TestOperatorClassFollowsTheDimension:
    """Above 2000 dimensions it MUST be halfvec, otherwise it is not buildable."""

    @pytest.mark.parametrize(
        "dim,erwartet_halfvec",
        [(768, False), (2000, False), (2001, True), (2560, True)],
    )
    async def test_index_is_actually_creatable_at_that_dimension(
        self, schema_conn, dim, erwartet_halfvec,
    ):
        conn, _ = schema_conn
        mig = _load_migration()
        await conn.execute(text(f"CREATE TABLE probe (id serial primary key, embedding vector({dim}))"))
        await conn.commit()

        got = await conn.run_sync(lambda c: mig._column_dim(c, "probe", "embedding"))
        expr = (f"((embedding::halfvec({got})) halfvec_cosine_ops)"
                if got > 2000 else "(embedding vector_cosine_ops)")
        assert ("halfvec" in expr) is erwartet_halfvec

        # Do not just check the string: Postgres has to actually build the
        # index. This is exactly where the old DDL for `intent_corrections`
        # dies, which pins `vector_cosine_ops` -- not indexable at 2560.
        await conn.execute(text(
            f"CREATE INDEX probe_hnsw ON probe USING hnsw {expr} "
            f"WITH (m = 16, ef_construction = 64)"
        ))
        await conn.commit()

        definition = (await conn.execute(text(
            "SELECT indexdef FROM pg_indexes WHERE indexname = 'probe_hnsw'"
        ))).scalar_one()
        if erwartet_halfvec:
            # The built dimension has to match the column -- otherwise no
            # query can ever use the index.
            assert f"halfvec({dim})" in definition
        else:
            assert "vector_cosine_ops" in definition
