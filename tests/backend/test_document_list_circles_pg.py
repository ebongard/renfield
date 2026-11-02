"""The recency list of ``GET /api/knowledge/documents`` honours document reach.

With auth on, the ``q`` branch ran through the circle gate of
``search_documents``, and a ``knowledge_base_id`` went through
``check_kb_access``. The plain recency list (no ``q``, no KB id) filtered only
by status: anyone holding ``kb.own`` got every user's document rows (filename,
title, generated title, KB, status, dates).

It now applies the same document filter as the search gate
(``document_search.visible_document_ids``), in SQL before LIMIT/OFFSET.
``kb.all`` and auth-off keep listing everything; a KB id with KB read access
keeps listing the whole KB.

Real Postgres: the filter is Postgres SQL (``circle_sql``).
"""
from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy import text, update

from models.database import CircleMembership, Document, KnowledgeBase, Role, User
from services.auth_service import get_optional_user
from services.document_search import search_documents, visible_document_ids
from services.kb_shares_service import share_kb
from services.rag_service import RAGService
from utils.config import settings

pytestmark = [pytest.mark.backend, pytest.mark.database]

TIER_SELF = 0
TIER_HOUSEHOLD = 2


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
    """alice and carol hold kb.own (the leak needed nothing more); admin kb.all."""
    return {
        "alice": await _user(db_session, "alice-dl", ["kb.own"]),
        "carol": await _user(db_session, "carol-dl", ["kb.own"]),
        "admin": await _user(db_session, "admin-dl", ["kb.all"]),
    }


async def _kb(db, owner: User, name: str, tier: int = TIER_SELF) -> KnowledgeBase:
    kb = KnowledgeBase(name=name, owner_id=owner.id, default_circle_tier=tier)
    db.add(kb)
    await db.commit()
    await db.refresh(kb)
    return kb


async def _doc(db, kb: KnowledgeBase, label: str) -> Document:
    """The real create path: atom owner = KB owner, tier = KB default."""
    doc = await RAGService(db).create_document_record(
        file_path=f"/tmp/{label}.pdf", knowledge_base_id=kb.id,
        filename=f"{label}.pdf", file_hash=f"h-{label}",
    )
    await db.execute(update(Document).where(Document.id == doc.id).values(status="completed"))
    await db.commit()
    return doc


async def _kbless_doc(db, owner: User, label: str) -> Document:
    """A document with no KB (global RAG / ingest): ``kb.owner_id`` is NULL, so
    only the atom-owner fallback of the owner branch reaches it."""
    doc = await RAGService(db).create_document_record(
        file_path=f"/tmp/{label}.pdf", knowledge_base_id=None,
        filename=f"{label}.pdf", file_hash=f"h-{label}",
        owner_user_id_override=owner.id, circle_tier_override=TIER_SELF,
    )
    await db.execute(update(Document).where(Document.id == doc.id).values(status="completed"))
    await db.commit()
    return doc


async def _grant(db, doc: Document, to_user: User, by_user: User) -> None:
    """A per-document explicit grant (branch 3), as the legacy share wrote it."""
    await db.execute(
        text("INSERT INTO atom_explicit_grants "
             "(atom_id, granted_to_user_id, permission_level, granted_by, granted_at) "
             "VALUES (:a, :to, 'read', :by, NOW())"),
        {"a": doc.atom_id, "to": to_user.id, "by": by_user.id},
    )
    await db.commit()


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


async def _listed(client: AsyncClient, **params) -> list[int]:
    resp = await client.get("/api/knowledge/documents", params=params)
    assert resp.status_code == 200, resp.text
    return [row["id"] for row in resp.json()]


# --------------------------------------------------------------------------- #
async def test_private_document_of_another_user_is_not_listed(
    async_client, db_session, people, auth_on
):
    """The leak: carol (kb.own, no share, no circle) listed alice's private row."""
    kb = await _kb(db_session, people["alice"], "Alice privat")
    private = await _doc(db_session, kb, "alice-steuer")
    carol_kb = await _kb(db_session, people["carol"], "Carol privat")
    own = await _doc(db_session, carol_kb, "carol-eigen")

    _as(people["carol"])
    ids = await _listed(async_client)
    assert private.id not in ids
    assert own.id in ids  # negative control: the filter did not just empty the list


async def test_owner_lists_own_private_document(async_client, db_session, people, auth_on):
    kb = await _kb(db_session, people["alice"], "Alice privat")
    private = await _doc(db_session, kb, "alice-own")

    _as(people["alice"])
    assert private.id in await _listed(async_client)


async def test_kb_share_opens_the_list(async_client, db_session, people, auth_on):
    kb = await _kb(db_session, people["alice"], "Alice geteilt")
    doc = await _doc(db_session, kb, "alice-geteilt")
    await share_kb(db_session, kb.id, people["carol"].id, "read", granted_by=people["alice"].id)
    await db_session.commit()

    _as(people["carol"])
    assert doc.id in await _listed(async_client)


async def test_circle_membership_opens_the_list(async_client, db_session, people, auth_on):
    kb = await _kb(db_session, people["alice"], "Alice Haushalt", tier=TIER_HOUSEHOLD)
    doc = await _doc(db_session, kb, "alice-haushalt")
    db_session.add(CircleMembership(
        circle_owner_id=people["alice"].id, member_user_id=people["carol"].id,
        dimension="tier", value=TIER_HOUSEHOLD, granted_by=people["alice"].id,
    ))
    await db_session.commit()

    _as(people["carol"])
    assert doc.id in await _listed(async_client)


async def test_kb_all_lists_everything(async_client, db_session, people, auth_on):
    a = await _doc(db_session, await _kb(db_session, people["alice"], "A"), "a-priv")
    c = await _doc(db_session, await _kb(db_session, people["carol"], "C"), "c-priv")

    _as(people["admin"])
    ids = await _listed(async_client)
    assert {a.id, c.id} <= set(ids)


async def test_auth_off_lists_everything(async_client, db_session, people, monkeypatch):
    monkeypatch.setattr(settings, "auth_enabled", False)
    a = await _doc(db_session, await _kb(db_session, people["alice"], "A"), "a-off")
    c = await _doc(db_session, await _kb(db_session, people["carol"], "C"), "c-off")

    _as(None)
    ids = await _listed(async_client)
    assert {a.id, c.id} <= set(ids)


async def test_kb_id_with_kb_read_access_lists_the_whole_kb(
    async_client, db_session, people, auth_on
):
    """Unchanged: a KB the caller may read lists all of its documents (the KB
    check is the gate there, not the per-document filter)."""
    kb = await _kb(db_session, people["alice"], "Alice geteilt KB")
    doc = await _doc(db_session, kb, "alice-kb")
    await share_kb(db_session, kb.id, people["carol"].id, "read", granted_by=people["alice"].id)
    await db_session.commit()

    _as(people["carol"])
    assert await _listed(async_client, knowledge_base_id=kb.id) == [doc.id]


async def test_kb_id_without_access_is_refused(async_client, db_session, people, auth_on):
    kb = await _kb(db_session, people["alice"], "Alice zu")
    await _doc(db_session, kb, "alice-zu")

    _as(people["carol"])
    resp = await async_client.get("/api/knowledge/documents", params={"knowledge_base_id": kb.id})
    assert resp.status_code == 403


async def test_pagination_and_sort_run_after_the_filter(
    async_client, db_session, people, auth_on
):
    """The filter sits in SQL before LIMIT/OFFSET: hidden rows interleaved with
    visible ones neither leave holes in a page nor shift the next page."""
    alice_kb = await _kb(db_session, people["alice"], "Alice viele")
    carol_kb = await _kb(db_session, people["carol"], "Carol viele")
    visible: list[int] = []
    for i in range(5):
        await _doc(db_session, alice_kb, f"hidden-{i}")
        visible.append((await _doc(db_session, carol_kb, f"vis-{i}")).id)

    _as(people["carol"])
    page1 = await _listed(async_client, limit=2, offset=0)
    page2 = await _listed(async_client, limit=2, offset=2)
    page3 = await _listed(async_client, limit=2, offset=4)
    assert page1 + page2 + page3 == await _listed(async_client, limit=100)
    assert sorted(page1 + page2 + page3) == sorted(visible)
    assert len(page1) == 2 and len(page2) == 2 and len(page3) == 1

    by_name = await _listed(async_client, sort="name", order="asc", limit=3)
    assert by_name == [visible[0], visible[1], visible[2]]  # vis-0 < vis-1 < vis-2


async def test_kbless_document_reaches_only_its_atom_owner(
    async_client, db_session, people, auth_on
):
    doc = await _kbless_doc(db_session, people["alice"], "alice-ohne-kb")

    _as(people["alice"])
    assert doc.id in await _listed(async_client)
    _as(people["carol"])
    assert doc.id not in await _listed(async_client)


async def test_explicit_grant_opens_the_list(async_client, db_session, people, auth_on):
    kb = await _kb(db_session, people["alice"], "Alice Einzelgrant")
    granted = await _doc(db_session, kb, "alice-grant")
    other = await _doc(db_session, kb, "alice-kein-grant")
    await _grant(db_session, granted, people["carol"], people["alice"])

    _as(people["carol"])
    ids = await _listed(async_client)
    assert granted.id in ids
    assert other.id not in ids  # the grant covers its one document, not the KB


async def test_recency_set_equals_the_search_gate_set(
    async_client, db_session, people, auth_on
):
    """Parity: one predicate for both branches. Every reach branch plus hidden
    rows; all filenames carry the token the search matches by name."""
    alice, carol = people["alice"], people["carol"]
    private_kb = await _kb(db_session, alice, "P privat")
    await _doc(db_session, private_kb, "parity-hidden")
    await _kbless_doc(db_session, alice, "parity-hidden-kbless")
    shared_kb = await _kb(db_session, alice, "P geteilt")
    await _doc(db_session, shared_kb, "parity-shared")
    await share_kb(db_session, shared_kb.id, carol.id, "read", granted_by=alice.id)
    house_kb = await _kb(db_session, alice, "P Haushalt", tier=TIER_HOUSEHOLD)
    await _doc(db_session, house_kb, "parity-house")
    db_session.add(CircleMembership(
        circle_owner_id=alice.id, member_user_id=carol.id,
        dimension="tier", value=TIER_HOUSEHOLD, granted_by=alice.id,
    ))
    grant_kb = await _kb(db_session, alice, "P Grant")
    await _grant(db_session, await _doc(db_session, grant_kb, "parity-grant"), carol, alice)
    await _doc(db_session, await _kb(db_session, carol, "P eigen"), "parity-own")
    await db_session.commit()

    _as(carol)
    listed = set(await _listed(async_client, limit=500))
    gate = {r[0] for r in (await db_session.execute(visible_document_ids(carol.id))).all()}
    searched = {d.id for d in await search_documents(
        db_session, "parity", asker_id=carol.id, enforce_circles=True, limit=500,
    )}
    assert len(listed) == 4  # shared, house, grant, own
    assert listed == gate == searched
