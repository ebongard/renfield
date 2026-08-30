#!/usr/bin/env python3
"""Backfill documents.content_embedding = mean of the doc's chunk embeddings.

Backs the KB near-duplicate TEXT-similarity signal (#1170 P3). For every completed
document with no content_embedding yet AND at least one embedded chunk, average its
``document_chunks.embedding`` vectors (np.mean, like the speaker centroid) and store
the result. Postgres-only. Idempotent (only NULL rows). No re-embedding — reuses the
chunk embeddings already computed at ingest.

Usage:
    python bin/backfill_document_content_embeddings.py --dry-run
    python bin/backfill_document_content_embeddings.py --commit [--limit N]
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src" / "backend"))

import numpy as np  # noqa: E402
from sqlalchemy import select  # noqa: E402

from models.database import DOC_STATUS_COMPLETED, Document, DocumentChunk  # noqa: E402
from services.database import AsyncSessionLocal  # noqa: E402


async def run(commit: bool, limit: int | None) -> None:
    set_count = 0
    scanned = 0
    async with AsyncSessionLocal() as db:
        if db.bind is None or db.bind.dialect.name != "postgresql":
            print("Not Postgres — content_embedding is a real vector only there; skipping.")
            return

        q = (
            select(Document.id)
            .where(
                Document.status == DOC_STATUS_COMPLETED,
                Document.content_embedding.is_(None),
            )
            .order_by(Document.id)
        )
        if limit:
            q = q.limit(limit)
        doc_ids = (await db.execute(q)).scalars().all()

        for doc_id in doc_ids:
            scanned += 1
            embs = (
                await db.execute(
                    select(DocumentChunk.embedding)
                    .where(
                        DocumentChunk.document_id == doc_id,
                        DocumentChunk.embedding.isnot(None),
                    )
                )
            ).scalars().all()
            vectors = [list(e) for e in embs if e is not None]
            if not vectors:
                continue
            mean = np.mean(np.array(vectors, dtype=float), axis=0).tolist()
            set_count += 1
            print(f"  doc {doc_id} -> content_embedding from {len(vectors)} chunk(s)"
                  f"{'' if commit else ' (dry-run)'}")
            if commit:
                doc = (await db.execute(select(Document).where(Document.id == doc_id))).scalar_one()
                doc.content_embedding = mean
                await db.flush()

        if commit and set_count:
            await db.commit()

    print(
        f"done: scanned {scanned} doc(s) without an embedding, "
        f"{set_count} embedded{'' if commit else ' (dry-run, no writes)'}"
    )


def main() -> None:
    ap = argparse.ArgumentParser(description="Backfill documents.content_embedding from chunk embeddings")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--dry-run", action="store_true", help="report only, no writes")
    g.add_argument("--commit", action="store_true", help="persist mean embeddings")
    ap.add_argument("--limit", type=int, default=None, help="cap documents scanned")
    args = ap.parse_args()
    asyncio.run(run(commit=args.commit, limit=args.limit))


if __name__ == "__main__":
    main()
