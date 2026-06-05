#!/usr/bin/env python3
"""CLI: de-magnetize person KG entities with generic meta-descriptions.

NULLs descriptions like "Vollständiger Name einer Person" on person entities and
re-embeds them from the bare name, so existing magnet hubs stop attracting bare
names in retrieval + the reconciler's same-name embedding dedup. The resolve
cascade already stopped folding persons via embedding; this repairs the data.

Run in a backend pod (needs the embedding model):
    # preview (writes nothing):
    kubectl -n renfield exec deploy/backend -c backend -- \\
      python bin/demagnetize_person_entities.py --dry-run
    # apply (optionally scope to one user):
    kubectl -n renfield exec deploy/backend -c backend -- \\
      python bin/demagnetize_person_entities.py --apply [--user-id 1]
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

_BACKEND = Path(__file__).resolve().parent.parent / "src" / "backend"
sys.path.insert(0, str(_BACKEND))

from services.database import AsyncSessionLocal  # noqa: E402
from services.kg_demagnetize import dry_run, run  # noqa: E402


async def main(apply: bool, user_id: int | None) -> int:
    async with AsyncSessionLocal() as db:
        if not apply:
            rep = await dry_run(db, user_id=user_id)
            print(f"[DRY-RUN] {rep.candidates} person entit{'y' if rep.candidates == 1 else 'ies'} "
                  f"would be de-magnetized:")
            for eid, name, desc in rep.samples:
                print(f"  #{eid:<5} {name!r:40s} desc={desc!r}")
            print("Re-run with --apply to NULL the descriptions and re-embed name-only.")
            return 0
        rep = await run(db, user_id=user_id)
        print(f"[APPLY] candidates={rep.candidates} updated={rep.updated} failed={rep.failed}")
        for eid, name, desc in rep.samples:
            print(f"  #{eid:<5} {name!r:40s} dropped desc={desc!r}")
        return 0 if rep.failed == 0 else 1


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--dry-run", action="store_true", help="preview only (default)")
    g.add_argument("--apply", action="store_true", help="write changes")
    ap.add_argument("--user-id", type=int, default=None, help="scope to one user")
    args = ap.parse_args()
    raise SystemExit(asyncio.run(main(apply=args.apply, user_id=args.user_id)))
