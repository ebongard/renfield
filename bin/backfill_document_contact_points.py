#!/usr/bin/env python3
"""
Backfill contact points from documents already in the knowledge base (#1240 stage 4).

For every document with an issuer fact, re-reads its STORED chunk text and runs the
same deterministic derivation as the ingest hook
(``services.document_contact_points.sync_document_contact_points``): keyword-gated
numbers in the issuer's own block, attached to exactly one matching person or
organization of the document's owner. No LLM, no re-OCR. Idempotent: a document's
prior document-sourced values are replaced, so re-runs converge.

Prints COUNTS only -- never a number, a name or a document title.
Needs CONTACT_POINTS_ENABLED (the table must be in use); the ingest-side flag
CONTACT_POINTS_FROM_DOCUMENTS_ENABLED is NOT required for a backfill.

Usage:
    python bin/backfill_document_contact_points.py --dry-run          # counts, no writes
    python bin/backfill_document_contact_points.py --commit           # store
    python bin/backfill_document_contact_points.py --commit --limit 50
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from collections.abc import Mapping
from pathlib import Path

# --- backend import path (identical block in every bin/backfill_*.py) ---------------
# These scripts are copied into the backend pod on their own, so this cannot live in a
# shared module: it is what makes the shared modules importable in the first place.


def _find_backend_dir(
    script: Path, env: Mapping[str, str] = os.environ, image_root: Path = Path("/app")
) -> Path:
    """The Renfield backend root: ``$RENFIELD_BACKEND_DIR`` (exclusive when set), else
    the repo layout ``bin/../src/backend``, else the image layout ``/app``."""
    override = env.get("RENFIELD_BACKEND_DIR")
    candidates = (
        [Path(override)] if override
        else [script.resolve().parent.parent / "src" / "backend", image_root]
    )
    for candidate in candidates:
        if (candidate / "services" / "__init__.py").is_file() and (candidate / "utils" / "config.py").is_file():
            return candidate
    print(
        f"{script.name}: Renfield backend not found (tried: {', '.join(map(str, candidates))}). "
        "Set RENFIELD_BACKEND_DIR to the directory holding services/ and utils/ "
        "(repo: src/backend, image: /app).",
        file=sys.stderr,
    )
    raise SystemExit(2)


_BACKEND = _find_backend_dir(Path(__file__))
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))
# --- end backend import path ---------------------------------------------------------
from sqlalchemy import text  # noqa: E402

from services.database import AsyncSessionLocal  # noqa: E402
from services.document_contact_points import scan_documents  # noqa: E402
from utils.config import settings  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
logger = logging.getLogger("backfill_document_contact_points")


async def _run(*, commit: bool, limit: int | None) -> int:
    if not settings.contact_points_enabled:
        logger.error("CONTACT_POINTS_ENABLED is off on this instance -- nothing to do.")
        return 2
    async with AsyncSessionLocal() as db:
        totals = await scan_documents(db, owner_id=None, commit=commit, limit=limit)
        live = (await db.execute(text(
            "SELECT count(*), count(DISTINCT entity_id) FROM kg_entity_contact_points "
            "WHERE source_type = 'document' AND valid_to IS NULL"
        ))).one()
    logger.info("Done%s: %s", "" if commit else " (DRY-RUN -- no writes)", dict(sorted(totals.items())))
    # After a dry-run this is the UNCHANGED state (every document was rolled back);
    # "stored" above is what a --commit would write.
    logger.info("Live document values %s: %d on %d entities",
                "now" if commit else "before (unchanged by the dry-run)", live[0], live[1])
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="Backfill contact points from stored document text.")
    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true", help="Count only; no writes.")
    mode.add_argument("--commit", action="store_true", help="Store the values.")
    p.add_argument("--limit", type=int, default=None, help="Cap the number of documents.")
    args = p.parse_args()
    return asyncio.run(_run(commit=args.commit, limit=args.limit))


if __name__ == "__main__":
    raise SystemExit(main())
