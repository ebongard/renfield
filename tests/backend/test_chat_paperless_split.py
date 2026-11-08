"""Split-aware filing of a chat attachment (services/chat_paperless_split.py).

A batch PDF attached in the chat is cut into its letters at ingest; "file it in
Paperless" must then file the letters, never the combined original. The intent
is recorded on the upload and settled onto the KB document by three callers
(the request, the auto-index, the reconciler) -- these tests run each state of
the design table against real Postgres.
"""

from __future__ import annotations

import hashlib
from unittest.mock import AsyncMock, patch

import pytest

from api.routes.chat_upload import _auto_index_to_kb
from models.database import (
    CHAT_PAPERLESS_INTENT_REQUESTED,
    CHAT_PAPERLESS_INTENT_SETTLED,
    PAPERLESS_STATE_DONE,
    PAPERLESS_STATE_PENDING,
    ChatUpload,
    Document,
    KnowledgeBase,
)
from services.chat_paperless_split import route_chat_filing, settle_open_intents
from tests.backend.test_chat_upload_worker_path import _patch_worker_path

pytestmark = [pytest.mark.unit, pytest.mark.database]

_ON = {
    "pdf_split_enabled": True,
    "chat_upload_auto_index": True,
    "folder_ingest_to_paperless": True,
}


@pytest.fixture
def split_on():
    with patch.multiple("services.chat_paperless_split.settings", **_ON):
        yield


@pytest.fixture
async def kb(db_session):
    kb = KnowledgeBase(name="Chat Uploads", description="test")
    db_session.add(kb)
    await db_session.commit()
    return kb


async def _doc(db, kb, *, status, parent=None, name="stapel.pdf", state=None):
    doc = Document(
        filename=name,
        file_path=f"/tmp/{name}",
        knowledge_base_id=kb.id,
        status=status,
        split_from_document_id=parent.id if parent else None,
        paperless_state=state,
    )
    db.add(doc)
    await db.commit()
    return doc


async def _upload(db, *, doc=None, name="stapel.pdf"):
    up = ChatUpload(
        session_id="s",
        filename=name,
        file_path=f"/tmp/{name}",
        status="completed",
        document_id=doc.id if doc else None,
    )
    db.add(up)
    await db.commit()
    return up


async def test_flag_off_keeps_the_ordinary_path(db_session, kb):
    up = await _upload(db_session)
    with patch.multiple("services.chat_paperless_split.settings", **{**_ON, "pdf_split_enabled": False}):
        assert await route_chat_filing(db_session, up) is None
    assert up.paperless_intent is None


async def test_no_filing_leg_keeps_the_ordinary_path(db_session, kb):
    # Without an ingest→Paperless flag no hook and no reconciler would ever act.
    up = await _upload(db_session)
    with patch.multiple(
        "services.chat_paperless_split.settings",
        **{**_ON, "folder_ingest_to_paperless": False, "email_ingest_to_paperless": False},
    ):
        assert await route_chat_filing(db_session, up) is None


async def test_non_pdf_and_finished_single_document_keep_the_ordinary_path(db_session, kb, split_on):
    assert await route_chat_filing(db_session, await _upload(db_session, name="foto.jpg")) is None
    single = await _doc(db_session, kb, status="completed")
    assert await route_chat_filing(db_session, await _upload(db_session, doc=single)) is None
    failed = await _doc(db_session, kb, status="failed", name="kaputt.pdf")
    assert await route_chat_filing(db_session, await _upload(db_session, doc=failed)) is None
    assert single.paperless_state is None


async def test_split_document_files_each_part_not_the_original(db_session, kb, split_on):
    parent = await _doc(db_session, kb, status="split_archived", state=PAPERLESS_STATE_DONE)
    a = await _doc(db_session, kb, status="completed", parent=parent, name="a.pdf")
    b = await _doc(db_session, kb, status="completed", parent=parent, name="b.pdf")
    up = await _upload(db_session, doc=parent)

    result = await route_chat_filing(db_session, up)

    assert result["success"] is True and result["action_taken"] is True
    assert result["data"]["parts"] == 2
    assert "2 Schreiben" in result["message"]
    await db_session.refresh(a)
    await db_session.refresh(b)
    await db_session.refresh(parent)
    assert (a.paperless_state, b.paperless_state) == (PAPERLESS_STATE_PENDING, PAPERLESS_STATE_PENDING)
    assert parent.paperless_state == PAPERLESS_STATE_DONE  # the original is never filed
    assert up.paperless_intent == CHAT_PAPERLESS_INTENT_SETTLED


async def test_a_part_already_filed_is_not_touched_again(db_session, kb, split_on):
    parent = await _doc(db_session, kb, status="split_archived", state=PAPERLESS_STATE_DONE)
    filed = await _doc(db_session, kb, status="completed", parent=parent, name="a.pdf", state=PAPERLESS_STATE_DONE)
    await route_chat_filing(db_session, await _upload(db_session, doc=parent))
    await route_chat_filing(db_session, await _upload(db_session, doc=parent))
    await db_session.refresh(filed)
    assert filed.paperless_state == PAPERLESS_STATE_DONE


async def test_split_under_review_marks_the_parent_and_waits(db_session, kb, split_on):
    parent = await _doc(db_session, kb, status="split_review")
    up = await _upload(db_session, doc=parent)

    result = await route_chat_filing(db_session, up)

    assert "Prüfen" in result["message"]
    await db_session.refresh(parent)
    # The split inherits `pending` to the children on approval; a rejection
    # ("one document") files the parent itself.
    assert parent.paperless_state == PAPERLESS_STATE_PENDING
    assert up.paperless_intent == CHAT_PAPERLESS_INTENT_REQUESTED


async def test_request_before_the_document_exists_lands_on_it_at_auto_index(
    db_session,
    async_engine,
    kb,
    split_on,
):
    up = await _upload(db_session)  # no KB document yet
    result = await route_chat_filing(db_session, up)
    assert "noch eingelesen" in result["message"]
    assert up.paperless_intent == CHAT_PAPERLESS_INTENT_REQUESTED

    created: list[Document] = []

    async def _stub_create(self, **kwargs):
        doc = Document(
            filename=kwargs["filename"],
            file_path=kwargs["file_path"],
            file_hash=kwargs["file_hash"],
            knowledge_base_id=kwargs["knowledge_base_id"],
            status="completed",
            chunk_count=1,
        )
        self.db.add(doc)
        await self.db.commit()
        created.append(doc)
        return doc

    with _patch_worker_path(
        async_engine,
        worker_alive=True,
        **{
            "api.routes.chat_upload._get_or_create_default_kb": AsyncMock(return_value=kb),
            "services.rag_service.RAGService.create_document_record": _stub_create,
            "api.websocket.shared.notify_session": AsyncMock(),
        },
    ):
        await _auto_index_to_kb(
            upload_id=up.id,
            file_path="/tmp/stapel.pdf",
            filename="stapel.pdf",
            file_hash=hashlib.sha256(b"stapel").hexdigest(),
            session_id="s",
        )

    doc = await db_session.get(Document, created[0].id, populate_existing=True)
    assert doc.paperless_state == PAPERLESS_STATE_PENDING


async def test_reconciler_pass_settles_an_intent_whose_document_split_later(db_session, kb, split_on):
    # The race: requested while still being read in, the split materialized
    # its children before the parent carried `pending`.
    parent = await _doc(db_session, kb, status="processing")
    up = await _upload(db_session, doc=parent)
    await route_chat_filing(db_session, up)
    parent.status = "split_archived"
    parent.paperless_state = PAPERLESS_STATE_DONE
    child = await _doc(db_session, kb, status="completed", parent=parent, name="a.pdf")

    assert await settle_open_intents(db_session) == 1

    await db_session.refresh(child)
    await db_session.refresh(up)
    assert child.paperless_state == PAPERLESS_STATE_PENDING
    assert up.paperless_intent == CHAT_PAPERLESS_INTENT_SETTLED
    assert await settle_open_intents(db_session) == 0  # nothing left open


async def test_auto_index_poll_treats_a_split_as_ready_and_a_review_as_terminal(
    db_session,
    async_engine,
    kb,
):
    for status, expect_ready in (("split_archived", True), ("split_review", False)):
        notes: list[dict] = []

        async def _notify(session_id, payload, _n=notes):
            _n.append(payload)

        h = hashlib.sha256(status.encode()).hexdigest()
        db_session.add(
            Document(filename="x.pdf", file_path="/tmp/x.pdf", file_hash=h, knowledge_base_id=kb.id, status=status)
        )
        await db_session.commit()
        up = await _upload(db_session, name=f"{status}.pdf")
        with _patch_worker_path(
            async_engine,
            worker_alive=True,
            **{
                "api.routes.chat_upload._get_or_create_default_kb": AsyncMock(return_value=kb),
                "api.websocket.shared.notify_session": _notify,
            },
        ):
            await _auto_index_to_kb(
                upload_id=up.id, file_path="/tmp/x.pdf", filename="x.pdf", file_hash=h, session_id="s"
            )
        types = [n["type"] for n in notes]
        assert "document_error" not in types, status
        assert ("document_ready" in types) is expect_ready, status


async def test_a_second_request_after_filing_does_not_upload_again(db_session, kb, split_on):
    # First request while being read in → the document got `pending`; it then
    # finished as ONE document. A second "lege ab" must not take the direct
    # upload of the original next to the filing leg.
    doc = await _doc(db_session, kb, status="completed", state=PAPERLESS_STATE_PENDING)
    result = await route_chat_filing(db_session, await _upload(db_session, doc=doc))
    assert result is not None and result["action_taken"] is False
    assert "bereits" in result["message"]


async def test_waiting_intents_do_not_fill_the_reconciler_batch(db_session, kb, split_on):
    for i in range(3):
        waiting = await _doc(db_session, kb, status="split_review", name=f"w{i}.pdf")
        await route_chat_filing(db_session, await _upload(db_session, doc=waiting, name=f"w{i}.pdf"))
    parent = await _doc(db_session, kb, status="processing", name="late.pdf")
    late = await _upload(db_session, doc=parent, name="late.pdf")
    await route_chat_filing(db_session, late)
    parent.status = "split_archived"
    child = await _doc(db_session, kb, status="completed", parent=parent, name="c.pdf")
    await db_session.commit()

    assert await settle_open_intents(db_session, limit=1) == 1

    await db_session.refresh(child)
    assert child.paperless_state == PAPERLESS_STATE_PENDING


async def test_an_upload_that_never_got_a_document_is_not_promised_a_filing(db_session, kb, split_on):
    from datetime import datetime, timedelta

    up = await _upload(db_session)
    up.created_at = datetime.utcnow() - timedelta(hours=1)
    await db_session.commit()
    assert await route_chat_filing(db_session, up) is None  # ordinary path

    up.paperless_intent = CHAT_PAPERLESS_INTENT_REQUESTED
    await db_session.commit()
    await settle_open_intents(db_session)
    await db_session.refresh(up)
    assert up.paperless_intent == CHAT_PAPERLESS_INTENT_SETTLED


async def test_an_archived_parent_never_comes_out_pending(db_session, kb, split_on):
    # The worker archived the parent after the settle had read it as split_pending.
    parent = await _doc(db_session, kb, status="split_pending")
    up = await _upload(db_session, doc=parent)
    from sqlalchemy import text

    from services.chat_paperless_split import settle_intent

    await db_session.get(Document, parent.id)  # loaded as split_pending
    await db_session.execute(
        text("UPDATE documents SET status = 'split_archived', paperless_state = NULL WHERE id = :i"),
        {"i": parent.id},
    )
    up.paperless_intent = CHAT_PAPERLESS_INTENT_REQUESTED
    await settle_intent(db_session, up)
    await db_session.commit()
    state = (
        await db_session.execute(text("SELECT paperless_state FROM documents WHERE id = :i"), {"i": parent.id})
    ).scalar()
    assert state is None
