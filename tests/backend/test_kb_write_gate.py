"""Write gate on caller-supplied knowledge bases (auth on).

Two endpoints take a ``knowledge_base_id`` from the caller and file a document
into it:

- ``POST /api/chat/upload/{id}/index`` checked only that the KB EXISTS;
- ``POST /api/knowledge/upload`` skipped the KB check for ``rag.manage``
  holders and for a KB id that resolves to no row.

The KB owner is granted every document in the KB and becomes the new
document's atom owner (``create_document_record``), so filing into a foreign
KB hands the content to that user. The target KB must be one the caller may
write: owner, ``kb.all``, or an explicit write/admin share.

Everything here runs against real Postgres with the real permission functions
(``check_kb_access``, ``share_kb``, ``get_user_kb_permission_level``, the
conversation circle filter). The only stub is the document pipeline itself
(Docling / the worker queue), which is not what is under test.
"""
from __future__ import annotations

import io
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select

from models.database import ChatUpload, Conversation, Document, KnowledgeBase, Role, User
from services.auth_service import get_optional_user
from services.kb_shares_service import get_user_kb_permission_level, share_kb
from services.rag_service import RAGService

pytestmark = [pytest.mark.backend, pytest.mark.database]


# ---------------------------------------------------------------------------
# Fixtures: three people, two KBs, one upload
# ---------------------------------------------------------------------------


async def _user(db, name: str, permissions: list[str]) -> User:
    role = Role(name=f"role-{name}", permissions=permissions, is_system=False)
    db.add(role)
    await db.flush()
    user = User(username=name, password_hash="x", role_id=role.id)
    db.add(user)
    await db.commit()
    await db.refresh(user, attribute_names=["role"])
    return user


@pytest.fixture
async def people(db_session):
    """uploader: a household member (kb.shared); owner: the foreign KB's owner;
    admin: kb.all; manager: rag.manage without kb.all."""
    return {
        "uploader": await _user(db_session, "uploader", ["kb.shared", "chat.own", "rag.use"]),
        "owner": await _user(db_session, "kbowner", ["kb.shared", "chat.own", "rag.use"]),
        "admin": await _user(db_session, "admin-x", ["admin", "kb.all", "rag.manage"]),
        "manager": await _user(db_session, "manager", ["kb.own", "rag.manage"]),
    }


async def _kb(db, name: str, owner: User) -> KnowledgeBase:
    kb = KnowledgeBase(name=name, owner_id=owner.id)
    db.add(kb)
    await db.commit()
    await db.refresh(kb)
    return kb


async def _seed_document(db, kb: KnowledgeBase, tag: str) -> Document:
    """A real document (with its atom) in ``kb`` — a KB share is stored as one
    grant per document atom, so a share needs a document to land on."""
    return await RAGService(db).create_document_record(
        file_path=f"/tmp/seed-{tag}.txt",
        knowledge_base_id=kb.id,
        filename=f"seed-{tag}.txt",
        file_hash=f"seed-{tag}",
    )


@pytest.fixture
def tmp_file():
    with tempfile.NamedTemporaryFile(suffix=".txt", delete=False) as f:
        f.write(b"content to index")
        path = f.name
    yield path
    Path(path).unlink(missing_ok=True)


async def _upload(db, *, session_id: str, conv_owner: User, path: str, tier: int = 0) -> ChatUpload:
    db.add(Conversation(session_id=session_id, user_id=conv_owner.id, circle_tier=tier))
    upload = ChatUpload(
        session_id=session_id, filename="u.txt", file_type="txt", file_size=16,
        file_hash=f"h-{session_id}", status="completed", file_path=path,
    )
    db.add(upload)
    await db.commit()
    await db.refresh(upload)
    return upload


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


def _fake_rag(db_session):
    """Stub only Docling: ``ingest_document`` returns a real document row."""
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


# ---------------------------------------------------------------------------
# POST /api/chat/upload/{id}/index — target KB
# ---------------------------------------------------------------------------


async def test_index_into_foreign_kb_is_refused(
    async_client: AsyncClient, db_session, people, auth_on, tmp_file,
):
    foreign = await _kb(db_session, "foreign", people["owner"])
    upload = await _upload(db_session, session_id="s-foreign", conv_owner=people["uploader"], path=tmp_file)

    _as(people["uploader"])
    resp, rag = await _index(async_client, db_session, upload.id, foreign.id)

    assert resp.status_code == 403, resp.text
    rag.ingest_document.assert_not_awaited()
    await db_session.refresh(upload)
    assert upload.document_id is None
    assert upload.knowledge_base_id is None


async def test_index_into_own_kb_is_allowed(
    async_client: AsyncClient, db_session, people, auth_on, tmp_file,
):
    """Negative control for the refusal above: the same caller, the same
    upload, the caller's OWN KB → 200. The 403 is the KB, nothing else."""
    own = await _kb(db_session, "own", people["uploader"])
    upload = await _upload(db_session, session_id="s-own", conv_owner=people["uploader"], path=tmp_file)

    _as(people["uploader"])
    resp, rag = await _index(async_client, db_session, upload.id, own.id)

    assert resp.status_code == 200, resp.text
    rag.ingest_document.assert_awaited_once()
    assert resp.json()["knowledge_base_id"] == own.id


async def test_index_into_kb_shared_with_write_is_allowed(
    async_client: AsyncClient, db_session, people, auth_on, tmp_file,
):
    shared = await _kb(db_session, "shared-w", people["owner"])
    await _seed_document(db_session, shared, "w")
    await share_kb(db_session, shared.id, people["uploader"].id, "write", granted_by=people["owner"].id)
    # The share is real, not assumed.
    assert await get_user_kb_permission_level(db_session, shared.id, people["uploader"].id) == "write"
    upload = await _upload(db_session, session_id="s-shared-w", conv_owner=people["uploader"], path=tmp_file)

    _as(people["uploader"])
    resp, _rag = await _index(async_client, db_session, upload.id, shared.id)

    assert resp.status_code == 200, resp.text


async def test_index_into_kb_shared_read_only_is_refused(
    async_client: AsyncClient, db_session, people, auth_on, tmp_file,
):
    shared = await _kb(db_session, "shared-r", people["owner"])
    await _seed_document(db_session, shared, "r")
    await share_kb(db_session, shared.id, people["uploader"].id, "read", granted_by=people["owner"].id)
    assert await get_user_kb_permission_level(db_session, shared.id, people["uploader"].id) == "read"
    upload = await _upload(db_session, session_id="s-shared-r", conv_owner=people["uploader"], path=tmp_file)

    _as(people["uploader"])
    resp, rag = await _index(async_client, db_session, upload.id, shared.id)

    assert resp.status_code == 403, resp.text
    rag.ingest_document.assert_not_awaited()


async def test_index_into_public_kb_is_refused(
    async_client: AsyncClient, db_session, people, auth_on, tmp_file,
):
    """A public KB is readable by kb.shared holders — not writable."""
    public = await _kb(db_session, "public", people["owner"])
    public.is_public = True
    await db_session.commit()
    upload = await _upload(db_session, session_id="s-public", conv_owner=people["uploader"], path=tmp_file)

    _as(people["uploader"])
    resp, _rag = await _index(async_client, db_session, upload.id, public.id)

    assert resp.status_code == 403, resp.text


async def test_admin_may_index_into_any_kb(
    async_client: AsyncClient, db_session, people, auth_on, tmp_file,
):
    foreign = await _kb(db_session, "foreign-admin", people["owner"])
    upload = await _upload(db_session, session_id="s-admin", conv_owner=people["admin"], path=tmp_file)

    _as(people["admin"])
    resp, _rag = await _index(async_client, db_session, upload.id, foreign.id)

    assert resp.status_code == 200, resp.text


async def test_index_unknown_kb_is_404(
    async_client: AsyncClient, db_session, people, auth_on, tmp_file,
):
    upload = await _upload(db_session, session_id="s-nokb", conv_owner=people["uploader"], path=tmp_file)

    _as(people["uploader"])
    resp, _rag = await _index(async_client, db_session, upload.id, 987654)

    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# POST /api/chat/upload/{id}/index — the upload itself
# ---------------------------------------------------------------------------


async def test_index_of_someone_elses_reachable_upload_is_refused(
    async_client: AsyncClient, db_session, people, auth_on, tmp_file,
):
    """The owner's conversation is public (tier 4), so the uploader REACHES the
    attachment — and may read it. Filing it into the uploader's own KB would
    make the uploader the owner of someone else's document: refused."""
    own = await _kb(db_session, "own-2", people["uploader"])
    upload = await _upload(
        db_session, session_id="s-theirs", conv_owner=people["owner"], path=tmp_file, tier=4,
    )

    _as(people["uploader"])
    # Reach is real: the read path serves the attachment.
    read = await async_client.get(f"/api/chat/upload/{upload.id}")
    assert read.status_code == 200, read.text

    resp, rag = await _index(async_client, db_session, upload.id, own.id)

    assert resp.status_code == 403, resp.text
    rag.ingest_document.assert_not_awaited()


async def test_index_of_unreachable_upload_is_404(
    async_client: AsyncClient, db_session, people, auth_on, tmp_file,
):
    own = await _kb(db_session, "own-3", people["uploader"])
    upload = await _upload(db_session, session_id="s-private", conv_owner=people["owner"], path=tmp_file)

    _as(people["uploader"])
    resp, _rag = await _index(async_client, db_session, upload.id, own.id)

    assert resp.status_code == 404


async def test_anonymous_index_is_refused_under_auth(
    async_client: AsyncClient, db_session, people, auth_on, tmp_file,
):
    own = await _kb(db_session, "own-anon", people["uploader"])
    upload = await _upload(db_session, session_id="s-anon", conv_owner=people["uploader"], path=tmp_file)

    _as(None)
    resp, rag = await _index(async_client, db_session, upload.id, own.id)

    assert resp.status_code == 401
    rag.ingest_document.assert_not_awaited()


async def test_anonymous_cannot_read_an_upload_under_auth(
    async_client: AsyncClient, db_session, people, auth_on, tmp_file,
):
    """``_get_owned_upload`` skipped its scope filter for ``user=None`` — under
    auth an anonymous GET returned any upload's text preview."""
    upload = await _upload(db_session, session_id="s-anon-read", conv_owner=people["owner"], path=tmp_file)

    _as(None)
    resp = await async_client.get(f"/api/chat/upload/{upload.id}")

    assert resp.status_code == 404


async def test_auth_off_index_is_unchanged(
    async_client: AsyncClient, db_session, people, tmp_file,
):
    """Auth off (single-user): no owner, no KB check — any KB, any upload."""
    foreign = await _kb(db_session, "foreign-authoff", people["owner"])
    upload = await _upload(db_session, session_id="s-authoff", conv_owner=people["owner"], path=tmp_file)

    _as(None)
    resp, _rag = await _index(async_client, db_session, upload.id, foreign.id)

    assert resp.status_code == 200, resp.text


# ---------------------------------------------------------------------------
# POST /api/knowledge/upload — same gap for rag.manage holders
# ---------------------------------------------------------------------------


async def _kb_upload(client, kb_id: int | None, name: str):
    url = "/api/knowledge/upload" + (f"?knowledge_base_id={kb_id}" if kb_id is not None else "")
    with patch(
        "api.routes.knowledge._worker_is_alive", new=AsyncMock(return_value=True),
    ), patch(
        "api.routes.knowledge.DocumentTaskQueue.enqueue", new=AsyncMock(return_value="1-0"),
    ):
        return await client.post(
            url, files=[("file", (name, io.BytesIO(name.encode()), "text/plain"))],
        )


async def _doc_count(db, kb_id: int) -> int:
    return (await db.execute(
        select(func.count()).select_from(Document).where(Document.knowledge_base_id == kb_id)
    )).scalar()


async def test_rag_manage_does_not_open_a_foreign_kb(
    async_client: AsyncClient, db_session, people, auth_on,
):
    foreign = await _kb(db_session, "foreign-km", people["owner"])

    _as(people["manager"])
    resp = await _kb_upload(async_client, foreign.id, "into-foreign.txt")

    assert resp.status_code == 403, resp.text
    assert await _doc_count(db_session, foreign.id) == 0


async def test_rag_manage_uploads_into_own_kb(
    async_client: AsyncClient, db_session, people, auth_on,
):
    """Negative control: the same manager into the manager's own KB → 202."""
    own = await _kb(db_session, "own-km", people["manager"])

    _as(people["manager"])
    resp = await _kb_upload(async_client, own.id, "into-own.txt")

    assert resp.status_code == 202, resp.text
    assert await _doc_count(db_session, own.id) == 1


async def test_member_without_share_cannot_upload_into_foreign_kb(
    async_client: AsyncClient, db_session, people, auth_on,
):
    foreign = await _kb(db_session, "foreign-member", people["owner"])

    _as(people["uploader"])
    resp = await _kb_upload(async_client, foreign.id, "member.txt")

    assert resp.status_code == 403, resp.text


async def test_member_with_write_share_uploads(
    async_client: AsyncClient, db_session, people, auth_on,
):
    shared = await _kb(db_session, "shared-km", people["owner"])
    await _seed_document(db_session, shared, "km")
    await share_kb(db_session, shared.id, people["uploader"].id, "write", granted_by=people["owner"].id)

    _as(people["uploader"])
    resp = await _kb_upload(async_client, shared.id, "shared.txt")

    assert resp.status_code == 202, resp.text


async def test_admin_uploads_into_any_kb(
    async_client: AsyncClient, db_session, people, auth_on,
):
    foreign = await _kb(db_session, "foreign-admin-km", people["owner"])

    _as(people["admin"])
    resp = await _kb_upload(async_client, foreign.id, "admin.txt")

    assert resp.status_code == 202, resp.text


async def test_upload_into_unknown_kb_is_404(
    async_client: AsyncClient, db_session, people, auth_on,
):
    """Was: ``if kb and …`` let a dangling id through to the INSERT."""
    _as(people["manager"])
    resp = await _kb_upload(async_client, 987654, "nowhere.txt")

    assert resp.status_code == 404, resp.text
