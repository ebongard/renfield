"""Real-Postgres integration tests for the ``pc20260423_atoms_per_document`` migration.

The migration collapses per-chunk atoms into per-document atoms. It ships with
service-level unit coverage, but nothing exercised the migration SQL itself
against a real Postgres — the three load-bearing pieces (the heterogeneous-tier
pre-migration gate, the MIN-based conservative tier collapse, and the lossy
downgrade) were untested. This module closes ebongard/renfield#447.

Harness
-------
Each test runs inside a dedicated, throwaway Postgres SCHEMA with ``search_path``
pinned to it, so it never touches the ``public``-schema tables the other
Postgres tests use, and teardown is a single ``DROP SCHEMA ... CASCADE`` (which
also side-steps the ``Base.metadata.create_all`` → ``drop_all`` FK-ordering drift
seen on the build box).

The migration's ``upgrade()`` / ``downgrade()`` are invoked by binding alembic's
``op`` proxy to a ``MigrationContext`` on the test connection (the same
``run_sync`` bridge ``alembic/env.py`` uses under asyncpg). We deliberately do
NOT run the full alembic chain: the history has 30+ independent roots, so
``upgrade base→pc20260422`` is a non-deterministic DAG walk. Instead we hand-build
the exact pre-``pc20260423`` shape of the five tables the migration touches
(``users``, ``knowledge_bases``, ``atoms``, ``documents``, ``document_chunks`` —
i.e. ``document_chunks`` still carries ``atom_id`` and ``documents`` does NOT yet
have ``atom_id`` / ``circle_tier``), seed it, and run the real migration functions.

Gated on ``RENFIELD_TEST_PG_URL`` (skipped cleanly when unset), matching the
other ``@pytest.mark.database`` Postgres tests. On the ``.159`` build box the
container points it at the dedicated ``renfield_test`` DB.
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


# ---------------------------------------------------------------------------
# Locate + load the migration module by file path.
#
# We load it directly rather than importing it: the top-level name ``alembic``
# resolves to the installed library, and ``alembic/versions/`` is not an
# importable package. ``services.database`` is used only to resolve the backend
# root (works both from the repo layout and the container's ``/app`` mount).
# ---------------------------------------------------------------------------
def _backend_root() -> Path:
    import services.database as _db

    return Path(_db.__file__).resolve().parents[1]


_MIG_PATH = _backend_root() / "alembic" / "versions" / "pc20260423_atoms_per_document.py"


def _load_migration():
    """Return a FRESH module instance of the migration under test.

    A fresh instance per call keeps the per-call ``op`` monkeypatch isolated
    (the upgrade→downgrade→upgrade cycle test runs it three times).
    """
    spec = importlib.util.spec_from_file_location("pc20260423_mig_under_test", _MIG_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _pg_test_dsn() -> str | None:
    return os.environ.get("RENFIELD_TEST_PG_URL")


# ---------------------------------------------------------------------------
# Dedicated-schema connection fixture.
# ---------------------------------------------------------------------------
@pytest.fixture
async def schema_conn():
    """Yield ``(AsyncConnection, schema_name)`` pinned to a throwaway schema.

    The connection is a single physical asyncpg connection (NullPool) so the
    session-level ``SET search_path`` persists across commits for the whole
    test. Torn down with ``DROP SCHEMA ... CASCADE``.
    """
    dsn = _pg_test_dsn()
    if dsn is None:
        pytest.skip("RENFIELD_TEST_PG_URL not set — Postgres tests disabled")
    if dsn.startswith("postgresql://"):
        dsn = dsn.replace("postgresql://", "postgresql+asyncpg://", 1)

    engine = create_async_engine(dsn, poolclass=NullPool, future=True)
    schema = f"pc447_{uuid.uuid4().hex[:12]}"
    conn = await engine.connect()
    try:
        await conn.exec_driver_sql(f'CREATE SCHEMA "{schema}"')
        # Session-level SET; committed so it persists on this physical
        # connection for the remainder of the test.
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


# ---------------------------------------------------------------------------
# Pre-``pc20260423`` schema (only the five tables the migration touches),
# in the exact BEFORE shape: chunks carry ``atom_id`` + the named FK, and
# ``documents`` has neither ``atom_id`` nor ``circle_tier`` yet.
# ---------------------------------------------------------------------------
_PRE_MIGRATION_DDL = [
    "CREATE TABLE users (id SERIAL PRIMARY KEY, username VARCHAR(100))",
    (
        "CREATE TABLE knowledge_bases ("
        " id SERIAL PRIMARY KEY,"
        " owner_id INTEGER REFERENCES users(id),"
        " default_circle_tier INTEGER NOT NULL DEFAULT 0)"
    ),
    (
        "CREATE TABLE atoms ("
        " atom_id VARCHAR(36) PRIMARY KEY,"
        " atom_type VARCHAR(32) NOT NULL,"
        " source_table VARCHAR(64) NOT NULL,"
        " source_id VARCHAR(64) NOT NULL,"
        " owner_user_id INTEGER NOT NULL REFERENCES users(id),"
        " policy JSON NOT NULL,"
        " created_at TIMESTAMP,"
        " updated_at TIMESTAMP,"
        " CONSTRAINT uq_atoms_source UNIQUE (atom_type, source_table, source_id))"
    ),
    (
        "CREATE TABLE documents ("
        " id SERIAL PRIMARY KEY,"
        " knowledge_base_id INTEGER REFERENCES knowledge_bases(id),"
        " filename VARCHAR(255),"
        " created_at TIMESTAMP DEFAULT NOW())"
    ),
    (
        "CREATE TABLE document_chunks ("
        " id SERIAL PRIMARY KEY,"
        " document_id INTEGER NOT NULL REFERENCES documents(id),"
        " circle_tier INTEGER NOT NULL DEFAULT 0,"
        " atom_id VARCHAR(36),"
        " created_at TIMESTAMP DEFAULT NOW(),"
        " CONSTRAINT fk_document_chunks_atom FOREIGN KEY (atom_id)"
        "   REFERENCES atoms(atom_id) ON DELETE CASCADE)"
    ),
]


async def _build_pre_migration_schema(conn) -> None:
    for stmt in _PRE_MIGRATION_DDL:
        await conn.exec_driver_sql(stmt)
    await conn.commit()


async def _scalar(conn, sql: str, params: dict | None = None):
    result = await conn.execute(text(sql), params or {})
    return result.scalar()


async def _seed_user(conn, username: str = "owner") -> int:
    return await _scalar(
        conn,
        "INSERT INTO users (username) VALUES (:u) RETURNING id",
        {"u": username},
    )


async def _seed_kb(conn, owner_id: int | None, default_tier: int = 0) -> int:
    return await _scalar(
        conn,
        "INSERT INTO knowledge_bases (owner_id, default_circle_tier) "
        "VALUES (:o, :t) RETURNING id",
        {"o": owner_id, "t": default_tier},
    )


async def _seed_document(conn, kb_id: int, filename: str = "doc.pdf") -> int:
    return await _scalar(
        conn,
        "INSERT INTO documents (knowledge_base_id, filename) "
        "VALUES (:kb, :fn) RETURNING id",
        {"kb": kb_id, "fn": filename},
    )


async def _seed_chunk(conn, document_id: int, circle_tier: int, atom_id: str | None = None) -> int:
    return await _scalar(
        conn,
        "INSERT INTO document_chunks (document_id, circle_tier, atom_id) "
        "VALUES (:d, :t, :a) RETURNING id",
        {"d": document_id, "t": circle_tier, "a": atom_id},
    )


async def _seed_kb_chunk_atom(conn, owner_id: int, source_id: int, tier: int) -> str:
    """Insert a faithful pre-migration per-chunk atom and return its id."""
    atom_id = str(uuid.uuid4())
    await conn.execute(
        text(
            "INSERT INTO atoms (atom_id, atom_type, source_table, source_id, "
            "                   owner_user_id, policy, created_at, updated_at) "
            "VALUES (:aid, 'kb_chunk', 'document_chunks', :sid, :owner, "
            "        json_build_object('tier', CAST(:tier AS INTEGER)), NOW(), NOW())"
        ),
        {"aid": atom_id, "sid": str(source_id), "owner": owner_id, "tier": tier},
    )
    return atom_id


async def _count_atoms(conn, atom_type: str) -> int:
    return await _scalar(
        conn,
        "SELECT COUNT(*) FROM atoms WHERE atom_type = :t",
        {"t": atom_type},
    )


async def _column_exists(conn, schema: str, table: str, column: str) -> bool:
    n = await _scalar(
        conn,
        "SELECT COUNT(*) FROM information_schema.columns "
        "WHERE table_schema = :s AND table_name = :t AND column_name = :c",
        {"s": schema, "t": table, "c": column},
    )
    return bool(n)


async def _run_migration(conn, direction: str) -> None:
    """Run the migration's ``upgrade`` / ``downgrade`` against ``conn``.

    Binds alembic's ``op`` to a MigrationContext on the (sync-bridged)
    connection, then invokes the real migration function. On failure the
    transaction is rolled back and the exception re-raised so callers can
    assert on it (e.g. the pre-migration gate's ``RuntimeError``).
    """
    from alembic.operations import Operations
    from alembic.runtime.migration import MigrationContext

    mig = _load_migration()

    def _apply(sync_conn):
        ctx = MigrationContext.configure(connection=sync_conn)
        op_obj = Operations(ctx)
        prev = mig.op
        mig.op = op_obj
        try:
            getattr(mig, direction)()
        finally:
            mig.op = prev

    try:
        await conn.run_sync(_apply)
    except Exception:
        await conn.rollback()
        raise
    await conn.commit()


# ===========================================================================
# 1. Pre-migration gate — heterogeneous chunk tiers must block the upgrade.
# ===========================================================================
async def test_gate_blocks_heterogeneous_tiers(schema_conn):
    conn, schema = schema_conn
    await _build_pre_migration_schema(conn)

    user_id = await _seed_user(conn)
    kb_id = await _seed_kb(conn, owner_id=user_id, default_tier=0)
    doc_id = await _seed_document(conn, kb_id)
    # Same document, two chunks at DIFFERENT tiers → the collapse would be a
    # silent tier-up leak, so the migration must refuse to run.
    await _seed_chunk(conn, doc_id, circle_tier=0)
    await _seed_chunk(conn, doc_id, circle_tier=2)
    await conn.commit()

    with pytest.raises(RuntimeError, match="Migration blocked"):
        await _run_migration(conn, "upgrade")

    # The gate fires BEFORE any DDL, so the new columns must not have appeared.
    assert not await _column_exists(conn, schema, "documents", "atom_id")
    assert not await _column_exists(conn, schema, "documents", "circle_tier")


# ===========================================================================
# 2. MIN-based collapse picks the most-restrictive tier.
# ===========================================================================
async def test_min_collapse_picks_most_restrictive(schema_conn):
    conn, _schema = schema_conn
    await _build_pre_migration_schema(conn)

    user_id = await _seed_user(conn)
    # KB default tier is 4 (public) but every chunk is tier 2 → the collapse
    # must pick MIN(chunk tiers) = 2, NOT the KB default, and NOT the max.
    kb_id = await _seed_kb(conn, owner_id=user_id, default_tier=4)
    doc_id = await _seed_document(conn, kb_id)
    for _ in range(3):
        await _seed_chunk(conn, doc_id, circle_tier=2)
    await conn.commit()

    await _run_migration(conn, "upgrade")

    row = (
        await conn.execute(
            text("SELECT atom_id, circle_tier FROM documents WHERE id = :d"),
            {"d": doc_id},
        )
    ).first()
    assert row is not None
    assert row.circle_tier == 2, "conservative collapse must use MIN(chunk tiers)"
    assert row.atom_id is not None, "back-fill must populate documents.atom_id"

    # The per-document atom carries the same collapsed tier + points back.
    atom = (
        await conn.execute(
            text(
                "SELECT atom_type, source_table, source_id, (policy->>'tier')::int AS tier "
                "FROM atoms WHERE atom_id = :a"
            ),
            {"a": row.atom_id},
        )
    ).first()
    assert atom is not None
    assert atom.atom_type == "kb_document"
    assert atom.source_table == "documents"
    assert atom.source_id == str(doc_id)
    assert atom.tier == 2


# ===========================================================================
# 3. upgrade → downgrade → upgrade cycle (idempotency / no schema corruption).
# ===========================================================================
async def test_upgrade_downgrade_cycle(schema_conn):
    conn, schema = schema_conn
    await _build_pre_migration_schema(conn)

    user_id = await _seed_user(conn)
    kb_id = await _seed_kb(conn, owner_id=user_id, default_tier=3)
    doc_id = await _seed_document(conn, kb_id)
    # Homogeneous tier (gate passes). Faithful pre-state: each chunk has its
    # own kb_chunk atom.
    c1 = await _seed_chunk(conn, doc_id, circle_tier=1)
    c2 = await _seed_chunk(conn, doc_id, circle_tier=1)
    a1 = await _seed_kb_chunk_atom(conn, user_id, c1, tier=1)
    a2 = await _seed_kb_chunk_atom(conn, user_id, c2, tier=1)
    await conn.execute(
        text("UPDATE document_chunks SET atom_id = :a WHERE id = :c"),
        {"a": a1, "c": c1},
    )
    await conn.execute(
        text("UPDATE document_chunks SET atom_id = :a WHERE id = :c"),
        {"a": a2, "c": c2},
    )
    await conn.commit()

    assert await _count_atoms(conn, "kb_chunk") == 2
    assert await _count_atoms(conn, "kb_document") == 0

    # --- upgrade -------------------------------------------------------------
    await _run_migration(conn, "upgrade")
    assert await _count_atoms(conn, "kb_document") >= 1
    assert await _count_atoms(conn, "kb_chunk") == 0, "old per-chunk atoms removed"
    assert await _column_exists(conn, schema, "documents", "atom_id")
    assert not await _column_exists(conn, schema, "document_chunks", "atom_id")

    # --- downgrade (documented-lossy; verify no corruption) ------------------
    await _run_migration(conn, "downgrade")
    assert await _count_atoms(conn, "kb_chunk") == 2, "per-chunk atoms rebuilt (one per chunk)"
    assert await _count_atoms(conn, "kb_document") == 0, "kb_document atoms removed"
    assert not await _column_exists(conn, schema, "documents", "atom_id"), (
        "documents.atom_id column dropped on downgrade"
    )
    assert not await _column_exists(conn, schema, "documents", "circle_tier")
    assert await _column_exists(conn, schema, "document_chunks", "atom_id"), (
        "document_chunks.atom_id restored on downgrade"
    )

    # --- upgrade AGAIN (must succeed — idempotency / clean re-apply) ---------
    await _run_migration(conn, "upgrade")
    assert await _count_atoms(conn, "kb_document") >= 1
    assert await _count_atoms(conn, "kb_chunk") == 0
    assert await _column_exists(conn, schema, "documents", "atom_id")
    assert not await _column_exists(conn, schema, "document_chunks", "atom_id")


# ===========================================================================
# 5. The 3-phase atom-first document write end-to-end (post-migration schema).
#
#    Uses the real ORM schema (full create_all into the throwaway schema) and
#    the real ``RAGService.create_document_record`` code path, so it verifies
#    the invariant the migration establishes: every document is created WITH a
#    per-document atom whose policy tier is the KB default.
# ===========================================================================
async def test_create_document_populates_atom_id_and_tier_from_kb(schema_conn):
    conn, _schema = schema_conn

    from models.database import Base

    await conn.run_sync(Base.metadata.create_all)
    await conn.commit()

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from models.database import KnowledgeBase, Role, User

    sm = async_sessionmaker(bind=conn, class_=AsyncSession, expire_on_commit=False)

    async with sm() as session:
        role = Role(name="doc_role")
        session.add(role)
        await session.flush()
        user = User(
            username="doc_owner",
            email="doc_owner@ex.test",
            password_hash="x",
            role_id=role.id,
            is_active=True,
        )
        session.add(user)
        await session.flush()
        kb = KnowledgeBase(name="Steuern KB", owner_id=user.id, default_circle_tier=3)
        session.add(kb)
        await session.commit()
        kb_id = kb.id

    async with sm() as session:
        from services.rag_service import RAGService

        svc = RAGService(session)
        doc = await svc.create_document_record(
            file_path="/tmp/rechnung.pdf",
            knowledge_base_id=kb_id,
            filename="rechnung.pdf",
            file_hash="hash-" + uuid.uuid4().hex,
        )
        doc_id = doc.id
        doc_atom_id = doc.atom_id
        # Phase result: the document is access-controlled from the first commit.
        assert doc_atom_id is not None, "create_document_record must populate atom_id"
        assert doc.circle_tier == 3, "document tier inherits the KB default tier"

    async with sm() as session:
        atom = (
            await session.execute(
                text(
                    "SELECT atom_type, source_table, source_id, (policy->>'tier')::int AS tier "
                    "FROM atoms WHERE atom_id = :a"
                ),
                {"a": doc_atom_id},
            )
        ).first()
        assert atom is not None
        assert atom.atom_type == "kb_document"
        assert atom.source_table == "documents"
        # 3-phase write: the atom's source_id is finalized to the real doc id.
        assert atom.source_id == str(doc_id)
        assert atom.tier == 3, "atom policy tier matches the KB default tier"
