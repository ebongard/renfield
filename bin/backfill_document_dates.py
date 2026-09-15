#!/usr/bin/env python3
"""Backfill ``documents.document_date`` from stored Schicht-A facts (#/wissen sort).

Default mode: for every completed document with no ``document_date`` yet, derive
the document's own date from its ``document_facts`` using the SAME
``services.document_date.derive_document_date`` helper the ingest hook uses
(explicit document-date kinds; never an obligation/deadline; event dates and the
title only up to the import date + tolerance), and store it. Documents with no
derivable date are left NULL (sorted last).

``--rederive``: re-derive ONLY documents whose stored ``document_date`` lies more
than ``FUTURE_TOLERANCE_DAYS`` after their import date — the shape the pre-fix
derivation produced from deadlines / validity / next-period facts. It never
touches a document outside that set, so a correct past date cannot be
overwritten. Each document gets one outcome: ``unchanged`` (the stored date is
still what the fixed derivation yields — e.g. a pre-dated invoice), ``changed``
(another source now wins), ``cleared`` (no trusted source → NULL). Output is ids
and outcomes only.

Usage:
    python bin/backfill_document_dates.py --dry-run
    python bin/backfill_document_dates.py --commit [--limit N]
    python bin/backfill_document_dates.py --rederive --dry-run
    python bin/backfill_document_dates.py --rederive --commit
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from collections import Counter
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src" / "backend"))

from sqlalchemy import select  # noqa: E402

from models.database import DOC_STATUS_COMPLETED, Document, DocumentFact  # noqa: E402
from services.database import AsyncSessionLocal  # noqa: E402
from services.document_date import FUTURE_TOLERANCE_DAYS, derive_document_date  # noqa: E402


async def _derive_for(db, doc):
    facts = (
        await db.execute(
            select(
                DocumentFact.category,
                DocumentFact.kind,
                DocumentFact.normalized_value,
                DocumentFact.value,
            ).where(DocumentFact.document_id == doc.id)
        )
    ).all()
    created = doc.created_at.date() if doc.created_at else None
    return derive_document_date(
        [tuple(f) for f in facts],
        [doc.generated_title, doc.title],
        reference_date=created,
    )


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
            ddate = await _derive_for(db, doc)
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


async def rederive(commit: bool, limit: int | None) -> None:
    outcomes: Counter[str] = Counter()
    async with AsyncSessionLocal() as db:
        docs = (
            await db.execute(
                select(Document)
                .where(
                    Document.status == DOC_STATUS_COMPLETED,
                    Document.document_date.is_not(None),
                    Document.created_at.is_not(None),
                )
                .order_by(Document.id)
            )
        ).scalars().all()
        affected = [
            d for d in docs
            if d.document_date > d.created_at.date() + timedelta(days=FUTURE_TOLERANCE_DAYS)
        ]
        if limit:
            affected = affected[:limit]

        for doc in affected:
            ddate = await _derive_for(db, doc)
            if ddate == doc.document_date:
                outcome = "unchanged"
            elif ddate is None:
                outcome = "cleared"
            else:
                outcome = "changed"
            outcomes[outcome] += 1
            print(f"  doc {doc.id}: {outcome}{'' if commit else ' (dry-run)'}")
            if commit and outcome != "unchanged":
                doc.document_date = ddate

        if commit and (outcomes["changed"] or outcomes["cleared"]):
            await db.commit()

    print(
        f"done: {len(affected)} doc(s) dated > import+{FUTURE_TOLERANCE_DAYS}d — "
        f"unchanged={outcomes['unchanged']} changed={outcomes['changed']} "
        f"cleared={outcomes['cleared']}{'' if commit else ' (dry-run, no writes)'}"
    )


def main() -> None:
    ap = argparse.ArgumentParser(description="Backfill documents.document_date from facts")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--dry-run", action="store_true", help="report only, no writes")
    g.add_argument("--commit", action="store_true", help="persist derived dates")
    ap.add_argument("--limit", type=int, default=None, help="cap documents scanned")
    ap.add_argument(
        "--rederive", action="store_true",
        help="re-derive only documents dated more than the tolerance after their import",
    )
    args = ap.parse_args()
    job = rederive if args.rederive else run
    asyncio.run(job(commit=args.commit, limit=args.limit))


if __name__ == "__main__":
    main()
