"""Filing a chat attachment into Paperless when the PDF holds several letters.

A chat attachment becomes a KB document at once (``_auto_index_to_kb``), and the
document-worker's split pre-stage cuts a batch PDF there -- long before the user
says "lege das in Paperless ab". Filing the attachment's ORIGINAL bytes then puts
the whole stack into Paperless as one document. So the filing becomes
split-aware: it records the intent on the upload and lets the decoupled filing
leg (``paperless_state='pending'`` → worker hook / reconciler) file the parts.

The intent lives on ``chat_uploads.paperless_intent`` because the KB document
may not exist yet when the request arrives (upload and request in one message).
``settle_intent`` moves it onto the document; it only ever turns a NULL
``paperless_state`` into ``pending`` and is called from three places -- the
request itself, the auto-index right after the document row exists, and every
reconciler tick -- so any interleaving converges on the next tick at the latest.
Design: ``docs/design/chat-upload-pdf-split.md``.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from loguru import logger
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from models.database import (
    CHAT_PAPERLESS_INTENT_REQUESTED,
    CHAT_PAPERLESS_INTENT_SETTLED,
    DOC_STATUS_COMPLETED,
    DOC_STATUS_FAILED,
    DOC_STATUS_SPLIT_ARCHIVED,
    DOC_STATUS_SPLIT_REVIEW,
    PAPERLESS_STATE_PENDING,
    ChatUpload,
    Document,
)
from utils.config import settings

# An upload whose text extraction finished this long ago without a KB document
# will not get one (auto-index failed, e.g. the worker was down): the request is
# not ours, and an open intent for it is given up.
_NO_DOCUMENT_AFTER = timedelta(minutes=10)


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def split_filing_active() -> bool:
    """Whether the split-aware route can carry a filing at all: the split
    pre-stage runs, chat attachments become KB documents, and the filing leg
    (hook + reconciler, gated on the ingest→Paperless flags) exists."""
    return bool(
        settings.pdf_split_enabled
        and settings.chat_upload_auto_index
        and (settings.folder_ingest_to_paperless or settings.email_ingest_to_paperless)
    )


def _is_pdf(upload: ChatUpload) -> bool:
    return (upload.filename or "").lower().endswith(".pdf")


@dataclass
class SettleResult:
    settled: bool
    status: str | None = None  # the KB document's status, None = no document yet
    parts: list[str] = field(default_factory=list)  # child names when split


async def settle_intent(db: AsyncSession, upload: ChatUpload) -> SettleResult:
    """Move a requested filing onto the KB document. Idempotent; commits nothing
    (the caller does)."""
    doc = await db.get(Document, upload.document_id) if upload.document_id else None
    if doc is None:
        return SettleResult(settled=False)

    if doc.status == DOC_STATUS_SPLIT_ARCHIVED:
        # Guarded in SQL, not on the loaded objects: a part the worker filed
        # meanwhile keeps its state.
        await db.execute(
            update(Document)
            .where(
                Document.split_from_document_id == doc.id,
                Document.paperless_state.is_(None),
            )
            .values(paperless_state=PAPERLESS_STATE_PENDING)
        )
        children = (
            await db.execute(
                select(Document)
                .where(Document.split_from_document_id == doc.id)
                .order_by(Document.id)
            )
        ).scalars().all()
        upload.paperless_intent = CHAT_PAPERLESS_INTENT_SETTLED
        return SettleResult(
            settled=True,
            status=doc.status,
            parts=[c.generated_title or c.title or c.filename for c in children],
        )

    if doc.status == DOC_STATUS_FAILED:
        # The KB pipeline gave up; nothing downstream will file it.
        upload.paperless_intent = CHAT_PAPERLESS_INTENT_SETTLED
        logger.warning(
            f"chat paperless intent: upload {upload.id} → doc {doc.id} failed; not filed"
        )
        return SettleResult(settled=True, status=doc.status)

    # Still being read in, split pending / under review, or done as ONE
    # document: `pending` on the document itself. A split inherits it to the
    # children (`pdf_splitter` derives the intent from the parent), a single
    # document is filed by the hook or the reconciler.
    # Guarded in SQL: if the worker archived the parent since it was read,
    # the archived original must not come out `pending` (the next pass stamps
    # its parts instead).
    await db.execute(
        update(Document)
        .where(
            Document.id == doc.id,
            Document.paperless_state.is_(None),
            Document.status != DOC_STATUS_SPLIT_ARCHIVED,
        )
        .values(paperless_state=PAPERLESS_STATE_PENDING)
    )
    settled = doc.status == DOC_STATUS_COMPLETED
    if settled:
        upload.paperless_intent = CHAT_PAPERLESS_INTENT_SETTLED
    return SettleResult(settled=settled, status=doc.status)


async def route_chat_filing(db: AsyncSession, upload: ChatUpload) -> dict | None:
    """The split-aware branch of "file this attachment". ``None`` means: not
    ours, take the ordinary path (extraction + confirm card / direct upload).

    Ours is a PDF whose KB document is not (yet) a finished single document --
    no document yet, still being read in, split or about to be. A completed
    single document and a failed one keep the ordinary path."""
    if not split_filing_active() or not _is_pdf(upload):
        return None
    doc = await db.get(Document, upload.document_id) if upload.document_id else None
    if doc is None and upload.status == "completed" and upload.created_at is not None \
            and upload.created_at < _now() - _NO_DOCUMENT_AFTER:
        return None  # auto-index never produced a document; none will come
    if doc is not None and doc.status == DOC_STATUS_COMPLETED and doc.paperless_state is not None:
        # Filed or being filed by an earlier request (a chat document carries
        # no filing state otherwise) -- a second direct upload would duplicate it.
        return {
            "success": True,
            "message": "Dieses Dokument ist bereits in Paperless abgelegt oder wird gerade abgelegt.",
            "action_taken": False,
            "data": {"filing": "split_aware", "document_status": doc.status, "parts": 0},
        }
    if doc is not None and doc.status in (DOC_STATUS_COMPLETED, DOC_STATUS_FAILED):
        return None

    upload.paperless_intent = CHAT_PAPERLESS_INTENT_REQUESTED
    result = await settle_intent(db, upload)
    await db.commit()

    if result.status == DOC_STATUS_SPLIT_ARCHIVED:
        names = "\n".join(f"- {n}" for n in result.parts)
        message = (
            f"Das PDF enthielt {len(result.parts)} Schreiben. Ich lege sie einzeln in "
            f"Paperless ab:\n{names}"
        )
    elif result.status == DOC_STATUS_SPLIT_REVIEW:
        message = (
            "Das PDF enthält vermutlich mehrere Schreiben, die Aufteilung wartet auf "
            "deine Prüfung unter Gehirn → Prüfen. Nach der Freigabe lege ich die Teile "
            "einzeln in Paperless ab."
        )
    else:
        message = (
            "Das PDF wird noch eingelesen und, falls es mehrere Schreiben enthält, "
            "aufgeteilt. Danach lege ich es (bzw. jedes Schreiben einzeln) in "
            "Paperless ab."
        )
    return {
        "success": True,
        "message": message,
        "action_taken": True,
        "data": {"filing": "split_aware", "document_status": result.status,
                 "parts": len(result.parts)},
    }


async def settle_open_intents(db: AsyncSession, limit: int = 100) -> int:
    """Reconciler pass: settle the requested intents whose document reached a
    state the settle acts on. Returns how many settled.

    Only those are selected: an intent still waiting (document being read in,
    split under review -- its parent already carries `pending`) has nothing to
    do, and selecting it would let a backlog of waiting rows fill every batch.
    An intent whose upload never got a document is given up."""
    abandoned = (
        await db.execute(
            update(ChatUpload)
            .where(
                ChatUpload.paperless_intent == CHAT_PAPERLESS_INTENT_REQUESTED,
                ChatUpload.document_id.is_(None),
                ChatUpload.created_at < _now() - _NO_DOCUMENT_AFTER,
            )
            .values(paperless_intent=CHAT_PAPERLESS_INTENT_SETTLED)
            .returning(ChatUpload.id)
        )
    ).scalars().all()
    if abandoned:
        logger.warning(
            f"chat paperless intent: {len(abandoned)} upload(s) never got a KB document; not filed"
        )
    uploads = (
        await db.execute(
            select(ChatUpload)
            .join(Document, Document.id == ChatUpload.document_id)
            .where(
                ChatUpload.paperless_intent == CHAT_PAPERLESS_INTENT_REQUESTED,
                Document.status.in_(
                    (DOC_STATUS_SPLIT_ARCHIVED, DOC_STATUS_COMPLETED, DOC_STATUS_FAILED)
                ),
            )
            .order_by(ChatUpload.id)
            .limit(limit)
        )
    ).scalars().all()
    n = 0
    for upload in uploads:
        if (await settle_intent(db, upload)).settled:
            n += 1
    await db.commit()
    return n
