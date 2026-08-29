#!/usr/bin/env python3
"""
Backfill documents.paperless_document_id from Paperless, matched by checksum.

The Paperless consume task frequently settles ``success`` with NO
``related_document``, even though Paperless created/holds the document — so the
filing leg recorded ``paperless_state='done'`` but left ``paperless_document_id``
NULL (observed across the whole xidra corpus: 398 done docs, 0 linked). Without
the id there is no KB↔Paperless deep-link and no way to verify a doc is really in
Paperless. renfield's ``documents.file_hash`` equals Paperless's ``checksum``, so
we can resolve the real id with one read-only ``?checksum__iexact=`` query per doc.

This is the batch companion to the live fix in
``services.folder_ingest_paperless`` (#1166) — it links the docs that were filed
BEFORE the fix. Idempotent: rows that already have a ``paperless_document_id`` are
skipped, and a doc with no checksum match in Paperless is left NULL (genuinely not
filed → surfaces honestly). Per-document commit, so a crash keeps earlier links.

ALWAYS --dry-run first (prints what WOULD link, no writes).

Usage:
    python bin/backfill_paperless_document_ids.py --dry-run             # preview
    python bin/backfill_paperless_document_ids.py --commit              # link
    python bin/backfill_paperless_document_ids.py --commit --limit 100  # cap
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

_BACKEND = Path(__file__).resolve().parent.parent / "src" / "backend"
sys.path.insert(0, str(_BACKEND))

from sqlalchemy import select  # noqa: E402

from models.database import Document  # noqa: E402
from services.database import AsyncSessionLocal  # noqa: E402
from services.folder_ingest_paperless import _resolve_paperless_id_by_checksum  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
logger = logging.getLogger("backfill_paperless_document_ids")


async def run(commit: bool, limit: int | None) -> None:
    async with AsyncSessionLocal() as db:
        stmt = (
            select(Document)
            .where(
                Document.paperless_document_id.is_(None),
                Document.file_hash.isnot(None),
                Document.paperless_state == "done",
            )
            .order_by(Document.id)
        )
        if limit:
            stmt = stmt.limit(limit)
        docs = (await db.execute(stmt)).scalars().all()
        logger.info(f"{len(docs)} done-but-unlinked documents to check")

        linked = missing = 0
        for doc in docs:
            pid = await _resolve_paperless_id_by_checksum(doc.file_hash)
            if pid is None:
                missing += 1
                logger.info(f"  doc {doc.id}: NO checksum match in Paperless (genuinely not filed?)")
                continue
            linked += 1
            logger.info(f"  doc {doc.id} -> paperless_id={pid}{'' if commit else ' (dry-run)'}")
            if commit:
                doc.paperless_document_id = pid
                await db.commit()

        logger.info(
            f"done: {linked} linked{'' if commit else ' (dry-run, no writes)'}, "
            f"{missing} with no Paperless match"
        )


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--dry-run", action="store_true", help="preview only (no writes)")
    g.add_argument("--commit", action="store_true", help="persist the resolved ids")
    ap.add_argument("--limit", type=int, default=None, help="cap documents processed")
    args = ap.parse_args()
    asyncio.run(run(commit=args.commit, limit=args.limit))


if __name__ == "__main__":
    main()
