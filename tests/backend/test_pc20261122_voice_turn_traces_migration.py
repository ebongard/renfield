"""Real-Postgres round trip for ``pc20261122_voice_turn_traces`` (#1613).

Harness as in ``test_pc20261110_routing_traces_migration.py``: a throwaway
schema and the real ``upgrade()`` / ``downgrade()`` bound to a
MigrationContext. Checks the table, its columns, both indexes and the
``resumed`` server default, the reverse path, that an upgrade over a table the
baseline's ``create_all`` already built is a no-op, and that the migration and
the model agree.
"""

from __future__ import annotations

import importlib.util
import uuid
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

pytestmark = [pytest.mark.database]


def _load_migration_path() -> Path:
    import services.database as _db

    return Path(_db.__file__).resolve().parents[1] / "alembic" / "versions" / "pc20261122_voice_turn_traces.py"


def _load_migration():
    path = _load_migration_path()
    spec = importlib.util.spec_from_file_location("pc20261122_voice_turn_traces_under_test", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.fixture
async def schema_conn(pg_url):
    engine = create_async_engine(pg_url, poolclass=NullPool, future=True)
    schema = f"pcvt_{uuid.uuid4().hex[:12]}"
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
                text("SELECT to_regclass(quote_ident(:s) || '.voice_turn_traces') IS NOT NULL"),
                {"s": schema},
            )
        ).scalar()
    )


async def _indexes(conn, schema) -> dict[str, str]:
    rows = await conn.execute(
        text(
            "SELECT indexname, indexdef FROM pg_indexes"
            " WHERE schemaname = :s AND tablename = 'voice_turn_traces'"
        ),
        {"s": schema},
    )
    return {r[0]: r[1] for r in rows}


async def _columns(conn, schema) -> dict[str, tuple[str, str]]:
    rows = await conn.execute(
        text(
            "SELECT column_name, data_type, is_nullable FROM information_schema.columns"
            " WHERE table_schema = :s AND table_name = 'voice_turn_traces'"
        ),
        {"s": schema},
    )
    return {r[0]: (r[1], r[2]) for r in rows}


async def _add_trace(conn) -> None:
    # `resumed` left out: the server default must fill it.
    await conn.execute(
        text(
            "INSERT INTO voice_turn_traces (created_at, finished_at, source, device_key, room, reason)"
            " VALUES (now(), now(), 'satellite', 'sat-k', 'Küche', 'silence')"
        )
    )
    await conn.commit()


async def test_round_trip(schema_conn):
    conn, schema = schema_conn
    await _run(conn, "upgrade")
    assert await _has_table(conn, schema)

    indexes = await _indexes(conn, schema)
    assert {"idx_voice_turn_traces_created", "idx_voice_turn_traces_source_created"} <= set(indexes)
    assert "(source, created_at)" in indexes["idx_voice_turn_traces_source_created"]

    columns = await _columns(conn, schema)
    assert set(columns) == {
        "id", "created_at", "finished_at", "source", "device_key", "room", "reason",
        "audio_seconds", "vad_speech_seen", "resumed", "turn_score",
    }
    assert columns["resumed"] == ("boolean", "NO")
    assert columns["device_key"][1] == "YES"
    assert columns["finished_at"][1] == "NO"

    await _add_trace(conn)
    assert (await conn.execute(text("SELECT resumed FROM voice_turn_traces"))).scalar() is False

    await _run(conn, "downgrade")
    assert not await _has_table(conn, schema)

    await _run(conn, "upgrade")
    assert await _has_table(conn, schema)


async def test_upgrade_over_an_existing_table_is_a_no_op(schema_conn):
    """A fresh install's baseline already built the table from the models."""
    conn, _schema = schema_conn
    await _run(conn, "upgrade")
    await _add_trace(conn)

    await _run(conn, "upgrade")
    assert (await conn.execute(text("SELECT count(*) FROM voice_turn_traces"))).scalar() == 1


def test_migration_and_model_agree():
    """Same columns, nullability and index names as the ORM (create_all on a
    fresh install, this migration on an existing one)."""
    from models.database import VoiceTurnTrace

    md = sa.MetaData()
    captured: dict = {}

    class _Op:
        def get_bind(self):
            class _Bind:
                def execute(self, *_a, **_k):
                    return type("R", (), {"scalar": lambda self: False})()
            return _Bind()

        def create_table(self, name, *cols):
            captured["table"] = sa.Table(name, md, *cols)

        def create_index(self, name, _table, cols):
            captured.setdefault("indexes", {})[name] = cols

    mig = _load_migration()
    prev = mig.op
    mig.op = _Op()
    try:
        mig.upgrade()
    finally:
        mig.op = prev

    model = VoiceTurnTrace.__table__
    table = captured["table"]
    assert table.name == model.name
    assert {c.name: c.nullable for c in table.columns} == {c.name: c.nullable for c in model.columns}
    assert captured["indexes"] == {i.name: [c.name for c in i.columns] for i in model.indexes}


def test_revision_chain():
    mig = _load_migration()
    assert mig.revision == "pc20261122_voice_turn_traces"
    assert mig.down_revision == "pc20261119_fk_rules_to_model"
    assert len(mig.revision) <= 32, "alembic_version.version_num is VARCHAR(32)"


def test_the_chain_still_has_exactly_one_head():
    """A second head would make `alembic upgrade head` refuse on deploy."""
    from alembic.script import ScriptDirectory

    script = ScriptDirectory(str(_load_migration_path().parents[1]))
    heads = script.get_heads()
    assert len(heads) == 1, heads
    lineage = {rev.revision for rev in script.iterate_revisions(heads[0], "pc20261119_fk_rules_to_model")}
    assert "pc20261122_voice_turn_traces" in lineage
