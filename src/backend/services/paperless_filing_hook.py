"""post_document_ingest hook: file a folder/email-ingest document into Paperless
from the **document-worker**, reusing the worker's high-quality Docling OCR.

Why here (not the backend): the Paperless leg needs Docling OCR for metadata
extraction, which is memory-heavy. Running it in the always-on backend (concurrent,
atop the full app) OOM'd the pod. The worker already OCRs every document — Docling's
correct home — and this hook rides that same OCR (``field_text``): it is reused for
metadata extraction AND written back as the Paperless document's searchable content,
so Paperless search uses Renfield's OCR rather than its own weaker consume-time OCR.

Gating:
  - Only documents the ingest bridge stamped ``paperless_state='pending'`` are
    filed — interactive KB uploads stay ``NULL`` and are skipped (provenance).
  - Registered only when folder- OR email-ingest → Paperless is enabled.

Best-effort: any failure leaves the doc ``pending`` for the retry re-enqueuer
(``paperless_reconciler``); the KB ingest is never affected. Uses a minimal
single-server Paperless MCP client so the worker keeps its memory budget.
"""

from __future__ import annotations

from typing import Any

from loguru import logger
from sqlalchemy import select

from models.database import (
    PAPERLESS_STATE_FAILED,
    PAPERLESS_STATE_PENDING,
    Document,
)
from services.database import AsyncSessionLocal


async def paperless_filing_post_ingest_hook(
    chunks: list[str] | None = None,
    document_id: int | None = None,
    user_id: int | None = None,
    field_text: str = "",
    lang: str | None = None,
    **kwargs: Any,
) -> None:
    """File a pending document into Paperless using the worker's OCR text."""
    if document_id is None:
        return
    async with AsyncSessionLocal() as db:
        doc = (
            await db.execute(select(Document).where(Document.id == document_id))
        ).scalar_one_or_none()
        # Provenance gate: only folder/email-ingest docs carry 'pending'.
        if doc is None or doc.paperless_state != PAPERLESS_STATE_PENDING:
            return

        from services.paperless_worker_client import get_paperless_mcp_manager

        mgr = await get_paperless_mcp_manager()
        if mgr is None:
            # Paperless unreachable/disabled → leave pending; the retry
            # re-enqueuer will refile once it's back.
            logger.info(
                f"paperless-filing-hook: paperless MCP unavailable; leaving "
                f"doc {document_id} pending"
            )
            return

        try:
            with open(doc.file_path, "rb") as f:
                file_bytes = f.read()
        except OSError as exc:
            # No bytes → can never file. Terminal, so the doc leaves the pending
            # working set instead of being retried forever.
            doc.paperless_state = PAPERLESS_STATE_FAILED
            await db.commit()
            logger.warning(
                f"paperless-filing-hook: doc {document_id} recovery bytes "
                f"unreadable ({exc}); marked paperless_state=failed"
            )
            return

        from services.folder_ingest import IngestMeta
        from services.folder_ingest_paperless import make_paperless_leg

        leg = make_paperless_leg(mgr, user_id=user_id, lang=(lang or "de"))
        try:
            await leg(
                db, doc, file_bytes, IngestMeta(filename=doc.filename), field_text
            )
        except Exception as exc:  # noqa: BLE001 - leg is best-effort
            logger.warning(
                f"paperless-filing-hook: leg error for doc {document_id} "
                f"(left pending for retry): {exc}"
            )


async def refile_document_paperless(
    document_id: int, user_id: int | None = None
) -> None:
    """Retry path (worker ``paperless_refile`` task): file a still-pending doc
    into Paperless. The initial ingest's ``field_text`` isn't persisted, so the
    leg falls back to a fresh Docling ``extract_from_file`` — full quality, and in
    the worker where Docling belongs. Best-effort; leaves the doc pending on any
    failure for the next re-enqueue."""
    async with AsyncSessionLocal() as db:
        doc = (
            await db.execute(select(Document).where(Document.id == document_id))
        ).scalar_one_or_none()
        if doc is None or doc.paperless_state != PAPERLESS_STATE_PENDING:
            return  # already settled / not a filing-wanted doc

        from services.paperless_worker_client import get_paperless_mcp_manager

        mgr = await get_paperless_mcp_manager()
        if mgr is None:
            logger.info(
                f"paperless-refile: paperless MCP unavailable; leaving doc "
                f"{document_id} pending"
            )
            return

        try:
            with open(doc.file_path, "rb") as f:
                file_bytes = f.read()
        except OSError as exc:
            doc.paperless_state = PAPERLESS_STATE_FAILED
            await db.commit()
            logger.warning(
                f"paperless-refile: doc {document_id} recovery bytes unreadable "
                f"({exc}); marked paperless_state=failed"
            )
            return

        from services.folder_ingest import IngestMeta
        from services.folder_ingest_paperless import make_paperless_leg
        from utils.config import settings

        # SHORT inline await on the retry path: this runs in the sequential
        # (replicas:1) document worker, so a long await here head-of-line-blocks all
        # other ingest. A refile that doesn't settle in this window leaves the doc
        # pending WITH its task_id, and the next reconciler cycle re-polls it cheaply
        # (the poll-first guard) — no re-upload, no worker starvation. The initial
        # fire-and-forget filing hook keeps the full paperless_consume_timeout_s (it
        # yields the loop, so a long await there is free).
        leg = make_paperless_leg(
            mgr, user_id=user_id,
            await_timeout_s=settings.paperless_refile_poll_timeout_s,
        )
        try:
            # doc_text=None → the leg re-OCRs via Docling (full quality) and
            # transports that OCR into Paperless content too.
            await leg(db, doc, file_bytes, IngestMeta(filename=doc.filename), None)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                f"paperless-refile: leg error for doc {document_id}: {exc}"
            )
