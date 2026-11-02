#!/usr/bin/env python3
"""
Purge document atoms whose document no longer exists (#1461).

Before #1461, deleting a document removed the ``documents`` row but not its
``kb_document`` atom (and, earlier still, not its facts' ``document_fact``
atoms). Those atoms keep a dangling ``source_id`` together with their
``atom_explicit_grants``. Retrieval never returns them (readers JOIN
``documents``); they are leftover personal data and inflate atom counts.

- Default is a DRY RUN: counts only (atoms per type, grants on them), no writes.
- ``--commit`` purges them through ``AtomPurgeService`` in one transaction and
  writes a log with every purged atom (id, type, source id, owner, tier,
  created_at, grantees + level — no content). The full list is written BEFORE
  the purge commits (``complete: false``) and marked ``complete: true`` after.
- Idempotent: a second run finds nothing.
- There is no ``--revert``: this is the erasure the document delete promised (an
  atom of a document that no longer exists guards nothing). The log is the record
  of what was erased.
- ``__pending__`` placeholders (in-flight creates) are never touched.

Usage:
    python bin/purge_orphan_document_atoms.py                    # dry run (default)
    python bin/purge_orphan_document_atoms.py --commit           # purge + log
    python bin/purge_orphan_document_atoms.py --commit --log f.json
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
from services.orphan_document_atoms import purge_orphan_document_atoms  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
logger = logging.getLogger("purge_orphan_document_atoms")


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Purge document/fact atoms whose source row is gone. Dry run by default.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--commit", action="store_true",
                   help="Purge the orphaned atoms (writes) and write a log.")
    p.add_argument("--log", default=None,
                   help="Run log written by --commit (default: "
                        "./orphan-document-atoms-<timestamp>.json).")
    return p


async def _run(args: argparse.Namespace) -> int:
    dry_run = not args.commit
    mode = "DRY RUN (no writes)" if dry_run else "WRITE RUN"
    log_path: Path | None = None
    if not dry_run:
        # Claim the log BEFORE the purge commits: an erasure nobody can name
        # afterwards is exactly what this script must not produce.
        log_path = Path(args.log or f"orphan-document-atoms-{datetime.now().strftime('%Y%m%d-%H%M%S')}.json")
        try:
            log_path.write_text(json.dumps({"purged": [], "complete": False}, indent=2))
        except OSError as err:
            logger.error("Log not writable (%s): %s; nothing purged.", log_path, err)
            return 2
    def _write_pending(entries: list[dict]) -> None:
        # Runs inside the purge transaction, before its commit: if the commit then
        # fails, the log names what was attempted (complete: false).
        assert log_path is not None
        log_path.write_text(json.dumps({"purged": entries, "complete": False}, indent=2))

    async with AsyncSessionLocal() as session:
        result = await purge_orphan_document_atoms(
            session, dry_run=dry_run, on_before_commit=None if dry_run else _write_pending,
        )
    logger.info("%s | orphaned kb_document atoms: %d | orphaned document_fact atoms: %d "
                "| grants on them: %d", mode, result.document_atoms, result.fact_atoms,
                result.grants)
    if dry_run:
        logger.info("Nothing written. Re-run with --commit to purge %d atom(s).",
                    result.document_atoms + result.fact_atoms)
        return 0
    assert log_path is not None
    log_path.write_text(json.dumps({"purged": result.purged, "complete": True}, indent=2))
    logger.info("Purged: %d | log written: %s", len(result.purged), log_path)
    return 0


def main() -> int:
    args = _build_parser().parse_args()
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
