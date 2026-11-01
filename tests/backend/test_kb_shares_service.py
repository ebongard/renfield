"""
Unit tests for kb_shares_service — the legacy KBPermission consumer rewrite.

These are pure surface tests: signature shape, error handling on bad inputs,
and the SQL-string contents (kb_shares + the legacy atoms → grants joins).
Real DB roundtrips live in tests/backend/test_circles_v1_migration.py and
the (deferred) integration suite.
"""
from __future__ import annotations

import os
import uuid
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from services import kb_shares_service


@pytest.mark.unit
def test_share_kb_rejects_unknown_permission_level():
    db = AsyncMock()
    with pytest.raises(ValueError, match="Invalid permission_level"):
        # Not awaited because we expect it to raise before any await
        coro = kb_shares_service.share_kb(
            db, kb_id=1, target_user_id=2, permission_level="god-mode", granted_by=3,
        )
        # Drain to surface the ValueError synchronously
        try:
            coro.send(None)
        except StopIteration:
            pass


@pytest.mark.asyncio
@pytest.mark.unit
async def test_share_kb_upserts_the_kb_shares_row_and_writes_no_document_grants():
    db = MagicMock()
    db.execute = AsyncMock()
    db.commit = AsyncMock()

    await kb_shares_service.share_kb(
        db, kb_id=42, target_user_id=7, permission_level="write", granted_by=99,
    )

    statements = [str(c.args[0]) for c in db.execute.call_args_list]
    upsert, align = statements
    # The share is ONE row, evaluated at query time — it covers later documents.
    assert "INSERT INTO kb_shares" in upsert
    assert "ON CONFLICT (kb_id, user_id) DO UPDATE" in upsert
    # Legacy per-document grants only get their level aligned; none are created.
    assert align.startswith("UPDATE atom_explicit_grants")
    assert not any("INSERT INTO atom_explicit_grants" in st for st in statements)
    assert not any("document_chunks" in st for st in statements)
    binds = db.execute.call_args_list[0].args[1]
    assert binds["target"] == 7
    assert binds["perm"] == "write"
    assert binds["granter"] == 99
    assert binds["kb_id"] == 42
    assert isinstance(binds["now"], datetime)
    db.commit.assert_called_once()


@pytest.mark.asyncio
@pytest.mark.unit
async def test_revoke_kb_share_returns_rowcount():
    db = MagicMock()
    db.execute = AsyncMock(side_effect=[MagicMock(rowcount=1), MagicMock(rowcount=13)])
    db.commit = AsyncMock()

    removed = await kb_shares_service.revoke_kb_share(db, kb_id=1, target_user_id=2)

    # The kb_shares row + the legacy per-document grants of that user.
    assert removed == 14
    share_sql, grants_sql = (str(c.args[0]) for c in db.execute.call_args_list)
    assert "DELETE FROM kb_shares" in share_sql
    assert "DELETE FROM atom_explicit_grants" in grants_sql
    assert "WHERE g.atom_id = a.atom_id" in grants_sql
    db.commit.assert_called_once()


@pytest.mark.asyncio
@pytest.mark.unit
async def test_revoke_kb_share_rowcount_none_returns_zero():
    db = MagicMock()
    fake_result = MagicMock()
    fake_result.rowcount = None  # some drivers return None on no-op
    db.execute = AsyncMock(return_value=fake_result)
    db.commit = AsyncMock()

    removed = await kb_shares_service.revoke_kb_share(db, kb_id=1, target_user_id=2)
    assert removed == 0


@pytest.mark.asyncio
@pytest.mark.unit
async def test_list_kb_shares_aggregates_to_max_permission():
    db = MagicMock()
    fake_result = MagicMock()
    # Three users, three different MAX ranks
    fake_result.fetchall.return_value = [
        MagicMock(user_id=10, rank=3, granted_by=1, granted_at=datetime(2026, 1, 1)),  # admin
        MagicMock(user_id=11, rank=2, granted_by=1, granted_at=datetime(2026, 2, 1)),  # write
        MagicMock(user_id=12, rank=1, granted_by=2, granted_at=datetime(2026, 3, 1)),  # read
    ]
    db.execute = AsyncMock(return_value=fake_result)

    rows = await kb_shares_service.list_kb_shares(db, kb_id=42)

    assert len(rows) == 3
    perms = {r["user_id"]: r["permission"] for r in rows}
    assert perms == {10: "admin", 11: "write", 12: "read"}


@pytest.mark.asyncio
@pytest.mark.unit
async def test_list_kb_shares_uses_distinct_on_for_paired_granter():
    """
    Review BLOCKING #4 regression guard: the aggregate must pair granted_by
    with the row producing granted_at (not arbitrary MAX(granted_by) with
    MIN(granted_at) as the legacy code did).
    """
    db = MagicMock()
    db.execute = AsyncMock(return_value=MagicMock(fetchall=lambda: []))
    await kb_shares_service.list_kb_shares(db, kb_id=1)

    sql = str(db.execute.call_args.args[0])
    # DISTINCT ON ensures one row per user, paired correctly.
    assert "DISTINCT ON" in sql
    # kb_shares rows and the most-recent legacy grant per (user, document)
    # are merged before the per-user aggregation.
    assert "per_source" in sql
    assert "FROM kb_shares ks" in sql
    # The legacy arbitrary MAX(granted_by) must not appear.
    assert "MAX(granted_by)" not in sql.replace(" ", "")
    assert "MAX(g.granted_by)" not in sql


@pytest.mark.asyncio
@pytest.mark.unit
async def test_get_user_kb_permission_level_none_when_no_rows():
    db = MagicMock()
    db.execute = AsyncMock(return_value=MagicMock(fetchall=lambda: []))

    level = await kb_shares_service.get_user_kb_permission_level(db, kb_id=1, user_id=2)
    assert level is None


@pytest.mark.asyncio
@pytest.mark.unit
async def test_get_user_kb_permission_level_translates_rank_to_string():
    cases = [(1, "read"), (2, "write"), (3, "admin")]
    for rank, expected in cases:
        db = MagicMock()
        fake_result = MagicMock()
        fake_result.fetchall.return_value = [MagicMock(kb_id=1, rank=rank)]
        db.execute = AsyncMock(return_value=fake_result)

        level = await kb_shares_service.get_user_kb_permission_level(db, kb_id=1, user_id=2)
        assert level == expected, f"rank {rank} should map to {expected}"


@pytest.mark.asyncio
@pytest.mark.unit
async def test_list_user_shared_kb_ids_returns_set_of_ints():
    db = MagicMock()
    fake_result = MagicMock()
    fake_result.fetchall.return_value = [
        MagicMock(kb_id=7, rank=1), MagicMock(kb_id=42, rank=2), MagicMock(kb_id=101, rank=3),
    ]
    db.execute = AsyncMock(return_value=fake_result)

    ids = await kb_shares_service.list_user_shared_kb_ids(db, user_id=42)

    assert isinstance(ids, set)
    assert ids == {7, 42, 101}


@pytest.mark.asyncio
@pytest.mark.unit
async def test_get_user_kb_permission_levels_batches_into_one_query():
    """Audit K2 regression guard: a list of KBs resolves with exactly one
    SQL execute call regardless of how many KBs are passed in."""
    db = MagicMock()
    fake_result = MagicMock()
    fake_result.fetchall.return_value = [
        MagicMock(kb_id=1, rank=3),
        MagicMock(kb_id=2, rank=2),
        MagicMock(kb_id=3, rank=1),
    ]
    db.execute = AsyncMock(return_value=fake_result)

    levels = await kb_shares_service.get_user_kb_permission_levels(
        db, user_id=42, kb_ids=[1, 2, 3, 4, 5]
    )

    db.execute.assert_awaited_once()
    assert levels == {1: "admin", 2: "write", 3: "read"}
    # GROUP BY must be present — single query, not per-KB
    sql = str(db.execute.call_args.args[0])
    assert "GROUP BY kb_id" in sql
    # Both sources are restricted: legacy grants and kb_shares.
    assert "d.knowledge_base_id = ANY(:kb_ids)" in sql
    assert "ks.kb_id = ANY(:kb_ids)" in sql


@pytest.mark.asyncio
@pytest.mark.unit
async def test_get_user_kb_permission_levels_empty_kb_ids_short_circuits():
    db = MagicMock()
    db.execute = AsyncMock()

    levels = await kb_shares_service.get_user_kb_permission_levels(
        db, user_id=42, kb_ids=[]
    )

    assert levels == {}
    db.execute.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.unit
async def test_get_user_kb_permission_levels_none_returns_all_grants():
    db = MagicMock()
    fake_result = MagicMock()
    fake_result.fetchall.return_value = [
        MagicMock(kb_id=7, rank=2),
    ]
    db.execute = AsyncMock(return_value=fake_result)

    levels = await kb_shares_service.get_user_kb_permission_levels(
        db, user_id=42, kb_ids=None
    )

    assert levels == {7: "write"}
    sql = str(db.execute.call_args.args[0])
    # Without a kb_ids filter, the ANY() clause is absent
    assert "ANY(:kb_ids)" not in sql


# ===========================================================================
# Real-Postgres integration test (ebongard/renfield#447)
#
# Since pc20261101 a KB share is ONE kb_shares row, evaluated at query time;
# share_kb writes no per-document grants. Legacy grants (one per document,
# never per chunk) are kept, and revoke deletes them with the share row.
# The behaviour across documents added later lives in
# test_kb_shares_cover_new_documents_pg.py.
#
# Runs inside a throwaway schema (search_path pinned) so it never touches the
# public-schema tables; teardown is DROP SCHEMA ... CASCADE. Skipped cleanly
# when RENFIELD_TEST_PG_URL is unset, matching the other @pytest.mark.database
# Postgres tests.
# ===========================================================================
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool


def _pg_test_dsn() -> str | None:
    return os.environ.get("RENFIELD_TEST_PG_URL")


@pytest.fixture
async def kb_schema_conn():
    dsn = _pg_test_dsn()
    if dsn is None:
        pytest.skip("RENFIELD_TEST_PG_URL not set — Postgres tests disabled")
    if dsn.startswith("postgresql://"):
        dsn = dsn.replace("postgresql://", "postgresql+asyncpg://", 1)

    engine = create_async_engine(dsn, poolclass=NullPool, future=True)
    schema = f"pc447kb_{uuid.uuid4().hex[:12]}"
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


@pytest.mark.database
async def test_share_is_one_row_and_revoke_clears_legacy_grants(kb_schema_conn):
    conn, schema = kb_schema_conn

    from models.database import Base, KnowledgeBase, Role, User
    from services.rag_service import RAGService

    # 🛑 Create the tables IN the throwaway schema, explicitly. A bare
    # `create_all` skips every table that is merely VISIBLE on the search
    # path -- and `public` is on it (for the pgvector type) -- so it created
    # nothing here, every row below landed in public.roles/users/... and was
    # committed there. The test passed once on a clean database and failed
    # on every later run with a UniqueViolation on `kb_role`, which read like
    # an ordering dependency.
    await conn.run_sync(
        lambda sync_conn: Base.metadata.create_all(
            sync_conn.execution_options(schema_translate_map={None: schema})
        )
    )
    await conn.commit()
    in_schema = (
        await conn.execute(
            text("SELECT to_regclass(:t) IS NOT NULL"), {"t": f'"{schema}".roles'}
        )
    ).scalar()
    assert in_schema, "tables were not created in the throwaway schema"

    sm = async_sessionmaker(bind=conn, class_=AsyncSession, expire_on_commit=False)

    # Seed an owner + a share target + a KB.
    async with sm() as session:
        role = Role(name="kb_role")
        session.add(role)
        await session.flush()
        owner = User(
            username="kb_owner",
            email="kb_owner@ex.test",
            password_hash="x",
            role_id=role.id,
            is_active=True,
        )
        target = User(
            username="kb_target",
            email="kb_target@ex.test",
            password_hash="x",
            role_id=role.id,
            is_active=True,
        )
        session.add_all([owner, target])
        await session.flush()
        kb = KnowledgeBase(name="Shared KB", owner_id=owner.id, default_circle_tier=0)
        session.add(kb)
        await session.commit()
        kb_id, owner_id, target_id = kb.id, owner.id, target.id

    # 3 documents (each gets one kb_document atom via the real create path),
    # 50 chunks each. If the grant logic ever reverts to per-chunk it would
    # count 150 instead of 3.
    async with sm() as session:
        svc = RAGService(session)
        for i in range(3):
            doc = await svc.create_document_record(
                file_path=f"/tmp/doc{i}.pdf",
                knowledge_base_id=kb_id,
                filename=f"doc{i}.pdf",
                file_hash="h-" + uuid.uuid4().hex,
            )
            await session.execute(
                text(
                    "INSERT INTO document_chunks (document_id, content, circle_tier) "
                    "SELECT :doc, 'chunk ' || g, 0 FROM generate_series(1, 50) AS g"
                ),
                {"doc": doc.id},
            )
        await session.commit()

    # Sanity: 3 document atoms, 150 chunks.
    async with sm() as session:
        doc_atoms = (
            await session.execute(
                text("SELECT COUNT(*) FROM atoms WHERE atom_type = 'kb_document'")
            )
        ).scalar()
        chunk_count = (
            await session.execute(text("SELECT COUNT(*) FROM document_chunks"))
        ).scalar()
        assert doc_atoms == 3
        assert chunk_count == 150

    # Three legacy per-document grants, as the pre-kb_shares share_kb wrote them.
    async with sm() as session:
        await session.execute(
            text(
                "INSERT INTO atom_explicit_grants "
                "(atom_id, granted_to_user_id, permission_level, granted_by, granted_at) "
                "SELECT atom_id, :t, 'read', :o, NOW() FROM atoms "
                "WHERE atom_type = 'kb_document'"
            ),
            {"t": target_id, "o": owner_id},
        )
        await session.commit()

    # Share → ONE kb_shares row (not one per document, let alone per chunk),
    # and no new grant: the share is evaluated at query time.
    async with sm() as session:
        await kb_shares_service.share_kb(
            session,
            kb_id=kb_id,
            target_user_id=target_id,
            permission_level="read",
            granted_by=owner_id,
        )

    async with sm() as session:
        share_count = (
            await session.execute(text("SELECT COUNT(*) FROM kb_shares"))
        ).scalar()
        grant_count = (
            await session.execute(text("SELECT COUNT(*) FROM atom_explicit_grants"))
        ).scalar()
        assert share_count == 1
        assert grant_count == 3, (
            f"expected the 3 legacy grants untouched, got {grant_count} "
            "(more would mean share_kb is materialising grants again)"
        )

    # Revoke → removes the share row AND the 3 legacy grants.
    async with sm() as session:
        removed = await kb_shares_service.revoke_kb_share(
            session, kb_id=kb_id, target_user_id=target_id
        )
        assert removed == 4

    async with sm() as session:
        remaining = (
            await session.execute(
                text("SELECT COUNT(*) FROM atom_explicit_grants")
            )
        ).scalar()
        assert remaining == 0
        assert (await session.execute(text("SELECT COUNT(*) FROM kb_shares"))).scalar() == 0
