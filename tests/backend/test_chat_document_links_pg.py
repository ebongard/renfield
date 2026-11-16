"""Chat document links: nobody gets a source for a document outside their circles.

Real Postgres -- the visibility decision is `documents_circles_filter`, which
is Postgres SQL. The unit tests (`test_chat_document_links.py`) replace the
lookup; here it is the real `_visible_document_meta` on the test database, so
the same call that names alice's document for alice is shown to name NOTHING
for carol.
"""
from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

import api.websocket.chat_handler as ch
import api.websocket.document_sources as ds
from models.database import Document, KnowledgeBase
from services import kb_shares_service as ks
from tests.backend.dbrows import ensure_role, ensure_user
from utils.config import settings

pytestmark = [pytest.mark.backend, pytest.mark.database]

PUBLIC = "https://paperless.example.org"
TIER_SELF = 0
PAPERLESS_ID = 7001


@pytest.fixture
async def people(db_session: AsyncSession, monkeypatch):
    """Alice (owner), Bob (gets a share), Carol (stranger); no circle
    memberships, so only ownership and the share open a tier-0 document."""
    monkeypatch.setattr(settings, "auth_enabled", True)
    monkeypatch.setattr(settings, "chat_document_links_enabled", True, raising=False)
    monkeypatch.setattr(settings, "paperless_public_url", PUBLIC, raising=False)

    async def _lookup(doc_ids, user_id):
        # The production lookup, on the test session instead of a new one.
        from services.knowledge_tool import _visible_document_meta

        return await _visible_document_meta(db_session, doc_ids, user_id)

    monkeypatch.setattr(ds, "_visible_meta", _lookup)
    await ensure_role(db_session)
    alice = await ensure_user(db_session, 1, username="alice")
    bob = await ensure_user(db_session, 2, username="bob")
    carol = await ensure_user(db_session, 3, username="carol")
    await db_session.commit()
    return alice, bob, carol


async def _private_document(db: AsyncSession, owner_id: int) -> tuple[int, Document]:
    """A tier-0 document in the owner's KB that is filed in Paperless."""
    from services.rag_service import RAGService

    kb = KnowledgeBase(name="Privat", is_active=True, owner_id=owner_id,
                       default_circle_tier=TIER_SELF)
    db.add(kb)
    await db.commit()
    doc = await RAGService(db).create_document_record(
        file_path="/tmp/doclinks.pdf", knowledge_base_id=kb.id,
        filename="doclinks.pdf", file_hash="h-doclinks",
    )
    doc.status = "completed"
    doc.title = "Privater Bescheid"
    doc.paperless_document_id = PAPERLESS_ID
    await db.commit()
    return kb.id, doc


def _rag(doc: Document) -> list[dict]:
    """What `rag.search` returned for the OWNER -- the worst case the chips
    must survive: results that reach another identity (a cached follow-up
    context after access changed, or a retrieval bug)."""
    return [{
        "chunk": {"id": 1, "content": "…"},
        "document": {"id": doc.id, "filename": doc.filename, "title": doc.title,
                     "circle_tier": doc.circle_tier},
        "similarity": 0.9,
    }]


class TestRagPathSourcesAreCircleFiltered:
    async def test_the_owner_gets_the_chip_with_the_server_built_link(self, db_session, people):
        alice, _bob, _carol = people
        _kb, doc = await _private_document(db_session, alice.id)

        sources = await ch._turn_sources([], _rag(doc), alice.id)

        assert sources == [{
            "document_id": doc.id, "filename": "doclinks.pdf", "title": "Privater Bescheid",
            "tier": TIER_SELF, "paperless_url": f"{PUBLIC}/documents/{PAPERLESS_ID}/details",
        }]

    async def test_a_stranger_gets_no_source_for_the_same_results(self, db_session, people):
        alice, _bob, carol = people
        _kb, doc = await _private_document(db_session, alice.id)
        # Control: the very same call names the document for its owner.
        assert await ch._turn_sources([], _rag(doc), alice.id)

        assert await ch._turn_sources([], _rag(doc), carol.id) == []

    async def test_an_unidentified_turn_gets_no_source(self, db_session, people):
        alice, _bob, _carol = people
        _kb, doc = await _private_document(db_session, alice.id)

        assert await ch._turn_sources([], _rag(doc), None) == []

    async def test_a_kb_share_opens_chip_and_link(self, db_session, people):
        alice, bob, carol = people
        kb_id, doc = await _private_document(db_session, alice.id)
        assert await ch._turn_sources([], _rag(doc), bob.id) == []

        await ks.share_kb(db_session, kb_id, bob.id, "read", granted_by=alice.id)

        [source] = await ch._turn_sources([], _rag(doc), bob.id)
        assert source["document_id"] == doc.id
        assert source["paperless_url"] == f"{PUBLIC}/documents/{PAPERLESS_ID}/details"
        assert await ch._turn_sources([], _rag(doc), carol.id) == []


class TestKnowledgeSearchSourcesGetNoLinkOutsideTheCircles:
    async def test_the_paperless_id_of_an_invisible_document_is_not_revealed(
        self, db_session, people,
    ):
        """A `knowledge_search` source keeps its chip (the tool filtered it),
        but the Paperless id behind a link is read through the document's own
        policy. A source naming a document the asker cannot see therefore
        carries no link -- and the id is in none of its fields."""
        alice, _bob, carol = people
        _kb, doc = await _private_document(db_session, alice.id)
        kb_source = {"document_id": doc.id, "filename": "x.pdf", "title": "X", "tier": 4}
        tool_results = [("internal.knowledge_search", {"sources": [kb_source]})]

        for_owner = await ch._turn_sources(tool_results, [], alice.id)
        for_stranger = await ch._turn_sources(tool_results, [], carol.id)

        assert for_owner == [
            {**kb_source, "paperless_url": f"{PUBLIC}/documents/{PAPERLESS_ID}/details"},
        ]
        assert for_stranger == [kb_source]
        assert str(PAPERLESS_ID) not in repr(for_stranger)

    async def test_a_paperless_hit_is_not_merged_into_an_invisible_document(
        self, db_session, people,
    ):
        """Carol's own Paperless search hit stays HER chip: whether it equals a
        knowledge-base document she cannot see must not change what she gets."""
        import json

        alice, _bob, carol = people
        _kb, doc = await _private_document(db_session, alice.id)
        hit = (
            "mcp.paperless.search_documents",
            [{"type": "text", "text": json.dumps(
                {"results": [{"id": PAPERLESS_ID, "title": "Treffer"}]})}],
        )

        sources = await ch._turn_sources([hit], _rag(doc), carol.id)

        assert sources == [{
            "kind": "paperless", "paperless_document_id": PAPERLESS_ID, "title": "Treffer",
            "paperless_url": f"{PUBLIC}/documents/{PAPERLESS_ID}/details",
        }]
