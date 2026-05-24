"""Tests for ToolOutcomeService.

Covers:
  - record / record_from_steps: upsert paths, success vs failure
  - get_health_warnings: gated on feature flags + min-uses + rate
  - candidate_tools filter narrows the warning set
  - format_for_prompt: empty → "", non-empty → header + lines
"""

from dataclasses import dataclass

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.database import ToolOutcomeStat, User


@dataclass
class _FakeStep:
    step_type: str
    content: str = ""
    tool: str | None = None
    success: bool | None = None
    step_number: int = 0


@pytest.fixture
async def th_user(db_session: AsyncSession) -> User:
    user = User(username="th_tester", hashed_password="x")
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user


@pytest.fixture(autouse=True)
def _enable_tool_health(monkeypatch):
    monkeypatch.setattr(
        "services.tool_outcome_service.settings.tool_health_tracking_enabled", True
    )
    monkeypatch.setattr(
        "services.tool_outcome_service.settings.tool_health_warn_enabled", True
    )


# =========================================================== record
@pytest.mark.asyncio
class TestRecord:
    async def test_disabled_flag_no_ops(
        self, db_session, th_user, monkeypatch
    ):
        from services.tool_outcome_service import ToolOutcomeService
        monkeypatch.setattr(
            "services.tool_outcome_service.settings.tool_health_tracking_enabled",
            False,
        )
        svc = ToolOutcomeService(db_session)
        await svc.record(user_id=th_user.id, tool_name="mcp.x", success=True)
        rows = (await db_session.execute(select(ToolOutcomeStat))).scalars().all()
        assert rows == []

    async def test_insert_then_upsert_success(self, db_session, th_user):
        from services.tool_outcome_service import ToolOutcomeService
        svc = ToolOutcomeService(db_session)
        await svc.record(user_id=th_user.id, tool_name="mcp.x", success=True)
        await svc.record(user_id=th_user.id, tool_name="mcp.x", success=True)
        row = (await db_session.execute(
            select(ToolOutcomeStat).where(ToolOutcomeStat.tool_name == "mcp.x")
        )).scalar_one()
        assert row.success_count == 2
        assert row.failure_count == 0
        assert row.last_used_at is not None

    async def test_failure_records_summary(self, db_session, th_user):
        from services.tool_outcome_service import ToolOutcomeService
        svc = ToolOutcomeService(db_session)
        await svc.record(
            user_id=th_user.id, tool_name="mcp.broken",
            success=False, failure_summary="boom: 500 from upstream",
        )
        row = (await db_session.execute(
            select(ToolOutcomeStat).where(ToolOutcomeStat.tool_name == "mcp.broken")
        )).scalar_one()
        assert row.failure_count == 1
        assert "boom" in row.last_failure_summary
        assert row.last_failure_at is not None

    async def test_per_user_isolation(self, db_session, th_user):
        from services.tool_outcome_service import ToolOutcomeService
        # Add a second user
        other = User(username="other_th", hashed_password="x")
        db_session.add(other)
        await db_session.commit()
        await db_session.refresh(other)

        svc = ToolOutcomeService(db_session)
        await svc.record(user_id=th_user.id, tool_name="mcp.x", success=True)
        await svc.record(user_id=other.id, tool_name="mcp.x", success=False)

        my_row = (await db_session.execute(
            select(ToolOutcomeStat).where(
                ToolOutcomeStat.user_id == th_user.id,
                ToolOutcomeStat.tool_name == "mcp.x",
            )
        )).scalar_one()
        their_row = (await db_session.execute(
            select(ToolOutcomeStat).where(
                ToolOutcomeStat.user_id == other.id,
                ToolOutcomeStat.tool_name == "mcp.x",
            )
        )).scalar_one()
        assert my_row.success_count == 1
        assert my_row.failure_count == 0
        assert their_row.success_count == 0
        assert their_row.failure_count == 1

    async def test_empty_tool_name_skipped(self, db_session, th_user):
        from services.tool_outcome_service import ToolOutcomeService
        svc = ToolOutcomeService(db_session)
        await svc.record(user_id=th_user.id, tool_name="", success=True)
        await svc.record(user_id=th_user.id, tool_name=None, success=True)  # type: ignore[arg-type]
        rows = (await db_session.execute(select(ToolOutcomeStat))).scalars().all()
        assert rows == []


# ===================================================== from_steps
@pytest.mark.asyncio
class TestRecordFromSteps:
    async def test_pairs_calls_with_results(self, db_session, th_user):
        from services.tool_outcome_service import ToolOutcomeService
        steps = [
            _FakeStep("tool_call", tool="mcp.a"),
            _FakeStep("tool_result", success=True, content="ok"),
            _FakeStep("tool_call", tool="mcp.b"),
            _FakeStep("tool_result", success=False, content="bad"),
            _FakeStep("tool_call", tool="mcp.a"),  # second call to same tool
            _FakeStep("tool_result", success=True, content="ok"),
            _FakeStep("final_answer", content="done"),
        ]
        svc = ToolOutcomeService(db_session)
        await svc.record_from_steps(user_id=th_user.id, steps=steps)

        rows = {r.tool_name: r for r in (await db_session.execute(
            select(ToolOutcomeStat)
        )).scalars().all()}
        assert rows["mcp.a"].success_count == 2
        assert rows["mcp.a"].failure_count == 0
        assert rows["mcp.b"].success_count == 0
        assert rows["mcp.b"].failure_count == 1

    async def test_orphan_result_without_call_skipped(self, db_session, th_user):
        from services.tool_outcome_service import ToolOutcomeService
        steps = [
            _FakeStep("tool_result", success=True),  # no preceding call
            _FakeStep("final_answer", content="x"),
        ]
        svc = ToolOutcomeService(db_session)
        await svc.record_from_steps(user_id=th_user.id, steps=steps)
        rows = (await db_session.execute(select(ToolOutcomeStat))).scalars().all()
        assert rows == []


# ==================================================== warnings
@pytest.mark.asyncio
class TestGetHealthWarnings:
    async def _seed(self, db, th_user, monkeypatch, *, fails: int, total: int, tool="mcp.x"):
        from services.tool_outcome_service import ToolOutcomeService
        svc = ToolOutcomeService(db)
        for _ in range(fails):
            await svc.record(user_id=th_user.id, tool_name=tool, success=False, failure_summary="bad")
        for _ in range(total - fails):
            await svc.record(user_id=th_user.id, tool_name=tool, success=True)
        return svc

    async def test_below_min_uses_no_warning(
        self, db_session, th_user, monkeypatch
    ):
        monkeypatch.setattr(
            "services.tool_outcome_service.settings.tool_health_warn_min_uses", 10,
        )
        monkeypatch.setattr(
            "services.tool_outcome_service.settings.tool_health_warn_success_rate", 0.5,
        )
        svc = await self._seed(db_session, th_user, monkeypatch, fails=3, total=4)
        warnings = await svc.get_health_warnings(user_id=th_user.id)
        assert warnings == []  # only 4 total, threshold is 10

    async def test_above_rate_no_warning(
        self, db_session, th_user, monkeypatch
    ):
        monkeypatch.setattr(
            "services.tool_outcome_service.settings.tool_health_warn_min_uses", 2,
        )
        monkeypatch.setattr(
            "services.tool_outcome_service.settings.tool_health_warn_success_rate", 0.5,
        )
        svc = await self._seed(db_session, th_user, monkeypatch, fails=1, total=10)
        warnings = await svc.get_health_warnings(user_id=th_user.id)
        assert warnings == []  # 9/10 success = 0.9 > 0.5

    async def test_below_rate_warns(self, db_session, th_user, monkeypatch):
        monkeypatch.setattr(
            "services.tool_outcome_service.settings.tool_health_warn_min_uses", 2,
        )
        monkeypatch.setattr(
            "services.tool_outcome_service.settings.tool_health_warn_success_rate", 0.5,
        )
        svc = await self._seed(db_session, th_user, monkeypatch, fails=8, total=10)
        warnings = await svc.get_health_warnings(user_id=th_user.id)
        assert len(warnings) == 1
        assert warnings[0]["tool_name"] == "mcp.x"
        assert warnings[0]["failure_count"] == 8
        assert warnings[0]["success_rate"] == 0.2

    async def test_candidate_tools_filter(self, db_session, th_user, monkeypatch):
        monkeypatch.setattr(
            "services.tool_outcome_service.settings.tool_health_warn_min_uses", 2,
        )
        monkeypatch.setattr(
            "services.tool_outcome_service.settings.tool_health_warn_success_rate", 0.5,
        )
        await self._seed(db_session, th_user, monkeypatch, fails=8, total=10, tool="mcp.a")
        await self._seed(db_session, th_user, monkeypatch, fails=8, total=10, tool="mcp.b")

        from services.tool_outcome_service import ToolOutcomeService
        svc = ToolOutcomeService(db_session)
        warnings = await svc.get_health_warnings(
            user_id=th_user.id, candidate_tools=["mcp.a"],
        )
        assert len(warnings) == 1
        assert warnings[0]["tool_name"] == "mcp.a"

    async def test_disabled_warn_flag(self, db_session, th_user, monkeypatch):
        monkeypatch.setattr(
            "services.tool_outcome_service.settings.tool_health_warn_enabled", False,
        )
        await self._seed(db_session, th_user, monkeypatch, fails=8, total=10)

        from services.tool_outcome_service import ToolOutcomeService
        svc = ToolOutcomeService(db_session)
        warnings = await svc.get_health_warnings(user_id=th_user.id)
        assert warnings == []


# ==================================================== formatting
class TestFormatForPrompt:
    def test_empty_returns_empty(self):
        from services.tool_outcome_service import ToolOutcomeService
        assert ToolOutcomeService.format_for_prompt([]) == ""

    def test_renders_de_header(self):
        from services.tool_outcome_service import ToolOutcomeService
        out = ToolOutcomeService.format_for_prompt([{
            "tool_name": "mcp.x",
            "success_count": 2,
            "failure_count": 8,
            "total": 10,
            "success_rate": 0.2,
            "last_failure_at": None,
            "last_failure_summary": "timeout",
        }])
        assert "TOOL-HEALTH-WARNUNGEN" in out
        assert "mcp.x" in out
        assert "timeout" in out

    def test_renders_en_header(self):
        from services.tool_outcome_service import ToolOutcomeService
        out = ToolOutcomeService.format_for_prompt(
            [{
                "tool_name": "mcp.x",
                "success_count": 2,
                "failure_count": 8,
                "total": 10,
                "success_rate": 0.2,
                "last_failure_at": None,
                "last_failure_summary": "timeout",
            }],
            lang="en",
        )
        assert "TOOL HEALTH WARNINGS" in out
