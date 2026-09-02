"""Unit tests for the shared AtomOwnerResolverMixin (#448).

Extracted from three identical `_resolve_owner_user_id` copies; these lock in
the back-fill policy (explicit user → first user → None) + the per-instance cache.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from services.atom_owner import AtomOwnerResolverMixin


class _Host(AtomOwnerResolverMixin):
    def __init__(self, db):
        self.db = db


def _db_returning(first_user_id):
    """A mock AsyncSession whose execute().scalar() yields first_user_id."""
    db = MagicMock()
    result = MagicMock()
    result.scalar = MagicMock(return_value=first_user_id)
    db.execute = AsyncMock(return_value=result)
    return db


@pytest.mark.unit
@pytest.mark.asyncio
class TestAtomOwnerResolver:
    async def test_explicit_user_id_wins_without_query(self):
        db = _db_returning(999)
        host = _Host(db)
        assert await host._resolve_owner_user_id(42) == 42
        db.execute.assert_not_awaited()  # explicit id → no DB hit

    async def test_falls_back_to_first_user_and_caches(self):
        db = _db_returning(7)
        host = _Host(db)
        assert await host._resolve_owner_user_id(None) == 7
        assert host._fallback_owner_id == 7
        # second call hits the cache — no further query
        assert await host._resolve_owner_user_id(None) == 7
        assert db.execute.await_count == 1

    async def test_none_when_no_users_and_not_cached(self):
        db = _db_returning(None)
        host = _Host(db)
        assert await host._resolve_owner_user_id(None) is None
        assert host._fallback_owner_id is None
        # not cached → a later call re-queries (a user may have been created)
        assert await host._resolve_owner_user_id(None) is None
        assert db.execute.await_count == 2

    async def test_cache_is_per_instance(self):
        host_a = _Host(_db_returning(1))
        host_b = _Host(_db_returning(2))
        assert await host_a._resolve_owner_user_id(None) == 1
        assert await host_b._resolve_owner_user_id(None) == 2
        assert host_a._fallback_owner_id == 1
        assert host_b._fallback_owner_id == 2
