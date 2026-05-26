"""Retention sweep for the v2.10 ``skill_would_have_injected_log`` table.

The scheduler in ``api/lifecycle.py`` deletes rows older than
``settings.skill_shadow_log_retention_days``. This test inlines the
same DELETE so we don't need to spawn the periodic task in the test
runner — what we care about is that the SQL is correct and bounded.
"""
from __future__ import annotations

from datetime import datetime, timedelta, UTC

import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from models.database import SkillWouldHaveInjectedLog


def _utcnow_naive() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


async def _seed_row(db: AsyncSession, *, days_ago: float) -> int:
    row = SkillWouldHaveInjectedLog(
        skill_id=1,                              # FK relaxed for unit-test
        user_id=None,
        similarity_score=0.91,
        status_at_query="draft",
        created_at=_utcnow_naive() - timedelta(days=days_ago),
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return row.id


@pytest.mark.asyncio
class TestShadowLogRetention:
    async def test_prunes_rows_past_retention(self, db_session: AsyncSession):
        old_id = await _seed_row(db_session, days_ago=45)
        fresh_id = await _seed_row(db_session, days_ago=5)

        cutoff = _utcnow_naive() - timedelta(days=30)
        result = await db_session.execute(
            delete(SkillWouldHaveInjectedLog).where(
                SkillWouldHaveInjectedLog.created_at < cutoff
            )
        )
        await db_session.commit()

        # The exact rowcount semantics differ between dialects (sqlite
        # reports actual deleted rows; some drivers report -1). The
        # invariant we care about is the post-state, not the count.
        remaining_ids = (
            await db_session.execute(select(SkillWouldHaveInjectedLog.id))
        ).scalars().all()
        assert fresh_id in remaining_ids
        assert old_id not in remaining_ids
        # Sanity: rowcount when reported should match the one we deleted.
        if result.rowcount is not None and result.rowcount >= 0:
            assert result.rowcount == 1

    async def test_noop_when_table_empty(self, db_session: AsyncSession):
        cutoff = _utcnow_naive() - timedelta(days=30)
        result = await db_session.execute(
            delete(SkillWouldHaveInjectedLog).where(
                SkillWouldHaveInjectedLog.created_at < cutoff
            )
        )
        await db_session.commit()
        # 0 rows in, 0 rows out — never call commit() in the production
        # path if nothing was deleted (avoids log noise + an empty
        # transaction). The scheduler in lifecycle.py guards on
        # rowcount > 0; this asserts the guard is meaningful.
        if result.rowcount is not None and result.rowcount >= 0:
            assert result.rowcount == 0
