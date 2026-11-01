"""``POST /api/chat/upload`` — the caller must be allowed to WRITE the session.

The endpoint took any ``session_id`` from the form: under auth a user could
attach a file to another user's conversation (and get the ``upload_processed``
push, with its text preview, sent into that user's chat).

The rule is the chat's own message-write rule
(``ConversationService.save_message`` with ``enforce_ownership``): an existing
conversation must be within REACH (``ConversationService.reaches`` — the
four-branch circle filter); a session with no row yet is a brand-new one the
client minted, allowed. Real Postgres, real circle filter, real memberships;
the background extraction task is stubbed (it is not what is under test).
"""
from __future__ import annotations

import io
from unittest.mock import patch

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select

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
    return {"me": await _user(db_session, "me"), "other": await _user(db_session, "other")}


async def _conversation(db, session_id: str, owner: User | None, tier: int = 0) -> None:
    db.add(Conversation(session_id=session_id, user_id=owner.id if owner else None, circle_tier=tier))
    await db.commit()


@pytest.fixture
def auth_on(monkeypatch):
    from utils.config import settings
    monkeypatch.setattr(settings, "auth_enabled", True)


def _as(user: User | None):
    from main import app
    app.dependency_overrides[get_optional_user] = lambda: user


@pytest.fixture(autouse=True)
def _clear_user_override():
    yield
    from main import app
    app.dependency_overrides.pop(get_optional_user, None)


async def _post(client, session_id: str):
    with patch("api.routes.chat_upload._extract_and_finalize"):
        return await client.post(
            "/api/chat/upload",
            files={"file": ("a.txt", io.BytesIO(b"attached"), "text/plain")},
            data={"session_id": session_id},
        )


async def _rows(db, session_id: str) -> int:
    return (await db.execute(
        select(func.count()).select_from(ChatUpload).where(ChatUpload.session_id == session_id)
    )).scalar()


async def test_upload_into_own_conversation_is_allowed(async_client: AsyncClient, db_session, people, auth_on):
    await _conversation(db_session, "s-mine", people["me"])

    _as(people["me"])
    resp = await _post(async_client, "s-mine")

    assert resp.status_code == 200, resp.text
    assert await _rows(db_session, "s-mine") == 1


async def test_upload_into_foreign_private_conversation_is_refused(
    async_client: AsyncClient, db_session, people, auth_on,
):
    """Negative control is the test above: same caller, same file, own thread → 200."""
    await _conversation(db_session, "s-theirs", people["other"])

    _as(people["me"])
    resp = await _post(async_client, "s-theirs")

    assert resp.status_code == 403, resp.text
    assert await _rows(db_session, "s-theirs") == 0


async def test_upload_into_unknown_session_is_allowed(async_client: AsyncClient, db_session, people, auth_on):
    """A fresh browser chat: the client minted the id, the first message has not
    created the row yet — the user attaches first. Same as ``save_message``."""
    _as(people["me"])
    resp = await _post(async_client, "s-brand-new")

    assert resp.status_code == 200, resp.text
    assert await _rows(db_session, "s-brand-new") == 1


async def test_upload_into_reachable_shared_conversation_is_allowed(
    async_client: AsyncClient, db_session, people, auth_on,
):
    """A household room thread: the owner's conversation at tier 2, the caller in
    the owner's circle at tier 2 → the caller may post there, so may attach."""
    await _conversation(db_session, "s-room", people["other"], tier=2)
    db_session.add(CircleMembership(
        circle_owner_id=people["other"].id, member_user_id=people["me"].id,
        dimension="tier", value=2, granted_by=people["other"].id,
    ))
    await db_session.commit()

    _as(people["me"])
    resp = await _post(async_client, "s-room")

    assert resp.status_code == 200, resp.text


async def test_shared_conversation_out_of_tier_is_refused(
    async_client: AsyncClient, db_session, people, auth_on,
):
    """Same membership, but the thread is at tier 1 (closer than the caller
    sits) → out of reach → refused. Proves the membership is what admitted
    the test above."""
    await _conversation(db_session, "s-close", people["other"], tier=1)
    db_session.add(CircleMembership(
        circle_owner_id=people["other"].id, member_user_id=people["me"].id,
        dimension="tier", value=2, granted_by=people["other"].id,
    ))
    await db_session.commit()

    _as(people["me"])
    resp = await _post(async_client, "s-close")

    assert resp.status_code == 403, resp.text


async def test_upload_into_ownerless_conversation_is_refused(
    async_client: AsyncClient, db_session, people, auth_on,
):
    """An auth-off-era row with no owner reaches nobody — as for chat writes."""
    await _conversation(db_session, "s-legacy", None)

    _as(people["me"])
    resp = await _post(async_client, "s-legacy")

    assert resp.status_code == 403, resp.text


async def test_anonymous_upload_is_401(async_client: AsyncClient, db_session, people, auth_on):
    _as(None)
    resp = await _post(async_client, "s-anon")

    assert resp.status_code == 401


async def test_auth_off_upload_into_any_session_is_unchanged(async_client: AsyncClient, db_session, people):
    await _conversation(db_session, "s-authoff", people["other"])

    _as(None)
    resp = await _post(async_client, "s-authoff")

    assert resp.status_code == 200, resp.text
