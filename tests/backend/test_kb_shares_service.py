"""
Unit tests for kb_shares_service — the legacy KBPermission consumer rewrite.

These are pure surface tests: signature shape, error handling on bad inputs,
and the SQL-string contents that wire the chunks → atoms → grants joins.
Real DB roundtrips live in tests/backend/test_circles_v1_migration.py and
the (deferred) integration suite.
"""
from __future__ import annotations

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
async def test_share_kb_emits_upsert_with_join_chain():
    db = MagicMock()
    db.execute = AsyncMock()
    db.commit = AsyncMock()

    await kb_shares_service.share_kb(
        db, kb_id=42, target_user_id=7, permission_level="write", granted_by=99,
    )

    db.execute.assert_called_once()
    sql_text = str(db.execute.call_args.args[0])
    # The join chain hits atoms → document_chunks → documents
    assert "INSERT INTO atom_explicit_grants" in sql_text
    assert "FROM atoms a" in sql_text
    assert "JOIN document_chunks dc" in sql_text
    assert "JOIN documents d" in sql_text
    assert "WHERE a.source_table = 'document_chunks'" in sql_text
    # Idempotent upsert
    assert "ON CONFLICT" in sql_text
    # Bind params
    binds = db.execute.call_args.args[1]
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
    fake_result = MagicMock()
    fake_result.rowcount = 13
    db.execute = AsyncMock(return_value=fake_result)
    db.commit = AsyncMock()

    removed = await kb_shares_service.revoke_kb_share(db, kb_id=1, target_user_id=2)

    assert removed == 13
    sql_text = str(db.execute.call_args.args[0])
    assert "DELETE FROM atom_explicit_grants" in sql_text
    assert "WHERE g.atom_id = a.atom_id" in sql_text
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
    # latest_per_chunk CTE picks most-recent grant per (user, chunk).
    assert "latest_per_chunk" in sql
    # The legacy arbitrary MAX(granted_by) must not appear.
    assert "MAX(granted_by)" not in sql.replace(" ", "")
    assert "MAX(g.granted_by)" not in sql


@pytest.mark.asyncio
@pytest.mark.unit
async def test_get_user_kb_permission_level_none_when_no_rows():
    db = MagicMock()
    fake_result = MagicMock()
    fake_result.scalar.return_value = None
    db.execute = AsyncMock(return_value=fake_result)

    level = await kb_shares_service.get_user_kb_permission_level(db, kb_id=1, user_id=2)
    assert level is None


@pytest.mark.asyncio
@pytest.mark.unit
async def test_get_user_kb_permission_level_translates_rank_to_string():
    cases = [(1, "read"), (2, "write"), (3, "admin")]
    for rank, expected in cases:
        db = MagicMock()
        fake_result = MagicMock()
        fake_result.scalar.return_value = rank
        db.execute = AsyncMock(return_value=fake_result)

        level = await kb_shares_service.get_user_kb_permission_level(db, kb_id=1, user_id=2)
        assert level == expected, f"rank {rank} should map to {expected}"


@pytest.mark.asyncio
@pytest.mark.unit
async def test_list_user_shared_kb_ids_returns_set_of_ints():
    db = MagicMock()
    fake_result = MagicMock()
    fake_result.fetchall.return_value = [(7,), (42,), (101,)]
    db.execute = AsyncMock(return_value=fake_result)

    ids = await kb_shares_service.list_user_shared_kb_ids(db, user_id=42)

    assert isinstance(ids, set)
    assert ids == {7, 42, 101}
