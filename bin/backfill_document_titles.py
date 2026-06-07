#!/usr/bin/env python3
"""
Backfill documents.generated_title from already-extracted Schicht A facts.

For every document that HAS facts but no generated_title, synthesize a short
human-meaningful title (issuer + document type + date) via
``services.schicht_a_extractor.generate_document_title`` — working from the stored
facts, NOT re-OCR/re-extraction. Idempotent: rows that already have a
generated_title are skipped, so re-runs only fill the gaps. Per-document commit,
so a crash leaves earlier titles persisted.

ALWAYS --dry-run first (prints what WOULD be generated, no writes).

Runtime note: this is serial (one LLM call per document). For a large corpus run
in batches with --limit. Safe to run alongside live ingest — both derive the title
from the same facts, so a concurrent re-ingest is last-writer-wins with identical
content (no lock needed).

Usage:
    python bin/backfill_document_titles.py --dry-run            # preview, no writes
    python bin/backfill_document_titles.py --commit             # generate + store
    python bin/backfill_document_titles.py --commit --limit 50  # cap documents
    python bin/backfill_document_titles.py --commit --overwrite # also re-title rows that already have one
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

from models.database import Document, DocumentFact  # noqa: E402
from services.database import AsyncSessionLocal  # noqa: E402
from services.schicht_a_extractor import generate_document_title  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
logger = logging.getLogger("backfill_document_titles")


async def _run(*, commit: bool, limit: int | None, overwrite: bool) -> None:
    async with AsyncSessionLocal() as db:
        # Documents that have at least one fact.
        stmt = (
            select(Document)
            .where(Document.id.in_(select(DocumentFact.document_id).distinct()))
            .order_by(Document.id.desc())
        )
        if not overwrite:
            stmt = stmt.where(Document.generated_title.is_(None))
        if limit:
            stmt = stmt.limit(limit)
        docs = (await db.execute(stmt)).scalars().all()
        logger.info("%d document(s) to title (overwrite=%s)", len(docs), overwrite)

        generated = skipped = 0
        for doc in docs:
            facts = (await db.execute(
                select(DocumentFact).where(DocumentFact.document_id == doc.id)
            )).scalars().all()
            title = await generate_document_title(facts, lang="de")
            if not title:
                skipped += 1
                logger.info("  doc %s: no title (facts=%d) — skipped", doc.id, len(facts))
                continue
            logger.info("  doc %s: %r  (was %r)", doc.id, title, doc.generated_title or doc.title or doc.filename)
            if commit:
                doc.generated_title = title
                await db.commit()
            generated += 1

        logger.info("Done: %d titled, %d skipped%s", generated, skipped, "" if commit else "  (DRY-RUN — no writes)")


def main() -> int:
    p = argparse.ArgumentParser(description="Backfill documents.generated_title from Schicht A facts.")
    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true", help="Preview generated titles; no writes.")
    mode.add_argument("--commit", action="store_true", help="Generate + store titles.")
    p.add_argument("--limit", type=int, default=None, help="Cap the number of documents.")
    p.add_argument("--overwrite", action="store_true", help="Also re-title documents that already have a generated_title.")
    args = p.parse_args()
    asyncio.run(_run(commit=args.commit, limit=args.limit, overwrite=args.overwrite))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
