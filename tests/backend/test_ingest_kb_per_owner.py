"""One ingest inbox per person.

The circle filter grants the KB owner access to EVERY document in the KB
(``circle_sql`` owner branch on ``kb.owner_id``). Folder and email ingest all
default to one KB name ("Eingang"), so a single shared KB with an owner would
show that owner everyone else's documents. Instead:

- ingest resolves its target KB by (name, owner) and creates it for exactly
  that owner;
- invariant: an ingest path never files a document into a KB whose owner
  differs from the owner the document is filed under;
- no resolvable owner → the shared ownerless KB of that name (as before);
- an existing shared "Eingang" is left exactly as it is;
- other by-name lookups (chat uploads, meetings) survive a repeated name.
"""
import json
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

import services.email_ingest as email_svc
from models.database import KnowledgeBase
from services import ingest_credentials as ic
from services.folder_ingest import (
    IngestResult,
    IngestStatus,
    resolve_owner_user_id,
    resolve_target_kb,
    target_kb_exists,
)

pytestmark = [pytest.mark.backend, pytest.mark.database]


async def _kbs(db, name):
    return list((await db.execute(
        select(KnowledgeBase).where(KnowledgeBase.name == name).order_by(KnowledgeBase.id)
    )).scalars())


async def _shared(db, name="Eingang", owner=None):
    kb = KnowledgeBase(name=name, owner_id=owner)
    db.add(kb)
    await db.commit()
    await db.refresh(kb)
    return kb


# --- folder ingest: the resolver -------------------------------------------

async def test_each_owner_gets_their_own_inbox(db_session, make_user):
    await make_user(7)
    await make_user(8)
    a = await resolve_target_kb(db_session, "Eingang", owner_user_id=7)
    b = await resolve_target_kb(db_session, "Eingang", owner_user_id=8)
    assert a.id != b.id
    assert (a.owner_id, b.owner_id) == (7, 8)
    # Same owner again → the same inbox, no third row.
    assert (await resolve_target_kb(db_session, "Eingang", owner_user_id=7)).id == a.id
    assert len(await _kbs(db_session, "Eingang")) == 2


async def test_existing_shared_inbox_is_left_alone(db_session, make_user):
    await make_user(7)
    shared = await _shared(db_session)
    mine = await resolve_target_kb(db_session, "Eingang", owner_user_id=7)
    assert mine.id != shared.id and mine.owner_id == 7
    await db_session.refresh(shared)
    assert shared.owner_id is None  # not adopted, not moved


async def test_no_owner_uses_the_shared_ownerless_inbox(db_session):
    shared = await _shared(db_session)
    assert (await resolve_target_kb(db_session, "Eingang", owner_user_id=None)).id == shared.id
    assert len(await _kbs(db_session, "Eingang")) == 1


async def test_no_owner_never_lands_in_somebody_elses_inbox(db_session, make_user):
    await make_user(8)
    owned = await _shared(db_session, owner=8)
    kb = await resolve_target_kb(db_session, "Eingang", owner_user_id=None)
    assert kb.id != owned.id and kb.owner_id is None


async def test_owner_falls_back_to_the_target_user(db_session, make_user, monkeypatch):
    await make_user(8, username="bob")
    monkeypatch.setattr("services.folder_ingest.settings.folder_ingest_target_user", "bob")
    owner = await resolve_owner_user_id(db_session, None)
    assert (await resolve_target_kb(db_session, None, owner_user_id=owner)).owner_id == 8


async def test_two_ownerless_kbs_of_one_name_are_impossible(db_session):
    # NULLS NOT DISTINCT: without it NULL <> NULL would let a second shared
    # inbox slip in.
    await _shared(db_session)
    db_session.add(KnowledgeBase(name="Eingang"))
    with pytest.raises(IntegrityError):
        await db_session.commit()
    await db_session.rollback()


async def test_health_probe_survives_two_inboxes_of_one_name(db_session, make_user, monkeypatch):
    await make_user(7, username="alice")
    await make_user(8, username="bob")
    monkeypatch.setattr("services.folder_ingest.settings.folder_ingest_kb_name", "Eingang")
    monkeypatch.setattr("services.folder_ingest.settings.folder_ingest_target_user", "alice")
    await _shared(db_session)
    await _shared(db_session, owner=8)
    # Neither is the configured target user's inbox.
    assert await target_kb_exists(db_session) is False
    await _shared(db_session, owner=7)
    assert await target_kb_exists(db_session) is True


# --- folder push route: the invariant end to end ---------------------------

@pytest.fixture
async def push_client(db_session):
    from unittest.mock import MagicMock

    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient
    from slowapi.errors import RateLimitExceeded

    from api.routes import folder_ingest as route
    from services.api_rate_limiter import limiter, rate_limit_exceeded_handler
    from services.auth_service import get_current_user
    from services.database import get_db

    app = FastAPI()
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, rate_limit_exceeded_handler)
    app.include_router(route.router, prefix="/api/folder-ingest")

    async def _db():
        yield db_session

    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[get_current_user] = lambda: MagicMock(id=1, username="admin")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        yield c


async def _kb_owner(db, kb_id):
    return (await db.execute(
        select(KnowledgeBase.owner_id).where(KnowledgeBase.id == kb_id))).scalar_one()


async def test_push_files_into_the_credential_owners_inbox(
    db_session, make_user, push_client, monkeypatch
):
    from api.routes import folder_ingest as route

    await make_user(7, username="alice")
    await make_user(8, username="bob")
    bobs = await _shared(db_session, owner=8)  # somebody else's "Eingang"
    shared = await _shared(db_session)  # the legacy shared ownerless "Eingang"
    monkeypatch.setattr(ic.settings, "ingest_credentials_enabled", True, raising=False)
    monkeypatch.setattr(route.settings, "folder_ingest_enabled", True, raising=False)
    monkeypatch.setattr(route.settings, "folder_ingest_kb_name", "Eingang", raising=False)
    monkeypatch.setattr(route, "_worker_is_alive", AsyncMock(return_value=True))
    captured = {}

    async def _capture(*args, **kwargs):
        captured.update(kwargs)
        return IngestResult(IngestStatus.INGESTED, document_id=1)

    monkeypatch.setattr(route, "ingest_document", _capture)
    token = await ic.mint_credential(
        db_session, client_id="scanner", label="Scanner", route=ic.ROUTE_FOLDER,
        owner="alice")

    resp = await push_client.post(
        "/api/folder-ingest/document",
        headers={"Authorization": f"Bearer {token}"},
        files={"file": ("x.pdf", b"%PDF-1.4", "application/pdf")},
        data={"metadata": json.dumps({"filename": "x.pdf"})},
    )
    assert resp.status_code == 200, resp.text
    assert captured["owner_user_id"] == 7
    assert captured["kb_id"] not in (bobs.id, shared.id)
    # THE invariant: the KB's owner is the document's owner.
    assert await _kb_owner(db_session, captured["kb_id"]) == captured["owner_user_id"]


# --- email ingest -------------------------------------------------------------

def _mailboxes(monkeypatch, owner, captured):
    monkeypatch.setattr(email_svc.settings, "email_ingest_mailboxes_json", json.dumps(
        [{"id": "privat", "owner": owner, "tier": 0, "kb": "Eingang"}]))
    monkeypatch.setattr(email_svc, "_record_ledger", AsyncMock())

    async def _capture(*args, **kwargs):
        captured.update(kwargs)
        return IngestResult(IngestStatus.INGESTED, document_id=1)

    monkeypatch.setattr(email_svc, "ingest_document", _capture)


async def test_mailbox_files_into_its_owners_inbox(db_session, make_user, monkeypatch):
    await make_user(7, username="alice")
    await make_user(8, username="bob")
    bobs = await _shared(db_session, owner=8)
    captured = {}
    _mailboxes(monkeypatch, "alice", captured)
    await email_svc.ingest_email_document(
        b"%PDF-1.4", db=db_session, mailbox_id="privat", message_id="m1", filename="x.pdf")
    assert captured["owner_user_id"] == 7 and captured["kb_id"] != bobs.id
    assert await _kb_owner(db_session, captured["kb_id"]) == 7


async def test_ownerless_mailbox_uses_the_shared_inbox(db_session, monkeypatch):
    shared = await _shared(db_session)
    captured = {}
    _mailboxes(monkeypatch, "", captured)
    await email_svc.ingest_email_document(
        b"%PDF-1.4", db=db_session, mailbox_id="privat", message_id="m1", filename="x.pdf")
    assert captured["owner_user_id"] is None and captured["kb_id"] == shared.id


# --- other by-name lookups survive a repeated name --------------------------

async def test_chat_upload_default_kb_takes_the_oldest_of_a_repeated_name(
    db_session, make_user, monkeypatch
):
    from api.routes import chat_upload

    await make_user(8)
    monkeypatch.setattr(chat_upload.settings, "chat_upload_default_kb_name", "Chat Uploads")
    system = await _shared(db_session, name="Chat Uploads")
    await _shared(db_session, name="Chat Uploads", owner=8)  # a user's own KB
    assert (await chat_upload._get_or_create_default_kb(db_session)).id == system.id


async def test_meetings_kb_takes_the_oldest_of_a_repeated_name(db_session, make_user):
    from services import meeting_pipeline

    await make_user(8)
    system = await _shared(db_session, name=meeting_pipeline._MEETINGS_KB_NAME)
    await _shared(db_session, name=meeting_pipeline._MEETINGS_KB_NAME, owner=8)
    assert (await meeting_pipeline._resolve_meetings_kb(db_session)).id == system.id
    count = (await db_session.execute(select(func.count()).select_from(KnowledgeBase))).scalar()
    assert count == 2


# --- manual KB creation (POST /api/knowledge/bases) ------------------------

async def _create_via_route(db, user, name):
    from api.routes import knowledge as route
    from api.routes.knowledge_schemas import KnowledgeBaseCreate
    from services.rag_service import RAGService

    return await route.create_knowledge_base(
        KnowledgeBaseCreate(name=name), rag=RAGService(db), user=user, db=db
    )


async def test_creating_a_kb_twice_leaves_no_ownerless_orphan(
    db_session, make_user, monkeypatch
):
    # The owner is part of the one INSERT. Committing the row ownerless first
    # and setting the owner afterwards made the second attempt commit an
    # ownerless "Projekte" (no ownerless one existed yet) and only then fail
    # on the owner's own KB of that name -- the 400 left the orphan behind.
    from fastapi import HTTPException

    from api.routes import knowledge as route

    monkeypatch.setattr(route.settings, "auth_enabled", False)
    alice = await make_user(7, username="alice")
    first = await _create_via_route(db_session, alice, "Projekte")
    assert first.owner_id == 7
    with pytest.raises(HTTPException) as exc:
        await _create_via_route(db_session, alice, "Projekte")
    assert exc.value.status_code == 400
    await db_session.rollback()
    assert [kb.owner_id for kb in await _kbs(db_session, "Projekte")] == [7]


async def test_a_user_may_name_a_kb_like_the_shared_ownerless_one(
    db_session, make_user, monkeypatch
):
    from api.routes import knowledge as route

    monkeypatch.setattr(route.settings, "auth_enabled", False)
    alice = await make_user(7, username="alice")
    shared = await _shared(db_session)
    mine = await _create_via_route(db_session, alice, "Eingang")
    assert mine.id != shared.id and mine.owner_id == 7
