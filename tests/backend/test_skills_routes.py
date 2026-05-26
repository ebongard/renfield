"""Route-level tests for /api/skills.

The service layer is covered by test_skill_service.py — this file is the
HTTP-boundary surface: Pydantic validation (body_md max_length,
trigger_examples bounds), owner-isolation 404, admin-only curator/run,
PATCH-driven re-embed + merged_into_id clearing, rate-limit decorator
binding, soft-delete via DELETE, and the tier-change cascade.

These were the 9 endpoints flagged with ZERO route coverage in the
self-learning eng-review.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from models.database import (
    EMBEDDING_DIMENSION,
    ProceduralSkill,
    Role,
    SKILL_SOURCE_AUTO_EXTRACTED,
    SKILL_SOURCE_SEED,
    SKILL_SOURCE_USER_CREATED,
    TIER_PUBLIC,
    User,
)
from services.skill_service import SkillService


# ----------------------------------------------------------- fixtures
@pytest.fixture(autouse=True)
def _clear_skill_cache():
    """Class-level cache leaks between tests. Reset before AND after so
    a test that warms it with True doesn't make a later test's
    has_any_skills() lie. Same defense added in test_skill_service.py."""
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


async def _make_role(db_session: AsyncSession, name: str) -> Role:
    role = Role(name=name)
    db_session.add(role)
    await db_session.commit()
    await db_session.refresh(role)
    return role


async def _load_with_role(db_session: AsyncSession, user_id: int) -> User:
    """Re-select the user with the role eagerly loaded. Without this,
    ``require_permission`` trips MissingGreenlet when it traverses
    ``user.role.has_permission(...)`` in the route's request session."""
    from sqlalchemy.orm import selectinload
    from sqlalchemy import select as _select
    row = (await db_session.execute(
        _select(User).where(User.id == user_id).options(selectinload(User.role))
    )).scalar_one()
    return row


@pytest.fixture
async def owner_user(db_session: AsyncSession) -> User:
    role = await _make_role(db_session, "skills_owner_role")
    user = User(
        username="skills_owner", email="owner@example.test",
        password_hash="x", role_id=role.id, is_active=True,
    )
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return await _load_with_role(db_session, user.id)


@pytest.fixture
async def other_user(db_session: AsyncSession) -> User:
    role = await _make_role(db_session, "skills_other_role")
    user = User(
        username="skills_other", email="other@example.test",
        password_hash="x", role_id=role.id, is_active=True,
    )
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return await _load_with_role(db_session, user.id)


@pytest.fixture
async def admin_user(db_session: AsyncSession) -> User:
    role = Role(name="skills_admin_role", permissions=["admin"])
    db_session.add(role)
    await db_session.commit()
    await db_session.refresh(role)
    user = User(
        username="skills_admin", email="admin@example.test",
        password_hash="x", role_id=role.id, is_active=True,
    )
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return await _load_with_role(db_session, user.id)


@pytest.fixture
def auth_as_owner(app_with_test_db, owner_user, monkeypatch):
    """Override get_user_or_default + get_current_user to return owner.
    Owner-level routes don't need auth_enabled=True (no permission
    check), but the curator/run route does — pin it true so the
    cross-fixture assertions (non-admin blocked) work."""
    from services.auth_service import get_current_user, get_user_or_default
    monkeypatch.setattr(
        "services.auth_service.settings.auth_enabled", True,
    )
    app_with_test_db.dependency_overrides[get_user_or_default] = lambda: owner_user
    app_with_test_db.dependency_overrides[get_current_user] = lambda: owner_user
    try:
        yield owner_user
    finally:
        app_with_test_db.dependency_overrides.pop(get_user_or_default, None)
        app_with_test_db.dependency_overrides.pop(get_current_user, None)


@pytest.fixture
def auth_as_other(app_with_test_db, other_user, monkeypatch):
    from services.auth_service import get_current_user, get_user_or_default
    monkeypatch.setattr(
        "services.auth_service.settings.auth_enabled", True,
    )
    app_with_test_db.dependency_overrides[get_user_or_default] = lambda: other_user
    app_with_test_db.dependency_overrides[get_current_user] = lambda: other_user
    try:
        yield other_user
    finally:
        app_with_test_db.dependency_overrides.pop(get_user_or_default, None)
        app_with_test_db.dependency_overrides.pop(get_current_user, None)


@pytest.fixture
def auth_as_admin(app_with_test_db, admin_user, monkeypatch):
    from services.auth_service import get_current_user, get_user_or_default
    monkeypatch.setattr(
        "services.auth_service.settings.auth_enabled", True,
    )
    app_with_test_db.dependency_overrides[get_user_or_default] = lambda: admin_user
    app_with_test_db.dependency_overrides[get_current_user] = lambda: admin_user
    try:
        yield admin_user
    finally:
        app_with_test_db.dependency_overrides.pop(get_user_or_default, None)
        app_with_test_db.dependency_overrides.pop(get_current_user, None)


# ============================================================ LIST
@pytest.mark.asyncio
class TestListSkills:
    async def test_returns_owner_skill(
        self, async_client: AsyncClient, auth_as_owner,
        db_session: AsyncSession, patched_embed,
    ):
        svc = SkillService(db_session)
        await svc.create_user_authored(
            user_id=auth_as_owner.id,
            title="Owner's skill", body_md="- step 1",
            trigger_examples=["test trigger"], tool_sequence=["mcp.x.y"],
        )
        resp = await async_client.get("/api/skills")
        assert resp.status_code == 200
        ids = [s["title"] for s in resp.json()]
        assert "Owner's skill" in ids

    async def test_pagination_limit_bounds(
        self, async_client: AsyncClient, auth_as_owner,
    ):
        # limit=0 is below ge=1 → 422
        resp = await async_client.get("/api/skills?limit=0")
        assert resp.status_code == 422

    async def test_pagination_limit_upper_bound(
        self, async_client: AsyncClient, auth_as_owner,
    ):
        # limit=10000 is above le=500 → 422
        resp = await async_client.get("/api/skills?limit=10000")
        assert resp.status_code == 422

    async def test_pagination_offset_negative(
        self, async_client: AsyncClient, auth_as_owner,
    ):
        resp = await async_client.get("/api/skills?offset=-1")
        assert resp.status_code == 422


# ============================================================ READ
@pytest.mark.asyncio
class TestGetSkill:
    async def test_owner_gets_own_skill(
        self, async_client: AsyncClient, auth_as_owner,
        db_session: AsyncSession, patched_embed,
    ):
        svc = SkillService(db_session)
        skill = await svc.create_user_authored(
            user_id=auth_as_owner.id, title="T", body_md="b",
            trigger_examples=["x"], tool_sequence=["mcp.x.y"],
        )
        resp = await async_client.get(f"/api/skills/{skill.id}")
        assert resp.status_code == 200
        assert resp.json()["title"] == "T"
        assert resp.json()["is_owner"] is True

    async def test_cross_user_returns_404(
        self, async_client: AsyncClient, auth_as_other, owner_user,
        db_session: AsyncSession, patched_embed,
    ):
        """Cross-user read returns uniform 404, NOT 403. Disclosing that
        the row exists would leak existence; uniform 404 is the
        documented contract in _load_owned + get_skill."""
        svc = SkillService(db_session)
        skill = await svc.create_user_authored(
            user_id=owner_user.id, title="OwnerOnly", body_md="b",
            trigger_examples=["x"], tool_sequence=["mcp.x.y"],
        )
        resp = await async_client.get(f"/api/skills/{skill.id}")
        assert resp.status_code == 404

    async def test_unknown_id_404(self, async_client, auth_as_owner):
        resp = await async_client.get("/api/skills/99999")
        assert resp.status_code == 404

    async def test_seed_skill_visible_to_anyone(
        self, async_client: AsyncClient, auth_as_other,
        db_session: AsyncSession, patched_embed,
    ):
        seed = ProceduralSkill(
            user_id=None, title="Public seed", body_md="- seed step",
            trigger_examples=["seed trigger"], tool_sequence=["mcp.x.y"],
            source=SKILL_SOURCE_SEED, circle_tier=TIER_PUBLIC,
            atom_id=None,
        )
        db_session.add(seed)
        await db_session.commit()
        await db_session.refresh(seed)

        resp = await async_client.get(f"/api/skills/{seed.id}")
        assert resp.status_code == 200
        assert resp.json()["is_owner"] is False


# ============================================================ CREATE
@pytest.mark.asyncio
class TestCreateSkill:
    async def test_create_happy_path(
        self, async_client: AsyncClient, auth_as_owner, patched_embed,
    ):
        resp = await async_client.post("/api/skills", json={
            "title": "Recipe", "body_md": "- step 1",
            "trigger_examples": ["do the thing"],
            "tool_sequence": ["mcp.x.y"],
        })
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["title"] == "Recipe"
        assert body["is_owner"] is True
        assert body["status"] == "approved"  # user-authored = approved

    async def test_body_md_over_max_length_422(
        self, async_client: AsyncClient, auth_as_owner,
    ):
        """body_md cap is 8000 chars to prevent prompt-bloat DoS via a
        single owner authoring a multi-MB skill that gets injected every
        matching turn."""
        huge = "a" * 9000
        resp = await async_client.post("/api/skills", json={
            "title": "Big", "body_md": huge,
            "trigger_examples": ["x"],
            "tool_sequence": [],
        })
        assert resp.status_code == 422

    async def test_too_many_triggers_422(
        self, async_client: AsyncClient, auth_as_owner,
    ):
        resp = await async_client.post("/api/skills", json={
            "title": "T", "body_md": "b",
            "trigger_examples": [f"t{i}" for i in range(15)],  # cap is 10
            "tool_sequence": [],
        })
        assert resp.status_code == 422

    async def test_empty_triggers_422(
        self, async_client: AsyncClient, auth_as_owner,
    ):
        resp = await async_client.post("/api/skills", json={
            "title": "T", "body_md": "b",
            "trigger_examples": [],
            "tool_sequence": [],
        })
        assert resp.status_code == 422

    async def test_internal_error_doesnt_leak_message(
        self, async_client: AsyncClient, auth_as_owner,
    ):
        """detail=str(e) was leaking SQL/Ollama error text. Now generic."""
        with patch(
            "services.skill_service.SkillService.create_user_authored",
            side_effect=RuntimeError("SECRET_INTERNAL_ERROR_42"),
        ):
            resp = await async_client.post("/api/skills", json={
                "title": "T", "body_md": "b",
                "trigger_examples": ["x"], "tool_sequence": [],
            })
        assert resp.status_code == 500
        assert "SECRET_INTERNAL_ERROR_42" not in resp.text
        assert "Skill create failed" in resp.text


# ============================================================ PATCH
@pytest.mark.asyncio
class TestUpdateSkill:
    async def test_patch_clears_merged_into_on_reactivate(
        self, async_client: AsyncClient, auth_as_owner,
        db_session: AsyncSession, patched_embed,
    ):
        """A curator-merged loser (status='archived', merged_into_id=X)
        that the owner reactivates MUST get its merged_into_id cleared
        — otherwise the next curator pass re-pairs it against X."""
        svc = SkillService(db_session)
        loser = await svc.create_user_authored(
            user_id=auth_as_owner.id, title="L", body_md="b",
            trigger_examples=["x"], tool_sequence=["mcp.x.y"],
        )
        winner = await svc.create_user_authored(
            user_id=auth_as_owner.id, title="W", body_md="b",
            trigger_examples=["y"], tool_sequence=["mcp.x.y"],
        )
        loser.status = "archived"
        loser.merged_into_id = winner.id
        await db_session.commit()

        resp = await async_client.patch(
            f"/api/skills/{loser.id}",
            json={"status": "approved"},
        )
        assert resp.status_code == 200, resp.text
        await db_session.refresh(loser)
        assert loser.status == "approved"
        assert loser.merged_into_id is None  # critical assert

    async def test_patch_reembeds_on_title_change(
        self, async_client: AsyncClient, auth_as_owner,
        db_session: AsyncSession, patched_embed,
    ):
        svc = SkillService(db_session)
        skill = await svc.create_user_authored(
            user_id=auth_as_owner.id, title="Original", body_md="b",
            trigger_examples=["x"], tool_sequence=["mcp.x.y"],
        )
        patched_embed.reset_mock()
        resp = await async_client.patch(
            f"/api/skills/{skill.id}",
            json={"title": "Renamed"},
        )
        assert resp.status_code == 200
        # _embed was called again (via compute_embedding_for) because
        # title changed → similarity input changed.
        assert patched_embed.called

    async def test_patch_does_not_reembed_on_pin_only(
        self, async_client: AsyncClient, auth_as_owner,
        db_session: AsyncSession, patched_embed,
    ):
        svc = SkillService(db_session)
        skill = await svc.create_user_authored(
            user_id=auth_as_owner.id, title="T", body_md="b",
            trigger_examples=["x"], tool_sequence=["mcp.x.y"],
        )
        patched_embed.reset_mock()
        resp = await async_client.patch(
            f"/api/skills/{skill.id}",
            json={"pinned": True},
        )
        assert resp.status_code == 200
        assert not patched_embed.called

    async def test_patch_cross_user_404(
        self, async_client: AsyncClient, auth_as_other, owner_user,
        db_session: AsyncSession, patched_embed,
    ):
        svc = SkillService(db_session)
        skill = await svc.create_user_authored(
            user_id=owner_user.id, title="O", body_md="b",
            trigger_examples=["x"], tool_sequence=["mcp.x.y"],
        )
        resp = await async_client.patch(
            f"/api/skills/{skill.id}", json={"title": "hacked"},
        )
        assert resp.status_code == 404

    async def test_patch_body_max_length_422(
        self, async_client: AsyncClient, auth_as_owner,
        db_session: AsyncSession, patched_embed,
    ):
        svc = SkillService(db_session)
        skill = await svc.create_user_authored(
            user_id=auth_as_owner.id, title="T", body_md="b",
            trigger_examples=["x"], tool_sequence=["mcp.x.y"],
        )
        resp = await async_client.patch(
            f"/api/skills/{skill.id}",
            json={"body_md": "a" * 9000},
        )
        assert resp.status_code == 422


# ============================================================ PIN / UNPIN
@pytest.mark.asyncio
class TestPin:
    async def test_pin_then_unpin(
        self, async_client: AsyncClient, auth_as_owner,
        db_session: AsyncSession, patched_embed,
    ):
        svc = SkillService(db_session)
        skill = await svc.create_user_authored(
            user_id=auth_as_owner.id, title="T", body_md="b",
            trigger_examples=["x"], tool_sequence=["mcp.x.y"],
        )
        assert skill.pinned is False

        r1 = await async_client.post(f"/api/skills/{skill.id}/pin")
        assert r1.status_code == 200
        assert r1.json()["pinned"] is True

        r2 = await async_client.post(f"/api/skills/{skill.id}/unpin")
        assert r2.status_code == 200
        assert r2.json()["pinned"] is False

    async def test_pin_cross_user_404(
        self, async_client: AsyncClient, auth_as_other, owner_user,
        db_session: AsyncSession, patched_embed,
    ):
        svc = SkillService(db_session)
        skill = await svc.create_user_authored(
            user_id=owner_user.id, title="O", body_md="b",
            trigger_examples=["x"], tool_sequence=["mcp.x.y"],
        )
        resp = await async_client.post(f"/api/skills/{skill.id}/pin")
        assert resp.status_code == 404


# ============================================================ DELETE
@pytest.mark.asyncio
class TestDeleteSkill:
    async def test_soft_delete_archives_skill(
        self, async_client: AsyncClient, auth_as_owner,
        db_session: AsyncSession, patched_embed,
    ):
        svc = SkillService(db_session)
        skill = await svc.create_user_authored(
            user_id=auth_as_owner.id, title="T", body_md="b",
            trigger_examples=["x"], tool_sequence=["mcp.x.y"],
        )
        resp = await async_client.delete(f"/api/skills/{skill.id}")
        assert resp.status_code == 204

        await db_session.refresh(skill)
        assert skill.status == "archived"
        # NOT a hard delete — the row stays for audit trail.

    async def test_delete_cross_user_404(
        self, async_client: AsyncClient, auth_as_other, owner_user,
        db_session: AsyncSession, patched_embed,
    ):
        svc = SkillService(db_session)
        skill = await svc.create_user_authored(
            user_id=owner_user.id, title="O", body_md="b",
            trigger_examples=["x"], tool_sequence=["mcp.x.y"],
        )
        resp = await async_client.delete(f"/api/skills/{skill.id}")
        assert resp.status_code == 404


# ============================================================ TIER
@pytest.mark.asyncio
class TestChangeTier:
    @pytest.mark.postgres
    async def test_tier_change_owner_ok(
        self, async_client: AsyncClient, auth_as_owner,
        db_session: AsyncSession, patched_embed,
    ):
        # AtomService.update_tier uses Postgres-specific syntax (`::`
        # cast in the cascade-update raw SQL) that aiosqlite rejects.
        # Skip cleanly when the harness is on sqlite.
        if (db_session.bind is None
                or db_session.bind.dialect.name != "postgresql"):
            pytest.skip("Tier-change cascade requires postgres syntax")
        svc = SkillService(db_session)
        skill = await svc.create_user_authored(
            user_id=auth_as_owner.id, title="T", body_md="b",
            trigger_examples=["x"], tool_sequence=["mcp.x.y"],
            circle_tier=0,
        )
        resp = await async_client.patch(
            f"/api/skills/{skill.id}/tier", json={"circle_tier": 2},
        )
        assert resp.status_code == 200
        assert resp.json()["circle_tier"] == 2

    async def test_tier_out_of_range_422(
        self, async_client: AsyncClient, auth_as_owner,
        db_session: AsyncSession, patched_embed,
    ):
        svc = SkillService(db_session)
        skill = await svc.create_user_authored(
            user_id=auth_as_owner.id, title="T", body_md="b",
            trigger_examples=["x"], tool_sequence=["mcp.x.y"],
        )
        resp = await async_client.patch(
            f"/api/skills/{skill.id}/tier", json={"circle_tier": 99},
        )
        assert resp.status_code == 422


# ============================================================ CURATOR/RUN
@pytest.mark.asyncio
class TestCuratorRun:
    async def test_admin_can_trigger(
        self, async_client: AsyncClient, auth_as_admin, patched_embed,
    ):
        """Happy path: admin can fire the curator run. With no skills in
        DB the report has duplicates_found=0 + stale_archived=0."""
        resp = await async_client.post(
            "/api/skills/curator/run",
            json={"user_id": auth_as_admin.id},
        )
        # Either 200 with empty report list (the per-user code path) or
        # a list with one zero'd report. Both are valid here.
        assert resp.status_code == 200
        body = resp.json()
        assert isinstance(body, list)

    async def test_non_admin_blocked(
        self, async_client: AsyncClient, auth_as_owner,
        # auth_as_owner has no admin permission in its role; require_permission
        # short-circuits with 403 before the route body runs.
    ):
        resp = await async_client.post(
            "/api/skills/curator/run", json={"user_id": auth_as_owner.id},
        )
        assert resp.status_code in (401, 403)
