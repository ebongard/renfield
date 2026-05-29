#!/usr/bin/env python3
"""
Purge low-quality chunks — reprocess transition tool.

Default: reprocess low-quality docs through the normal quality gate, which OCRs
only when the text layer is garbled/absent (and unions the raw text layer in,
per Schicht A T-A0-2). Pass ``--force-ocr`` ONLY for a batch you know is
genuinely scanned/garbled — forcing OCR on a good digital PDF degrades it by
dropping positioned text-layer tokens. (Historically this script hardcoded
force_ocr=True, which degraded text-layer PDFs; that is now opt-in.)

ASCII flow:

::

    ┌─────────────────┐    keyset-page over           ┌────────────────┐
    │ documents table │ ─── id > last_id              │ scan candidates│
    └─────────────────┘    LIMIT batch_size           └────────┬───────┘
                                                              │
                                       per doc                ▼
                                ┌──────────────────────────────────────┐
                                │ count_low_quality_chunks(doc)        │
                                │   (Python ratio via                  │
                                │    utils.content_quality)            │
                                └──────────────────────┬───────────────┘
                                                       │ ≥ threshold?
                                                       │ yes
                                                       ▼
                                ┌──────────────────────────────────────┐
                                │ already processed? (idempotence)     │ ─yes→ skip
                                │  force mode: has_force_ocr_succeeded │
                                │  default:    has_successful_gate_    │
                                │              reprocess               │
                                └──────────────────────┬───────────────┘
                                                       │ no
                                                       ▼
                                ┌──────────────────────────────────────┐
                                │ pg_try_advisory_lock(NS, doc_id)     │ ─false→ skip
                                │ (raw asyncpg conn — bypasses pool    │
                                │  checkin hook that drops locks)      │
                                └──────────────────────┬───────────────┘
                                                       │ true
                                                       ▼
                                ┌──────────────────────────────────────┐
                                │ BEGIN; SELECT ... FOR UPDATE NOWAIT  │ ─lock_not_avail→ skip
                                │ on documents row                     │
                                └──────────────────────┬───────────────┘
                                                       │ acquired
                                                       ▼
                                ┌──────────────────────────────────────┐
                                │ rag.reindex_document(                │
                                │   force_ocr=args.force_ocr,          │
                                │   trigger=SCRIPT_PURGE)              │
                                │ → history row written by track()     │
                                └──────────────────────┬───────────────┘
                                                       │
                                                       ▼
                                          release advisory lock,
                                          increment counters

Safety:
    - Advisory lock (per-doc, dedicated asyncpg connection) protects
      against script-vs-script races for the FULL duration of a re-OCR.
    - FOR UPDATE NOWAIT on the documents row is a START gate only — it
      detects an in-flight API reindex of the same doc and skips, but
      the lock is released BEFORE ``reindex_document`` runs (it can't
      span the reindex because reindex commits multiple times). If an
      API user reindexes the same doc concurrently with a script run,
      the two writers race. Mitigation: run the script in an offline
      window with no user-driven reindexes.
    - Dry-run by DEFAULT. Pass ``--apply`` to mutate.
    - Gate-decides by DEFAULT. Pass ``--force-ocr`` only for genuinely
      scanned/garbled batches (forcing OCR on a good text-layer PDF degrades it).
    - Idempotent in BOTH modes (repeated runs converge):
      * ``--force-ocr``: ``has_force_ocr_succeeded`` skips docs already force-OCR'd.
      * default: ``has_successful_gate_reprocess`` skips docs already
        gate-reprocessed (a ``force_ocr=false`` script_purge success). Old
        ``force_ocr=true`` rows do NOT count, so a previously force-OCR'd doc is
        still reprocessed ONCE (recovering text-layer tokens an earlier forced
        run dropped), then skipped thereafter.
    - Per-doc fault isolation: a single failure is logged and the loop
      continues with the next doc.

Usage::

    # Dry-run scan of the whole catalogue
    python bin/purge_low_quality_chunks.py

    # Apply to one doc
    python bin/purge_low_quality_chunks.py --apply --doc-id 4711

    # Apply across catalogue, in batches of 500, low threshold
    python bin/purge_low_quality_chunks.py --apply \\
        --batch-size 500 --reason-threshold 1

    # Hard cap on docs touched in this run (parachute)
    python bin/purge_low_quality_chunks.py --apply --limit 100
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path

# Put src/backend on PYTHONPATH so this script can be run as ``python bin/...``
# from the repo root without installing the package.
_BACKEND = Path(__file__).resolve().parent.parent / "src" / "backend"
sys.path.insert(0, str(_BACKEND))

import asyncpg  # noqa: E402
from sqlalchemy import select, text  # noqa: E402

from models.database import Document, DocumentChunk  # noqa: E402
from services.database import AsyncSessionLocal  # noqa: E402
from services.document_processing_history import (  # noqa: E402
    DocumentProcessingHistoryService,
    ProcessingTrigger,
)
from services.rag_service import RAGService  # noqa: E402
from utils.config import settings  # noqa: E402
from utils.content_quality import is_low_quality_text  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger("purge_low_quality_chunks")

# Advisory-lock namespace key (high 32 bits of the 2-arg
# pg_try_advisory_lock(key1, key2) signature). Fixed magic — keeps script
# locks disjoint from the memory-extraction advisory locks
# (see services/database.py).
_ADVISORY_NAMESPACE_HI = 0x7E0CC1AB


# ----------------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="purge_low_quality_chunks.py",
        description="Re-OCR documents whose chunks contain OCR garbage.",
    )
    p.add_argument(
        "--apply",
        action="store_true",
        help="Actually mutate. Default = dry-run (scan + report only).",
    )
    p.add_argument(
        "--doc-id",
        type=int,
        default=None,
        help="Process exactly one document. Skips the full-catalogue scan.",
    )
    p.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Max number of documents to PROCESS (after threshold + idempotence "
        "filters) in this run. Parachute for first-time use.",
    )
    p.add_argument(
        "--batch-size",
        type=int,
        default=500,
        help="Keyset page size for the document scan. Default 500.",
    )
    p.add_argument(
        "--reason-threshold",
        type=int,
        default=1,
        help="Min # of low-quality chunks in a document to make it a candidate. "
        "Default 1 (any).",
    )
    p.add_argument(
        "--force-ocr",
        action="store_true",
        help="Force full-page OCR on reprocess (ignores the embedded text layer). "
        "DEFAULT OFF: reprocess via the normal quality gate, which OCRs only when "
        "the text layer is garbled/absent. Forcing OCR on a good digital PDF "
        "DEGRADES it (drops positioned text-layer tokens — see Schicht A T-A0-2). "
        "Use this only for a batch you KNOW is genuinely scanned/garbled.",
    )
    return p


def _validate_args(args: argparse.Namespace) -> None:
    if args.batch_size <= 0:
        raise SystemExit("--batch-size must be > 0")
    if args.reason_threshold < 1:
        raise SystemExit("--reason-threshold must be >= 1")
    if args.limit is not None and args.limit <= 0:
        raise SystemExit("--limit must be > 0")
    if args.doc_id is not None and args.doc_id <= 0:
        raise SystemExit("--doc-id must be > 0")


# ----------------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------------

def _asyncpg_dsn() -> str:
    """asyncpg wants a plain ``postgresql://`` DSN (no SQLAlchemy ``+asyncpg``)."""
    raw = settings.database_url
    return raw.replace("postgresql+asyncpg://", "postgresql://")


async def _count_low_quality_chunks(session, document_id: int) -> int:
    """Stream the doc's chunks; return how many trip is_low_quality_text.

    Streams via ``yield_per``-equivalent paging so we never load all chunks
    for a giant document into memory at once.
    """
    page = 1000
    last_id = 0
    found = 0
    while True:
        stmt = (
            select(DocumentChunk.id, DocumentChunk.content)
            .where(DocumentChunk.document_id == document_id)
            .where(DocumentChunk.id > last_id)
            .order_by(DocumentChunk.id.asc())
            .limit(page)
        )
        rows = (await session.execute(stmt)).all()
        if not rows:
            return found
        for row_id, content in rows:
            if is_low_quality_text(content):
                found += 1
        last_id = rows[-1][0]


async def _try_lock_doc(lock_conn: asyncpg.Connection, doc_id: int) -> bool:
    """Acquire script-vs-script advisory lock. False if another process holds it."""
    return await lock_conn.fetchval(
        "SELECT pg_try_advisory_lock($1, $2)",
        _ADVISORY_NAMESPACE_HI,
        doc_id,
    )


async def _release_lock(lock_conn: asyncpg.Connection, doc_id: int) -> None:
    await lock_conn.fetchval(
        "SELECT pg_advisory_unlock($1, $2)",
        _ADVISORY_NAMESPACE_HI,
        doc_id,
    )


async def _try_row_lock(session, doc_id: int) -> bool:
    """SELECT ... FOR UPDATE NOWAIT on the documents row.

    Returns True if locked, False if another writer holds it (NOWAIT
    fires immediately rather than blocking). The lock auto-releases when
    the surrounding transaction commits/rolls back.
    """
    try:
        result = await session.execute(
            text("SELECT id FROM documents WHERE id = :id FOR UPDATE NOWAIT"),
            {"id": doc_id},
        )
        return result.scalar() is not None
    except Exception as e:  # pragma: no cover — asyncpg LockNotAvailableError
        # Postgres SQLSTATE 55P03 = lock_not_available
        if "55P03" in str(e) or "lock_not_available" in str(e).lower():
            return False
        raise


# ----------------------------------------------------------------------------
# Per-doc processing
# ----------------------------------------------------------------------------

async def _process_one(
    lock_conn: asyncpg.Connection,
    doc_id: int,
    *,
    apply: bool,
    reason_threshold: int,
    force_ocr: bool,
) -> str:
    """Process one document; returns a result code for the run summary.

    Result codes: ``skipped_already_re_ocrd`` (force mode),
    ``skipped_already_reprocessed`` (default mode), ``skipped_below_threshold``,
    ``skipped_locked_by_script``, ``skipped_row_locked``, ``would_purge``
    (dry-run), ``purged`` (apply), ``error``.
    """
    # Open a fresh session for THIS doc so its transaction is short-lived
    # and the FOR UPDATE NOWAIT row lock doesn't span the whole script run.
    async with AsyncSessionLocal() as session:
        # Count low-quality chunks (read-only, no transaction needed).
        low_q = await _count_low_quality_chunks(session, doc_id)
        if low_q < reason_threshold:
            return "skipped_below_threshold"

        # Idempotence guards (both ensure repeated runs converge):
        #   --force-ocr mode: skip docs already force-OCR'd (don't re-force).
        #   default mode:     skip docs already GATE-reprocessed (force_ocr=false
        #                     script_purge success). A doc's *old* force_ocr=true
        #                     rows do NOT count here, so the recovery pass still
        #                     reprocesses the previously force-OCR'd corpus once;
        #                     after that the force_ocr=false row makes it skip.
        hist = DocumentProcessingHistoryService(session)
        if force_ocr:
            if await hist.has_force_ocr_succeeded(doc_id):
                logger.info("doc_id=%s already force-OCR'd → skip", doc_id)
                return "skipped_already_re_ocrd"
        elif await hist.has_successful_gate_reprocess(doc_id):
            logger.info("doc_id=%s already gate-reprocessed → skip", doc_id)
            return "skipped_already_reprocessed"

        if not apply:
            logger.info(
                "doc_id=%s would re-OCR (%s low-quality chunks)", doc_id, low_q
            )
            return "would_purge"

        # Script-vs-script gate.
        if not await _try_lock_doc(lock_conn, doc_id):
            logger.warning("doc_id=%s locked by another script → skip", doc_id)
            return "skipped_locked_by_script"

        try:
            # Script-vs-API START gate. SELECT FOR UPDATE NOWAIT inside a
            # short-lived explicit transaction tells us "no one is mutating
            # this doc RIGHT NOW". The lock auto-releases on commit at the
            # end of the ``async with session.begin()`` block — it does NOT
            # span the subsequent ``reindex_document`` call. That's a
            # deliberate trade: holding the row lock across reindex is
            # impossible because reindex_document does its own commits, which
            # would close the outer txn. So if an API user reindexes the same
            # doc CONCURRENTLY with our reindex, the two writers race on the
            # chunks. Mitigation: the corpus-cleanup script is meant for an
            # offline window. The advisory lock (different connection) still
            # protects against script-vs-script during the reindex.
            #
            # The earlier read queries (_count_low_quality_chunks,
            # has_force_ocr_succeeded) auto-begin an implicit txn. SA 2.0
            # rejects ``session.begin()`` while a txn is already open, so
            # roll back the implicit one first. Safe because no writes
            # have happened yet on this session.
            await session.rollback()
            async with session.begin():
                if not await _try_row_lock(session, doc_id):
                    logger.warning(
                        "doc_id=%s documents row locked → skip", doc_id
                    )
                    return "skipped_row_locked"

            rag = RAGService(session)
            await rag.reindex_document(
                document_id=doc_id,
                force_ocr=force_ocr,
                trigger=ProcessingTrigger.SCRIPT_PURGE,
            )
            logger.info(
                "doc_id=%s purged + reprocessed (%s)",
                doc_id,
                "forced OCR" if force_ocr else "gate-decided",
            )
            return "purged"
        except Exception as e:
            # Per-doc fault isolation — log and let the caller continue.
            logger.error("doc_id=%s FAILED: %s", doc_id, e, exc_info=True)
            return "error"
        finally:
            await _release_lock(lock_conn, doc_id)


# ----------------------------------------------------------------------------
# Catalogue scan
# ----------------------------------------------------------------------------

async def _iter_doc_ids(batch_size: int):
    """Keyset-paginate over document ids. Yields one id at a time."""
    last_id = 0
    while True:
        async with AsyncSessionLocal() as session:
            stmt = (
                select(Document.id)
                .where(Document.id > last_id)
                .order_by(Document.id.asc())
                .limit(batch_size)
            )
            ids = [r[0] for r in (await session.execute(stmt)).all()]
        if not ids:
            return
        for doc_id in ids:
            yield doc_id
        last_id = ids[-1]


async def _run(args: argparse.Namespace) -> int:
    counters: dict[str, int] = {}
    processed_count = 0

    lock_conn = await asyncpg.connect(dsn=_asyncpg_dsn())
    try:
        if args.doc_id is not None:
            doc_ids_iter = _single_id(args.doc_id)
        else:
            doc_ids_iter = _iter_doc_ids(args.batch_size)

        async for doc_id in doc_ids_iter:
            code = await _process_one(
                lock_conn,
                doc_id,
                apply=args.apply,
                reason_threshold=args.reason_threshold,
                force_ocr=args.force_ocr,
            )
            counters[code] = counters.get(code, 0) + 1

            if code in ("would_purge", "purged"):
                processed_count += 1
                if args.limit is not None and processed_count >= args.limit:
                    logger.info("--limit %s reached → stop", args.limit)
                    break
    finally:
        await lock_conn.close()

    _print_summary(counters, applied=args.apply)
    return 0 if counters.get("error", 0) == 0 else 1


async def _single_id(doc_id: int):
    yield doc_id


def _print_summary(counters: dict[str, int], *, applied: bool) -> None:
    mode = "APPLY" if applied else "DRY-RUN"
    logger.info("=" * 60)
    logger.info("Summary (%s)", mode)
    logger.info("-" * 60)
    for code in (
        "would_purge",
        "purged",
        "skipped_already_re_ocrd",
        "skipped_below_threshold",
        "skipped_locked_by_script",
        "skipped_row_locked",
        "error",
    ):
        if counters.get(code):
            logger.info("  %-30s %d", code, counters[code])
    logger.info("=" * 60)


def main() -> int:
    args = _build_parser().parse_args()
    _validate_args(args)
    return asyncio.run(_run(args))


if __name__ == "__main__":
    sys.exit(main())
