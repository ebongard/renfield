#!/usr/bin/env python3
"""
Backfill conversation_memories.subject_entity_id (Structured Memory Phase 3b).

Thin CLI over ``services.memory_bridge_backfill`` (the testable core). Links each
decomposable memory (category fact/preference) that carries a subject_name but no
entity link to its canonical KG entity — linking existing entities and creating
one (at the memory's own circle_tier, type-scoped to person) for subjects with no
entity yet. After this runs, entity-augmented retrieval ("Was weiß ich über X")
reads every memory about a person deterministically instead of by embedding alone.

ALWAYS --dry-run first: it prints link-vs-create estimates WITHOUT any write.
Per-row create+link is committed atomically (no orphan entity on crash); re-runs
are idempotent (already-linked rows are excluded). The reconciler's Phase 3a
same-name gate ensures any duplicate entities this mints surface in the owner
review queue instead of being auto-merged.

Usage:
    python bin/backfill_subject_entity_ids.py --dry-run             # estimate, no writes
    python bin/backfill_subject_entity_ids.py --commit             # do it
    python bin/backfill_subject_entity_ids.py --commit --user-id 1 # one user
    python bin/backfill_subject_entity_ids.py --commit --limit 500 # cap rows
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

from services.database import AsyncSessionLocal  # noqa: E402
from services.memory_bridge_backfill import (  # noqa: E402
    dry_run_backfill,
    run_backfill,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
logger = logging.getLogger("backfill_subject_entity_ids")


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Backfill conversation_memories.subject_entity_id (Phase 3b).")
    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true", help="Estimate link/create counts; no writes.")
    mode.add_argument("--commit", action="store_true", help="Perform the backfill (writes).")
    p.add_argument("--user-id", type=int, default=None, help="Limit to one user (default: all).")
    p.add_argument("--limit", type=int, default=None, help="Cap the number of memories processed.")
    return p


async def _run(args: argparse.Namespace) -> int:
    async with AsyncSessionLocal() as session:
        if args.dry_run:
            rep = await dry_run_backfill(session, args.user_id, args.limit)
            logger.info(
                "DRY-RUN: %d candidates | ~%d would link to an existing entity | "
                "~%d would create a new entity (%d distinct subjects). "
                "Estimate uses exact-name match only; the real run's embedding step may link a few more. "
                "No writes performed.",
                rep.candidates, rep.would_link, rep.would_create, rep.distinct_subjects,
            )
        else:
            rep = await run_backfill(session, args.user_id, args.limit)
            logger.info(
                "BACKFILL DONE: %d linked (%d new entities created), %d failed, %d candidates.",
                rep.linked, rep.created, rep.failed, rep.candidates,
            )
    return 0


def main() -> int:
    return asyncio.run(_run(_build_parser().parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
