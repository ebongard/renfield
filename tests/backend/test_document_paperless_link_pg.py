"""The document API serves the Paperless link only with `mcp.paperless.read`.

The circles decide whether a member sees a knowledge-base document. The
Paperless link names the document's Paperless id, and that is the Paperless
permission's to give (`services/paperless_visibility.py` -- the same rule as
the chat sources). Here: bob reaches alice's document through a KB share;
whether he also gets its link depends on his role alone. Single, list and
batch all build their rows through `_doc_to_response_kwargs`, and each is
driven through the real route on real Postgres.
"""
from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy import update

from models.database import Document, KnowledgeBase, Role, User
from services.auth_service import get_optional_user
from services.kb_shares_service import share_kb
from services.rag_service import RAGService
from utils.config import settings

pytestmark = [pytest.mark.backend, pytest.mark.database]

PUBLIC = "https://paperless.example.org"
PAPERLESS_ID = 7001
LINK = f"{PUBLIC}/documents/{PAPERLESS_ID}/details"


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
async def filed(db_session, monkeypatch):
    """alice's private document, filed in Paperless, in a KB she shares."""
    monkeypatch.setattr(settings, "auth_enabled", True)
    monkeypatch.setattr(settings, "paperless_public_url", PUBLIC, raising=False)
    alice = await _user(db_session, "alice-pl", ["kb.own", "mcp.paperless.read"])
    kb = KnowledgeBase(name="Alice privat", owner_id=alice.id, default_circle_tier=0)
    db_session.add(kb)
    await db_session.commit()
    await db_session.refresh(kb)
    doc = await RAGService(db_session).create_document_record(
        file_path="/tmp/pl-link.pdf", knowledge_base_id=kb.id,
        filename="pl-link.pdf", file_hash="h-pl-link",
    )
    await db_session.execute(
        update(Document).where(Document.id == doc.id).values(
            status="completed", paperless_document_id=PAPERLESS_ID, paperless_state="done",
        )
    )
    await db_session.commit()
    return {"alice": alice, "kb": kb, "doc_id": doc.id}


async def _shared_reader(db, filed, permissions: list[str]) -> User:
    bob = await _user(db, f"bob-{'-'.join(permissions) or 'none'}", ["kb.own", *permissions])
    await share_kb(db, filed["kb"].id, bob.id, "read", granted_by=filed["alice"].id)
    await db.commit()
    return bob


def _as(user: User | None):
    from main import app
    app.dependency_overrides[get_optional_user] = lambda: user


@pytest.fixture(autouse=True)
def _clear_user_override():
    yield
    from main import app
    app.dependency_overrides.pop(get_optional_user, None)


async def _rows(client: AsyncClient, filed) -> dict[str, dict]:
    """The document as single, list and batch serve it to the current user."""
    doc_id = filed["doc_id"]
    single = await client.get(f"/api/knowledge/documents/{doc_id}")
    listed = await client.get(
        "/api/knowledge/documents", params={"knowledge_base_id": filed["kb"].id},
    )
    batch = await client.get("/api/knowledge/documents/batch", params={"ids": str(doc_id)})
    for name, resp in (("single", single), ("list", listed), ("batch", batch)):
        assert resp.status_code == 200, (name, resp.text)
    [list_row] = [r for r in listed.json() if r["id"] == doc_id]
    [batch_row] = batch.json()
    return {"single": single.json(), "list": list_row, "batch": batch_row}


class TestTheDocumentApiServesThePaperlessLinkOnlyWithThePermission:
    async def test_a_member_who_sees_the_document_without_the_permission_gets_no_link(
        self, async_client, db_session, filed,
    ):
        bob = await _shared_reader(db_session, filed, [])
        _as(bob)

        rows = await _rows(async_client, filed)

        for name, row in rows.items():
            assert row["id"] == filed["doc_id"], name  # he does see the document ...
            assert row["paperless_url"] is None, name  # ... but not its link
            assert row["in_paperless"] is True, name   # the filing status stays
            # ... and the client learns the link was WITHHELD, not that the
            # document is a duplicate without an id of its own.
            assert row["paperless_link_withheld"] is True, name
            assert str(PAPERLESS_ID) not in repr(row), name

    @pytest.mark.parametrize("permission", ["mcp.paperless.read", "mcp.paperless", "mcp.*"])
    async def test_with_the_permission_the_link_is_there(
        self, async_client, db_session, filed, permission,
    ):
        bob = await _shared_reader(db_session, filed, [permission])
        _as(bob)

        rows = await _rows(async_client, filed)

        for name, row in rows.items():
            assert row["paperless_url"] == LINK, name
            assert row["paperless_link_withheld"] is False, name

    async def test_the_owner_with_the_permission_gets_the_link(
        self, async_client, db_session, filed,
    ):
        _as(filed["alice"])

        for name, row in (await _rows(async_client, filed)).items():
            assert row["paperless_url"] == LINK, name

    async def test_auth_off_is_unchanged(self, async_client, db_session, filed, monkeypatch):
        monkeypatch.setattr(settings, "auth_enabled", False)
        _as(None)

        for name, row in (await _rows(async_client, filed)).items():
            assert row["paperless_url"] == LINK, name


class TestTheTierChangeAnswersWithTheCallersLink:
    """`PATCH /documents/{id}/tier` re-reads the document after
    `db.expire_all()`. The caller's row lives on the same request session, so
    its permissions must be decided BEFORE that -- after it, reading the role
    is a lazy load outside the greenlet, which the visibility check would
    swallow into a denial and a false warning."""

    @staticmethod
    def _warnings():
        from loguru import logger

        lines: list[str] = []
        sink = logger.add(lambda m: lines.append(str(m)), level="WARNING")
        return lines, lambda: logger.remove(sink)

    async def test_a_permitted_owner_gets_the_link_and_no_warning(
        self, async_client, db_session, filed,
    ):
        _as(filed["alice"])
        lines, stop = self._warnings()
        try:
            resp = await async_client.patch(
                f"/api/knowledge/documents/{filed['doc_id']}/tier", json={"tier": 2},
            )
        finally:
            stop()

        assert resp.status_code == 200, resp.text
        assert resp.json()["circle_tier"] == 2
        assert resp.json()["paperless_url"] == LINK
        assert not [line for line in lines if "Paperless id withheld" in line]

    async def test_an_owner_without_the_permission_gets_none(
        self, async_client, db_session, filed,
    ):
        alice = filed["alice"]
        alice.role.permissions = ["kb.own"]
        await db_session.commit()
        _as(alice)

        resp = await async_client.patch(
            f"/api/knowledge/documents/{filed['doc_id']}/tier", json={"tier": 2},
        )

        assert resp.status_code == 200, resp.text
        assert resp.json()["paperless_url"] is None
        assert resp.json()["paperless_link_withheld"] is True


class TestADuplicateProposalNamesThePaperlessIdOnlyWithThePermission:
    async def test_the_brief_withholds_the_id_and_keeps_the_status(self, db_session, filed):
        from api.routes.document_dedupe import _brief

        hidden = await _brief(db_session, filed["doc_id"], False)
        shown = await _brief(db_session, filed["doc_id"], True)

        assert hidden.paperless_document_id is None
        assert hidden.in_paperless is True
        assert shown.paperless_document_id == PAPERLESS_ID
        assert shown.in_paperless is True
