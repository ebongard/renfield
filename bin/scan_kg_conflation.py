#!/usr/bin/env python3
"""CLI: KG conflation tripwire scan (read-only).

Lists distinct-name, same-type, same-tier entity pairs that embed >= the
configured threshold — a forming generic-centroid magnet / mis-embedding. Never
mutates. Expected output: no pairs. Runs the same scan as the scheduled monitor
(``kg_conflation_monitor_enabled``) on demand, so you can check without enabling
the periodic job.

Run in a backend pod:
    kubectl -n renfield exec deploy/backend -c backend -- \\
      python bin/scan_kg_conflation.py [--user-id 1] [--threshold 0.85]
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

_BACKEND = Path(__file__).resolve().parent.parent / "src" / "backend"
sys.path.insert(0, str(_BACKEND))

from services.database import AsyncSessionLocal  # noqa: E402
from services.kg_conflation_monitor import KgConflationMonitor  # noqa: E402
from utils.config import settings  # noqa: E402


async def main(user_id: int | None, threshold: float | None) -> int:
    if threshold is not None:
        settings.kg_conflation_monitor_threshold = threshold
    async with AsyncSessionLocal() as db:
        rep = await KgConflationMonitor(db).scan_all(user_id=user_id)
        print(f"[SCAN] users={rep.scanned_users} threshold>={settings.kg_conflation_monitor_threshold} "
              f"suspicious_pairs={len(rep.pairs)}")
        for p in rep.pairs:
            print(f"  {p.entity_type:12s} #{p.id_a} {p.name_a!r} ~ #{p.id_b} {p.name_b!r} "
                  f"tier={p.tier} cosine={p.similarity}")
        if not rep.pairs:
            print("  (clean — no distinct-name same-type pairs above threshold)")
        return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--user-id", type=int, default=None, help="scope to one user")
    ap.add_argument("--threshold", type=float, default=None, help="override cosine threshold")
    args = ap.parse_args()
    raise SystemExit(asyncio.run(main(user_id=args.user_id, threshold=args.threshold)))
