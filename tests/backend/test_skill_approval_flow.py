"""End-to-end tests for the v2.10 skill approval flow.

Covers the human-in-the-loop draft-gate that ships in v2.10:
  - draft -> approved via POST /api/skills/{id}/approve
  - draft -> rejected via POST /api/skills/{id}/reject
  - GET /api/skills/draft-count (admin = global; owner = own only)
  - GET /api/skills?admin_view=true (admin-only)
  - non-admin blocked from approve/reject
  - find_similar excludes non-approved rows
  - SkillCuratorService.run_curator writes a SkillCuratorRun row
  - GET /api/skills/curator/runs surfaces audit history
"""
from __future__ import annotations

from unittest.mock import patch

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.database import (
    EMBEDDING_DIMENSION,
    ProceduralSkill,
    Role,
    SkillCuratorRun,
    User,
)
from services.skill_service import SkillService


@pytest.fixture(autouse=True)
def _clear_skill_cache():
    SkillService.invalidate_has_skills_cache()
    yield
    SkillService.invalidate_has_skills_cache()


@pytest.fixture
def patched_embed():
    with patch(
        "services.skill_service.SkillService._embed",
        return_value=[0.1] * EMBEDDING_DIMENSION,
    ) as p:
        yield p


async def _load_with_role(db: AsyncSession, user_id: int) -> User:
    from sqlalchemy.orm import selectinload
    return (await db.execute(
        select(User).where(User.id == user_id).options(selectinload(User.role))
    )).scalar_one()


@pytest.fixture
async def owner_user(db_session: AsyncSession) -> User:
    role = Role(name="approval_owner_role")
    db_session.add(role)
    await db_session.commit()
    await db_session.refresh(role)
    user = User(
        username="approval_owner", email="o@x.test", password_hash="x",
        role_id=role.id, is_active=True,
    )
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return await _load_with_role(db_session, user.id)


@pytest.fixture
async def admin_user(db_session: AsyncSession) -> User:
    role = Role(name="approval_admin_role", permissions=["admin"])
    db_session.add(role)
    await db_session.commit()
    await db_session.refresh(role)
    user = User(
        username="approval_admin", email="a@x.test", password_hash="x",
        role_id=role.id, is_active=True,
    )
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return await _load_with_role(db_session, user.id)


@pytest.fixture
def auth_as_owner(app_with_test_db, owner_user, monkeypatch):
    from services.auth_service import get_current_user, get_user_or_default
    monkeypatch.setattr("services.auth_service.settings.auth_enabled", True)
    app_with_test_db.dependency_overrides[get_user_or_default] = lambda: owner_user
    app_with_test_db.dependency_overrides[get_current_user] = lambda: owner_user
    try:
        yield owner_user
    finally:
        app_with_test_db.dependency_overrides.pop(get_user_or_default, None)
        app_with_test_db.dependency_overrides.pop(get_current_user, None)


@pytest.fixture
def auth_as_admin(app_with_test_db, admin_user, monkeypatch):
    from services.auth_service import get_current_user, get_user_or_default
    monkeypatch.setattr("services.auth_service.settings.auth_enabled", True)
    app_with_test_db.dependency_overrides[get_user_or_default] = lambda: admin_user
    app_with_test_db.dependency_overrides[get_current_user] = lambda: admin_user
    try:
        yield admin_user
    finally:
        app_with_test_db.dependency_overrides.pop(get_user_or_default, None)
        app_with_test_db.dependency_overrides.pop(get_current_user, None)


@pytest.mark.asyncio
class TestApproveSkill:
    async def test_admin_can_approve_draft(
        self, async_client: AsyncClient, auth_as_admin, owner_user,
        db_session: AsyncSession, patched_embed,
    ):
        svc = SkillService(db_session)
        skill = await svc.create_auto_extracted(
            user_id=owner_user.id, title="Draft skill",
            body_md="- step", trigger_examples=["x"],
            tool_sequence=["mcp.x"],
        )
        assert skill.status == "draft"

        resp = await async_client.post(f"/api/skills/{skill.id}/approve")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["status"] == "approved"
        await db_session.refresh(skill)
        assert skill.status == "approved"

    async def test_owner_blocked_without_admin(
        self, async_client: AsyncClient, auth_as_owner,
        db_session: AsyncSession, patched_embed,
    ):
        svc = SkillService(db_session)
        skill = await svc.create_auto_extracted(
            user_id=auth_as_owner.id, title="Draft", body_md="x",
            trigger_examples=["x"], tool_sequence=["mcp.x"],
        )
        resp = await async_client.post(f"/api/skills/{skill.id}/approve")
        assert resp.status_code == 403

    async def test_approve_clears_merged_into_id(
        self, async_client: AsyncClient, auth_as_admin, owner_user,
        db_session: AsyncSession, patched_embed,
    ):
        svc = SkillService(db_session)
        loser = await svc.create_user_authored(
            user_id=owner_user.id, title="L", body_md="x",
            trigger_examples=["a"], tool_sequence=["mcp.x"],
        )
        winner = await svc.create_user_authored(
            user_id=owner_user.id, title="W", body_md="x",
            trigger_examples=["b"], tool_sequence=["mcp.x"],
        )
        loser.status = "rejected"
        loser.merged_into_id = winner.id
        await db_session.commit()

        resp = await async_client.post(f"/api/skills/{loser.id}/approve")
        assert resp.status_code == 200
        await db_session.refresh(loser)
        assert loser.merged_into_id is None

    async def test_approve_is_idempotent(
        self, async_client: AsyncClient, auth_as_admin, owner_user,
        db_session: AsyncSession, patched_embed,
    ):
        svc = SkillService(db_session)
        skill = await svc.create_user_authored(
            user_id=owner_user.id, title="Approved", body_md="x",
            trigger_examples=["x"], tool_sequence=["mcp.x"],
        )
        resp = await async_client.post(f"/api/skills/{skill.id}/approve")
        assert resp.status_code == 200
        resp = await async_client.post(f"/api/skills/{skill.id}/approve")
        assert resp.status_code == 200


@pytest.mark.asyncio
class TestRejectSkill:
    async def test_admin_can_reject_draft(
        self, async_client: AsyncClient, auth_as_admin, owner_user,
        db_session: AsyncSession, patched_embed,
    ):
        svc = SkillService(db_session)
        skill = await svc.create_auto_extracted(
            user_id=owner_user.id, title="Spam", body_md="x",
            trigger_examples=["x"], tool_sequence=["mcp.x"],
        )
        resp = await async_client.post(f"/api/skills/{skill.id}/reject")
        assert resp.status_code == 200
        await db_session.refresh(skill)
        assert skill.status == "rejected"

    async def test_reject_blocked_for_approved_skill(
        self, async_client: AsyncClient, auth_as_admin, owner_user,
        db_session: AsyncSession, patched_embed,
    ):
        svc = SkillService(db_session)
        skill = await svc.create_user_authored(
            user_id=owner_user.id, title="Approved", body_md="x",
            trigger_examples=["x"], tool_sequence=["mcp.x"],
        )
        resp = await async_client.post(f"/api/skills/{skill.id}/reject")
        assert resp.status_code == 409


@pytest.mark.asyncio
class TestDraftCount:
    async def test_admin_sees_global_count(
        self, async_client: AsyncClient, auth_as_admin, owner_user,
        db_session: AsyncSession, patched_embed,
    ):
        svc = SkillService(db_session)
        await svc.create_auto_extracted(
            user_id=owner_user.id, title="d1", body_md="x",
            trigger_examples=["x"], tool_sequence=["mcp.x"],
        )
        await svc.create_auto_extracted(
            user_id=owner_user.id, title="d2", body_md="x",
            trigger_examples=["x"], tool_sequence=["mcp.x"],
        )
        resp = await async_client.get("/api/skills/draft-count")
        assert resp.status_code == 200
        assert resp.json()["count"] == 2

    async def test_owner_sees_only_own(
        self, async_client: AsyncClient, auth_as_owner,
        db_session: AsyncSession, patched_embed,
    ):
        svc = SkillService(db_session)
        await svc.create_auto_extracted(
            user_id=auth_as_owner.id, title="my-draft", body_md="x",
            trigger_examples=["x"], tool_sequence=["mcp.x"],
        )
        resp = await async_client.get("/api/skills/draft-count")
        assert resp.status_code == 200
        assert resp.json()["count"] == 1


@pytest.mark.asyncio
class TestAdminViewListFilter:
    async def test_admin_view_returns_other_user_drafts(
        self, async_client: AsyncClient, auth_as_admin, owner_user,
        db_session: AsyncSession, patched_embed,
    ):
        svc = SkillService(db_session)
        await svc.create_auto_extracted(
            user_id=owner_user.id, title="Other-user draft", body_md="x",
            trigger_examples=["x"], tool_sequence=["mcp.x"],
        )
        resp = await async_client.get(
            "/api/skills?admin_view=true&status=draft",
        )
        assert resp.status_code == 200
        titles = [s["title"] for s in resp.json()]
        assert "Other-user draft" in titles

    async def test_non_admin_blocked_from_admin_view(
        self, async_client: AsyncClient, auth_as_owner,
    ):
        resp = await async_client.get("/api/skills?admin_view=true")
        assert resp.status_code == 403


@pytest.mark.asyncio
class TestFindSimilarStatusGate:
    async def test_draft_excluded_from_retrieval(
        self, db_session: AsyncSession, patched_embed, owner_user,
    ):
        """Draft skills must NOT show up in agent retrieval — that's the
        whole point of the v2.10 gate."""
        svc = SkillService(db_session)
        await svc.create_auto_extracted(
            user_id=owner_user.id, title="Should not match",
            body_md="x", trigger_examples=["unique trigger"],
            tool_sequence=["mcp.x"],
        )
        out = await svc.find_similar("anything", asker_id=owner_user.id)
        assert out == []


@pytest.mark.asyncio
class TestCuratorOrchestrator:
    async def test_run_curator_writes_audit_row(
        self, db_session: AsyncSession,
    ):
        from services.skill_curator_service import SkillCuratorService
        svc = SkillCuratorService(db_session)
        run = await svc.run_curator(
            triggered_by_user_id=None,
            run_type="scheduled",
        )
        assert run.id is not None
        assert run.run_type == "scheduled"
        assert run.finished_at is not None
        assert run.status in ("success", "partial", "failed")

        rows = (await db_session.execute(select(SkillCuratorRun))).scalars().all()
        assert any(r.id == run.id for r in rows)

    async def test_run_curator_rejects_bad_run_type(
        self, db_session: AsyncSession,
    ):
        from services.skill_curator_service import SkillCuratorService
        svc = SkillCuratorService(db_session)
        with pytest.raises(ValueError):
            await svc.run_curator(run_type="bogus")


@pytest.mark.asyncio
class TestCuratorRunsRoute:
    async def test_admin_can_list_runs(
        self, async_client: AsyncClient, auth_as_admin,
        db_session: AsyncSession,
    ):
        from services.skill_curator_service import SkillCuratorService
        svc = SkillCuratorService(db_session)
        await svc.run_curator(run_type="manual", triggered_by_user_id=auth_as_admin.id)
        resp = await async_client.get("/api/skills/curator/runs")
        assert resp.status_code == 200
        rows = resp.json()
        assert len(rows) >= 1
        assert rows[0]["run_type"] in ("manual", "scheduled")

    async def test_non_admin_blocked(
        self, async_client: AsyncClient, auth_as_owner,
    ):
        resp = await async_client.get("/api/skills/curator/runs")
        assert resp.status_code == 403
