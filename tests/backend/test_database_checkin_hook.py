"""The connection-checkin hook must leave the connection with NO open
transaction.

Regression: the checkin hook runs ``SELECT pg_advisory_unlock_all()`` through
SQLAlchemy's asyncpg DBAPI adapter, which is not autocommit — the SELECT opens a
real transaction. Before the fix nothing closed it, so the connection returned to
the pool ``idle in transaction`` holding a virtualxid lock; a rarely-reused
overflow connection could sit wedged for hours and block a
``CREATE INDEX CONCURRENTLY`` migration (observed on xidra 2026-08-29). The hook
now issues an explicit rollback after the unlock (safe — the unlock is
session-level / non-transactional).
"""
from unittest.mock import MagicMock

import pytest

from services.database import _release_leaked_advisory_locks_on_checkin as checkin

pytestmark = pytest.mark.unit


def _conn():
    c = MagicMock()
    c.cursor.return_value = MagicMock()
    return c


def test_checkin_rolls_back_after_unlock():
    """The reset SELECT ran, then the transaction it opened is rolled back."""
    conn = _conn()
    checkin(conn, MagicMock())
    conn.cursor.return_value.execute.assert_called_once_with(
        "SELECT pg_advisory_unlock_all();"
    )
    conn.rollback.assert_called_once()


def test_checkin_rolls_back_even_if_select_fails():
    """A failed unlock still leaves no dangling transaction behind."""
    conn = _conn()
    conn.cursor.return_value.execute.side_effect = RuntimeError("boom")
    checkin(conn, MagicMock())  # must not raise
    conn.rollback.assert_called_once()


def test_checkin_never_raises_when_rollback_fails():
    """A throwing rollback is swallowed — a checkin hook that raises breaks pool
    return for every caller."""
    conn = _conn()
    conn.rollback.side_effect = RuntimeError("rollback boom")
    checkin(conn, MagicMock())  # must not raise
    conn.rollback.assert_called_once()


def test_checkin_noop_on_none_connection():
    """A dead/None connection (checked in after its loop closed) is a no-op."""
    checkin(None, MagicMock())  # must not raise
