#!/usr/bin/env python3
"""
Backfill kb_shares from the legacy per-document grants (pc20261101_kb_shares).

Before ``kb_shares`` existed, a knowledge-base share was stored ONLY as one
``atom_explicit_grants`` row per document that was in the KB at share time. Such
a share does not cover documents added later. This script derives the missing
``kb_shares`` row for every (KB, user) pair that holds a legacy grant on a
document of the KB: MAX level over that user's grants on the KB, granted_by /
granted_at of the most recent one.

🛑 It MUST run before the #1446 code is deployed, and again after every
pc20261101 downgrade → upgrade (the downgrade turns shares back into grants).
A document grant no longer gives KB level: until the row exists, the grantee
keeps the documents but loses the KB in his list and every KB-level right
(upload, write). Precondition: the dry run reports ``clean: 0``.

🛑 Pairs it cannot trust are NEVER inserted. Before #1464 a document move kept
its grants, so a grant may have travelled into another KB; converting it would
share the whole destination KB. The dry run lists these pairs as SUSPICIOUS
(ids, counts, level, reasons — see ``UNCOVERED_PAIRS_SQL``); the owner adopts one
explicitly with ``--adopt KB_ID:USER_ID --commit`` (logged, revertible). The
old move left NO trace (it set ``documents.knowledge_base_id`` only), so the
guard bounds the exposure instead: a pair is suspicious as soon as the KB holds
a document the person has no grant on, whenever it arrived. A clean pair thus
gains no document it cannot already read. That also flags every legacy share
whose KB received uploads since (the gap #1464 closed) -- adopt those after a
look at ``kb_docs`` / ``docs_after_grant``. Not detectable: documents moved by
their own owner, without split_batch, into a KB older than the grant that holds
nothing but them; converting such a pair shares only what is uploaded there
LATER. The dry run therefore lists every CLEAN pair too: read it before
``--commit``.

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
    python bin/backfill_kb_shares.py --adopt 12:7               # dry run of one pair
    python bin/backfill_kb_shares.py --adopt 12:7 --commit --log adopt.json
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
    adopt_kb_share_pairs,
    backfill_kb_shares,
    revert_kb_share_backfill,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
logger = logging.getLogger("backfill_kb_shares")


def _pair(value: str) -> tuple[int, int]:
    """``KB_ID:USER_ID`` → (kb_id, user_id)."""
    try:
        kb_id, user_id = value.split(":")
        return int(kb_id), int(user_id)
    except ValueError as err:
        raise argparse.ArgumentTypeError(f"expected KB_ID:USER_ID, got {value!r}") from err


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Derive kb_shares rows from legacy per-document grants. Dry run by default.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--commit", action="store_true",
                      help="Insert the kb_shares rows of the CLEAN pairs (writes) and "
                           "write a log. Suspicious pairs are never inserted.")
    mode.add_argument("--revert", action="store_true",
                      help="Delete exactly the rows a --commit or --adopt run inserted "
                           "— needs --log.")
    p.add_argument("--adopt", type=_pair, nargs="+", metavar="KB_ID:USER_ID", default=None,
                   help="The owner's decision: turn exactly these pairs (typically "
                        "listed as suspicious) into kb_shares rows. Dry run unless "
                        "--commit is given too.")
    p.add_argument("--log", default=None,
                   help="Run log. --commit WRITES it (default: "
                        "./kb-shares-backfill-<timestamp>.json); --revert READS it. "
                        "Without it, --revert refuses: the inserted rows are not "
                        "distinguishable from shares made through the UI afterwards.")
    return p


def _report_suspicious(suspicious: list[dict]) -> None:
    for s in suspicious:
        logger.info("  SUSPICIOUS (owner decides) | kb_id=%s user_id=%s | grants=%s | "
                    "level=%s | reasons=%s | kb_docs=%s docs_after_grant=%s",
                    s["kb_id"], s["user_id"], s["grants"], s["permission"],
                    ",".join(s["reasons"]), s.get("kb_docs"), s.get("docs_after_grant"))


def _report_clean(clean: list[dict]) -> None:
    for c in clean:
        logger.info("  CLEAN (converted by --commit) | kb_id=%s user_id=%s | grants=%s | "
                    "level=%s | kb_docs=%s docs_after_grant=%s", c["kb_id"], c["user_id"],
                    c["grants"], c["permission"], c.get("kb_docs"), c.get("docs_after_grant"))


def _claim_log(path_arg: str | None, prefix: str) -> Path | None:
    """Claim the log BEFORE the insert commits: a log that cannot be written
    afterwards would leave inserted rows that no --revert can name."""
    log_path = Path(path_arg or f"{prefix}-{datetime.now().strftime('%Y%m%d-%H%M%S')}.json")
    try:
        log_path.write_text(json.dumps({"inserted": [], "complete": False}, indent=2))
    except OSError as err:
        logger.error("Log not writable (%s): %s; nothing written to the database.", log_path, err)
        return None
    return log_path


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
    adopt = getattr(args, "adopt", None)
    log_path: Path | None = None
    if not dry_run:
        log_path = _claim_log(args.log, "kb-shares-adopt" if adopt else "kb-shares-backfill")
        if log_path is None:
            return 2

    if adopt:
        async with AsyncSessionLocal() as session:
            result = await adopt_kb_share_pairs(session, adopt, dry_run=dry_run)
        logger.info("ADOPT %s | requested: %d | adoptable: %d | skipped (no uncovered "
                    "legacy pair): %d", mode, len(adopt), len(result.suspicious),
                    len(result.skipped))
        _report_suspicious(result.suspicious)
        for s in result.skipped:
            logger.info("  SKIPPED | kb_id=%s user_id=%s", s["kb_id"], s["user_id"])
    else:
        async with AsyncSessionLocal() as session:
            result = await backfill_kb_shares(session, dry_run=dry_run)
        logger.info("%s | (kb, user) pairs with legacy grants: %d | already in kb_shares: %d "
                    "| missing: %d (clean: %d, suspicious: %d)", mode,
                    result.pairs_with_grants, result.already_shared, result.missing,
                    result.clean_missing, len(result.suspicious))
        _report_clean(result.clean)
        _report_suspicious(result.suspicious)
        if result.suspicious:
            logger.info("Suspicious pairs are NEVER inserted by this run: their grants may "
                        "have moved with a document into another KB. Adopt a pair "
                        "explicitly with --adopt KB_ID:USER_ID --commit.")
    if dry_run:
        logger.info("Nothing written.")
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
