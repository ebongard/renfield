"""``POST /api/chat/upload/{id}/email`` — only the upload's owner may forward it.

Forwarding sends the ORIGINAL file to any address the caller names. Reaching the
upload through a shared conversation (circle reach) lets a member read its
preview; it must not let them send the file out of the house. The rule is the
one ``/index`` applies: the caller must own the conversation the upload hangs
in. Auth off: unchanged.

Real Postgres, real circle filter, real memberships; the only stub is the
Email MCP manager (the mail transport is not what is under test).
"""
from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from httpx import AsyncClient

from models.database import ChatUpload, CircleMembership, Conversation, Role, User
from services.auth_service import get_optional_user

pytestmark = [pytest.mark.backend, pytest.mark.database]


async def _user(db, name: str) -> User:
    role = Role(name=f"role-{name}", permissions=["kb.shared", "chat.own", "rag.use"], is_system=False)
    db.add(role)
    await db.flush()
    user = User(username=name, password_hash="x", role_id=role.id)
    db.add(user)
    await db.commit()
    await db.refresh(user, attribute_names=["role"])
    return user


@pytest.fixture
async def people(db_session):
    return {"owner": await _user(db_session, "thread-owner"), "member": await _user(db_session, "member")}


@pytest.fixture
def tmp_file():
    with tempfile.NamedTemporaryFile(suffix=".txt", delete=False) as f:
        f.write(b"attached file")
        path = f.name
    yield path
    Path(path).unlink(missing_ok=True)


async def _shared_thread_upload(db, people, path: str) -> ChatUpload:
    """A room thread owned by ``owner`` at tier 2, ``member`` in owner's circle
    at tier 2 → the member REACHES the thread and its upload."""
    db.add(Conversation(session_id="s-room", user_id=people["owner"].id, circle_tier=2))
    db.add(CircleMembership(
        circle_owner_id=people["owner"].id, member_user_id=people["member"].id,
        dimension="tier", value=2, granted_by=people["owner"].id,
    ))
    upload = ChatUpload(
        session_id="s-room", filename="u.txt", file_type="txt", file_size=13,
        file_hash="h-room", status="completed", file_path=path,
    )
    db.add(upload)
    await db.commit()
    await db.refresh(upload)
    return upload


@pytest.fixture
def auth_on(monkeypatch):
    from utils.config import settings
    monkeypatch.setattr(settings, "auth_enabled", True)


@pytest.fixture
def mail():
    """Stub Email MCP manager on the app; returns the mock to assert on."""
    from main import app
    manager = MagicMock()
    manager.execute_tool = AsyncMock(return_value={"success": True})
    previous = getattr(app.state, "mcp_manager", None)
    app.state.mcp_manager = manager
    yield manager
    app.state.mcp_manager = previous


def _as(user: User | None):
    from main import app
    app.dependency_overrides[get_optional_user] = lambda: user


@pytest.fixture(autouse=True)
def _clear_user_override():
    yield
    from main import app
    app.dependency_overrides.pop(get_optional_user, None)


async def _forward(client, upload_id: int):
    return await client.post(
        f"/api/chat/upload/{upload_id}/email", json={"to": "someone@example.org"},
    )


async def test_owner_may_forward(async_client: AsyncClient, db_session, people, auth_on, tmp_file, mail):
    upload = await _shared_thread_upload(db_session, people, tmp_file)

    _as(people["owner"])
    resp = await _forward(async_client, upload.id)

    assert resp.status_code == 200, resp.text
    mail.execute_tool.assert_awaited_once()


async def test_member_of_shared_thread_may_not_forward(
    async_client: AsyncClient, db_session, people, auth_on, tmp_file, mail,
):
    """Negative control is the test above: same upload, same thread, only the
    caller differs. The read below proves the member really reaches it."""
    upload = await _shared_thread_upload(db_session, people, tmp_file)

    _as(people["member"])
    read = await async_client.get(f"/api/chat/upload/{upload.id}")
    assert read.status_code == 200, read.text

    resp = await _forward(async_client, upload.id)

    assert resp.status_code == 403, resp.text
    mail.execute_tool.assert_not_awaited()


async def test_anonymous_forward_is_404_under_auth(
    async_client: AsyncClient, db_session, people, auth_on, tmp_file, mail,
):
    upload = await _shared_thread_upload(db_session, people, tmp_file)

    _as(None)
    resp = await _forward(async_client, upload.id)

    assert resp.status_code == 404
    mail.execute_tool.assert_not_awaited()


async def test_auth_off_forward_is_unchanged(async_client: AsyncClient, db_session, people, tmp_file, mail):
    upload = await _shared_thread_upload(db_session, people, tmp_file)

    _as(None)
    resp = await _forward(async_client, upload.id)

    assert resp.status_code == 200, resp.text
    mail.execute_tool.assert_awaited_once()
