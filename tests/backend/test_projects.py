"""Route tests for /api/projects (business-instance Phase 1).

Covers the feature-flag gate, the create-project-creates-linked-KB invariant,
owner-scoping under auth, and delete-keeps-the-KB. Pure route validation on the
default SQLite harness — no Postgres semantics needed (the shared `db_session`
is what `override_get_db` yields, so seeding + the route hit the same DB).

The migration itself is exercised by a real `alembic upgrade` on the .159 build
box (create_all skips the migration body — see memory
feedback_alembic_test_real_upgrade), not here.
"""
from __future__ import annotations

import pytest

from models.database import Document, KnowledgeBase, Project, User
from utils.config import settings

pytestmark = [pytest.mark.asyncio]


def _override_user(user: User | None) -> None:
    """Force get_optional_user to resolve to `user` (or anonymous) for the app."""
    from main import app
    from services.auth_service import get_optional_user

    app.dependency_overrides[get_optional_user] = lambda: user


def _enable(monkeypatch, *, auth: bool) -> None:
    monkeypatch.setattr(settings, "projects_enabled", True)
    monkeypatch.setattr(settings, "auth_enabled", auth)


# ---------------------------------------------------------------------------
# Feature-flag gate
# ---------------------------------------------------------------------------

async def test_all_routes_404_when_flag_off(async_client, monkeypatch):
    monkeypatch.setattr(settings, "projects_enabled", False)
    assert (await async_client.post("/api/projects", json={"name": "X"})).status_code == 404
    assert (await async_client.get("/api/projects")).status_code == 404
    assert (await async_client.get("/api/projects/1")).status_code == 404
    assert (await async_client.delete("/api/projects/1")).status_code == 404


# ---------------------------------------------------------------------------
# Create → dedicated 1:1 KnowledgeBase
# ---------------------------------------------------------------------------

async def test_create_project_creates_linked_kb(async_client, db_session, monkeypatch):
    _enable(monkeypatch, auth=False)
    resp = await async_client.post(
        "/api/projects", json={"name": "Alpha", "description": "d"}
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["name"] == "Alpha"
    assert body["description"] == "d"
    assert body["status"] == "active"
    assert body["circle_tier"] == 2
    assert body["document_count"] == 0
    assert body["knowledge_base_id"] is not None

    # The KB really exists, is tier-scoped to the project, and is distinct per project.
    kb = await db_session.get(KnowledgeBase, body["knowledge_base_id"])
    assert kb is not None
    assert kb.default_circle_tier == 2

    # A second project gets its OWN fresh KB (1:1, unique name).
    resp2 = await async_client.post("/api/projects", json={"name": "Alpha"})
    assert resp2.status_code == 200
    assert resp2.json()["knowledge_base_id"] != body["knowledge_base_id"]


async def test_create_honors_explicit_tier(async_client, db_session, monkeypatch):
    _enable(monkeypatch, auth=False)
    resp = await async_client.post(
        "/api/projects", json={"name": "Tiered", "circle_tier": 4}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["circle_tier"] == 4
    kb = await db_session.get(KnowledgeBase, body["knowledge_base_id"])
    assert kb.default_circle_tier == 4


# ---------------------------------------------------------------------------
# List / get / delete (auth off = single-user sees all)
# ---------------------------------------------------------------------------

async def test_list_get_delete_lifecycle_auth_off(async_client, db_session, monkeypatch):
    _enable(monkeypatch, auth=False)
    created = (await async_client.post("/api/projects", json={"name": "Beta"})).json()
    pid = created["id"]
    kb_id = created["knowledge_base_id"]

    listing = await async_client.get("/api/projects")
    assert listing.status_code == 200
    assert any(p["id"] == pid for p in listing.json())

    got = await async_client.get(f"/api/projects/{pid}")
    assert got.status_code == 200
    assert got.json()["id"] == pid

    deleted = await async_client.delete(f"/api/projects/{pid}")
    assert deleted.status_code == 200
    assert deleted.json()["knowledge_base_retained"] is True
    assert deleted.json()["knowledge_base_id"] == kb_id

    # Project row gone; the KB is deliberately retained.
    assert (await async_client.get(f"/api/projects/{pid}")).status_code == 404
    assert await db_session.get(KnowledgeBase, kb_id) is not None


async def test_get_missing_project_404(async_client, monkeypatch):
    _enable(monkeypatch, auth=False)
    assert (await async_client.get("/api/projects/999999")).status_code == 404


# ---------------------------------------------------------------------------
# Owner scoping under auth
# ---------------------------------------------------------------------------

async def test_owner_scoping_under_auth(async_client, monkeypatch):
    _enable(monkeypatch, auth=True)
    user_a = User(id=1, username="a", password_hash="x", is_active=True, role_id=1)
    user_b = User(id=2, username="b", password_hash="x", is_active=True, role_id=1)

    _override_user(user_a)
    created = await async_client.post("/api/projects", json={"name": "Gamma"})
    assert created.status_code == 200
    assert created.json()["owner_id"] == 1
    pid = created.json()["id"]

    # User B cannot see A's project (owner-gated 404 + absent from list).
    _override_user(user_b)
    assert (await async_client.get(f"/api/projects/{pid}")).status_code == 404
    assert all(p["id"] != pid for p in (await async_client.get("/api/projects")).json())

    # Owner A can.
    _override_user(user_a)
    assert (await async_client.get(f"/api/projects/{pid}")).status_code == 200
    assert any(p["id"] == pid for p in (await async_client.get("/api/projects")).json())


async def test_create_requires_auth_when_enabled(async_client, monkeypatch):
    _enable(monkeypatch, auth=True)
    _override_user(None)
    assert (await async_client.post("/api/projects", json={"name": "X"})).status_code == 401


async def test_delete_owner_gated_under_auth(async_client, monkeypatch):
    _enable(monkeypatch, auth=True)
    user_a = User(id=1, username="a", password_hash="x", is_active=True, role_id=1)
    user_b = User(id=2, username="b", password_hash="x", is_active=True, role_id=1)

    _override_user(user_a)
    pid = (await async_client.post("/api/projects", json={"name": "Delta"})).json()["id"]

    _override_user(user_b)
    assert (await async_client.delete(f"/api/projects/{pid}")).status_code == 404

    _override_user(user_a)
    assert (await async_client.delete(f"/api/projects/{pid}")).status_code == 200


# ---------------------------------------------------------------------------
# Regression: KB name must not overflow KnowledgeBase.name (String(255))
# ---------------------------------------------------------------------------

async def test_max_length_name_does_not_overflow_kb_name(async_client, db_session, monkeypatch):
    """A 255-char project name (the schema max) must not push the composed KB
    name past its 255-char column — the `#<id>` uniqueness suffix stays intact.
    (SQLite doesn't enforce VARCHAR length, so assert the length directly.)"""
    _enable(monkeypatch, auth=False)
    long_name = "x" * 255
    resp = await async_client.post("/api/projects", json={"name": long_name})
    assert resp.status_code == 200, resp.text
    kb_id = resp.json()["knowledge_base_id"]
    kb = await db_session.get(KnowledgeBase, kb_id)
    assert len(kb.name) <= 255
    assert kb.name.endswith(f"#{resp.json()['id']}")  # unique suffix preserved


# ---------------------------------------------------------------------------
# document_count reflects the project's KB (batch path in the list route)
# ---------------------------------------------------------------------------

async def test_list_reports_document_count(async_client, db_session, monkeypatch):
    _enable(monkeypatch, auth=False)
    created = (await async_client.post("/api/projects", json={"name": "Docs"})).json()
    kb_id = created["knowledge_base_id"]
    db_session.add_all([
        Document(filename="a.pdf", file_path="/x/a.pdf", knowledge_base_id=kb_id, status="completed"),
        Document(filename="b.pdf", file_path="/x/b.pdf", knowledge_base_id=kb_id, status="completed"),
    ])
    await db_session.commit()

    listing = await async_client.get("/api/projects")
    assert listing.status_code == 200
    row = next(p for p in listing.json() if p["id"] == created["id"])
    assert row["document_count"] == 2
