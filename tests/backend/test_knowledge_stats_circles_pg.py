"""``GET /api/knowledge/stats`` counts per caller.

With auth on, the endpoint returned global counts (documents, completed
documents, chunks, KBs) to every ``kb.own`` holder, so a user learned how much
the others store. It now counts, below ``kb.all``, documents and chunks through
the document reach filter of the document list (``visible_document_ids``) and
KBs through the KB-list rule (``_readable_knowledge_bases``). ``kb.all`` and
auth-off keep the global numbers. The per-KB ``document_count`` of
``GET /bases`` and ``GET /bases/{id}`` counts through the same filter.

Real Postgres: the filter is Postgres SQL (``circle_sql``).
"""
from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy import select, update

from models.database import TIER_PUBLIC, Document, DocumentChunk, KnowledgeBase, Role, User
from services.auth_service import get_optional_user
from services.kb_shares_service import share_kb
from services.rag_service import RAGService
from utils.config import settings

pytestmark = [pytest.mark.backend, pytest.mark.database]

TIER_SELF = 0


async def _user(db, name: str, permissions: list[str]) -> User:
    role = Role(name=f"role-{name}", permissions=permissions, is_system=False)
    db.add(role)
    await db.flush()
    user = User(username=name, password_hash="x", role_id=role.id)
    db.add(user)
    await db.commit()
    await db.refresh(user, attribute_names=["role"])
    return user


async def _kb(db, owner: User, name: str, *, public: bool = False) -> KnowledgeBase:
    kb = KnowledgeBase(name=name, owner_id=owner.id, default_circle_tier=TIER_SELF, is_public=public)
    db.add(kb)
    await db.commit()
    await db.refresh(kb)
    return kb


async def _doc(
    db, label: str, *, kb: KnowledgeBase | None = None, owner: User | None = None,
    chunks: int = 1, status: str = "completed",
) -> Document:
    """The real create path (atom owner = KB owner, or ``owner`` for a KB-less
    document), plus ``chunks`` chunk rows mirroring the document tier."""
    doc = await RAGService(db).create_document_record(
        file_path=f"/tmp/{label}.pdf", knowledge_base_id=kb.id if kb else None,
        filename=f"{label}.pdf", file_hash=f"h-{label}",
        owner_user_id_override=None if kb else owner.id,
        circle_tier_override=None if kb else TIER_SELF,
    )
    await db.execute(update(Document).where(Document.id == doc.id).values(status=status))
    db.add_all([
        DocumentChunk(document_id=doc.id, content=f"{label} {i}", chunk_index=i,
                      circle_tier=doc.circle_tier)
        for i in range(chunks)
    ])
    await db.commit()
    return doc


@pytest.fixture
async def world(db_session):
    """alice: private KB (2 docs, one pending; 3 chunks), a KB shared to carol
    (1 doc, 2 chunks), a public KB (1 doc, 1 chunk), a KB-less doc (1 chunk).
    carol: own KB (1 doc, 1 chunk). dave: kb.shared. admin: kb.all."""
    alice = await _user(db_session, "alice-st", ["kb.own"])
    carol = await _user(db_session, "carol-st", ["kb.own"])
    dave = await _user(db_session, "dave-st", ["kb.shared"])
    admin = await _user(db_session, "admin-st", ["kb.all"])

    private = await _kb(db_session, alice, "Alice privat")
    await _doc(db_session, "a-priv-1", kb=private, chunks=2)
    await _doc(db_session, "a-priv-2", kb=private, chunks=1, status="pending")
    shared = await _kb(db_session, alice, "Alice geteilt")
    await _doc(db_session, "a-shared", kb=shared, chunks=2)
    await share_kb(db_session, shared.id, carol.id, "read", granted_by=alice.id)
    await db_session.commit()
    public = await _kb(db_session, alice, "Alice oeffentlich", public=True)
    await _doc(db_session, "a-public", kb=public, chunks=1)
    await _doc(db_session, "a-kbless", owner=alice, chunks=1)

    carol_kb = await _kb(db_session, carol, "Carol privat")
    await _doc(db_session, "c-own", kb=carol_kb, chunks=1)
    return {"alice": alice, "carol": carol, "dave": dave, "admin": admin}


@pytest.fixture
def auth_on(monkeypatch):
    monkeypatch.setattr(settings, "auth_enabled", True)


def _as(user: User | None):
    from main import app
    app.dependency_overrides[get_optional_user] = lambda: user


@pytest.fixture(autouse=True)
def _clear_user_override():
    yield
    from main import app
    app.dependency_overrides.pop(get_optional_user, None)


async def _stats(client: AsyncClient) -> tuple[int, int, int, int]:
    resp = await client.get("/api/knowledge/stats")
    assert resp.status_code == 200, resp.text
    d = resp.json()
    return (d["document_count"], d["completed_documents"], d["chunk_count"],
            d["knowledge_base_count"])


GLOBAL = (6, 5, 8, 4)  # docs, completed, chunks, KBs over the whole world


# --------------------------------------------------------------------------- #
async def test_owner_counts_own_data_only(async_client, world, auth_on):
    """alice: her 4 KB docs + the KB-less one; not carol's KB or document."""
    _as(world["alice"])
    assert await _stats(async_client) == (5, 4, 7, 3)


async def test_other_user_counts_own_plus_shared_only(async_client, world, auth_on):
    """The leak: carol (kb.own) saw the global numbers. Now: her own KB + the
    KB alice shared with her; alice's private, public and KB-less data stay out."""
    _as(world["carol"])
    assert await _stats(async_client) == (2, 2, 3, 2)


async def test_public_kb_counts_for_kb_shared_as_in_the_kb_list(async_client, world, auth_on):
    """dave (kb.shared, no own data): the public KB shows in his KB list, so it
    counts as a KB. Its document is tier self, so the document filter (the
    same one as the document list, cf. #1469) does not count it."""
    _as(world["dave"])
    assert await _stats(async_client) == (0, 0, 0, 1)
    resp = await async_client.get("/api/knowledge/bases")
    assert len(resp.json()) == 1  # the KB count matches the KB list


async def test_kb_all_counts_globally(async_client, world, auth_on):
    _as(world["admin"])
    assert await _stats(async_client) == GLOBAL


async def test_auth_off_counts_globally(async_client, world, monkeypatch):
    monkeypatch.setattr(settings, "auth_enabled", False)
    _as(None)
    assert await _stats(async_client) == GLOBAL


async def test_auth_on_without_user_is_refused(async_client, world, auth_on):
    _as(None)
    assert (await async_client.get("/api/knowledge/stats")).status_code == 401


async def test_kb_none_is_refused(async_client, world, auth_on, db_session):
    _as(await _user(db_session, "nobody-st", ["kb.none"]))
    assert (await async_client.get("/api/knowledge/stats")).status_code == 403


async def test_enforcing_without_kb_ids_fails_closed(db_session, world):
    """A per-caller count without the caller's KB set must not fall back to the
    global KB count."""
    with pytest.raises(ValueError):
        await RAGService(db_session).get_stats(
            asker_id=world["carol"].id, enforce_circles=True, knowledge_base_ids=None
        )


# --------------------------------------------------------------------------- #
# The per-KB ``document_count`` of ``GET /bases`` and ``GET /bases/{id}`` is the
# same class: a readable KB of another user must not reveal how many documents
# its owner keeps there out of the caller's reach.

async def _kb_counts(client: AsyncClient) -> dict[str, int]:
    resp = await client.get("/api/knowledge/bases")
    assert resp.status_code == 200, resp.text
    return {kb["name"]: kb["document_count"] for kb in resp.json()}


async def _one_kb_count(client: AsyncClient, db_session, name: str) -> int:
    kb_id = (await db_session.execute(
        select(KnowledgeBase.id).where(KnowledgeBase.name == name)
    )).scalar_one()
    resp = await client.get(f"/api/knowledge/bases/{kb_id}")
    assert resp.status_code == 200, resp.text
    return resp.json()["document_count"]


async def test_public_kb_of_another_user_shows_only_reachable_documents(
    async_client, db_session, world, auth_on
):
    """dave (kb.shared) sees alice's public KB, but its one document is tier
    self: the count is 0, in the list and on the detail route."""
    _as(world["dave"])
    assert await _kb_counts(async_client) == {"Alice oeffentlich": 0}
    assert await _one_kb_count(async_client, db_session, "Alice oeffentlich") == 0


async def test_kb_document_count_rises_with_reach(async_client, db_session, world, auth_on):
    """Negative control on the filter itself: once the public KB's document is
    on the public tier, dave's count follows -- the filter counts reach, it
    does not just zero foreign KBs."""
    await db_session.execute(
        update(Document).where(Document.filename == "a-public.pdf").values(circle_tier=TIER_PUBLIC)
    )
    await db_session.commit()
    _as(world["dave"])
    assert await _kb_counts(async_client) == {"Alice oeffentlich": 1}
    assert await _one_kb_count(async_client, db_session, "Alice oeffentlich") == 1


async def test_owner_and_share_counts_are_whole(async_client, db_session, world, auth_on):
    _as(world["alice"])
    assert await _kb_counts(async_client) == {
        "Alice privat": 2, "Alice geteilt": 1, "Alice oeffentlich": 1,
    }
    _as(world["carol"])
    assert await _kb_counts(async_client) == {"Alice geteilt": 1, "Carol privat": 1}
    assert await _one_kb_count(async_client, db_session, "Alice geteilt") == 1


async def test_kb_all_and_auth_off_count_kb_documents_globally(
    async_client, db_session, world, auth_on, monkeypatch
):
    expected = {"Alice privat": 2, "Alice geteilt": 1, "Alice oeffentlich": 1, "Carol privat": 1}
    _as(world["admin"])
    assert await _kb_counts(async_client) == expected
    assert await _one_kb_count(async_client, db_session, "Alice privat") == 2
    monkeypatch.setattr(settings, "auth_enabled", False)
    _as(None)
    assert await _kb_counts(async_client) == expected
    assert await _one_kb_count(async_client, db_session, "Alice privat") == 2



async def test_get_knowledge_base_loads_documents_only_on_request(db_session, world):
    """``get_knowledge_base`` no longer eager-loads documents by default (the
    detail route counts per caller instead); the KB delete asks for them with
    ``with_documents=True`` because it removes their files."""
    kb = await _kb(db_session, world["carol"], "Carol weg")
    doc = await _doc(db_session, "c-weg", kb=kb)
    kb_id, doc_id = kb.id, doc.id
    db_session.expunge_all()

    rag = RAGService(db_session)
    loaded = await rag.get_knowledge_base(kb_id, with_documents=True)
    assert [d.id for d in loaded.documents] == [doc_id]
    db_session.expunge_all()
    bare = await rag.get_knowledge_base(kb_id)
    assert bare.id == kb_id
    assert "documents" not in bare.__dict__  # not loaded
