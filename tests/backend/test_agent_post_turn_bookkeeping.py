"""Integration tests for _post_turn_skill_bookkeeping.

This is the central join-point for all 4 self-learning phases. It runs
fire-and-forget after every agent turn and gates four independent
sub-features on four independent feature flags:

  1. SKILLS_ENABLED              → record outcome on injected skills
  2. TOOL_HEALTH_TRACKING_ENABLED → record per-tool outcomes
  3. SKILLS_ENABLED + SKILL_EXTRACT_ENABLED + turn_success + user_id
                                  → auto-extract a new skill from the turn
  4. TRAJECTORY_CAPTURE_ENABLED   → persist the full trace

Each sub-feature's gate is independent (per the eng-review). The tests
below assert that flipping one flag does NOT spuriously fire any other
branch. Errors in one branch must not abort the others.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from models.database import (
    AgentTrajectory,
    EMBEDDING_DIMENSION,
    ProceduralSkill,
    Role,
    SKILL_SOURCE_AUTO_EXTRACTED,
    ToolOutcomeStat,
    User,
)
from services.agent_service import AgentStep, _post_turn_skill_bookkeeping
from services.skill_extractor import SkillDraft
from services.skill_service import SkillService


# ----------------------------------------------------------------- fixtures
@pytest.fixture(autouse=True)
def _clear_skill_cache():
    SkillService.invalidate_has_skills_cache()
    yield
    SkillService.invalidate_has_skills_cache()


@pytest.fixture(autouse=True)
def _patch_async_session_local(async_engine, monkeypatch):
    """The post-turn task opens its OWN AsyncSessionLocal sessions for
    every sub-feature (skill outcome, tool outcome, trajectory save,
    conversation-id lookup). The get_db dependency override only reaches
    route handlers — point AsyncSessionLocal at the test engine so
    background writes land in the same in-memory DB that db_session
    reads from."""
    test_sessionmaker = async_sessionmaker(
        async_engine, class_=AsyncSession, expire_on_commit=False
    )
    monkeypatch.setattr(
        "services.database.AsyncSessionLocal", test_sessionmaker, raising=False,
    )


@pytest.fixture
def patched_embed():
    with patch(
        "services.skill_service.SkillService._embed",
        return_value=[0.1] * EMBEDDING_DIMENSION,
    ) as p:
        yield p


@pytest.fixture
async def user_in_db(db_session: AsyncSession) -> User:
    role = Role(name="ptbk_role")
    db_session.add(role)
    await db_session.commit()
    await db_session.refresh(role)
    user = User(
        username="ptbk_user", email="ptbk@example.test",
        password_hash="x", role_id=role.id, is_active=True,
    )
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user


def _success_steps() -> list[AgentStep]:
    """A 2-tool success trace: search → write → answer."""
    return [
        AgentStep(step_number=1, step_type="tool_call",
                  tool="mcp.search.find", parameters={"q": "x"}),
        AgentStep(step_number=2, step_type="tool_result",
                  tool="mcp.search.find", content="found", success=True),
        AgentStep(step_number=3, step_type="tool_call",
                  tool="mcp.media.play", parameters={"id": 7}),
        AgentStep(step_number=4, step_type="tool_result",
                  tool="mcp.media.play", content="ok", success=True),
        AgentStep(step_number=5, step_type="final_answer",
                  content="done"),
    ]


def _failure_steps() -> list[AgentStep]:
    """One tool that failed, then a final_answer (recovered) — still
    counts as a turn-level success per the heuristic but the per-tool
    counter for the failed tool should bump."""
    return [
        AgentStep(step_number=1, step_type="tool_call",
                  tool="mcp.bad.tool", parameters={}),
        AgentStep(step_number=2, step_type="tool_result",
                  tool="mcp.bad.tool", content="ECONNREFUSED",
                  success=False),
        AgentStep(step_number=3, step_type="final_answer",
                  content="recovered"),
    ]


def _aborted_steps() -> list[AgentStep]:
    """A turn that errored without producing a final_answer."""
    return [
        AgentStep(step_number=1, step_type="tool_call",
                  tool="mcp.bad.tool", parameters={}),
        AgentStep(step_number=2, step_type="error",
                  content="step timed out"),
    ]


# ============================================================ GATING
@pytest.mark.asyncio
class TestIndependentGates:
    async def test_all_flags_off_is_noop(
        self, user_in_db: User, monkeypatch,
        db_session: AsyncSession, patched_embed,
    ):
        monkeypatch.setattr(
            "services.agent_service.settings.skills_enabled", False,
        )
        monkeypatch.setattr(
            "services.agent_service.settings.tool_health_tracking_enabled",
            False,
        )
        monkeypatch.setattr(
            "services.agent_service.settings.trajectory_capture_enabled",
            False,
        )
        await _post_turn_skill_bookkeeping(
            steps=_success_steps(),
            injected_skill_ids=[],
            user_id=user_in_db.id,
            user_message="play something",
            lang="de",
        )
        # No rows in any of the three tables.
        skills = (await db_session.execute(select(ProceduralSkill))).scalars().all()
        tools = (await db_session.execute(select(ToolOutcomeStat))).scalars().all()
        traj = (await db_session.execute(select(AgentTrajectory))).scalars().all()
        assert skills == []
        assert tools == []
        assert traj == []

    async def test_tool_health_isolated_from_skills(
        self, user_in_db: User, monkeypatch,
        db_session: AsyncSession,
    ):
        """tool_health_tracking_enabled=true with skills_enabled=false:
        only the per-tool counter table fills. No skill writes, no
        trajectory."""
        monkeypatch.setattr(
            "services.agent_service.settings.skills_enabled", False,
        )
        monkeypatch.setattr(
            "services.agent_service.settings.tool_health_tracking_enabled",
            True,
        )
        monkeypatch.setattr(
            "services.agent_service.settings.trajectory_capture_enabled",
            False,
        )
        monkeypatch.setattr(
            "services.tool_outcome_service.settings.tool_health_tracking_enabled",
            True,
        )

        await _post_turn_skill_bookkeeping(
            steps=_success_steps(),
            injected_skill_ids=[],
            user_id=user_in_db.id,
            user_message="play",
            lang="de",
        )

        tools = (await db_session.execute(select(ToolOutcomeStat))).scalars().all()
        assert len(tools) == 2
        names = {t.tool_name for t in tools}
        assert names == {"mcp.search.find", "mcp.media.play"}
        for t in tools:
            assert t.success_count == 1
            assert t.failure_count == 0

        skills = (await db_session.execute(select(ProceduralSkill))).scalars().all()
        traj = (await db_session.execute(select(AgentTrajectory))).scalars().all()
        assert skills == []
        assert traj == []

    async def test_trajectory_isolated_from_skills(
        self, user_in_db: User, monkeypatch,
        db_session: AsyncSession,
    ):
        monkeypatch.setattr(
            "services.agent_service.settings.skills_enabled", False,
        )
        monkeypatch.setattr(
            "services.agent_service.settings.tool_health_tracking_enabled",
            False,
        )
        monkeypatch.setattr(
            "services.agent_service.settings.trajectory_capture_enabled",
            True,
        )
        monkeypatch.setattr(
            "services.trajectory_service.settings.trajectory_capture_enabled",
            True,
        )
        monkeypatch.setattr(
            "services.trajectory_service.settings.trajectory_capture_outcomes",
            "success,tool_fail,abort",
        )
        monkeypatch.setattr(
            "services.trajectory_service.settings.trajectory_max_per_user", 0,
        )

        await _post_turn_skill_bookkeeping(
            steps=_success_steps(),
            injected_skill_ids=[],
            user_id=user_in_db.id,
            user_message="play",
            lang="de",
        )

        traj = (await db_session.execute(select(AgentTrajectory))).scalars().all()
        assert len(traj) == 1
        assert traj[0].outcome == "success"
        assert traj[0].tool_count == 2
        assert traj[0].user_id == user_in_db.id


# ============================================================ AUTO-EXTRACT
@pytest.mark.asyncio
class TestAutoExtract:
    async def test_anon_user_skips_auto_extract(
        self, monkeypatch, db_session: AsyncSession, patched_embed,
    ):
        """user_id=None must skip auto-extract (no owner to attribute
        the skill to). Per-tool counter also skips because that path
        requires user_id."""
        monkeypatch.setattr(
            "services.agent_service.settings.skills_enabled", True,
        )
        monkeypatch.setattr(
            "services.agent_service.settings.skill_extract_enabled", True,
        )
        monkeypatch.setattr(
            "services.agent_service.settings.tool_health_tracking_enabled",
            False,
        )
        monkeypatch.setattr(
            "services.agent_service.settings.trajectory_capture_enabled",
            False,
        )

        # If we DID call the extractor, this would observe it.
        with patch(
            "services.skill_extractor.SkillExtractor.extract",
            new_callable=AsyncMock,
        ) as mock_extract:
            await _post_turn_skill_bookkeeping(
                steps=_success_steps(),
                injected_skill_ids=[],
                user_id=None,
                user_message="play",
                lang="de",
            )
            mock_extract.assert_not_called()

        skills = (await db_session.execute(select(ProceduralSkill))).scalars().all()
        assert skills == []

    async def test_aborted_turn_skips_auto_extract(
        self, user_in_db: User, monkeypatch, db_session: AsyncSession,
        patched_embed,
    ):
        """has_final=False means turn_success=False → don't train the
        corpus on aborted turns."""
        monkeypatch.setattr(
            "services.agent_service.settings.skills_enabled", True,
        )
        monkeypatch.setattr(
            "services.agent_service.settings.skill_extract_enabled", True,
        )

        with patch(
            "services.skill_extractor.SkillExtractor.extract",
            new_callable=AsyncMock,
        ) as mock_extract:
            await _post_turn_skill_bookkeeping(
                steps=_aborted_steps(),
                injected_skill_ids=[],
                user_id=user_in_db.id,
                user_message="play",
                lang="de",
            )
            mock_extract.assert_not_called()

    async def test_successful_turn_calls_extractor(
        self, user_in_db: User, monkeypatch, db_session: AsyncSession,
        patched_embed,
    ):
        monkeypatch.setattr(
            "services.agent_service.settings.skills_enabled", True,
        )
        monkeypatch.setattr(
            "services.agent_service.settings.skill_extract_enabled", True,
        )
        monkeypatch.setattr(
            "services.agent_service.settings.tool_health_tracking_enabled",
            False,
        )
        monkeypatch.setattr(
            "services.agent_service.settings.trajectory_capture_enabled",
            False,
        )

        draft = SkillDraft(
            title="Play album on DLNA",
            body_md="- step 1: search\n- step 2: play",
            trigger_examples=["spiel das Album X"],
            tool_sequence=["mcp.search.find", "mcp.media.play"],
        )
        with patch(
            "services.skill_extractor.SkillExtractor.extract",
            new_callable=AsyncMock,
            return_value=draft,
        ):
            await _post_turn_skill_bookkeeping(
                steps=_success_steps(),
                injected_skill_ids=[],
                user_id=user_in_db.id,
                user_message="play album",
                lang="de",
            )

        skills = (await db_session.execute(select(ProceduralSkill))).scalars().all()
        assert len(skills) == 1
        new_skill = skills[0]
        assert new_skill.title == "Play album on DLNA"
        assert new_skill.user_id == user_in_db.id
        assert new_skill.source == SKILL_SOURCE_AUTO_EXTRACTED
        # Owner-approval gate: auto-extracted skills land inactive.
        assert new_skill.is_active is False


# ============================================================ ERROR ISOLATION
@pytest.mark.asyncio
class TestErrorIsolation:
    async def test_skill_outcome_error_does_not_abort_other_branches(
        self, user_in_db: User, monkeypatch, db_session: AsyncSession,
        patched_embed,
    ):
        """If record_outcome raises (e.g., DB hiccup), the trajectory
        capture branch must still run. The wrapper around each
        sub-feature is best-effort by design."""
        monkeypatch.setattr(
            "services.agent_service.settings.skills_enabled", True,
        )
        monkeypatch.setattr(
            "services.agent_service.settings.skill_extract_enabled", False,
        )
        monkeypatch.setattr(
            "services.agent_service.settings.tool_health_tracking_enabled",
            False,
        )
        monkeypatch.setattr(
            "services.agent_service.settings.trajectory_capture_enabled",
            True,
        )
        monkeypatch.setattr(
            "services.trajectory_service.settings.trajectory_capture_enabled",
            True,
        )
        monkeypatch.setattr(
            "services.trajectory_service.settings.trajectory_capture_outcomes",
            "success,tool_fail,abort",
        )
        monkeypatch.setattr(
            "services.trajectory_service.settings.trajectory_max_per_user", 0,
        )

        with patch(
            "services.skill_service.SkillService.record_outcome",
            new_callable=AsyncMock,
            side_effect=RuntimeError("boom"),
        ):
            # Fake an injected skill so the outcome-recording branch fires.
            await _post_turn_skill_bookkeeping(
                steps=_success_steps(),
                injected_skill_ids=[1],
                user_id=user_in_db.id,
                user_message="play",
                lang="de",
            )

        # Trajectory branch must still have produced a row.
        traj = (await db_session.execute(select(AgentTrajectory))).scalars().all()
        assert len(traj) == 1
