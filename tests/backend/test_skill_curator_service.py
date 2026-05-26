"""Tests for SkillCuratorService.

Pgvector cosine queries don't run on the sqlite test harness, so the
duplicate-finding SQL path is not exercised here — find_duplicate_pairs
short-circuits to [] on non-postgres dialects. The interesting curator
logic that DOES run on sqlite:

  - merge_pair: combines triggers, archives loser, bumps winner version,
    sets loser.merged_into_id, carries outcome counts
  - _pick_winner: success-rate tie-break, fallback to last_used_at
  - archive_stale: respects pinned, min_uses, success_rate, recency
  - list_active_user_ids: distinct user_ids with active non-seed skills
  - run_for_user: full pipeline returns CuratorReport
"""

from datetime import datetime, timedelta, UTC
from unittest.mock import patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.database import (
    EMBEDDING_DIMENSION,
    ProceduralSkill,
    Role,
    SKILL_SOURCE_AUTO_EXTRACTED,
    SKILL_SOURCE_SEED,
    User,
)


@pytest.fixture
def patched_embed():
    """Pgvector's SQLAlchemy type processor enforces vector dimension
    client-side; use the configured dimension instead of a short stub."""
    with patch(
        "services.skill_service.SkillService._embed",
        return_value=[0.1] * EMBEDDING_DIMENSION,
    ) as p:
        yield p


async def _make_role(db_session: AsyncSession, name: str) -> Role:
    role = Role(name=name)
    db_session.add(role)
    await db_session.commit()
    await db_session.refresh(role)
    return role


@pytest.fixture
async def c_user(db_session: AsyncSession) -> User:
    role = await _make_role(db_session, "curator_test_role")
    user = User(username="curator_tester", password_hash="x", role_id=role.id)
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user


async def _seed_skill(
    db, *, user_id, title, trigger_examples=None, tool_sequence=None,
    successes=0, failures=0, pinned=False, last_used_at=None,
    status="approved", source=SKILL_SOURCE_AUTO_EXTRACTED,
):
    """Direct ORM insert — bypasses SkillService so we can control
    every field including success_count/failure_count without going
    through the atom-registration path (those tests live in
    test_skill_service.py)."""
    from services.skill_service import SkillService
    skill = ProceduralSkill(
        user_id=user_id,
        title=title,
        body_md=f"body of {title}",
        trigger_examples=trigger_examples or [title.lower()],
        tool_sequence=tool_sequence or ["mcp.x"],
        source=source,
        success_count=successes,
        failure_count=failures,
        last_used_at=last_used_at,
        status=status,
        pinned=pinned,
        embedding=[0.1] * EMBEDDING_DIMENSION,
        circle_tier=0,
    )
    db.add(skill)
    await db.commit()
    await db.refresh(skill)
    # Cache invariant the service maintains.
    SkillService._has_skills_cache.clear()
    return skill


# ============================================================ winner pick
@pytest.mark.asyncio
class TestPickWinner:
    """Tests the rank-key logic via the bulk-load helper.

    Curator originally exposed `_pick_winner(id_a, id_b)` which issued a
    SELECT per call (N+1). The refactor swapped that for
    `_pick_winner_from_cache(id_a, id_b, by_id)` driven by a single
    bulk SELECT in `find_duplicate_pairs`. Tests construct the by_id
    dict directly so we still cover the rank-key tie-break path.
    """

    async def test_higher_success_rate_wins(self, db_session, c_user):
        from services.skill_curator_service import SkillCuratorService
        worse = await _seed_skill(
            db_session, user_id=c_user.id, title="Worse",
            successes=2, failures=8,
        )
        better = await _seed_skill(
            db_session, user_id=c_user.id, title="Better",
            successes=9, failures=1,
        )
        by_id = {worse.id: worse, better.id: better}
        winner, loser = SkillCuratorService._pick_winner_from_cache(
            worse.id, better.id, by_id,
        )
        assert winner == better.id
        assert loser == worse.id

    async def test_tie_breaks_on_usage_count(self, db_session, c_user):
        """Both 100% success — bigger sample size wins."""
        from services.skill_curator_service import SkillCuratorService
        sparse = await _seed_skill(
            db_session, user_id=c_user.id, title="Sparse",
            successes=1, failures=0,
        )
        dense = await _seed_skill(
            db_session, user_id=c_user.id, title="Dense",
            successes=10, failures=0,
        )
        by_id = {sparse.id: sparse, dense.id: dense}
        winner, _ = SkillCuratorService._pick_winner_from_cache(
            sparse.id, dense.id, by_id,
        )
        assert winner == dense.id


# ================================================================ merge
@pytest.mark.asyncio
class TestMergePair:
    async def test_combines_triggers_dedup(self, db_session, c_user):
        from services.skill_curator_service import SkillCuratorService
        a = await _seed_skill(
            db_session, user_id=c_user.id, title="A",
            trigger_examples=["t1", "t2"], successes=5,
        )
        b = await _seed_skill(
            db_session, user_id=c_user.id, title="B",
            trigger_examples=["t2", "t3"], successes=1,
        )
        svc = SkillCuratorService(db_session)
        await svc.merge_pair(loser_id=b.id, winner_id=a.id)

        winner = (await db_session.execute(
            select(ProceduralSkill).where(ProceduralSkill.id == a.id)
        )).scalar_one()
        loser = (await db_session.execute(
            select(ProceduralSkill).where(ProceduralSkill.id == b.id)
        )).scalar_one()

        # Dedup: t2 only appears once
        assert sorted(winner.trigger_examples) == sorted(["t1", "t2", "t3"])
        # Outcome counts merge into winner
        assert winner.success_count == 6
        # Loser archived + pointer set
        assert loser.status == "archived"
        assert loser.merged_into_id == a.id
        # Version bumped
        assert winner.version == 2

    async def test_carries_outcome_counts(self, db_session, c_user):
        from services.skill_curator_service import SkillCuratorService
        a = await _seed_skill(
            db_session, user_id=c_user.id, title="A",
            successes=10, failures=2,
        )
        b = await _seed_skill(
            db_session, user_id=c_user.id, title="B",
            successes=4, failures=1,
        )
        svc = SkillCuratorService(db_session)
        await svc.merge_pair(loser_id=b.id, winner_id=a.id)
        winner = (await db_session.execute(
            select(ProceduralSkill).where(ProceduralSkill.id == a.id)
        )).scalar_one()
        assert winner.success_count == 14
        assert winner.failure_count == 3

    async def test_caps_trigger_count_at_ten(self, db_session, c_user):
        from services.skill_curator_service import SkillCuratorService
        a = await _seed_skill(
            db_session, user_id=c_user.id, title="A",
            trigger_examples=[f"trig_a_{i}" for i in range(8)],
            successes=5,
        )
        b = await _seed_skill(
            db_session, user_id=c_user.id, title="B",
            trigger_examples=[f"trig_b_{i}" for i in range(8)],
            successes=1,
        )
        svc = SkillCuratorService(db_session)
        await svc.merge_pair(loser_id=b.id, winner_id=a.id)
        winner = (await db_session.execute(
            select(ProceduralSkill).where(ProceduralSkill.id == a.id)
        )).scalar_one()
        assert len(winner.trigger_examples) <= 10

    async def test_cap_enforced_when_winner_already_over_cap(
        self, db_session, c_user
    ):
        """Legacy data (or a manual API write that bypassed schema
        validation) can leave a winner with > 10 triggers. The merge
        must trim to the cap, not pile loser triggers on top."""
        from services.skill_curator_service import SkillCuratorService
        a = await _seed_skill(
            db_session, user_id=c_user.id, title="bloated",
            trigger_examples=[f"old_{i}" for i in range(12)],  # over cap
            successes=5,
        )
        b = await _seed_skill(
            db_session, user_id=c_user.id, title="other",
            trigger_examples=[f"new_{i}" for i in range(3)],
            successes=1,
        )
        svc = SkillCuratorService(db_session)
        await svc.merge_pair(loser_id=b.id, winner_id=a.id)
        winner = (await db_session.execute(
            select(ProceduralSkill).where(ProceduralSkill.id == a.id)
        )).scalar_one()
        # MUST cap at 10 even though the winner came in with 12 — the
        # pre-fix code would have left 13 because the append-then-check
        # logic only stopped AFTER appending the first loser trigger.
        assert len(winner.trigger_examples) == 10


# =============================================================== stale
@pytest.mark.asyncio
class TestArchiveStale:
    async def test_archives_stale_low_rate(
        self, db_session, c_user, monkeypatch
    ):
        from services.skill_curator_service import SkillCuratorService
        monkeypatch.setattr(
            "services.skill_curator_service.settings.skill_curator_stale_days", 30,
        )
        monkeypatch.setattr(
            "services.skill_curator_service.settings.skill_curator_min_uses_to_consider_stale", 3,
        )
        monkeypatch.setattr(
            "services.skill_curator_service.settings.skill_curator_stale_success_rate", 0.5,
        )

        old_low = await _seed_skill(
            db_session, user_id=c_user.id, title="stale-low",
            successes=1, failures=10,
            last_used_at=datetime.now(UTC).replace(tzinfo=None) - timedelta(days=60),
        )
        svc = SkillCuratorService(db_session)
        archived = await svc.archive_stale(user_id=c_user.id)
        assert archived == 1

        row = (await db_session.execute(
            select(ProceduralSkill).where(ProceduralSkill.id == old_low.id)
        )).scalar_one()
        assert row.status == "archived"
        assert row.merged_into_id is None  # not a merge, just archive

    async def test_skips_pinned(self, db_session, c_user, monkeypatch):
        from services.skill_curator_service import SkillCuratorService
        monkeypatch.setattr(
            "services.skill_curator_service.settings.skill_curator_stale_days", 30,
        )
        monkeypatch.setattr(
            "services.skill_curator_service.settings.skill_curator_min_uses_to_consider_stale", 3,
        )
        monkeypatch.setattr(
            "services.skill_curator_service.settings.skill_curator_stale_success_rate", 0.5,
        )
        await _seed_skill(
            db_session, user_id=c_user.id, title="pinned-stale",
            successes=1, failures=10, pinned=True,
            last_used_at=datetime.now(UTC).replace(tzinfo=None) - timedelta(days=60),
        )
        svc = SkillCuratorService(db_session)
        archived = await svc.archive_stale(user_id=c_user.id)
        assert archived == 0

    async def test_skips_below_min_uses(
        self, db_session, c_user, monkeypatch
    ):
        from services.skill_curator_service import SkillCuratorService
        monkeypatch.setattr(
            "services.skill_curator_service.settings.skill_curator_stale_days", 30,
        )
        monkeypatch.setattr(
            "services.skill_curator_service.settings.skill_curator_min_uses_to_consider_stale", 5,
        )
        monkeypatch.setattr(
            "services.skill_curator_service.settings.skill_curator_stale_success_rate", 0.5,
        )
        # 2 calls < threshold of 5
        await _seed_skill(
            db_session, user_id=c_user.id, title="rarely-used",
            successes=0, failures=2,
            last_used_at=datetime.now(UTC).replace(tzinfo=None) - timedelta(days=60),
        )
        svc = SkillCuratorService(db_session)
        archived = await svc.archive_stale(user_id=c_user.id)
        assert archived == 0

    async def test_skips_high_rate(self, db_session, c_user, monkeypatch):
        from services.skill_curator_service import SkillCuratorService
        monkeypatch.setattr(
            "services.skill_curator_service.settings.skill_curator_stale_days", 30,
        )
        monkeypatch.setattr(
            "services.skill_curator_service.settings.skill_curator_min_uses_to_consider_stale", 3,
        )
        monkeypatch.setattr(
            "services.skill_curator_service.settings.skill_curator_stale_success_rate", 0.5,
        )
        # Stale by date but successful when used
        await _seed_skill(
            db_session, user_id=c_user.id, title="seasonal",
            successes=10, failures=0,
            last_used_at=datetime.now(UTC).replace(tzinfo=None) - timedelta(days=60),
        )
        svc = SkillCuratorService(db_session)
        archived = await svc.archive_stale(user_id=c_user.id)
        assert archived == 0  # rate above floor → kept


# ===================================================== list_active_users
@pytest.mark.asyncio
class TestListActiveUserIds:
    async def test_returns_distinct_owners(self, db_session, c_user):
        from services.skill_curator_service import SkillCuratorService
        other_role = await _make_role(db_session, "curator_test_role_other")
        other = User(
            username="curator_other", password_hash="x", role_id=other_role.id,
        )
        db_session.add(other)
        await db_session.commit()
        await db_session.refresh(other)

        await _seed_skill(db_session, user_id=c_user.id, title="A1")
        await _seed_skill(db_session, user_id=c_user.id, title="A2")
        await _seed_skill(db_session, user_id=other.id, title="B1")
        # Seeds (NULL user_id, source=seed) are excluded
        await _seed_skill(
            db_session, user_id=None, title="Seed1", source=SKILL_SOURCE_SEED,
        )

        svc = SkillCuratorService(db_session)
        ids = set(await svc.list_active_user_ids())
        assert ids == {c_user.id, other.id}


# ============================================================== full run
@pytest.mark.asyncio
class TestRunForUser:
    async def test_reactivating_merged_skill_clears_pointer(
        self, db_session, c_user, patched_embed
    ):
        """Curator marked A merged into B. Owner then explicitly
        reactivates A via PATCH status='approved'. merged_into_id MUST
        be cleared, otherwise the next curator pass re-pairs A
        against B and either double-merges or creates an audit-loop."""
        from services.skill_curator_service import SkillCuratorService
        a = await _seed_skill(
            db_session, user_id=c_user.id, title="A",
            trigger_examples=["x"], successes=5,
        )
        b = await _seed_skill(
            db_session, user_id=c_user.id, title="B",
            trigger_examples=["y"], successes=10,
        )
        svc = SkillCuratorService(db_session)
        await svc.merge_pair(loser_id=a.id, winner_id=b.id)

        # Sanity — A is archived with pointer set
        a_after_merge = (await db_session.execute(
            select(ProceduralSkill).where(ProceduralSkill.id == a.id)
        )).scalar_one()
        assert a_after_merge.status == "archived"
        assert a_after_merge.merged_into_id == b.id

        # Simulate the PATCH route's logic directly (the route handler
        # tests this with httpx; here we cover the model state).
        a_after_merge.status = "approved"
        if a_after_merge.merged_into_id is not None:
            a_after_merge.merged_into_id = None
        await db_session.commit()

        # Re-fetch and assert clean state
        final = (await db_session.execute(
            select(ProceduralSkill).where(ProceduralSkill.id == a.id)
        )).scalar_one()
        assert final.status == "approved"
        assert final.merged_into_id is None

    async def test_archive_only_no_pgvector(
        self, db_session, c_user, monkeypatch
    ):
        """On sqlite, find_duplicate_pairs returns []; archive_stale
        still runs. This exercises the report-shape path."""
        from services.skill_curator_service import SkillCuratorService
        monkeypatch.setattr(
            "services.skill_curator_service.settings.skill_curator_stale_days", 30,
        )
        monkeypatch.setattr(
            "services.skill_curator_service.settings.skill_curator_min_uses_to_consider_stale", 3,
        )
        monkeypatch.setattr(
            "services.skill_curator_service.settings.skill_curator_stale_success_rate", 0.5,
        )
        await _seed_skill(
            db_session, user_id=c_user.id, title="stale",
            successes=0, failures=10,
            last_used_at=datetime.now(UTC).replace(tzinfo=None) - timedelta(days=60),
        )
        svc = SkillCuratorService(db_session)
        report = await svc.run_for_user(c_user.id)
        assert report.user_id == c_user.id
        assert report.duplicates_found == 0  # sqlite: no pgvector
        assert report.stale_archived == 1
        assert report.notes == []
