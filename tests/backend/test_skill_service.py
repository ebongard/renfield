"""Tests for SkillService.

DB-backed tests using the in-memory sqlite fixture from conftest.py.
The pgvector embedding column degrades to Text on sqlite (see
PGVECTOR_AVAILABLE branch in models/database.py), so similarity-search
SQL tests are skipped here — see test_skill_service_pg.py in CI for
the postgres-side coverage. What we DO cover end-to-end:

  - load_seed: insert + idempotent re-insert
  - create_user_authored: writes ProceduralSkill + Atom registration
  - record_outcome: increments + auto-demote
  - format_for_prompt: rendering
  - has_any_skills + cache invalidation
"""

from unittest.mock import patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.database import (
    SKILL_SOURCE_AUTO_EXTRACTED,
    SKILL_SOURCE_SEED,
    SKILL_SOURCE_USER_CREATED,
    ProceduralSkill,
    User,
)


# -------------------------------------------------------------- fixtures
@pytest.fixture
def patched_embed():
    """Patch SkillService._embed to return a deterministic short vector
    so DB writes don't try to call Ollama."""
    with patch(
        "services.skill_service.SkillService._embed",
        return_value=[0.1] * 8,  # tiny vector; sqlite stores as Text
    ) as p:
        yield p


@pytest.fixture
async def test_user(db_session: AsyncSession) -> User:
    user = User(username="skill_tester", hashed_password="x")
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user


# ============================================================== seeds
@pytest.mark.asyncio
class TestLoadSeed:
    async def test_inserts_seed(self, db_session, patched_embed):
        from services.skill_service import SkillService
        svc = SkillService(db_session)
        result = await svc.load_seed(
            title="Seed test",
            body_md="- step",
            trigger_examples=["trigger"],
            tool_sequence=["mcp.a"],
        )
        assert result is not None
        assert result.source == SKILL_SOURCE_SEED
        assert result.user_id is None
        assert result.circle_tier == 4  # TIER_PUBLIC

    async def test_idempotent_by_title(self, db_session, patched_embed):
        from services.skill_service import SkillService
        svc = SkillService(db_session)
        first = await svc.load_seed(
            title="Idem", body_md="x", trigger_examples=["a"],
        )
        second = await svc.load_seed(
            title="Idem", body_md="x", trigger_examples=["a"],
        )
        assert first is not None
        assert second is None
        # Only one row in the DB
        rows = (await db_session.execute(
            select(ProceduralSkill).where(ProceduralSkill.title == "Idem")
        )).scalars().all()
        assert len(rows) == 1

    async def test_seed_bypasses_atom_registry(self, db_session, patched_embed):
        from services.skill_service import SkillService
        svc = SkillService(db_session)
        result = await svc.load_seed(
            title="No-atom", body_md="x", trigger_examples=["a"],
        )
        assert result is not None
        assert result.atom_id is None


# ====================================================== user creation
@pytest.mark.asyncio
class TestCreateUserAuthored:
    async def test_creates_skill_and_registers_atom(
        self, db_session, patched_embed, test_user
    ):
        from services.skill_service import SkillService
        svc = SkillService(db_session)
        skill = await svc.create_user_authored(
            user_id=test_user.id,
            title="Mine",
            body_md="- step",
            trigger_examples=["do mine"],
            tool_sequence=["mcp.x"],
            circle_tier=0,
        )
        assert skill.source == SKILL_SOURCE_USER_CREATED
        assert skill.user_id == test_user.id
        # Re-fetch to confirm atom_id was set after upsert_atom.
        refreshed = (await db_session.execute(
            select(ProceduralSkill).where(ProceduralSkill.id == skill.id)
        )).scalar_one()
        assert refreshed.atom_id is not None
        assert len(refreshed.atom_id) == 36  # UUID

    async def test_auto_extracted_discriminator(
        self, db_session, patched_embed, test_user
    ):
        from services.skill_service import SkillService
        svc = SkillService(db_session)
        skill = await svc.create_auto_extracted(
            user_id=test_user.id,
            title="Learned",
            body_md="- step",
            trigger_examples=["learn"],
            tool_sequence=["mcp.x"],
        )
        assert skill.source == SKILL_SOURCE_AUTO_EXTRACTED


# ============================================================ outcomes
@pytest.mark.asyncio
class TestRecordOutcome:
    async def test_success_increments_success_count(
        self, db_session, patched_embed, test_user
    ):
        from services.skill_service import SkillService
        svc = SkillService(db_session)
        skill = await svc.create_user_authored(
            user_id=test_user.id, title="x",
            body_md="x", trigger_examples=["x"],
        )
        await svc.record_outcome(skill.id, success=True)
        refreshed = (await db_session.execute(
            select(ProceduralSkill).where(ProceduralSkill.id == skill.id)
        )).scalar_one()
        assert refreshed.success_count == 1
        assert refreshed.failure_count == 0
        assert refreshed.last_used_at is not None

    async def test_failure_increments_failure_count(
        self, db_session, patched_embed, test_user
    ):
        from services.skill_service import SkillService
        svc = SkillService(db_session)
        skill = await svc.create_user_authored(
            user_id=test_user.id, title="x",
            body_md="x", trigger_examples=["x"],
        )
        await svc.record_outcome(skill.id, success=False)
        refreshed = (await db_session.execute(
            select(ProceduralSkill).where(ProceduralSkill.id == skill.id)
        )).scalar_one()
        assert refreshed.failure_count == 1

    async def test_auto_demote_below_threshold(
        self, db_session, patched_embed, test_user, monkeypatch
    ):
        from services.skill_service import SkillService
        # Lower thresholds for the test so we don't have to rack up 5 failures
        monkeypatch.setattr("services.skill_service.settings.skill_auto_demote_threshold", 2)
        monkeypatch.setattr("services.skill_service.settings.skill_auto_demote_success_rate", 0.5)

        svc = SkillService(db_session)
        skill = await svc.create_user_authored(
            user_id=test_user.id, title="bad",
            body_md="x", trigger_examples=["x"],
        )
        await svc.record_outcome(skill.id, success=False)
        await svc.record_outcome(skill.id, success=False)
        refreshed = (await db_session.execute(
            select(ProceduralSkill).where(ProceduralSkill.id == skill.id)
        )).scalar_one()
        # 0 successes / 2 failures → success rate 0 < 0.5, failure_count 2 >= 2 → demoted
        assert refreshed.is_active is False

    async def test_pinned_skill_not_demoted(
        self, db_session, patched_embed, test_user, monkeypatch
    ):
        from services.skill_service import SkillService
        monkeypatch.setattr("services.skill_service.settings.skill_auto_demote_threshold", 2)
        monkeypatch.setattr("services.skill_service.settings.skill_auto_demote_success_rate", 0.5)

        svc = SkillService(db_session)
        skill = await svc.create_user_authored(
            user_id=test_user.id, title="pinned",
            body_md="x", trigger_examples=["x"],
        )
        # Pin via raw update — there's no helper on the service for this
        # (the API route does it directly on the ORM object).
        skill.pinned = True
        await db_session.commit()

        await svc.record_outcome(skill.id, success=False)
        await svc.record_outcome(skill.id, success=False)
        refreshed = (await db_session.execute(
            select(ProceduralSkill).where(ProceduralSkill.id == skill.id)
        )).scalar_one()
        assert refreshed.is_active is True  # protected


# ============================================================= format
class TestFormatForPrompt:
    def test_empty_returns_empty(self):
        from services.skill_service import SkillService
        svc = SkillService.__new__(SkillService)  # no DB needed
        assert svc.format_for_prompt([]) == ""

    def test_single_skill_renders_title_and_body(self):
        from services.skill_service import SkillService
        svc = SkillService.__new__(SkillService)
        out = svc.format_for_prompt([{
            "title": "Test",
            "body_md": "- one\n- two",
            "trigger_examples": ["t1"],
            "tool_sequence": ["mcp.a"],
        }])
        assert "Test" in out
        assert "- one" in out
        assert "mcp.a" in out
        assert "GELERNTE PROZEDUREN" in out  # de header

    def test_english_header(self):
        from services.skill_service import SkillService
        svc = SkillService.__new__(SkillService)
        out = svc.format_for_prompt(
            [{"title": "T", "body_md": "x",
              "trigger_examples": ["a"], "tool_sequence": []}],
            lang="en",
        )
        assert "LEARNED PROCEDURES" in out


# ====================================================== has_any cache
@pytest.mark.asyncio
class TestHasAnySkills:
    async def test_false_on_empty(self, db_session):
        from services.skill_service import SkillService
        SkillService._has_skills_cache.clear()
        svc = SkillService(db_session)
        assert await svc.has_any_skills() is False

    async def test_true_after_insert(self, db_session, patched_embed):
        from services.skill_service import SkillService
        SkillService._has_skills_cache.clear()
        svc = SkillService(db_session)
        await svc.load_seed(
            title="x", body_md="x", trigger_examples=["x"],
        )
        # load_seed clears the cache, so this should hit the DB and see TRUE
        assert await svc.has_any_skills() is True

    async def test_cache_invalidated_on_create(
        self, db_session, patched_embed, test_user
    ):
        from services.skill_service import SkillService
        SkillService._has_skills_cache.clear()
        svc = SkillService(db_session)
        # First call: empty DB
        assert await svc.has_any_skills() is False
        # Create — must bust the cache
        await svc.create_user_authored(
            user_id=test_user.id, title="x", body_md="x",
            trigger_examples=["x"],
        )
        assert await svc.has_any_skills() is True
