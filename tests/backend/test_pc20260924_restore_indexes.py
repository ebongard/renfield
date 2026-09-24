"""Real-Postgres tests für ``pc20260924_restore_idx`` — die Dimensionsfalle.

Die Migration stellt neun (acht baubare) Indizes wieder her, die einer
Neuinstallation verlorengehen, weil `create_all` + Alembic-Stempel die
Migrationshistorie überspringt. Sie wurde gegen einen echten Schemaabzug der
Produktion durchlaufen (326 → 334 → 326 → 334 Indizes).

Was dieser Test bewacht, ist etwas anderes und kleiner: die eine Zeile, an der
das alte DDL scheiterte und die man beim Abschreiben ungeprüft übernimmt.

🛑 **pgvector legt die Dimension DIREKT im typmod ab.** Der `+4`-Versatz ist die
varlena-Konvention (`varchar`), nicht die von `vector`. Eine `vector(2560)`-Spalte
hat `atttypmod = 2560`, nicht 2564. `pc20260402_add_episodic_hnsw_index.py:60`
rechnet `atttypmod - 4` und hätte einen Index auf `halfvec(2556)` gebaut —
Abfragen casten auf `halfvec(2560)`, der Index wäre also NIE benutzbar gewesen:
gebaut, bei jedem Schreibvorgang gepflegt, nutzlos. Ein solcher Fehler bricht
nichts, er macht nur alles langsam und sieht dabei gesund aus.

Zweitens: bei 2560 Dimensionen ist `vector_cosine_ops` gar nicht indizierbar
(pgvector begrenzt den regulären Typ auf 2000). Die Operatorklasse MUSS sich
deshalb nach der gemessenen Dimension richten, statt festgeschrieben zu sein.

Gated auf ``RENFIELD_TEST_PG_URL`` wie die übrigen Postgres-Tests.
"""
from __future__ import annotations

import importlib.util
import os
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


def _pg_test_dsn() -> str | None:
    return os.environ.get("RENFIELD_TEST_PG_URL")


@pytest.fixture
async def schema_conn():
    """(AsyncConnection, schema) in einem Wegwerf-Schema, wie in #447."""
    dsn = _pg_test_dsn()
    if not dsn:
        pytest.skip("RENFIELD_TEST_PG_URL nicht gesetzt")
    schema = f"t_{uuid.uuid4().hex[:12]}"
    engine = create_async_engine(dsn, poolclass=NullPool)
    async with engine.connect() as conn:
        await conn.execute(text(f'CREATE SCHEMA "{schema}"'))
        # `public` MUSS im Pfad bleiben: die `vector`-Erweiterung liegt dort,
        # und ohne sie ist der Typ im Wegwerf-Schema nicht auflösbar
        # ("type \"vector\" does not exist"). Neue Objekte entstehen trotzdem
        # im Wegwerf-Schema, weil es vorne steht.
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
    """Die Dimension wird ROH gelesen — kein `- 4`."""

    @pytest.mark.parametrize("dim", [768, 1536, 2560])
    async def test_column_dim_matches_the_declared_dimension(self, schema_conn, dim):
        conn, _ = schema_conn
        mig = _load_migration()
        await conn.execute(text(f"CREATE TABLE probe (id serial primary key, embedding vector({dim}))"))
        await conn.commit()

        got = await conn.run_sync(lambda sync_conn: mig._column_dim(sync_conn, "probe", "embedding"))

        # Der eigentliche Punkt: EXAKT die deklarierte Zahl. `atttypmod - 4`
        # ergäbe hier dim-4 und der Test wäre rot — so soll er sein.
        assert got == dim

    async def test_missing_table_and_column_yield_none(self, schema_conn):
        conn, _ = schema_conn
        mig = _load_migration()
        await conn.execute(text("CREATE TABLE ohne_vektor (id serial primary key)"))
        await conn.commit()

        # Eine Instanz muss nicht jede Tabelle haben — das ist ein Auslassen,
        # kein Fehler. Sonst bräche die Migration auf der schlankeren Instanz.
        no_table = await conn.run_sync(
            lambda c: mig._column_dim(c, "gibt_es_nicht", "embedding"))
        no_column = await conn.run_sync(
            lambda c: mig._column_dim(c, "ohne_vektor", "embedding"))

        assert no_table is None
        assert no_column is None
        assert await conn.run_sync(lambda c: mig._table_exists(c, "gibt_es_nicht")) is False
        assert await conn.run_sync(lambda c: mig._table_exists(c, "ohne_vektor")) is True


class TestOperatorClassFollowsTheDimension:
    """Über 2000 Dimensionen MUSS es halfvec sein, sonst ist es nicht baubar."""

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

        # Nicht nur die Zeichenkette prüfen: Postgres muss den Index wirklich
        # bauen. Genau hier stirbt das alte DDL für `intent_corrections`, das
        # `vector_cosine_ops` festschreibt — bei 2560 nicht indizierbar.
        await conn.execute(text(
            f"CREATE INDEX probe_hnsw ON probe USING hnsw {expr} "
            f"WITH (m = 16, ef_construction = 64)"
        ))
        await conn.commit()

        definition = (await conn.execute(text(
            "SELECT indexdef FROM pg_indexes WHERE indexname = 'probe_hnsw'"
        ))).scalar_one()
        if erwartet_halfvec:
            # Die gebaute Dimension muss der Spalte entsprechen — sonst kann
            # keine Abfrage den Index je benutzen.
            assert f"halfvec({dim})" in definition
        else:
            assert "vector_cosine_ops" in definition
