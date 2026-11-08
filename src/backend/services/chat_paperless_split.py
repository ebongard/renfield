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

from loguru import logger
from sqlalchemy import select
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
        children = (
            await db.execute(
                select(Document)
                .where(Document.split_from_document_id == doc.id)
                .order_by(Document.id)
            )
        ).scalars().all()
        for child in children:
            if child.paperless_state is None:
                child.paperless_state = PAPERLESS_STATE_PENDING
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
    if doc.paperless_state is None:
        doc.paperless_state = PAPERLESS_STATE_PENDING
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
    """Reconciler pass: settle every requested intent. Returns how many settled."""
    uploads = (
        await db.execute(
            select(ChatUpload)
            .where(ChatUpload.paperless_intent == CHAT_PAPERLESS_INTENT_REQUESTED)
            .order_by(ChatUpload.id)
            .limit(limit)
        )
    ).scalars().all()
    n = 0
    for upload in uploads:
        if (await settle_intent(db, upload)).settled:
            n += 1
    if uploads:
        await db.commit()
    return n
