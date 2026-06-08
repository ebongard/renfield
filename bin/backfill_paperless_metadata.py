#!/usr/bin/env python3
"""
Backfill the Paperless **correspondent** for folder-ingested documents that were
filed without one.

Two cohorts are repaired:
  - the "broken-window" docs uploaded while Docling was unavailable (the
    transformers<5 / torch-2.6 outage) → metadata extraction had failed entirely,
    so they were bare-uploaded (filename title, no correspondent);
  - docs whose sender simply wasn't an existing Paperless correspondent, so the
    autonomous folder-ingest leg left the field blank (pre-Option-A behaviour).

For each candidate it RE-RUNS the metadata extraction (Docling works now),
resolves-or-creates the correspondent with the SAME full-taxonomy guardrail as
the live leg (``services.folder_ingest_paperless.resolve_or_create_correspondent``),
and PATCHes the Paperless document via ``mcp.paperless.update_document``.

Idempotent + conservative:
  - only touches docs with ``paperless_state='done'`` (folder-ingest filed);
  - SKIPS any Paperless document that already has a correspondent (gap-fill only);
  - sets ONLY the correspondent — never title/type/tags (so manual edits stand);
  - locates the Paperless doc by the stored ``paperless_document_id`` when present
    (filed after pc20260613), else by a created-date window + exact
    ``original_file_name`` match (the historical gap), skipping on any ambiguity.

ALWAYS --dry-run first.

Usage:
    python bin/backfill_paperless_metadata.py --dry-run
    python bin/backfill_paperless_metadata.py --commit
    python bin/backfill_paperless_metadata.py --commit --limit 50
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

from models.database import PAPERLESS_STATE_DONE, Document  # noqa: E402
from services.database import AsyncSessionLocal  # noqa: E402
from services.folder_ingest_paperless import (  # noqa: E402
    _fetch_correspondent_names,
    _parse_paperless_result,
    resolve_correspondent_from_metadata,
)
from services.paperless_metadata_extractor import PaperlessMetadataExtractor  # noqa: E402
from utils.config import settings  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
logger = logging.getLogger("backfill_paperless_metadata")


async def _build_mcp_manager():
    """A connected MCPManager, mirroring api.lifecycle's construction."""
    from services.mcp_client import MCPManager

    manager = MCPManager()
    manager.load_config(settings.mcp_config_path)
    await manager.connect_all()
    return manager


async def _build_filename_index(manager) -> dict[str, int]:
    """``{original_file_name(lower): paperless_id}`` over the most recently ADDED
    Paperless documents (one pass). Keyed on the upload filename — the leg sends
    ``title``+``filename = meta.filename`` — which sidesteps the document-date vs
    ingest-date mismatch a ``created`` window would suffer (Paperless's ``created``
    is the parsed *document* date, not when we filed it). Bounded by max_results,
    so the backfill targets recent ingests (older docs need the stored id)."""
    search = _parse_paperless_result(
        await manager.execute_tool(
            "mcp.paperless.search_documents", {"ordering": "-added", "max_results": 500}
        )
    )
    index: dict[str, int] = {}
    for r in search.get("results") or []:
        pid = r.get("id")
        if pid is None:
            continue
        got = _parse_paperless_result(
            await manager.execute_tool("mcp.paperless.get_document", {"document_id": pid})
        )
        ofn = (got.get("original_file_name") or "").strip().lower()
        if ofn and ofn not in index:  # most-recently-added wins on a duplicate filename
            index[ofn] = pid
    return index


async def _run(*, commit: bool, limit: int | None) -> None:
    manager = await _build_mcp_manager()
    extractor = PaperlessMetadataExtractor(mcp_manager=manager)
    fixed = skipped = no_corr = unmatched = already = 0
    try:
        names = await _fetch_correspondent_names(manager)  # full taxonomy, fetched once
        async with AsyncSessionLocal() as db:
            stmt = (
                select(Document)
                .where(Document.paperless_state == PAPERLESS_STATE_DONE)
                .order_by(Document.id.desc())
            )
            if limit:
                stmt = stmt.limit(limit)
            docs = (await db.execute(stmt)).scalars().all()
            logger.info("%d filed folder-ingest document(s) to check", len(docs))

            # Build the filename→id index once, only if some doc lacks a stored id.
            index: dict[str, int] = {}
            if any(d.paperless_document_id is None for d in docs):
                index = await _build_filename_index(manager)

            for doc in docs:
                pid = doc.paperless_document_id or index.get((doc.filename or "").strip().lower())
                if pid is None:
                    unmatched += 1
                    logger.info("  doc %s (%s): no Paperless match — skipped", doc.id, doc.filename)
                    continue

                got = _parse_paperless_result(
                    await manager.execute_tool("mcp.paperless.get_document", {"document_id": pid})
                )
                if got.get("error"):
                    unmatched += 1
                    logger.info("  doc %s: get_document(%s) error: %s", doc.id, pid, got.get("error"))
                    continue
                if (got.get("correspondent") or "").strip():
                    already += 1
                    continue  # gap-fill only — never overwrite an existing correspondent

                if not doc.file_path or not Path(doc.file_path).exists():
                    skipped += 1
                    logger.info("  doc %s: recovery file missing (%s) — skipped", doc.id, doc.file_path)
                    continue

                result = await extractor.extract_from_file(
                    doc.file_path, user_id=getattr(doc, "user_id", None), lang="de"
                )
                if result.error:
                    skipped += 1
                    logger.info("  doc %s: extraction failed (%s) — skipped", doc.id, result.error)
                    continue

                # Same resolve-or-create path as the live leg (shared helper);
                # full taxonomy passed in so it isn't re-fetched per document.
                corr = await resolve_correspondent_from_metadata(manager, result.metadata, names=names)
                if not corr:
                    no_corr += 1
                    logger.info("  doc %s (paperless %s): no correspondent resolved — left blank", doc.id, pid)
                    continue

                logger.info("  doc %s (paperless %s): set correspondent -> %r", doc.id, pid, corr)
                if commit:
                    patch = _parse_paperless_result(
                        await manager.execute_tool(
                            "mcp.paperless.update_document",
                            {"document_id": pid, "correspondent": corr},
                        )
                    )
                    if patch.get("error"):
                        logger.warning("    update_document(%s) failed: %s", pid, patch.get("error"))
                        continue
                    if doc.paperless_document_id != pid:
                        doc.paperless_document_id = pid  # backfill the linkage (filename-matched only)
                        await db.commit()
                fixed += 1

        logger.info(
            "Done: %d set, %d already-had, %d no-correspondent, %d unmatched, %d skipped%s",
            fixed, already, no_corr, unmatched, skipped, "" if commit else "  (DRY-RUN — no writes)",
        )
    finally:
        try:
            await manager.shutdown()
        except Exception:  # noqa: BLE001 - teardown is best-effort
            pass


def main() -> int:
    p = argparse.ArgumentParser(description="Backfill the Paperless correspondent for folder-ingested documents.")
    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true", help="Preview correspondents to set; no writes.")
    mode.add_argument("--commit", action="store_true", help="Resolve/create + PATCH the Paperless correspondent.")
    p.add_argument("--limit", type=int, default=None, help="Cap the number of documents.")
    args = p.parse_args()
    asyncio.run(_run(commit=args.commit, limit=args.limit))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
