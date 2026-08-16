"""Execute a multi-document PDF split (docs/design/pdf-split.md).

Writes each validated boundary piece as its own PDF (pypdfium2 — already a
dependency via Docling) and re-enters it through the normal
``folder_ingest.ingest_document`` bridge, so every child gets dedup, owner/tier,
Atom, the Paperless-pending stamp and the worker enqueue exactly like any other
upload. The combined original is archived LAST: ``status='split_archived'``,
``paperless_state='done'`` (settled — never filed), zero chunks (it never
entered Docling) → excluded from retrieval; its bytes stay on the uploads PVC.

Idempotent resume: pdfium output bytes are not guaranteed identical across
runs, so a crash-retry cannot key on content hashes. Instead each piece gets a
DETERMINISTIC child filename (parent stem + part index + title slug); a part
whose exact (filename, kb) Document row already exists is skipped, closing the
crash window between child-create and the ``split_from_document_id`` stamp.
The parent is archived only after every piece is accounted for.
"""
from __future__ import annotations

import asyncio
import io
import re
from pathlib import Path

from loguru import logger
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from models.database import (
    DOC_STATUS_SPLIT_ARCHIVED,
    DOC_STATUS_SPLIT_PENDING,
    DOC_STATUS_SPLIT_REVIEW,
    PAPERLESS_STATE_DONE,
    PAPERLESS_STATE_PENDING,
    PDF_SPLIT_CHILD_SOURCE,
    Atom,
    Document,
    DocumentChunk,
)
from services.folder_ingest import IngestMeta, IngestStatus, ingest_document
from services.pdf_split_detector import (
    VERDICT_MULTI,
    SplitPiece,
    classify_slow_lane,
    detect_boundaries,
    extract_page_signals,
)
from utils.config import settings


class SplitExecutionError(RuntimeError):
    """A child piece could not be ingested; the parent stays unarchived so a
    retry resumes idempotently (or the normal failure paths take over)."""


def _slug(title: str, cap: int = 40) -> str:
    text = (title or "").lower()
    text = (
        text.replace("ä", "ae").replace("ö", "oe").replace("ü", "ue")
        .replace("ß", "ss")
    )
    text = re.sub(r"[^a-z0-9]+", "-", text).strip("-")
    return text[:cap].rstrip("-") or "dokument"


def child_filename(parent_filename: str, part_index: int, title: str) -> str:
    """Deterministic per-piece filename — the resume key. ``part_index`` is
    1-based and position-stable within one validated piece list."""
    stem = _slug(Path(parent_filename or "dokument").stem, cap=60)
    return f"{stem}_teil{part_index:02d}_{_slug(title)}.pdf"


def split_pdf_bytes(file_path: str, pieces: list[SplitPiece]) -> list[bytes]:
    """Write one PDF per piece (blocking — callers use ``run_in_executor``).
    Page ranges are 1-based inclusive; pdfium indices are 0-based."""
    import pypdfium2 as pdfium

    src = pdfium.PdfDocument(file_path)
    try:
        out: list[bytes] = []
        for piece in pieces:
            dest = pdfium.PdfDocument.new()
            try:
                dest.import_pages(
                    src, list(range(piece.start_page - 1, piece.end_page))
                )
                buf = io.BytesIO()
                dest.save(buf)
                out.append(buf.getvalue())
            finally:
                dest.close()
        return out
    finally:
        src.close()


async def _resolve_parent_owner(db: AsyncSession, parent: Document) -> int | None:
    if not parent.atom_id:
        return None
    return (
        await db.execute(
            select(Atom.owner_user_id).where(Atom.atom_id == parent.atom_id)
        )
    ).scalar_one_or_none()


async def _existing_child(
    db: AsyncSession, kb_id: int | None, filename: str
) -> Document | None:
    return (
        await db.execute(
            select(Document).where(
                Document.filename == filename,
                Document.knowledge_base_id == kb_id,
            )
        )
    ).scalar_one_or_none()


async def execute_split(
    db: AsyncSession,
    parent: Document,
    pieces: list[SplitPiece],
    *,
    user_id: int | None = None,
) -> list[int]:
    """Split the parent into its pieces and archive it. Returns the child
    document ids (existing + created). Raises :class:`SplitExecutionError`
    when a piece cannot be ingested — the parent is then left unarchived so a
    redelivery resumes exactly where this run stopped.

    Safe to re-run: already-materialized parts (matched by their deterministic
    filename) are skipped; an already-archived parent is a no-op."""
    if parent.status == DOC_STATUS_SPLIT_ARCHIVED:
        children = (
            (
                await db.execute(
                    select(Document.id).where(
                        Document.split_from_document_id == parent.id
                    )
                )
            )
            .scalars()
            .all()
        )
        return list(children)
    if len(pieces) < 2:
        raise SplitExecutionError("refusing to split into fewer than 2 pieces")

    # The child's Paperless intent mirrors the parent's at detection time: only
    # a filing-wanted parent (stamped 'pending' by its entry point) produces
    # filing-wanted children.
    to_paperless = parent.paperless_state == PAPERLESS_STATE_PENDING
    owner_user_id = await _resolve_parent_owner(db, parent)
    if owner_user_id is None:
        owner_user_id = user_id
    source = parent.source or PDF_SPLIT_CHILD_SOURCE

    # Which parts still need materializing? (resume key = deterministic name)
    names = [
        child_filename(parent.filename, i + 1, piece.title)
        for i, piece in enumerate(pieces)
    ]
    child_ids: list[int | None] = [None] * len(pieces)
    missing: list[int] = []
    for i, name in enumerate(names):
        existing = await _existing_child(db, parent.knowledge_base_id, name)
        if existing is not None:
            if existing.split_from_document_id is None:
                existing.split_from_document_id = parent.id
                await db.commit()
            child_ids[i] = existing.id
        else:
            missing.append(i)

    if missing:
        loop = asyncio.get_running_loop()
        piece_bytes = await loop.run_in_executor(
            None, split_pdf_bytes, parent.file_path, [pieces[i] for i in missing]
        )
        for i, data in zip(missing, piece_bytes, strict=True):
            result = await ingest_document(
                data,
                IngestMeta(filename=names[i], root="pdf_split"),
                db=db,
                kb_id=parent.knowledge_base_id,
                owner_user_id=owner_user_id,
                default_tier=parent.circle_tier,
                file_to_paperless=to_paperless,
                source=source,
            )
            if result.status is IngestStatus.INGESTED:
                child = await db.get(Document, result.document_id)
                if child is not None and child.split_from_document_id is None:
                    child.split_from_document_id = parent.id
                    await db.commit()
                child_ids[i] = result.document_id
            elif result.status is IngestStatus.DUPLICATE:
                # Byte-identical content already in the KB (e.g. the same
                # single-page document scanned twice). The part is covered;
                # don't re-stamp lineage on a row that may belong elsewhere.
                logger.info(
                    f"pdf-split: part {i + 1} of doc {parent.id} deduped onto "
                    f"existing doc {result.document_id}"
                )
                child_ids[i] = result.document_id
            else:
                raise SplitExecutionError(
                    f"part {i + 1} ({names[i]!r}) not ingested: "
                    f"{result.status.value}/{result.detail}"
                )

    # Archive LAST — only when every piece is accounted for. 'done' settles the
    # Paperless leg explicitly (the reconciler additionally requires
    # status='completed', so the archive is doubly excluded from filing).
    # Purge any partial chunks a pre-split ingest attempt may have left — the
    # archived combined original must never surface in retrieval.
    await db.execute(
        delete(DocumentChunk).where(DocumentChunk.document_id == parent.id)
    )
    parent.chunk_count = 0
    parent.status = DOC_STATUS_SPLIT_ARCHIVED
    parent.paperless_state = PAPERLESS_STATE_DONE
    parent.error_message = None
    await db.commit()
    logger.info(
        f"pdf-split: doc {parent.id} ({parent.filename!r}) split into "
        f"{len(pieces)} documents: {[c for c in child_ids if c is not None]}"
    )
    return [c for c in child_ids if c is not None]


# ---------------------------------------------------------------------------
# Document-worker pre-stage orchestration
# ---------------------------------------------------------------------------

async def maybe_split_at_ingest(
    db: AsyncSession,
    doc_id: int,
    *,
    skip_split: bool = False,
    user_id: int | None = None,
) -> bool:
    """Run PDF-split detection for one enqueued document. Returns True when
    the split lifecycle now OWNS the document (split executed, or the row is
    already in a split state) — the caller must then ACK and skip normal
    processing. False → proceed with the normal ingest pipeline.

    Guards: ``pdf_split_enabled`` off, a ``skip_split`` task param (the
    loop-breaker set by treat-as-single re-enqueues), or a non-PDF are all
    no-ops. Detection errors degrade to False (detection must never break
    ingest); an error while EXECUTING an approved split propagates — falling
    through to normal ingest after children were partially created would
    double-ingest the combined file.
    """
    if not settings.pdf_split_enabled or skip_split:
        return False
    doc = await db.get(Document, doc_id)
    if doc is None:
        return False
    if doc.status in (
        DOC_STATUS_SPLIT_ARCHIVED,
        DOC_STATUS_SPLIT_PENDING,
        DOC_STATUS_SPLIT_REVIEW,
    ):
        # Redelivery of an entry whose doc the split lifecycle already owns.
        return True
    if not (doc.filename or doc.file_path or "").lower().endswith(".pdf"):
        return False

    # -- Detection (best-effort; any failure → single-document status quo) --
    verdict = None
    try:
        loop = asyncio.get_running_loop()
        signals = await loop.run_in_executor(
            None, extract_page_signals, doc.file_path
        )
        if not signals:
            return False
        slow_reason = classify_slow_lane(signals)
        if slow_reason:
            # PR3 routes these to the dedicated split worker (VLM fill-in /
            # multi-window). Until then: status quo, loudly.
            logger.warning(
                f"pdf-split: doc {doc.id} needs the slow split lane "
                f"({slow_reason}) — processing as a single document until the "
                f"split worker ships"
            )
            return False
        verdict = await detect_boundaries(signals)
    except Exception as e:  # noqa: BLE001 - detection must never break ingest
        logger.warning(f"pdf-split: detection failed for doc {doc_id}: {e}")
        return False

    if verdict is None or verdict.kind != VERDICT_MULTI:
        return False
    if verdict.min_confidence < settings.pdf_split_auto_threshold:
        # PR2 files a pdf_split_proposals row for owner review. Until the
        # review surface exists, holding the doc would strand it: status quo.
        logger.warning(
            f"pdf-split: doc {doc.id} looks like {len(verdict.pieces)} "
            f"documents but min confidence {verdict.min_confidence:.2f} < "
            f"{settings.pdf_split_auto_threshold} — processing as a single "
            f"document until the review flow ships"
        )
        return False

    # -- Execution (NOT swallowed — see docstring) --
    await execute_split(db, doc, verdict.pieces, user_id=user_id)
    return True
