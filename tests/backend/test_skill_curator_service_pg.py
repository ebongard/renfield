"""Postgres-only tests for SkillCuratorService.find_duplicate_pairs.

The cosine self-join in find_duplicate_pairs short-circuits to [] on
any non-postgres dialect because pgvector isn't installed in the sqlite
test harness. That means EVERYTHING about the duplicate-detection path
— the SQL operator, the threshold gate, the fetch_cap, the seed
exclusion, the cross-user isolation — ships untested under the default
test run.

These tests run on real PostgreSQL only. They are gated by
``@pytest.mark.postgres`` (registered in pyproject.toml) AND by an
autouse skip if the test session's DB is sqlite. When the test runner
on .159 uses a postgres-backed conftest, this file becomes the gate.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from models.database import (
    EMBEDDING_DIMENSION,
    ProceduralSkill,
    Role,
    SKILL_SOURCE_AUTO_EXTRACTED,
    SKILL_SOURCE_SEED,
    TIER_PUBLIC,
    TIER_SELF,
    User,
)
from services.skill_curator_service import SkillCuratorService
from services.skill_service import SkillService


pytestmark = [pytest.mark.postgres, pytest.mark.asyncio]


@pytest.fixture(autouse=True)
def _require_postgres(db_session: AsyncSession):
    """Skip cleanly when the test session is on sqlite — pgvector + the
    halfvec cast that find_duplicate_pairs uses don't exist there."""
    dialect = db_session.bind.dialect.name if db_session.bind else ""
    if dialect != "postgresql":
        pytest.skip("postgres-only test; sqlite harness skipped")


# ---------------------------------------------------------------- helpers
def _unit_vec(seed: int, dim: int = EMBEDDING_DIMENSION) -> list[float]:
    """Build a deterministic unit-norm-ish vector. Two seeds produce
    near-identical vectors when they differ by a small delta; otherwise
    they're effectively orthogonal."""
    v = [0.0] * dim
    # Put most of the mass in one position so cosine distance is dominated
    # by which position the mass lands in.
    v[seed % dim] = 1.0
    return v


def _near_vec(base_seed: int, jitter: float = 0.01,
              dim: int = EMBEDDING_DIMENSION) -> list[float]:
    v = _unit_vec(base_seed, dim)
    # Move a tiny bit of mass to the next position so cos(a, b) < 1 but
    # still >> threshold.
    next_pos = (base_seed + 1) % dim
    v[next_pos] = jitter
    return v


async def _make_role(db_session: AsyncSession, name: str) -> Role:
    role = Role(name=name)
    db_session.add(role)
    await db_session.commit()
    await db_session.refresh(role)
    return role


async def _make_user(db_session: AsyncSession, name: str) -> User:
    role = await _make_role(db_session, f"{name}_role")
    user = User(
        username=name, email=f"{name}@ex.test",
        password_hash="x", role_id=role.id, is_active=True,
    )
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user


async def _seed_skill(
    db_session: AsyncSession,
    *,
    user_id: int | None,
    title: str,
    embedding: list[float],
    successes: int = 0,
    failures: int = 0,
    source: str = SKILL_SOURCE_AUTO_EXTRACTED,
    is_active: bool = True,
    tier: int = TIER_SELF,
) -> ProceduralSkill:
    s = ProceduralSkill(
        user_id=user_id, title=title, body_md="- body",
        trigger_examples=["t"], tool_sequence=["mcp.x.y"],
        source=source, embedding=embedding,
        success_count=successes, failure_count=failures,
        is_active=is_active, circle_tier=tier, atom_id=None,
    )
    db_session.add(s)
    await db_session.commit()
    await db_session.refresh(s)
    return s


# ======================================================== find_duplicate_pairs
class TestFindDuplicatePairs:
    async def test_returns_pair_above_threshold(self, db_session, monkeypatch):
        monkeypatch.setattr(
            "services.skill_curator_service.settings.skill_curator_duplicate_threshold",
            0.9,
        )
        user = await _make_user(db_session, "dp1")
        a = await _seed_skill(
            db_session, user_id=user.id, title="A",
            embedding=_unit_vec(5), successes=2, failures=8,
        )
        b = await _seed_skill(
            db_session, user_id=user.id, title="B",
            embedding=_near_vec(5, jitter=0.001),  # essentially identical
            successes=9, failures=1,
        )

        svc = SkillCuratorService(db_session)
        pairs = await svc.find_duplicate_pairs(user_id=user.id)

        assert len(pairs) == 1
        # winner = b (higher success rate); loser = a.
        assert pairs[0].winner_id == b.id
        assert pairs[0].loser_id == a.id
        assert pairs[0].similarity > 0.9

    async def test_excludes_below_threshold(self, db_session, monkeypatch):
        monkeypatch.setattr(
            "services.skill_curator_service.settings.skill_curator_duplicate_threshold",
            0.99,
        )
        user = await _make_user(db_session, "dp2")
        # Different seeds → orthogonal vectors → sim ~ 0.
        await _seed_skill(db_session, user_id=user.id, title="A",
                          embedding=_unit_vec(1))
        await _seed_skill(db_session, user_id=user.id, title="B",
                          embedding=_unit_vec(50))

        svc = SkillCuratorService(db_session)
        pairs = await svc.find_duplicate_pairs(user_id=user.id)

        assert pairs == []

    async def test_excludes_seeds(self, db_session, monkeypatch):
        """Seed skills are public-tier, system-owned. The duplicate
        scan must never pair them with anything — they live in the
        git repo, not the curator's mutation surface."""
        monkeypatch.setattr(
            "services.skill_curator_service.settings.skill_curator_duplicate_threshold",
            0.9,
        )
        user = await _make_user(db_session, "dp3")
        await _seed_skill(
            db_session, user_id=None, title="Seed",
            embedding=_unit_vec(7), source=SKILL_SOURCE_SEED,
            tier=TIER_PUBLIC,
        )
        await _seed_skill(
            db_session, user_id=user.id, title="UserCopy",
            embedding=_near_vec(7, jitter=0.001),
        )

        svc = SkillCuratorService(db_session)
        pairs = await svc.find_duplicate_pairs(user_id=user.id)

        assert pairs == []

    async def test_cross_user_isolation(self, db_session, monkeypatch):
        monkeypatch.setattr(
            "services.skill_curator_service.settings.skill_curator_duplicate_threshold",
            0.9,
        )
        userA = await _make_user(db_session, "dp4a")
        userB = await _make_user(db_session, "dp4b")
        await _seed_skill(
            db_session, user_id=userA.id, title="A",
            embedding=_unit_vec(11),
        )
        await _seed_skill(
            db_session, user_id=userB.id, title="B",
            embedding=_near_vec(11, jitter=0.001),
        )

        svc = SkillCuratorService(db_session)
        pairs_a = await svc.find_duplicate_pairs(user_id=userA.id)
        pairs_b = await svc.find_duplicate_pairs(user_id=userB.id)

        assert pairs_a == []
        assert pairs_b == []

    async def test_active_only(self, db_session, monkeypatch):
        monkeypatch.setattr(
            "services.skill_curator_service.settings.skill_curator_duplicate_threshold",
            0.9,
        )
        user = await _make_user(db_session, "dp5")
        await _seed_skill(
            db_session, user_id=user.id, title="A",
            embedding=_unit_vec(13), is_active=False,
        )
        await _seed_skill(
            db_session, user_id=user.id, title="B",
            embedding=_near_vec(13, jitter=0.001),
        )

        svc = SkillCuratorService(db_session)
        pairs = await svc.find_duplicate_pairs(user_id=user.id)

        assert pairs == []

    async def test_respects_fetch_cap(self, db_session, monkeypatch):
        """fetch_cap = max_merges_per_run * 2 caps the SQL LIMIT."""
        monkeypatch.setattr(
            "services.skill_curator_service.settings.skill_curator_duplicate_threshold",
            0.5,
        )
        monkeypatch.setattr(
            "services.skill_curator_service.settings.skill_curator_max_merges_per_run",
            3,  # → fetch_cap = 6
        )
        user = await _make_user(db_session, "dp6")
        # 10 skills, each pair similar enough to qualify → 45 candidate pairs
        # before LIMIT. After LIMIT we should see exactly 6.
        for i in range(10):
            await _seed_skill(
                db_session, user_id=user.id, title=f"S{i}",
                embedding=_near_vec(20, jitter=0.0001 * (i + 1)),
            )

        svc = SkillCuratorService(db_session)
        pairs = await svc.find_duplicate_pairs(user_id=user.id)

        assert len(pairs) <= 6, "fetch_cap should bound returned pairs"


# ====================================================== find_similar circle-reach
class TestCircleReachPg:
    """find_similar's third OR-arm (EXISTS over circle_memberships) is
    unreachable on sqlite. Test the tier-reach + orphan-source predicate
    here against real postgres."""

    @pytest.fixture
    def patched_embed(self):
        with patch(
            "services.skill_service.SkillService._embed",
            return_value=_unit_vec(99),
        ) as p:
            yield p

    async def test_owner_sees_own_self_tier(
        self, db_session, patched_embed,
    ):
        owner = await _make_user(db_session, "cr1")
        await _seed_skill(
            db_session, user_id=owner.id, title="Mine",
            embedding=_unit_vec(99), tier=TIER_SELF,
        )

        svc = SkillService(db_session)
        SkillService.invalidate_has_skills_cache()
        matches = await svc.find_similar(
            "anything", asker_id=owner.id,
            threshold=-1.0, top_k=10,
        )
        titles = [m["title"] for m in matches]
        assert "Mine" in titles

    async def test_non_peer_does_not_see_self_tier(
        self, db_session, patched_embed,
    ):
        owner = await _make_user(db_session, "cr2a")
        outsider = await _make_user(db_session, "cr2b")
        await _seed_skill(
            db_session, user_id=owner.id, title="Private",
            embedding=_unit_vec(99), tier=TIER_SELF,
        )

        svc = SkillService(db_session)
        SkillService.invalidate_has_skills_cache()
        matches = await svc.find_similar(
            "anything", asker_id=outsider.id,
            threshold=-1.0, top_k=10,
        )
        assert all(m["title"] != "Private" for m in matches)

    async def test_seed_visible_to_anyone(
        self, db_session, patched_embed,
    ):
        outsider = await _make_user(db_session, "cr3")
        await _seed_skill(
            db_session, user_id=None, title="Seed",
            embedding=_unit_vec(99), source=SKILL_SOURCE_SEED,
            tier=TIER_PUBLIC,
        )

        svc = SkillService(db_session)
        SkillService.invalidate_has_skills_cache()
        matches = await svc.find_similar(
            "anything", asker_id=outsider.id,
            threshold=-1.0, top_k=10,
        )
        titles = [m["title"] for m in matches]
        assert "Seed" in titles

    async def test_orphan_user_skill_with_non_seed_source_not_visible(
        self, db_session, patched_embed,
    ):
        """The retrieval predicate was tightened so a row with
        (user_id IS NULL, tier=4, source != 'seed') — the deleted-user
        orphan case — does NOT slip into find_similar as if it were a
        curated system seed."""
        outsider = await _make_user(db_session, "cr4")
        # Orphan: user_id NULL but source = auto_extracted
        await _seed_skill(
            db_session, user_id=None, title="OrphanLeak",
            embedding=_unit_vec(99),
            source=SKILL_SOURCE_AUTO_EXTRACTED,
            tier=TIER_PUBLIC,
        )

        svc = SkillService(db_session)
        SkillService.invalidate_has_skills_cache()
        matches = await svc.find_similar(
            "anything", asker_id=outsider.id,
            threshold=-1.0, top_k=10,
        )
        assert all(m["title"] != "OrphanLeak" for m in matches)
