#!/usr/bin/env python3
"""Backfill ``documents.document_date`` from stored Schicht-A facts (#/wissen sort).

For every completed document with no ``document_date`` yet, derive the document's
own date (invoice/letter date) from its ``document_facts`` (rechnungsdatum → other
date facts → the generated title's date) using the SAME
``services.document_date.derive_document_date`` helper the ingest hook uses, and
store it. Documents with no derivable date are left NULL (sorted last).

Usage:
    python bin/backfill_document_dates.py --dry-run
    python bin/backfill_document_dates.py --commit [--limit N]
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src" / "backend"))

from sqlalchemy import select  # noqa: E402

from models.database import DOC_STATUS_COMPLETED, Document, DocumentFact  # noqa: E402
from services.database import AsyncSessionLocal  # noqa: E402
from services.document_date import derive_document_date  # noqa: E402


async def run(commit: bool, limit: int | None) -> None:
    set_count = 0
    scanned = 0
    async with AsyncSessionLocal() as db:
        q = (
            select(Document)
            .where(
                Document.status == DOC_STATUS_COMPLETED,
                Document.document_date.is_(None),
            )
            .order_by(Document.id)
        )
        if limit:
            q = q.limit(limit)
        docs = (await db.execute(q)).scalars().all()

        for doc in docs:
            scanned += 1
            facts = (
                await db.execute(
                    select(DocumentFact.kind, DocumentFact.normalized_value, DocumentFact.value)
                    .where(DocumentFact.document_id == doc.id)
                )
            ).all()
            fact_tuples = [(k, nv, v) for k, nv, v in facts]
            ddate = derive_document_date(fact_tuples, [doc.generated_title, doc.title])
            if ddate is None:
                continue
            set_count += 1
            print(f"  doc {doc.id} -> document_date={ddate.isoformat()}"
                  f"{'' if commit else ' (dry-run)'}")
            if commit:
                doc.document_date = ddate

        if commit and set_count:
            await db.commit()

    print(
        f"done: scanned {scanned} doc(s) without a date, "
        f"{set_count} dated{'' if commit else ' (dry-run, no writes)'}"
    )


def main() -> None:
    ap = argparse.ArgumentParser(description="Backfill documents.document_date from facts")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--dry-run", action="store_true", help="report only, no writes")
    g.add_argument("--commit", action="store_true", help="persist derived dates")
    ap.add_argument("--limit", type=int, default=None, help="cap documents scanned")
    args = ap.parse_args()
    asyncio.run(run(commit=args.commit, limit=args.limit))


if __name__ == "__main__":
    main()
