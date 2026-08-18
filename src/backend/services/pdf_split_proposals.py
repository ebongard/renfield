"""PDF-split review proposals (PR2 of docs/design/pdf-split.md).

An uncertain boundary verdict (whole-file confidence gate) is not auto-split:
it lands here as a PENDING ``pdf_split_proposals`` row, the parent parks in
``status='split_review'`` (the worker acks its entry; the MCP re-push keeps the
source file in the inbox via the RETRY dedup state), and the owner decides on
/brain/review: approve (optionally with edited page ranges) or reject
(treat-as-single).

Resolution NEVER executes the split in the API pod. Both actions persist state
and re-enqueue the parent on the document queue:

- approve → proposal APPROVED (+ edited ranges written back), parent
  ``split_pending`` → the worker pre-stage loads the stored plan and executes
  through the full crash-safe machinery (persisted per-part resolutions,
  transient PEL retries, archive-last).
- reject → proposal REJECTED, parent back to ``pending`` and enqueued with
  ``skip_split=True`` (the loop-breaker) → normal single-document ingest.

Proposal creation fires a best-effort personal proactive notification to the
owner (delivery is presence-gated + ``PROACTIVE_ENABLED``-gated downstream).
"""
from __future__ import annotations

from datetime import UTC, datetime

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from models.database import (
    DOC_STATUS_PENDING,
    DOC_STATUS_SPLIT_PENDING,
    DOC_STATUS_SPLIT_REVIEW,
    PDF_SPLIT_PROPOSAL_APPROVED,
    PDF_SPLIT_PROPOSAL_PENDING,
    PDF_SPLIT_PROPOSAL_REJECTED,
    Document,
    PdfSplitProposal,
)
from services.pdf_split_detector import SplitPiece, SplitVerdict, validate_boundaries
from services.pdf_splitter import _resolve_parent_owner
from services.redis_client import get_redis
from services.task_queue import DocumentTaskQueue

NOTIFY_EVENT_TYPE = "pdf_split_review"
NOTIFY_SOURCE = "pdf_split"


class ProposalStateError(RuntimeError):
    """The proposal is not in the state the action requires (route → 409)."""


class ProposalRangeError(ValueError):
    """Edited ranges do not cover the document contiguously (route → 422)."""


async def create_review_proposal(
    db: AsyncSession,
    parent: Document,
    verdict: SplitVerdict,
    user_id: int | None,
) -> PdfSplitProposal:
    """File (or refresh) the PENDING proposal for an uncertain verdict and
    park the parent in ``split_review``. Idempotent per document: the partial
    unique index allows ONE pending row, so a re-detection (e.g. after a
    REINGEST of a previously failed parent) refreshes the existing row instead
    of violating the constraint."""
    owner_user_id = await _resolve_parent_owner(db, parent)
    if owner_user_id is None:
        owner_user_id = user_id

    existing = (
        await db.execute(
            select(PdfSplitProposal).where(
                PdfSplitProposal.document_id == parent.id,
                PdfSplitProposal.status == PDF_SPLIT_PROPOSAL_PENDING,
            )
        )
    ).scalar_one_or_none()

    proposal_json = [p.to_dict() for p in verdict.pieces]
    signals_json = [s.to_dict() for s in verdict.page_signals]
    page_count = verdict.page_signals[-1].page if verdict.page_signals else 0

    if existing is not None:
        existing.proposal = proposal_json
        existing.page_signals = signals_json
        existing.page_count = page_count
        existing.overall_confidence = verdict.min_confidence
        if owner_user_id is not None:
            existing.user_id = owner_user_id
        flag_modified(existing, "proposal")
        flag_modified(existing, "page_signals")
        row = existing
    else:
        row = PdfSplitProposal(
            document_id=parent.id,
            user_id=owner_user_id,
            status=PDF_SPLIT_PROPOSAL_PENDING,
            proposal=proposal_json,
            page_signals=signals_json,
            page_count=page_count,
            overall_confidence=verdict.min_confidence,
        )
        db.add(row)

    parent.status = DOC_STATUS_SPLIT_REVIEW
    await db.commit()
    await db.refresh(row)

    await _notify_owner(db, parent, row)
    return row


async def _notify_owner(
    db: AsyncSession, parent: Document, row: PdfSplitProposal
) -> None:
    """Best-effort personal proactive notification — a held review must not be
    invisible. Never breaks proposal creation."""
    try:
        from services.notification_service import NotificationService

        n_docs = len(row.proposal or [])
        await NotificationService(db).process_webhook(
            event_type=NOTIFY_EVENT_TYPE,
            title="PDF-Prüfung wartet",
            message=(
                f"Ein gescanntes PDF ({parent.filename}) enthält vermutlich "
                f"{n_docs} Dokumente — bitte die Aufteilung unter "
                f"Gehirn → Review prüfen."
            ),
            urgency="info",
            source=NOTIFY_SOURCE,
            privacy="personal",
            target_user_id=row.user_id,
            data={
                "proposal_id": row.id,
                "document_id": parent.id,
                "pieces": n_docs,
                "confidence": row.overall_confidence,
            },
        )
    except Exception as e:  # noqa: BLE001 - notification is a nice-to-have
        logger.warning(f"pdf-split: review notification failed: {e}")


def _coerce_override(
    override: list[dict], page_count: int
) -> list[SplitPiece]:
    """Validate owner-edited ranges: contiguous, non-overlapping, covering
    1..page_count. Raises :class:`ProposalRangeError` with a human message."""
    pieces = validate_boundaries({"documents": override}, 1, page_count)
    if pieces is None:
        raise ProposalRangeError(
            "Die Seitenbereiche müssen lückenlos, überlappungsfrei und "
            "aufsteigend alle Seiten von 1 bis "
            f"{page_count} abdecken."
        )
    if len(pieces) < 2:
        raise ProposalRangeError(
            "Weniger als 2 Teilstücke — zum Verarbeiten als EIN Dokument "
            "bitte ablehnen (Treat-as-single)."
        )
    return pieces


async def approve_proposal(
    db: AsyncSession,
    row: PdfSplitProposal,
    *,
    documents_override: list[dict] | None = None,
    resolved_by: int | None = None,
) -> None:
    """Approve: persist the (possibly edited) plan, park the parent
    ``split_pending`` and enqueue it — the WORKER executes the split via the
    stored plan (full crash-safe machinery; nothing heavy in the API pod)."""
    if row.status != PDF_SPLIT_PROPOSAL_PENDING:
        raise ProposalStateError(f"proposal is {row.status}, not pending")
    if documents_override is not None:
        pieces = _coerce_override(documents_override, row.page_count)
        row.proposal = [p.to_dict() for p in pieces]
        flag_modified(row, "proposal")
    else:
        # The stored proposal must itself be executable.
        if validate_boundaries({"documents": row.proposal}, 1, row.page_count) is None:
            raise ProposalRangeError(
                "Der gespeicherte Vorschlag ist nicht mehr gültig — bitte "
                "Bereiche anpassen."
            )
    row.status = PDF_SPLIT_PROPOSAL_APPROVED
    row.resolved_at = datetime.now(UTC).replace(tzinfo=None)
    row.resolved_by_user_id = resolved_by

    parent = await db.get(Document, row.document_id)
    if parent is None:
        raise ProposalStateError("parent document no longer exists")
    parent.status = DOC_STATUS_SPLIT_PENDING
    await db.commit()

    await DocumentTaskQueue(redis_client=get_redis()).enqueue(
        {"document_id": parent.id, "force_ocr": False, "user_id": resolved_by}
    )


async def reject_proposal(
    db: AsyncSession,
    row: PdfSplitProposal,
    *,
    resolved_by: int | None = None,
) -> None:
    """Reject (treat-as-single): un-park the parent and re-enqueue it with the
    ``skip_split`` loop-breaker → normal single-document ingest."""
    if row.status != PDF_SPLIT_PROPOSAL_PENDING:
        raise ProposalStateError(f"proposal is {row.status}, not pending")
    row.status = PDF_SPLIT_PROPOSAL_REJECTED
    row.resolved_at = datetime.now(UTC).replace(tzinfo=None)
    row.resolved_by_user_id = resolved_by

    parent = await db.get(Document, row.document_id)
    if parent is None:
        raise ProposalStateError("parent document no longer exists")
    parent.status = DOC_STATUS_PENDING
    await db.commit()

    await DocumentTaskQueue(redis_client=get_redis()).enqueue(
        {
            "document_id": parent.id,
            "force_ocr": False,
            "user_id": resolved_by,
            "skip_split": True,
        }
    )
