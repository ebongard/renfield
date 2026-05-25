"""Tests for TrajectoryService.

DB-backed via the sqlite in-memory fixture. Covers:
  - outcome inference from AgentStep traces
  - capture gate (feature flag + outcomes filter)
  - flag-for-retention behavior when a skill was extracted
  - export_jsonl streaming + flagged_only filter
  - purge_expired retention semantics
  - per-user soft cap drops oldest non-flagged
"""

from dataclasses import dataclass
from datetime import datetime, timedelta, UTC

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.database import (
    AgentTrajectory,
    Role,
    TRAJECTORY_OUTCOME_ABORT,
    TRAJECTORY_OUTCOME_SUCCESS,
    TRAJECTORY_OUTCOME_TOOL_FAIL,
    User,
)


@dataclass
class _FakeStep:
    step_type: str
    content: str = ""
    tool: str | None = None
    parameters: dict | None = None
    reason: str | None = None
    success: bool | None = None
    step_number: int = 0


@pytest.fixture
async def t_user(db_session: AsyncSession) -> User:
    role = Role(name="trajectory_test_role")
    db_session.add(role)
    await db_session.commit()
    await db_session.refresh(role)

    user = User(
        username="traj_tester", password_hash="x", role_id=role.id,
    )
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user


def _success_trace() -> list[_FakeStep]:
    return [
        _FakeStep("tool_call", tool="mcp.a"),
        _FakeStep("tool_result", success=True, content="ok"),
        _FakeStep("tool_call", tool="mcp.b"),
        _FakeStep("tool_result", success=True, content="ok"),
        _FakeStep("final_answer", content="done"),
    ]


def _tool_fail_trace() -> list[_FakeStep]:
    return [
        _FakeStep("tool_call", tool="mcp.a"),
        _FakeStep("tool_result", success=False, content="bad"),
        _FakeStep("tool_call", tool="mcp.b"),
        _FakeStep("tool_result", success=True, content="ok"),
        _FakeStep("final_answer", content="recovered"),
    ]


def _abort_trace() -> list[_FakeStep]:
    return [
        _FakeStep("tool_call", tool="mcp.a"),
        _FakeStep("tool_result", success=True, content="ok"),
        # no final_answer
    ]


# =========================================================== outcome
class TestOutcomeFromSteps:
    def test_success(self):
        from services.trajectory_service import outcome_from_steps
        assert outcome_from_steps(_success_trace()) == TRAJECTORY_OUTCOME_SUCCESS

    def test_tool_fail_but_recovered(self):
        from services.trajectory_service import outcome_from_steps
        assert outcome_from_steps(_tool_fail_trace()) == TRAJECTORY_OUTCOME_TOOL_FAIL

    def test_abort_no_final_answer(self):
        from services.trajectory_service import outcome_from_steps
        assert outcome_from_steps(_abort_trace()) == TRAJECTORY_OUTCOME_ABORT

    def test_explicit_error_step_is_tool_fail(self):
        from services.trajectory_service import outcome_from_steps
        steps = [
            _FakeStep("error", content="x"),
            _FakeStep("final_answer", content="meh"),
        ]
        assert outcome_from_steps(steps) == TRAJECTORY_OUTCOME_TOOL_FAIL


# ============================================================== save
@pytest.mark.asyncio
class TestSave:
    async def test_disabled_feature_returns_none(
        self, db_session, t_user, monkeypatch
    ):
        from services.trajectory_service import TrajectoryService
        monkeypatch.setattr(
            "services.trajectory_service.settings.trajectory_capture_enabled", False
        )
        svc = TrajectoryService(db_session)
        result = await svc.save(
            user_id=t_user.id, conversation_id=None,
            user_message="hi", steps=_success_trace(), lang="de",
        )
        assert result is None

    async def test_persists_success_trace(
        self, db_session, t_user, monkeypatch
    ):
        from services.trajectory_service import TrajectoryService
        monkeypatch.setattr(
            "services.trajectory_service.settings.trajectory_capture_enabled", True
        )
        monkeypatch.setattr(
            "services.trajectory_service.settings.trajectory_capture_outcomes",
            "success,tool_fail",
        )
        svc = TrajectoryService(db_session)
        row = await svc.save(
            user_id=t_user.id, conversation_id=None,
            user_message="play album", steps=_success_trace(), lang="de",
            tools_available=["mcp.a", "mcp.b", "mcp.c"],
            used_skill_ids=[42],
        )
        assert row is not None
        assert row.outcome == TRAJECTORY_OUTCOME_SUCCESS
        assert row.tool_count == 2
        assert row.distinct_tool_count == 2
        assert row.used_skill_ids == [42]
        assert row.flagged_for_retention is False  # no extracted skill
        # Payload sanity
        assert row.raw_payload["user_message"] == "play album"
        assert row.raw_payload["final_answer"] == "done"
        assert len(row.raw_payload["steps"]) == 5

    async def test_outcome_not_in_capture_set_skipped(
        self, db_session, t_user, monkeypatch
    ):
        """abort outcomes aren't captured by default."""
        from services.trajectory_service import TrajectoryService
        monkeypatch.setattr(
            "services.trajectory_service.settings.trajectory_capture_enabled", True
        )
        monkeypatch.setattr(
            "services.trajectory_service.settings.trajectory_capture_outcomes",
            "success,tool_fail",  # no 'abort'
        )
        svc = TrajectoryService(db_session)
        row = await svc.save(
            user_id=t_user.id, conversation_id=None,
            user_message="x", steps=_abort_trace(), lang="de",
        )
        assert row is None

    async def test_extracted_skill_id_None_does_not_flag(
        self, db_session, t_user, monkeypatch
    ):
        """The 'no skill extracted' control case — flag must stay False."""
        from services.trajectory_service import TrajectoryService
        monkeypatch.setattr(
            "services.trajectory_service.settings.trajectory_capture_enabled", True
        )
        monkeypatch.setattr(
            "services.trajectory_service.settings.trajectory_capture_outcomes",
            "success",
        )
        svc = TrajectoryService(db_session)
        row = await svc.save(
            user_id=t_user.id, conversation_id=None,
            user_message="x", steps=_success_trace(), lang="de",
            extracted_skill_id=None,
        )
        assert row.flagged_for_retention is False

    async def test_extracted_skill_id_set_flags_for_retention(
        self, db_session, t_user, monkeypatch
    ):
        """The Phase-2 gold-example invariant: turns that produced a new
        skill auto-flag for indefinite retention. Without this assertion,
        a regression flipping the `is not None` condition would still
        pass the no-op test above.

        We use a small but real ProceduralSkill row to satisfy the FK
        (extracted_skill_id is a real foreign key to procedural_skills.id);
        the value just needs to point at an existing row.
        """
        from models.database import (
            ProceduralSkill, SKILL_SOURCE_AUTO_EXTRACTED,
        )
        from services.trajectory_service import TrajectoryService
        monkeypatch.setattr(
            "services.trajectory_service.settings.trajectory_capture_enabled", True
        )
        monkeypatch.setattr(
            "services.trajectory_service.settings.trajectory_capture_outcomes",
            "success",
        )

        # Seed a skill so the FK satisfies
        skill = ProceduralSkill(
            user_id=t_user.id, title="gold", body_md="x",
            trigger_examples=["t"], tool_sequence=[],
            source=SKILL_SOURCE_AUTO_EXTRACTED,
            circle_tier=0,
        )
        db_session.add(skill)
        await db_session.commit()
        await db_session.refresh(skill)

        svc = TrajectoryService(db_session)
        row = await svc.save(
            user_id=t_user.id, conversation_id=None,
            user_message="x", steps=_success_trace(), lang="de",
            extracted_skill_id=skill.id,
        )
        assert row.flagged_for_retention is True
        assert row.extracted_skill_id == skill.id


# =========================================================== export
@pytest.mark.asyncio
class TestExportJsonl:
    async def _seed(self, db, t_user, monkeypatch, count=3):
        from services.trajectory_service import TrajectoryService
        monkeypatch.setattr(
            "services.trajectory_service.settings.trajectory_capture_enabled", True
        )
        monkeypatch.setattr(
            "services.trajectory_service.settings.trajectory_capture_outcomes",
            "success",
        )
        svc = TrajectoryService(db)
        rows = []
        for i in range(count):
            r = await svc.save(
                user_id=t_user.id, conversation_id=None,
                user_message=f"msg {i}", steps=_success_trace(), lang="de",
            )
            rows.append(r)
        return svc, rows

    async def test_streams_all_rows(self, db_session, t_user, monkeypatch):
        svc, rows = await self._seed(db_session, t_user, monkeypatch, count=3)
        collected = []
        async for obj in svc.export_jsonl():
            collected.append(obj)
        assert len(collected) == 3
        assert all("trace" in c for c in collected)
        assert {c["id"] for c in collected} == {r.id for r in rows}

    async def test_flagged_only_filter(self, db_session, t_user, monkeypatch):
        svc, rows = await self._seed(db_session, t_user, monkeypatch, count=3)
        rows[1].flagged_for_retention = True
        await db_session.commit()
        collected = [obj async for obj in svc.export_jsonl(flagged_only=True)]
        assert len(collected) == 1
        assert collected[0]["id"] == rows[1].id

    async def test_require_redacted_skips_unredacted(
        self, db_session, t_user, monkeypatch
    ):
        """Phase-4 gate: rows whose redacted_payload is NULL are skipped
        when the consumer demands redaction."""
        svc, _ = await self._seed(db_session, t_user, monkeypatch, count=2)
        collected = [obj async for obj in svc.export_jsonl(require_redacted=True)]
        assert collected == []


# ============================================================ purge
@pytest.mark.asyncio
class TestPurgeExpired:
    async def test_deletes_old_unflagged(
        self, db_session, t_user, monkeypatch
    ):
        from services.trajectory_service import TrajectoryService
        monkeypatch.setattr(
            "services.trajectory_service.settings.trajectory_capture_enabled", True
        )
        monkeypatch.setattr(
            "services.trajectory_service.settings.trajectory_capture_outcomes",
            "success",
        )
        monkeypatch.setattr(
            "services.trajectory_service.settings.trajectory_retention_days", 7
        )
        svc = TrajectoryService(db_session)
        row = await svc.save(
            user_id=t_user.id, conversation_id=None,
            user_message="old", steps=_success_trace(), lang="de",
        )
        # Backdate the row past the retention window.
        row.created_at = datetime.now(UTC).replace(tzinfo=None) - timedelta(days=10)
        await db_session.commit()

        deleted = await svc.purge_expired()
        assert deleted == 1

        remaining = (await db_session.execute(
            select(AgentTrajectory)
        )).scalars().all()
        assert remaining == []

    async def test_keeps_flagged_rows(
        self, db_session, t_user, monkeypatch
    ):
        from services.trajectory_service import TrajectoryService
        monkeypatch.setattr(
            "services.trajectory_service.settings.trajectory_capture_enabled", True
        )
        monkeypatch.setattr(
            "services.trajectory_service.settings.trajectory_capture_outcomes",
            "success",
        )
        monkeypatch.setattr(
            "services.trajectory_service.settings.trajectory_retention_days", 7
        )
        svc = TrajectoryService(db_session)
        row = await svc.save(
            user_id=t_user.id, conversation_id=None,
            user_message="gold", steps=_success_trace(), lang="de",
        )
        row.flagged_for_retention = True
        row.created_at = datetime.now(UTC).replace(tzinfo=None) - timedelta(days=10)
        await db_session.commit()

        deleted = await svc.purge_expired()
        assert deleted == 0
        # Row still there.
        survivor = (await db_session.execute(
            select(AgentTrajectory).where(AgentTrajectory.id == row.id)
        )).scalar_one_or_none()
        assert survivor is not None


# ========================================================== soft cap
@pytest.mark.asyncio
class TestPerUserCap:
    async def test_drops_oldest_unflagged_over_cap(
        self, db_session, t_user, monkeypatch
    ):
        from services.trajectory_service import TrajectoryService
        monkeypatch.setattr(
            "services.trajectory_service.settings.trajectory_capture_enabled", True
        )
        monkeypatch.setattr(
            "services.trajectory_service.settings.trajectory_capture_outcomes",
            "success",
        )
        monkeypatch.setattr(
            "services.trajectory_service.settings.trajectory_max_per_user", 2
        )
        # Cap check is probabilistic (every Nth save) for hot-path cost
        # reasons — force every-save in tests so the cap actually fires.
        monkeypatch.setattr(
            "services.trajectory_service.settings.trajectory_cap_check_every", 1
        )

        svc = TrajectoryService(db_session)
        r1 = await svc.save(
            user_id=t_user.id, conversation_id=None,
            user_message="1", steps=_success_trace(), lang="de",
        )
        r2 = await svc.save(
            user_id=t_user.id, conversation_id=None,
            user_message="2", steps=_success_trace(), lang="de",
        )
        # Third insert pushes count past cap=2 → r1 (oldest) gets dropped.
        r3 = await svc.save(
            user_id=t_user.id, conversation_id=None,
            user_message="3", steps=_success_trace(), lang="de",
        )

        ids = {row.id for row in (await db_session.execute(
            select(AgentTrajectory)
        )).scalars().all()}
        assert r1.id not in ids
        assert {r2.id, r3.id} == ids

    async def test_anonymous_user_cap_enforced(
        self, db_session, monkeypatch
    ):
        """Single-user / AUTH_ENABLED=false produces user_id=None turns.
        Without the cap, anonymous traffic would grow agent_trajectories
        unbounded between the daily cleanup ticks. Verifies the cap
        applies symmetrically to the NULL-user pool."""
        from services.trajectory_service import TrajectoryService
        monkeypatch.setattr(
            "services.trajectory_service.settings.trajectory_capture_enabled", True
        )
        monkeypatch.setattr(
            "services.trajectory_service.settings.trajectory_capture_outcomes",
            "success",
        )
        monkeypatch.setattr(
            "services.trajectory_service.settings.trajectory_max_per_user", 2
        )
        monkeypatch.setattr(
            "services.trajectory_service.settings.trajectory_cap_check_every", 1
        )

        svc = TrajectoryService(db_session)
        r1 = await svc.save(
            user_id=None, conversation_id=None,
            user_message="anon1", steps=_success_trace(), lang="de",
        )
        r2 = await svc.save(
            user_id=None, conversation_id=None,
            user_message="anon2", steps=_success_trace(), lang="de",
        )
        r3 = await svc.save(
            user_id=None, conversation_id=None,
            user_message="anon3", steps=_success_trace(), lang="de",
        )

        ids = {row.id for row in (await db_session.execute(
            select(AgentTrajectory).where(AgentTrajectory.user_id.is_(None))
        )).scalars().all()}
        assert r1.id not in ids  # oldest dropped
        assert {r2.id, r3.id} == ids
