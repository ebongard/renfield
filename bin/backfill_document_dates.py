#!/usr/bin/env python3
"""Backfill / re-derive ``documents.document_date`` from stored Schicht-A facts.

Default mode: for every completed document with no ``document_date`` yet, derive
the document's own date from its ``document_facts`` using the SAME
``services.document_date.derive_document_date`` helper the ingest hook uses
(explicit document-date kinds; never an obligation/deadline; event dates and the
title only up to the import date + tolerance), and store it. Documents with no
derivable date are left NULL (sorted last).

``--rederive``: re-derive documents that ALREADY have a date
(``services.document_date_backfill``):

* ``--scope future`` (default): only dates more than ``FUTURE_TOLERANCE_DAYS``
  after the import — the visible pre-fix shape;
* ``--scope all``: every dated document — also repairs a past deadline taken as
  the document date.

A document is written only where the fresh derivation differs, so a correct
date is never overwritten and a second run is a no-op. Outcomes:
``unchanged`` / ``changed_earlier`` / ``changed_later`` / ``cleared``. Output is
ids and counts only.

``--paperless-ids-out FILE`` writes the Paperless ids of the CHANGED documents,
one per line — the documents whose Paperless ``created`` date the follow-up
``bin/backfill_paperless_metadata.py --mode created-date`` should revisit.
``cleared`` documents are NOT in that file (their date is now NULL; the
created-date backfill skips NULL dates and must not touch them) and are printed
separately.

Usage:
    python bin/backfill_document_dates.py --dry-run
    python bin/backfill_document_dates.py --commit [--limit N]
    python bin/backfill_document_dates.py --rederive [--scope future|all] --dry-run
    python bin/backfill_document_dates.py --rederive --scope all --commit \\
        --paperless-ids-out /tmp/document_date_changed_pids.txt
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src" / "backend"))

from sqlalchemy import select  # noqa: E402

from models.database import DOC_STATUS_COMPLETED, Document  # noqa: E402
from services.database import AsyncSessionLocal  # noqa: E402
from services.document_date_backfill import (  # noqa: E402
    SCOPE_FUTURE,
    SCOPES,
    derive_for_document,
    rederive_document_dates,
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
            ddate = await derive_for_document(db, doc)
            if ddate is None:
                continue
            set_count += 1
            print(f"  doc {doc.id} -> dated{'' if commit else ' (dry-run)'}")
            if commit:
                doc.document_date = ddate

        if commit and set_count:
            await db.commit()

    print(
        f"done: scanned {scanned} doc(s) without a date, "
        f"{set_count} dated{'' if commit else ' (dry-run, no writes)'}"
    )


async def rederive(commit: bool, limit: int | None, scope: str, ids_out: str | None) -> None:
    report = await rederive_document_dates(
        AsyncSessionLocal, scope=scope, commit=commit, limit=limit
    )
    print(json.dumps({
        "summary": report.summary(),
        "document_ids": {
            "changed_earlier": report.changed_earlier,
            "changed_later": report.changed_later,
            "cleared": report.cleared,
        },
        "paperless_ids": report.paperless_ids,
        "cleared_paperless_ids_do_not_backfill": report.cleared_paperless_ids,
    }, indent=2))
    if ids_out:
        Path(ids_out).write_text("".join(f"{pid}\n" for pid in report.paperless_ids))
        print(f"wrote {len(report.paperless_ids)} Paperless id(s) to {ids_out}")
    if not commit:
        print("dry-run: no writes")


def main() -> None:
    ap = argparse.ArgumentParser(description="Backfill / re-derive documents.document_date from facts")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--dry-run", action="store_true", help="report only, no writes")
    g.add_argument("--commit", action="store_true", help="persist derived dates")
    ap.add_argument("--limit", type=int, default=None, help="cap documents scanned")
    ap.add_argument("--rederive", action="store_true",
                    help="re-derive documents that already have a date")
    ap.add_argument("--scope", choices=SCOPES, default=SCOPE_FUTURE,
                    help="with --rederive: 'future' (dated past import+tolerance, default) or 'all'")
    ap.add_argument("--paperless-ids-out", default=None,
                    help="with --rederive: write the changed documents' Paperless ids (one per line)")
    args = ap.parse_args()
    if not args.rederive and (args.scope != SCOPE_FUTURE or args.paperless_ids_out):
        ap.error("--scope / --paperless-ids-out require --rederive")
    if args.rederive:
        asyncio.run(rederive(args.commit, args.limit, args.scope, args.paperless_ids_out))
    else:
        asyncio.run(run(commit=args.commit, limit=args.limit))


if __name__ == "__main__":
    main()
