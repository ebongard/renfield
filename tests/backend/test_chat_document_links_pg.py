"""Chat document links: nobody gets a source for a document outside their circles.

Real Postgres -- the visibility decision is `documents_circles_filter`, which
is Postgres SQL. The unit tests (`test_chat_document_links.py`) replace the
lookup; here it is the real `_visible_document_meta` on the test database, so
the same call that names alice's document for alice is shown to name NOTHING
for carol.
"""
from __future__ import annotations

import json

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
#: Retrieved chunk text of the asker's private document.
SECRET_CONTEXT = "Rueckzahlung 4711 an die Antragstellerin"


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


class TestCardFieldsComeFromTheCircleFilteredRow:
    """Document cards: the document's own date and its page count are columns
    of the row `_visible_document_meta` already selects -- the same statement,
    the same circle filter, no further query."""

    @staticmethod
    async def _dated(db, owner_id):
        import datetime

        kb_id, doc = await _private_document(db, owner_id)
        doc.document_date = datetime.date(2025, 3, 14)
        doc.page_count = 3
        await db.commit()
        return kb_id, doc

    async def test_the_owner_gets_date_and_pages(self, db_session, people):
        alice, _bob, _carol = people
        _kb, doc = await self._dated(db_session, alice.id)

        row = (await ds._visible_meta({doc.id}, alice.id))[doc.id]
        [source] = await ch._turn_sources([], _rag(doc), alice.id)

        # The lookup hands out a JSON value, not a `date`: the row is persisted.
        assert row["document_date"] == "2025-03-14" and row["page_count"] == 3
        assert source == {
            "document_id": doc.id, "filename": "doclinks.pdf", "title": "Privater Bescheid",
            "tier": TIER_SELF, "document_date": "2025-03-14", "page_count": 3,
            "paperless_url": f"{PUBLIC}/documents/{PAPERLESS_ID}/details",
        }

    async def test_a_stranger_gets_neither_from_lookup_nor_from_a_tool_source(
        self, db_session, people,
    ):
        alice, _bob, carol = people
        _kb, doc = await self._dated(db_session, alice.id)
        kb_source = {"document_id": doc.id, "filename": "x.pdf", "title": "X", "tier": 4}
        tool_results = [("internal.knowledge_search", {"sources": [kb_source]})]
        # Control: the owner's turn carries both.
        [for_owner] = await ch._turn_sources(tool_results, [], alice.id)
        assert for_owner["document_date"] == "2025-03-14" and for_owner["page_count"] == 3

        assert await ds._visible_meta({doc.id}, carol.id) == {}
        assert await ch._turn_sources(tool_results, [], carol.id) == [kb_source]
        assert await ch._turn_sources([], _rag(doc), carol.id) == []

    async def test_a_document_without_a_date_carries_no_date_and_never_the_upload_day(
        self, db_session, people,
    ):
        alice, _bob, _carol = people
        _kb, doc = await _private_document(db_session, alice.id)
        assert doc.document_date is None and doc.created_at is not None

        [source] = await ch._turn_sources([], _rag(doc), alice.id)

        assert "document_date" not in source and "created_at" not in source
        assert "page_count" not in source

    async def test_with_the_flag_off_the_turn_has_no_card_fields(
        self, db_session, people, monkeypatch,
    ):
        alice, _bob, _carol = people
        _kb, doc = await self._dated(db_session, alice.id)
        kb_source = {"document_id": doc.id, "filename": "x.pdf", "title": "X", "tier": 0}
        tool_results = [("internal.knowledge_search", {"sources": [kb_source]})]
        monkeypatch.setattr(settings, "chat_document_links_enabled", False, raising=False)

        sources = await ch._turn_sources(tool_results, _rag(doc), alice.id)

        assert sources == [kb_source] and sources[0] is kb_source


class TestADocumentWithoutAName:
    async def test_the_lookup_invents_no_title_and_the_chip_carries_none(self, db_session, people):
        alice, _bob, _carol = people
        _kb, doc = await _private_document(db_session, alice.id)
        doc.title = None
        doc.filename = ""
        await db_session.commit()

        row = (await ds._visible_meta({doc.id}, alice.id))[doc.id]
        [source] = await ch._turn_sources([], _rag(doc), alice.id)

        assert not row["title"] and row["filename"] == ""
        assert source["title"] == "" and source["filename"] == ""
        assert "Dokument" not in repr(source)


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


# --------------------------------------------------------------------------- #
# Read side: a shared conversation is read by people who did not ask
# --------------------------------------------------------------------------- #
async def _reader(db: AsyncSession, user_id: int):
    """The user as `get_current_user` hands it to a route: role loaded."""
    from sqlalchemy import select
    from sqlalchemy.orm import selectinload

    from models.database import User

    return (await db.execute(
        select(User).options(selectinload(User.role)).where(User.id == user_id)
    )).scalar_one()


@pytest.fixture
async def kitchen(db_session: AsyncSession, monkeypatch, own_session_to_test_db):
    """The shared room thread (auth-on §8.1): owned by the device account at
    tier 2, read by every member. Anna asked a turn in it; her answer's sources
    name HER private document and two Paperless documents. Bert is a member of
    the room but not of Anna's circles, and holds no Paperless permission.

    `own_session_to_test_db`: the read side looks visibility up on a session of
    its own, as production does -- here against the test database."""
    from sqlalchemy import text

    from services.conversation_service import ConversationService

    monkeypatch.setattr(settings, "auth_enabled", True)
    monkeypatch.setattr(settings, "paperless_public_url", PUBLIC, raising=False)
    device = await ensure_user(db_session, 1, username="haushalt")
    device.is_device_account = True
    anna = await ensure_user(
        db_session, 2, username="anna", role_name="mit-paperless",
        permissions=["mcp.paperless.read"],
    )
    bert = await ensure_user(db_session, 3, username="bert", role_name="ohne-paperless")
    cleo = await ensure_user(
        db_session, 4, username="cleo", role_name="mit-paperless",
    )
    await db_session.commit()
    for member in (anna, bert, cleo):
        await db_session.execute(
            text(
                "INSERT INTO circle_memberships "
                "(circle_owner_id, member_user_id, dimension, value, granted_by, granted_at) "
                "VALUES (:o, :m, 'tier', '2', :o, NOW())"
            ),
            {"o": device.id, "m": member.id},
        )
    await db_session.commit()

    _kb, doc = await _private_document(db_session, anna.id)
    import datetime

    # What the document says now -- equal to what the turn stored.
    doc.document_date = datetime.date(2025, 3, 14)
    doc.page_count = 3
    await db_session.commit()
    sources = [
        {"document_id": doc.id, "filename": "doclinks.pdf", "title": "Privater Bescheid",
         "tier": TIER_SELF, "document_date": "2025-03-14", "page_count": 3,
         "paperless_url": f"{PUBLIC}/documents/{PAPERLESS_ID}/details"},
        # The first Paperless source is a document CARD (date, type,
        # correspondent, pages); the second is one stored before cards existed.
        {"kind": "paperless", "paperless_document_id": 8001, "title": "Annas Beleg",
         "created": "2026-01-02", "correspondent": "Annas Bank", "document_type": "Beleg",
         "page_count": 2, "paperless_url": f"{PUBLIC}/documents/8001/details"},
        {"kind": "paperless", "paperless_document_id": 8002, "title": "Annas Vertrag"},
    ]
    # The metadata a REAL turn stores, built by the handler's own builders --
    # a hand-written dict left `action_summary` out, and with it the field
    # that carries the same documents past a filter on `sources`.
    knowledge_turn = _stored_metadata(
        "internal.knowledge_search",
        {
            "query": "Bescheid", "results_count": 1, "facts_count": 0,
            "context": f"[doclinks.pdf] {SECRET_CONTEXT}",
            "sources": sources[:1], "facts": [],
        },
        sources,
    )
    paperless_turn = _stored_metadata(
        "mcp.paperless.search_documents",
        [{"type": "text", "text": json.dumps({"results": [
            {"id": 8001, "title": "Annas Beleg", "created": "2026-01-02"},
            {"id": 8002, "title": "Annas Vertrag", "created": "2026-01-03"},
            {"id": 8003, "title": "Annas Mahnung", "created": "2026-01-04"},
            {"id": 8004, "title": "Annas Police", "created": "2026-01-05"},
        ]})}],
        sources[1:],
    )
    assert SECRET_CONTEXT in knowledge_turn["action_summary"]
    assert "Annas Police" in paperless_turn["action_summary"]

    svc = ConversationService(db_session)
    await svc.save_message("kueche", "user", "Was steht im Bescheid?",
                           user_id=device.id, circle_tier=2)
    await svc.save_message(
        "kueche", "assistant", "Im Bescheid steht …",
        metadata=knowledge_turn, user_id=device.id,
    )
    await svc.save_message("kueche", "user", "Und in Paperless?", user_id=device.id)
    await svc.save_message(
        "kueche", "assistant", "Dort liegen vier Dokumente.",
        metadata=paperless_turn, user_id=device.id,
    )
    await db_session.commit()
    return {"anna": anna.id, "bert": bert.id, "cleo": cleo.id, "doc": doc, "sources": sources}


def _stored_metadata(tool: str, data, sources: list) -> dict:
    """`assistant_metadata` as `chat_handler` assembles it for an agent turn
    that ran `tool` (same keys, same builders)."""
    action_result = ch._build_agent_action_result([(tool, data)])
    summary = ch._build_action_summary({"intent": tool}, action_result)
    return {
        "intent": tool,
        "action_success": True,
        "agent_role": "documents",
        "action_summary": summary,
        "sources": sources,
        "subsystems_used": ["knowledge"],
    }


async def _history(db: AsyncSession, reader_id: int | None) -> list[dict]:
    """The metadata of the assistant messages `GET /api/chat/history/kueche`
    serves this reader, oldest first."""
    from api.routes.chat import get_history

    reader = await _reader(db, reader_id) if reader_id is not None else None
    payload = await get_history("kueche", limit=50, db=db, current_user=reader)
    return [m["metadata"] for m in payload["messages"] if m["role"] == "assistant"]


async def _history_sources(db: AsyncSession, reader_id: int | None) -> tuple[dict, list]:
    """The knowledge turn's metadata and its sources, as served to this reader."""
    meta = (await _history(db, reader_id))[0]
    return meta, meta.get("sources", [])


class TestAReaderOfASharedConversation:
    async def test_the_asker_reads_her_own_sources(self, db_session, kitchen):
        meta, sources = await _history_sources(db_session, kitchen["anna"])

        assert sources == kitchen["sources"]
        assert meta["agent_role"] == "documents"

    async def test_another_member_gets_no_source_he_could_not_have_got_himself(
        self, db_session, kitchen,
    ):
        """Bert reaches the THREAD (and reads the answer text) -- but neither
        Anna's private document nor Paperless."""
        # Control: Bert really reads this conversation ...
        knowledge_turn, paperless_turn = await _history(db_session, kitchen["bert"])
        assert knowledge_turn["agent_role"] == "documents"
        # ... and the same call serves the asker all three sources.
        assert len((await _history_sources(db_session, kitchen["anna"]))[1]) == 3

        assert "sources" not in knowledge_turn and "sources" not in paperless_turn
        served = repr([knowledge_turn, paperless_turn])
        assert "Privater Bescheid" not in served and "Annas" not in served
        assert SECRET_CONTEXT not in served
        assert str(PAPERLESS_ID) not in served and "800" not in served
        # ... nor a card field of either: document date, Paperless date, type.
        assert "2025-03-14" not in served and "2026-01-02" not in served
        assert "page_count" not in served and "Beleg" not in served

    async def test_only_the_keys_the_client_reads_leave_the_server(self, db_session, kitchen):
        """The history route is an allowlist. `action_summary` is the agent's
        working copy of the tool result (ids, titles, retrieved text) and is
        for the next prompt, not for a reader -- the asker included."""
        for reader in ("anna", "bert", "cleo"):
            for meta in await _history(db_session, kitchen[reader]):
                assert set(meta) <= {"agent_role", "sources"}, reader
                assert meta["agent_role"] == "documents"

    async def test_auth_off_serves_the_same_keys(self, db_session, kitchen, monkeypatch):
        monkeypatch.setattr(settings, "auth_enabled", False)

        for meta in await _history(db_session, None):
            assert set(meta) == {"agent_role", "sources"}

    async def test_the_paperless_permission_opens_paperless_sources_only(
        self, db_session, kitchen,
    ):
        """Cleo may read Paperless herself, so those chips are hers to see;
        Anna's private knowledge-base document still is not."""
        _meta, sources = await _history_sources(db_session, kitchen["cleo"])

        assert sources == kitchen["sources"][1:]

    async def test_a_share_opens_the_knowledge_base_source(self, db_session, kitchen):
        kb_id = kitchen["doc"].knowledge_base_id
        await ks.share_kb(db_session, kb_id, kitchen["bert"], "read", granted_by=kitchen["anna"])

        _meta, sources = await _history_sources(db_session, kitchen["bert"])

        assert sources == kitchen["sources"][:1]

    async def test_the_stored_row_is_not_touched(self, db_session, kitchen):
        """`message_metadata` is a plain JSON column (no `MutableDict`): an
        in-place change never marks the row dirty, so a re-select after a
        commit would not see it. The guard is the loaded object itself, in the
        route's own session, never expired."""
        import copy

        from sqlalchemy import select

        from models.database import Message

        rows = (await db_session.execute(
            select(Message).order_by(Message.id)
        )).scalars().all()
        before = copy.deepcopy([m.message_metadata for m in rows])
        assert before[1]["action_summary"] and len(before[1]["sources"]) == 3

        await _history(db_session, kitchen["bert"])

        # Same identity map, nothing expired: these are the dicts the route read.
        assert [m.message_metadata for m in rows] == before
        assert not db_session.dirty

    async def test_the_card_fields_are_what_the_document_says_now(self, db_session, kitchen):
        """The date was derived anew after the turn: under auth the reader gets
        the document's CURRENT date and page count, read through the same
        circle-filtered statement that decides the source."""
        import datetime

        doc = kitchen["doc"]
        doc.document_date = datetime.date(2024, 11, 30)
        doc.page_count = 5
        await db_session.commit()

        _meta, sources = await _history_sources(db_session, kitchen["anna"])

        assert sources[0]["document_date"] == "2024-11-30"
        assert sources[0]["page_count"] == 5
        # Control: the stored source still says otherwise.
        assert kitchen["sources"][0]["document_date"] == "2025-03-14"

    async def test_auth_off_is_one_trust_domain(self, db_session, kitchen, monkeypatch):
        monkeypatch.setattr(settings, "auth_enabled", False)

        _meta, sources = await _history_sources(db_session, None)

        assert sources == kitchen["sources"]

    async def test_the_flag_does_not_matter_for_the_read_side(
        self, db_session, kitchen, monkeypatch,
    ):
        """Knowledge-base sources were persisted long before the flag existed,
        and were served to every reader of the thread."""
        monkeypatch.setattr(settings, "chat_document_links_enabled", False, raising=False)

        assert (await _history_sources(db_session, kitchen["bert"]))[1] == []

    async def test_a_failed_lookup_leaves_the_route_session_usable(
        self, db_session, kitchen, monkeypatch,
    ):
        """The lookup's failure is swallowed -- so it must not be the ROUTE's
        transaction that a failed statement aborted (`InFailedSQLTransaction`
        on whatever the route queries next)."""
        from sqlalchemy import text

        import services.knowledge_tool as kt

        async def broken(db, doc_ids, user_id):
            await db.execute(text("SELECT * FROM a_table_that_does_not_exist"))

        monkeypatch.setattr(kt, "_visible_document_meta", broken)

        knowledge_turn, paperless_turn = await _history(db_session, kitchen["cleo"])

        # Fail-closed for the knowledge-base source, the rest is served ...
        assert knowledge_turn["sources"] == kitchen["sources"][1:]
        assert paperless_turn["sources"] == kitchen["sources"][1:]
        # ... and the session the route was given still answers.
        assert (await db_session.execute(text("SELECT 1"))).scalar_one() == 1
