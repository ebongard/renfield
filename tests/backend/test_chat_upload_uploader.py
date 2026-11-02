"""#1459 — a chat upload records its uploader, and the uploader may file it.

Before, ``chat_uploads`` carried only ``session_id``: the conversation owner was
the only record of whose upload it was. In a shared room thread (the kitchen
device account owns the thread) a member who attached a file got 403 on
``/index`` and ``/email`` for their OWN attachment, while the device account
was let through.

Now:
- ``POST /api/chat/upload`` sets ``uploaded_by_user_id`` from the authenticated
  caller — never from a request field;
- ``/index`` and ``/email`` admit the uploader OR the conversation owner;
- a row without an uploader (written before the column existed) keeps the
  owner-only rule — nobody is guessed;
- reach is still checked first (an uploader who lost reach gets 404), and the
  target KB must still be writable.

Real Postgres, real circle filter, real memberships. Stubbed: the background
extraction task, Docling (``ingest_document``) and the Email MCP transport.
"""
from __future__ import annotations

import io
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from models.database import ChatUpload, CircleMembership, Conversation, Document, KnowledgeBase, Role, User
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
    """device: owns the kitchen thread; member + other: in device's circle at tier 2."""
    return {
        "device": await _user(db_session, "kitchen-device"),
        "member": await _user(db_session, "member"),
        "other": await _user(db_session, "other-member"),
    }


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


@pytest.fixture
def tmp_file():
    with tempfile.NamedTemporaryFile(suffix=".txt", delete=False) as f:
        f.write(b"attached file")
        path = f.name
    yield path
    Path(path).unlink(missing_ok=True)


@pytest.fixture
def mail():
    from main import app
    manager = MagicMock()
    manager.execute_tool = AsyncMock(return_value={"success": True})
    previous = getattr(app.state, "mcp_manager", None)
    app.state.mcp_manager = manager
    yield manager
    app.state.mcp_manager = previous


async def _room_thread(db, people, session_id: str = "s-kitchen", tier: int = 2) -> None:
    """The device's thread at ``tier``; member and other sit in its circle at 2."""
    db.add(Conversation(session_id=session_id, user_id=people["device"].id, circle_tier=tier))
    for who in ("member", "other"):
        db.add(CircleMembership(
            circle_owner_id=people["device"].id, member_user_id=people[who].id,
            dimension="tier", value=2, granted_by=people["device"].id,
        ))
    await db.commit()


async def _upload(db, path: str, uploader: User | None, session_id: str = "s-kitchen") -> ChatUpload:
    upload = ChatUpload(
        session_id=session_id, filename="u.txt", file_type="txt", file_size=13,
        file_hash=f"h-{uploader.id if uploader else 'none'}", status="completed",
        file_path=path, uploaded_by_user_id=uploader.id if uploader else None,
    )
    db.add(upload)
    await db.commit()
    await db.refresh(upload)
    return upload


async def _kb(db, owner: User, name: str) -> KnowledgeBase:
    kb = KnowledgeBase(name=name, owner_id=owner.id)
    db.add(kb)
    await db.commit()
    await db.refresh(kb)
    return kb


def _fake_rag(db_session):
    async def _ingest(**kw):
        doc = Document(
            filename=kw["filename"], file_path=kw["file_path"],
            knowledge_base_id=kw["knowledge_base_id"], status="completed",
        )
        db_session.add(doc)
        await db_session.flush()
        doc.chunk_count = 1
        return doc

    rag = MagicMock()
    rag.ingest_document = AsyncMock(side_effect=_ingest)
    return rag


async def _index(client, db_session, upload_id: int, kb_id: int):
    rag = _fake_rag(db_session)
    with patch("services.rag_service.RAGService", return_value=rag):
        resp = await client.post(
            f"/api/chat/upload/{upload_id}/index", json={"knowledge_base_id": kb_id},
        )
    return resp, rag


async def _post_upload(client, session_id: str, extra: dict | None = None):
    with patch("api.routes.chat_upload._extract_and_finalize"):
        return await client.post(
            "/api/chat/upload",
            files={"file": ("a.txt", io.BytesIO(b"attached"), "text/plain")},
            data={"session_id": session_id, **(extra or {})},
        )


async def _stored_uploader(db, upload_id: int) -> int | None:
    return (await db.execute(
        select(ChatUpload.uploaded_by_user_id).where(ChatUpload.id == upload_id)
    )).scalar_one()


# ---------------------------------------------------------------------------
# Recording the uploader
# ---------------------------------------------------------------------------


async def test_upload_records_the_authenticated_caller(
    async_client: AsyncClient, db_session, people, auth_on,
):
    await _room_thread(db_session, people)

    _as(people["member"])
    resp = await _post_upload(async_client, "s-kitchen")

    assert resp.status_code == 200, resp.text
    assert await _stored_uploader(db_session, resp.json()["id"]) == people["member"].id


async def test_uploader_is_never_taken_from_the_request(
    async_client: AsyncClient, db_session, people, auth_on,
):
    """Regression guard, NOT proof of the fix: today FastAPI drops the
    undeclared form field before the handler runs, so this cannot fail. It
    fails the day someone adds an ``uploaded_by_user_id`` Form parameter and
    lets it reach the row — the uploader must stay the authenticated caller."""
    await _room_thread(db_session, people)

    _as(people["member"])
    resp = await _post_upload(
        async_client, "s-kitchen", {"uploaded_by_user_id": str(people["other"].id)},
    )

    assert resp.status_code == 200, resp.text
    assert await _stored_uploader(db_session, resp.json()["id"]) == people["member"].id


async def test_auth_off_anonymous_upload_records_no_uploader(
    async_client: AsyncClient, db_session, people,
):
    _as(None)
    resp = await _post_upload(async_client, "s-authoff")

    assert resp.status_code == 200, resp.text
    assert await _stored_uploader(db_session, resp.json()["id"]) is None


# ---------------------------------------------------------------------------
# /index
# ---------------------------------------------------------------------------


async def test_member_may_index_own_attachment_from_a_room_thread(
    async_client: AsyncClient, db_session, people, auth_on, tmp_file,
):
    """The case of the issue: the member attached it in the device's thread
    and files it into their own KB."""
    await _room_thread(db_session, people)
    upload = await _upload(db_session, tmp_file, people["member"])
    own = await _kb(db_session, people["member"], "member-kb")

    _as(people["member"])
    resp, rag = await _index(async_client, db_session, upload.id, own.id)

    assert resp.status_code == 200, resp.text
    rag.ingest_document.assert_awaited_once()


async def test_legacy_row_without_uploader_keeps_owner_only(
    async_client: AsyncClient, db_session, people, auth_on, tmp_file,
):
    """Negative control for the test above: same thread, same caller, same KB —
    only the uploader is unknown (a row from before the column) → 403."""
    await _room_thread(db_session, people)
    upload = await _upload(db_session, tmp_file, None)
    own = await _kb(db_session, people["member"], "member-kb-legacy")

    _as(people["member"])
    resp, rag = await _index(async_client, db_session, upload.id, own.id)

    assert resp.status_code == 403, resp.text
    rag.ingest_document.assert_not_awaited()


async def test_another_members_attachment_is_still_refused(
    async_client: AsyncClient, db_session, people, auth_on, tmp_file,
):
    await _room_thread(db_session, people)
    upload = await _upload(db_session, tmp_file, people["other"])
    own = await _kb(db_session, people["member"], "member-kb-other")

    _as(people["member"])
    read = await async_client.get(f"/api/chat/upload/{upload.id}")
    assert read.status_code == 200, read.text  # the member REACHES it

    resp, rag = await _index(async_client, db_session, upload.id, own.id)

    assert resp.status_code == 403, resp.text
    rag.ingest_document.assert_not_awaited()


async def test_thread_owner_may_still_index_a_members_attachment(
    async_client: AsyncClient, db_session, people, auth_on, tmp_file,
):
    """The owner rule stands next to the uploader rule (issue: "uploader OR owner")."""
    await _room_thread(db_session, people)
    upload = await _upload(db_session, tmp_file, people["member"])
    device_kb = await _kb(db_session, people["device"], "device-kb")

    _as(people["device"])
    resp, _rag = await _index(async_client, db_session, upload.id, device_kb.id)

    assert resp.status_code == 200, resp.text


async def test_uploader_still_needs_write_on_the_target_kb(
    async_client: AsyncClient, db_session, people, auth_on, tmp_file,
):
    await _room_thread(db_session, people)
    upload = await _upload(db_session, tmp_file, people["member"])
    device_kb = await _kb(db_session, people["device"], "device-kb-2")

    _as(people["member"])
    resp, rag = await _index(async_client, db_session, upload.id, device_kb.id)

    assert resp.status_code == 403, resp.text
    assert "knowledge base" in resp.json()["detail"]
    rag.ingest_document.assert_not_awaited()


async def test_uploader_who_lost_reach_gets_404(
    async_client: AsyncClient, db_session, people, auth_on, tmp_file,
):
    """Reach comes first: the thread moved to tier 1 (closer than the member
    sits), so the member's own attachment is out of reach."""
    await _room_thread(db_session, people, tier=1)
    upload = await _upload(db_session, tmp_file, people["member"])
    own = await _kb(db_session, people["member"], "member-kb-noreach")

    _as(people["member"])
    resp, rag = await _index(async_client, db_session, upload.id, own.id)

    assert resp.status_code == 404, resp.text
    rag.ingest_document.assert_not_awaited()


# ---------------------------------------------------------------------------
# /email
# ---------------------------------------------------------------------------


async def _forward(client, upload_id: int):
    return await client.post(
        f"/api/chat/upload/{upload_id}/email", json={"to": "someone@example.org"},
    )


async def test_member_may_forward_own_attachment(
    async_client: AsyncClient, db_session, people, auth_on, tmp_file, mail,
):
    await _room_thread(db_session, people)
    upload = await _upload(db_session, tmp_file, people["member"])

    _as(people["member"])
    resp = await _forward(async_client, upload.id)

    assert resp.status_code == 200, resp.text
    mail.execute_tool.assert_awaited_once()


async def test_member_may_not_forward_a_legacy_attachment(
    async_client: AsyncClient, db_session, people, auth_on, tmp_file, mail,
):
    """Negative control for the test above: only the recorded uploader differs."""
    await _room_thread(db_session, people)
    upload = await _upload(db_session, tmp_file, None)

    _as(people["member"])
    resp = await _forward(async_client, upload.id)

    assert resp.status_code == 403, resp.text
    mail.execute_tool.assert_not_awaited()


# ---------------------------------------------------------------------------
# Account deletion
# ---------------------------------------------------------------------------


async def test_deleting_the_uploader_clears_the_column(db_session, people, tmp_file):
    """``ON DELETE SET NULL``: an attachment does not block deleting an account,
    and the row falls back to the owner-only rule."""
    await _room_thread(db_session, people)
    leaver = await _user(db_session, "leaver")
    upload = await _upload(db_session, tmp_file, leaver)
    upload_id = upload.id

    await db_session.delete(leaver)
    await db_session.commit()

    assert await _stored_uploader(db_session, upload_id) is None
