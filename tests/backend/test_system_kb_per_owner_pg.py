"""Chat uploads and meeting transcripts reach exactly the right people.

`circle_sql`'s document owner branch reads `kb.owner_id` (with an atom-owner
fallback), and its tier-reach branch keys on `cm.circle_owner_id = kb.owner_id`.
So the KB a document is filed into decides who reads it:

- one shared KB owned by somebody (the circles backfill gave the old ownerless
  "Chat Uploads"/"Meetings" KBs to the admin) → that owner reads every user's
  uploads and transcripts;
- one shared OWNERLESS KB → the membership branch never matches (NULL owner),
  so a household-tier meeting reaches nobody but its owner; and the chat-upload
  atom owner falls back to the first user (admin), so the uploader loses their
  own upload;
- one KB per owner → owner branch, atom owner and tier reach all name the
  uploader / meeting owner.

The documents are created through the real code: the per-owner KB lookup plus
`RAGService.create_document_record` (which derives the atom owner), and the
visibility query is the one `rag_retrieval` composes. Real Postgres.
"""
from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from models.database import Document, DocumentChunk, KnowledgeBase
from tests.backend.dbrows import ensure_role, ensure_user
from utils.config import settings

pytestmark = [pytest.mark.backend, pytest.mark.database]

ADMIN, ALICE, BOB, CAROL = 1, 2, 3, 4
TIER_SELF, TIER_HOUSEHOLD = 0, 2


@pytest.fixture
async def household(db_session: AsyncSession, monkeypatch):
    """Admin (1, the backfill owner), Alice (2), Bob (3) in Alice's household
    circle, Carol (4) in nobody's circle."""
    monkeypatch.setattr(settings, "auth_enabled", True)
    monkeypatch.setattr(settings, "chat_upload_default_kb_name", "Chat Uploads")
    await ensure_role(db_session)
    for uid, name in ((ADMIN, "admin"), (ALICE, "alice"), (BOB, "bob"), (CAROL, "carol")):
        await ensure_user(db_session, uid, username=name)
    await db_session.commit()
    await db_session.execute(
        text(
            "INSERT INTO circle_memberships "
            "(circle_owner_id, member_user_id, dimension, value, granted_by, granted_at) "
            "VALUES (:o, :m, 'tier', :v, :o, NOW())"
        ),
        {"o": ALICE, "m": BOB, "v": str(TIER_HOUSEHOLD)},
    )
    await db_session.commit()


async def _admin_owned(db: AsyncSession, name: str) -> KnowledgeBase:
    """The legacy system KB as the circles backfill left it: owned by the admin."""
    kb = KnowledgeBase(name=name, owner_id=ADMIN)
    db.add(kb)
    await db.commit()
    await db.refresh(kb)
    return kb


_seq = 0


async def _file(db: AsyncSession, kb_id: int, **overrides) -> Document:
    """A document through the real record path, plus one chunk to retrieve."""
    from services.rag_service import RAGService

    global _seq
    _seq += 1
    doc = await RAGService(db).create_document_record(
        file_path=f"/x/{_seq}.pdf", knowledge_base_id=kb_id,
        filename=f"d{_seq}.pdf", file_hash=f"h{_seq}", **overrides,
    )
    db.add(DocumentChunk(document_id=doc.id, content="Zaunkoenig", chunk_index=0,
                         circle_tier=doc.circle_tier))
    await db.commit()
    return doc


async def _readers(db: AsyncSession, doc_id: int) -> set[int]:
    """Who of the four reaches the document's chunks, via the RAG filter."""
    from services.circle_sql import document_chunks_circles_filter

    seen = set()
    for asker in (ADMIN, ALICE, BOB, CAROL):
        clause, params = document_chunks_circles_filter(asker)
        hit = (await db.execute(
            text(
                "SELECT 1 FROM document_chunks dc "
                "  JOIN documents d ON d.id = dc.document_id "
                "  LEFT JOIN knowledge_bases kb ON kb.id = d.knowledge_base_id "
                f" WHERE d.id = :doc AND ({clause})"
            ),
            {**params, "doc": doc_id},
        )).first()
        if hit:
            seen.add(asker)
    return seen


# ------------------------------------------------------------ chat uploads ---
async def test_a_chat_upload_reaches_only_its_uploader(db_session, household):
    from api.routes.chat_upload import _get_or_create_default_kb

    legacy = await _admin_owned(db_session, "Chat Uploads")
    kb = await _get_or_create_default_kb(db_session, ALICE)
    assert kb.id != legacy.id, "the upload landed in the admin-owned legacy KB"
    doc = await _file(db_session, kb.id)
    assert doc.circle_tier == TIER_SELF
    assert await _readers(db_session, doc.id) == {ALICE}


async def test_the_legacy_admin_owned_kb_keeps_its_documents(db_session, household):
    """No data migration: the old KB and its documents stay where they are and
    stay readable by their owner; new uploads of other users go elsewhere."""
    from api.routes.chat_upload import _get_or_create_default_kb

    legacy = await _admin_owned(db_session, "Chat Uploads")
    old = await _file(db_session, legacy.id)
    new = await _file(db_session, (await _get_or_create_default_kb(db_session, BOB)).id)
    assert await _readers(db_session, old.id) == {ADMIN}
    assert await _readers(db_session, new.id) == {BOB}
    # The admin's own uploads keep using the admin's KB of that name.
    assert (await _get_or_create_default_kb(db_session, ADMIN)).id == legacy.id


async def test_the_same_file_from_two_users_is_two_documents(db_session, household):
    """The hash dedupe is per KB. In one shared KB Bob's upload of Alice's file
    would be linked to Alice's document, which Bob cannot read."""
    from api.routes.chat_upload import _get_or_create_default_kb

    a = await _get_or_create_default_kb(db_session, ALICE)
    b = await _get_or_create_default_kb(db_session, BOB)
    assert a.id != b.id


# ---------------------------------------------------------------- meetings ---
async def _meeting_transcript(db, owner_id: int, tier: int) -> Document:
    """As `meeting_pipeline._ingest_transcript` files it: the owner's Meetings
    KB, atom owner and tier from the meeting (ingest_document's D4 overrides)."""
    from services.meeting_pipeline import _resolve_meetings_kb

    kb = await _resolve_meetings_kb(db, owner_id)
    return await _file(db, kb.id, owner_user_id_override=owner_id,
                       circle_tier_override=tier)


async def test_a_household_meeting_reaches_the_owners_household_only(db_session, household):
    from services.meeting_pipeline import _MEETINGS_KB_NAME

    await _admin_owned(db_session, _MEETINGS_KB_NAME)
    doc = await _meeting_transcript(db_session, ALICE, TIER_HOUSEHOLD)
    assert await _readers(db_session, doc.id) == {ALICE, BOB}


async def test_a_private_meeting_reaches_only_its_owner(db_session, household):
    from services.meeting_pipeline import _MEETINGS_KB_NAME

    await _admin_owned(db_session, _MEETINGS_KB_NAME)
    doc = await _meeting_transcript(db_session, ALICE, TIER_SELF)
    assert await _readers(db_session, doc.id) == {ALICE}
