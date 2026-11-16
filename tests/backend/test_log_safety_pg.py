"""What survives `error_text_without_content` of a REAL Postgres error.

Why this file exists (2026-11-16): the cut stood at `[SQL:` only. Since the
log guard routes commit and flush handlers through it, constraint violations
arrive there, and Postgres puts the offending ROW VALUES into a `DETAIL:`
line that sits BEFORE the `[SQL:` tail:

    duplicate key value violates unique constraint "ix_users_username"
    DETAIL:  Key (username)=(<the value>) already exists.

The errors below are raised by the database, not written by hand -- the
driver and SQLAlchemy build the text, and what they build is the subject.

Applies only with `RENFIELD_TEST_PG_URL` (skipped otherwise).
"""
from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError

from models.database import FederationUserLink, Role, User
from utils.log_safety import error_text_without_content

pytestmark = [pytest.mark.postgres, pytest.mark.asyncio]

#: Distinctive, so that "absent" cannot be an accident of truncation alone.
_VALUE = "zz-private-value-zz"
_MISSING_ID = 987654321


async def _role(db) -> int:
    role = Role(name="log-safety-role", description="t", permissions=[])
    db.add(role)
    await db.flush()
    return role.id


class TestTheRowValuesOfAConstraintViolationAreCut:
    async def test_a_unique_violation_keeps_the_constraint_and_drops_the_key(
        self, pg_db_session
    ):
        db = pg_db_session
        role_id = await _role(db)
        db.add(User(username=_VALUE, password_hash="x", is_active=True, role_id=role_id))
        await db.flush()
        db.add(User(username=_VALUE, password_hash="x", is_active=True, role_id=role_id))
        with pytest.raises(IntegrityError) as raised:
            await db.flush()
        await db.rollback()

        raw = str(raised.value)
        # The premise: the database DID put the value into its message, and
        # before the SQL tail.
        assert "DETAIL:" in raw and _VALUE in raw.split("[SQL:")[0]

        safe = error_text_without_content(raised.value)
        assert _VALUE not in safe
        assert "DETAIL" not in safe
        assert "duplicate key value violates unique constraint" in safe
        assert "username" in safe            # the constraint's name: the diagnosis

    async def test_a_foreign_key_violation_keeps_the_constraint_and_drops_the_key(
        self, pg_db_session
    ):
        db = pg_db_session
        db.add(FederationUserLink(
            peer_id=_MISSING_ID, querier_ref="ab" * 32, local_user_id=_MISSING_ID))
        with pytest.raises(IntegrityError) as raised:
            await db.flush()
        await db.rollback()

        raw = str(raised.value)
        assert "DETAIL:" in raw and str(_MISSING_ID) in raw.split("[SQL:")[0]

        safe = error_text_without_content(raised.value)
        assert str(_MISSING_ID) not in safe
        assert "violates foreign key constraint" in safe
        assert "federation_user_links" in safe

    async def test_a_not_null_violation_drops_the_failing_row(self, pg_db_session):
        """`Failing row contains (...)` lists EVERY column of the row."""
        db = pg_db_session
        role_id = await _role(db)
        with pytest.raises(IntegrityError) as raised:
            await db.execute(text(
                "INSERT INTO users (username, password_hash, role_id, is_active) "
                "VALUES (NULL, :secret, :role, true)"), {"secret": _VALUE, "role": role_id})
        await db.rollback()

        raw = str(raised.value)
        assert "Failing row contains" in raw and _VALUE in raw.split("[SQL:")[0]

        safe = error_text_without_content(raised.value)
        assert _VALUE not in safe
        assert "not-null constraint" in safe and "username" in safe

    async def test_a_raised_notice_with_hint_and_context_keeps_only_its_message(
        self, pg_db_session
    ):
        """`HINT:` and `CONTEXT:` are written by whoever raises -- a trigger
        or function may put row data there just as Postgres does in DETAIL."""
        db = pg_db_session
        with pytest.raises(DBAPIError) as raised:
            await db.execute(text(
                "DO $$ BEGIN RAISE EXCEPTION 'refused by the function' "
                f"USING DETAIL = 'detail {_VALUE}', HINT = 'hint {_VALUE}'; END $$"))
        await db.rollback()

        assert _VALUE in str(raised.value).split("[SQL:")[0]
        safe = error_text_without_content(raised.value)
        assert _VALUE not in safe
        assert "refused by the function" in safe
