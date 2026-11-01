#!/usr/bin/env python3
"""
Backfill kb_shares from the legacy per-document grants (pc20261101_kb_shares).

Before ``kb_shares`` existed, a knowledge-base share was stored ONLY as one
``atom_explicit_grants`` row per document that was in the KB at share time. Such
a share does not cover documents added later. This script derives the missing
``kb_shares`` row for every (KB, user) pair that holds a legacy grant on a
document of the KB: MAX level over that user's grants on the KB, granted_by /
granted_at of the most recent one.

Access does NOT depend on running it: until then a legacy grant still counts.
What it adds is coverage of documents added to (or moved into) the KB later.

- Default is a DRY RUN: counts only, no writes.
- Idempotent: an existing kb_shares row is never touched (ON CONFLICT DO NOTHING),
  so a second run inserts nothing. Legacy grants are never modified.
- Output is counts only — no KB names, no document content.
- ``--commit`` writes a log with the (kb_id, user_id, granted_at) of every row it
  inserted; ``--revert --log <file>`` deletes exactly those rows (and only while
  ``granted_at`` is unchanged, i.e. nobody re-shared through the UI since).

Usage:
    python bin/backfill_kb_shares.py                     # dry run (default)
    python bin/backfill_kb_shares.py --commit            # write + log
    python bin/backfill_kb_shares.py --commit --log f.json
    python bin/backfill_kb_shares.py --revert --log f.json
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from collections.abc import Mapping
from datetime import datetime
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

from services.database import AsyncSessionLocal  # noqa: E402
from services.kb_shares_service import (  # noqa: E402
    backfill_kb_shares,
    revert_kb_share_backfill,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
logger = logging.getLogger("backfill_kb_shares")


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Derive kb_shares rows from legacy per-document grants. Dry run by default.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--commit", action="store_true",
                      help="Insert the missing kb_shares rows (writes) and write a log.")
    mode.add_argument("--revert", action="store_true",
                      help="Delete exactly the rows a --commit run inserted — needs --log.")
    p.add_argument("--log", default=None,
                   help="Run log. --commit WRITES it (default: "
                        "./kb-shares-backfill-<timestamp>.json); --revert READS it. "
                        "Without it, --revert refuses: the inserted rows are not "
                        "distinguishable from shares made through the UI afterwards.")
    return p


async def _run(args: argparse.Namespace) -> int:
    if args.revert:
        if not args.log:
            logger.error("--revert needs --log <file of the --commit run>; refusing to guess.")
            return 2
        log_path = Path(args.log)
        if not log_path.is_file():
            logger.error("Log not found: %s", log_path)
            return 2
        inserted = json.loads(log_path.read_text()).get("inserted", [])
        async with AsyncSessionLocal() as session:
            deleted = await revert_kb_share_backfill(session, inserted)
        logger.info("REVERT | logged rows: %d | deleted: %d | kept (changed since): %d",
                    len(inserted), deleted, len(inserted) - deleted)
        return 0

    dry_run = not args.commit
    mode = "DRY RUN (no writes)" if dry_run else "WRITE RUN"
    log_path: Path | None = None
    if not dry_run:
        # Claim the log BEFORE the insert commits: a log that cannot be written
        # afterwards would leave inserted rows that no --revert can name.
        log_path = Path(args.log or f"kb-shares-backfill-{datetime.now().strftime('%Y%m%d-%H%M%S')}.json")
        try:
            log_path.write_text(json.dumps({"inserted": [], "complete": False}, indent=2))
        except OSError as err:
            logger.error("Log not writable (%s): %s; nothing written to the database.", log_path, err)
            return 2
    async with AsyncSessionLocal() as session:
        result = await backfill_kb_shares(session, dry_run=dry_run)
    logger.info("%s | (kb, user) pairs with legacy grants: %d | already in kb_shares: %d "
                "| missing: %d", mode, result.pairs_with_grants, result.already_shared,
                result.missing)
    if dry_run:
        logger.info("Nothing written. Re-run with --commit to insert %d row(s).", result.missing)
        return 0
    assert log_path is not None
    log_path.write_text(json.dumps({"inserted": result.inserted, "complete": True}, indent=2))
    logger.info("Inserted: %d | log written: %s", len(result.inserted), log_path)
    return 0


def main() -> int:
    args = _build_parser().parse_args()
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
