"""Execute a multi-document PDF split (docs/design/pdf-split.md).

Writes each validated boundary piece as its own PDF (pypdfium2 — already a
dependency via Docling) and re-enters it through the normal
``folder_ingest.ingest_document`` bridge, so every child gets dedup, owner/tier,
Atom, the Paperless-pending stamp and the worker enqueue exactly like any other
upload. The combined original is archived LAST: ``status='split_archived'``,
``paperless_state='done'`` (settled — never filed), zero chunks (it never
entered Docling) → excluded from retrieval; its bytes stay on the uploads PVC.

Crash-safety is built from three pieces (each closes a /review finding):

- **Persisted plan.** The boundary LLM is nondeterministic, so a crash-resume
  must NEVER re-detect: the confident verdict is stored as an APPROVED
  ``pdf_split_proposals`` row BEFORE execution, and a redelivered entry replays
  that stored plan verbatim.
- **Deterministic, parent-scoped resume keys.** Each piece's child filename
  embeds the parent's content-hash prefix + part index, and the resume probe
  additionally requires the row's ``split_from_document_id`` to be this parent
  (or still unstamped — the create-vs-stamp crash window). A same-name row from
  a DIFFERENT batch scan can no longer be adopted as a child.
- **Transient vs terminal.** A child ingest that comes back RETRY (disk full,
  lost create race) raises :class:`SplitTransientError` — the worker leaves the
  entry in the PEL and the resume continues later. Only genuinely terminal
  child results raise :class:`SplitExecutionError`. The parent is archived only
  after every piece is accounted for.
"""
from __future__ import annotations

import asyncio
import io
import re
from datetime import UTC, datetime
from pathlib import Path

from loguru import logger
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from models.database import (
    DOC_SPLIT_OWNED_STATUSES,
    DOC_STATUS_SPLIT_ARCHIVED,
    PAPERLESS_STATE_DONE,
    PAPERLESS_STATE_PENDING,
    PDF_SPLIT_CHILD_SOURCE,
    PDF_SPLIT_PROPOSAL_APPROVED,
    Atom,
    Document,
    DocumentChunk,
    PdfSplitProposal,
)
from services.folder_ingest import IngestMeta, IngestStatus, ingest_document
from services.pdf_split_detector import (
    VERDICT_MULTI,
    SplitPiece,
    SplitVerdict,
    classify_slow_lane,
    detect_boundaries,
    extract_page_signals,
    validate_boundaries,
)

# Re-exported for callers/tests — the classes live in the import-light errors
# module so the document worker can extend its transient taxonomy without
# pulling this module's LLM import graph.
from services.pdf_split_errors import (  # noqa: F401  (re-export)
    SplitExecutionError,
    SplitTransientError,
)
from utils.config import settings

# Statuses meaning "the split lifecycle owns this document" — canonical tuple
# lives in models.database (import-light for the worker's flag-independent
# guard); re-exported here for the service-side guard + tests.
SPLIT_OWNED_STATUSES = DOC_SPLIT_OWNED_STATUSES


def _slug(title: str, cap: int = 40) -> str:
    text = (title or "").lower()
    text = (
        text.replace("ä", "ae").replace("ö", "oe").replace("ü", "ue")
        .replace("ß", "ss")
    )
    text = re.sub(r"[^a-z0-9]+", "-", text).strip("-")
    return text[:cap].rstrip("-") or "dokument"


def child_filename(
    parent_filename: str, parent_hash: str | None, part_index: int, title: str
) -> str:
    """Deterministic per-piece filename — the resume key. ``part_index`` is
    1-based and position-stable within one PERSISTED piece list. The parent's
    content-hash prefix makes the name unique per source file, so recurring
    scanner names + recurring title slugs ('rechnung-stadtwerke' every month)
    cannot collide across different batch scans."""
    stem = _slug(Path(parent_filename or "dokument").stem, cap=60)
    token = (parent_hash or "nohash")[:8]
    return f"{stem}_{token}_teil{part_index:02d}_{_slug(title)}.pdf"


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


async def _existing_children(
    db: AsyncSession, parent: Document, names: list[str]
) -> dict[str, Document]:
    """One batched probe for already-materialized parts, scoped to THIS parent:
    a row only counts when its lineage points at the parent, or is still
    unstamped (the crash window between child-create and the lineage stamp —
    the hash-token filename makes an unstamped same-name foreign row
    practically impossible). Rows with foreign lineage are ignored."""
    rows = (
        (
            await db.execute(
                select(Document)
                .where(
                    Document.filename.in_(names),
                    Document.knowledge_base_id == parent.knowledge_base_id,
                    (Document.split_from_document_id == parent.id)
                    | (Document.split_from_document_id.is_(None)),
                )
                .order_by(Document.id)
            )
        )
        .scalars()
        .all()
    )
    by_name: dict[str, Document] = {}
    for row in rows:
        # Prefer a lineage-stamped row over an unstamped one; first-by-id wins
        # within the same class.
        current = by_name.get(row.filename)
        if current is None or (
            current.split_from_document_id is None
            and row.split_from_document_id == parent.id
        ):
            by_name[row.filename] = row
    return by_name


async def execute_split(
    db: AsyncSession,
    parent: Document,
    pieces: list[SplitPiece],
    *,
    user_id: int | None = None,
) -> list[int]:
    """Split the parent into its pieces and archive it. Returns the child
    document ids (existing + created). Raises :class:`SplitTransientError` for
    retryable child outcomes and :class:`SplitExecutionError` for terminal
    ones — the parent is then left unarchived so a redelivery resumes exactly
    where this run stopped.

    Safe to re-run: already-materialized parts (matched by their deterministic,
    parent-scoped filename) are skipped; an already-archived parent is a no-op."""
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
        child_filename(parent.filename, parent.file_hash, i + 1, piece.title)
        for i, piece in enumerate(pieces)
    ]
    existing = await _existing_children(db, parent, names)
    child_ids: list[int | None] = [None] * len(pieces)
    stamped = False
    for i, name in enumerate(names):
        row = existing.get(name)
        if row is not None:
            if row.split_from_document_id is None:
                row.split_from_document_id = parent.id
                stamped = True
            child_ids[i] = row.id
    if stamped:
        await db.commit()

    loop = asyncio.get_running_loop()
    for i, piece in enumerate(pieces):
        if child_ids[i] is not None:
            continue
        # Render one piece at a time — a large scan must not hold every part's
        # bytes in RAM at once on the worker pod.
        data = (
            await loop.run_in_executor(
                None, split_pdf_bytes, parent.file_path, [piece]
            )
        )[0]
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
        elif result.status is IngestStatus.RETRY:
            # Transient (disk full persisting the recovery copy, a lost create
            # race still in flight): leave the entry in the PEL — the resume
            # re-runs this part later. NOT terminal.
            raise SplitTransientError(
                f"part {i + 1} ({names[i]!r}) transient: {result.detail}"
            )
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
# Persisted split plan (crash-resume determinism)
# ---------------------------------------------------------------------------

async def _load_stored_plan(
    db: AsyncSession, document_id: int, page_count: int
) -> list[SplitPiece] | None:
    """Newest APPROVED plan for this document, revalidated. A corrupt stored
    plan is ignored (fall through to fresh detection) rather than fatal."""
    row = (
        await db.execute(
            select(PdfSplitProposal)
            .where(
                PdfSplitProposal.document_id == document_id,
                PdfSplitProposal.status == PDF_SPLIT_PROPOSAL_APPROVED,
            )
            .order_by(PdfSplitProposal.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if row is None:
        return None
    pieces = validate_boundaries({"documents": row.proposal}, 1, page_count)
    if pieces is None or len(pieces) < 2:
        logger.warning(
            f"pdf-split: stored plan for doc {document_id} failed revalidation "
            f"— ignoring it"
        )
        return None
    return pieces


async def _store_plan(
    db: AsyncSession, parent: Document, verdict: SplitVerdict, user_id: int | None
) -> None:
    """Persist the confident verdict BEFORE executing it, so a crash-resume
    replays THIS plan instead of re-running the nondeterministic boundary LLM
    (whose drift could silently drop pages from the resume-keyed parts)."""
    db.add(
        PdfSplitProposal(
            document_id=parent.id,
            user_id=user_id,
            status=PDF_SPLIT_PROPOSAL_APPROVED,
            proposal=[p.to_dict() for p in verdict.pieces],
            page_signals=[s.to_dict() for s in verdict.page_signals],
            page_count=verdict.page_signals[-1].page if verdict.page_signals else 0,
            overall_confidence=verdict.min_confidence,
            resolved_at=datetime.now(UTC).replace(tzinfo=None),
        )
    )
    await db.commit()


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

    Guards: ``pdf_split_enabled`` off (authoritative gate — the worker's own
    flag check only avoids the lazy import), a ``skip_split`` task param (the
    loop-breaker set by treat-as-single re-enqueues), a split CHILD
    (``split_from_document_id`` set — children never re-enter detection, which
    would waste an LLM call per child and risk recursive re-splitting), or a
    non-PDF are all no-ops.

    Error taxonomy: detection errors degrade to False (detection must never
    break ingest) EXCEPT transient LLM-infra failures, which raise
    :class:`SplitTransientError` so the worker PEL-retries instead of
    permanently committing a multi-document PDF as one document. An error while
    EXECUTING a split always propagates — falling through to normal ingest
    after children were partially created would double-ingest the combined
    file.
    """
    if not settings.pdf_split_enabled or skip_split:
        return False
    doc = await db.get(Document, doc_id)
    if doc is None:
        return False
    if doc.status in SPLIT_OWNED_STATUSES:
        # Redelivery of an entry whose doc the split lifecycle already owns.
        # (The worker also guards this OUTSIDE the flag gate — see
        # _split_owned_status in the worker — so a flag-off rollback can't
        # resurrect an archived parent either.)
        return True
    if doc.split_from_document_id is not None:
        return False
    if not (doc.filename or doc.file_path or "").lower().endswith(".pdf"):
        return False

    # -- Resume: a persisted plan replays verbatim (never re-detect) --
    loop = asyncio.get_running_loop()
    try:
        page_count = await loop.run_in_executor(
            None, _pdfium_page_count_safe, doc.file_path
        )
        stored = await _load_stored_plan(db, doc.id, page_count or 10**9)
    except Exception as e:  # noqa: BLE001 - plan lookup must not break ingest
        logger.warning(f"pdf-split: stored-plan lookup failed for doc {doc_id}: {e}")
        stored = None
    if stored is not None:
        await execute_split(db, doc, stored, user_id=user_id)
        return True

    # -- Detection (best-effort; failures → single-document status quo,
    #    transient LLM failures → SplitTransientError, see docstring) --
    try:
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
    except SplitTransientError:
        raise
    except Exception as e:  # noqa: BLE001 - detection must never break ingest
        logger.warning(f"pdf-split: detection failed for doc {doc_id}: {e}")
        return False

    if verdict.kind != VERDICT_MULTI:
        return False
    if verdict.min_confidence < settings.pdf_split_auto_threshold:
        # PR2 files a pending pdf_split_proposals row for owner review. Until
        # the review surface exists, holding the doc would strand it: status quo.
        logger.warning(
            f"pdf-split: doc {doc.id} looks like {len(verdict.pieces)} "
            f"documents but min confidence {verdict.min_confidence:.2f} < "
            f"{settings.pdf_split_auto_threshold} — processing as a single "
            f"document until the review flow ships"
        )
        return False

    # -- Execution: persist the plan FIRST, then execute (NOT swallowed) --
    await _store_plan(db, doc, verdict, user_id)
    await execute_split(db, doc, verdict.pieces, user_id=user_id)
    return True


def _pdfium_page_count_safe(file_path: str) -> int | None:
    from services.pdf_split_detector import _pdfium_page_count

    return _pdfium_page_count(file_path)
