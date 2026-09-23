#!/usr/bin/env python3
"""
Re-home the household's data before the auth-on cutover (§4, P0 Nr. 8).

Thin CLI over ``services.household_backfill`` (the testable core). Under
``AUTH_ENABLED=false`` everything was written onto one fallback owner at tier 0,
typed conversations have no owner at all, and the access filters were never
evaluated. Flipping the flag without re-homing first leaves every member but the
admin seeing nothing.

RUNS WHILE AUTH IS STILL OFF, and is inert at the moment it runs: tiers and
memberships are not evaluated under auth-off. That is the point — the data is in
place before the switch.

ALWAYS --dry-run first. It prints per-class counts and writes nothing.

The member list is REQUIRED and never guessed: who is family, who is a guest and
which account is the satellite's are facts about a household, not about a
database.

Usage:
    python bin/backfill_household_tiers.py --dry-run --admin 1 --family 1,2,3
    python bin/backfill_household_tiers.py --dry-run --admin 1 --family 1,2,3 \
        --guests 4,5 --device-account 6
    python bin/backfill_household_tiers.py --commit  --admin 1 --family 1,2,3 \
        --device-account 6
    python bin/backfill_household_tiers.py --dry-run --admin 1 --family 1,2,3 \
        --only kg,mitgliedschaften
    python bin/backfill_household_tiers.py --commit  --admin 1 --family 1,2,3 \
        --revert --log household-backfill-20260923-101500.json

Classes (--only, comma-separated; default: all, in this order):
    kg                  knowledge-graph atoms 0 → 2 (the only class that moves rows)
    admin-atome         documents/facts/memories — REPORT only, already tier 0
    null-relationen     kg_relations without an owner → admin (tier comes from the cascade)
    kb-eigentümer       knowledge_bases.owner_id NULL → admin
    unterhaltungen      ownerless conversations → speaker's user or admin, + atom
    kopplungs-rest      delete the stale (admin, admin, tier) pairing row
    mitgliedschaften    family × family, device ↔ admin, family → device's circle
    erfassungsvorgabe   capture default per account: family 2, guests 0

--revert undoes the knowledge-graph tier and the memberships, and it REQUIRES the
--log the commit run wrote. That is not bookkeeping fussiness: reverting by
inference is what would do the damage. "Admin-owned node at tier 2" is exactly
the shape of the 19 hand-set tier-2 atoms the forward run refuses to touch, and
"every planned membership that exists" includes one somebody created by hand at a
different tier. The log names what THIS run changed, so the revert can undo that
and nothing else.

It deliberately does NOT undo the classes where "before" is not recoverable —
once an ownerless row has an owner, nothing records that it had none. Those
classes say so in their output instead of pretending.
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
from services.household_backfill import (  # noqa: E402
    CLASSES,
    MemberList,
    RevertWithoutLog,
    RunLog,
    run_backfill,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
logger = logging.getLogger("backfill_household_tiers")


def _ids(raw: str | None) -> list[int]:
    if not raw:
        return []
    return [int(part) for part in raw.split(",") if part.strip()]


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Re-home the household's data before the auth-on cutover (§4).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true", help="Per-class counts; no writes.")
    mode.add_argument("--commit", action="store_true", help="Perform the backfill (writes).")
    p.add_argument("--admin", type=int, required=True,
                   help="The admin account id — owner of the legacy corpus.")
    p.add_argument("--family", required=True,
                   help="Comma-separated family account ids (include the admin).")
    p.add_argument("--guests", default=None,
                   help="Comma-separated guest ids: no memberships, capture tier 0.")
    p.add_argument("--device-account", type=int, default=None,
                   help="The satellite's device account id (users.is_device_account).")
    p.add_argument("--only", default=None,
                   help=f"Comma-separated classes. Known: {','.join(CLASSES)}")
    p.add_argument("--revert", action="store_true",
                   help="Undo what can be undone (kg tiers, memberships) — needs --log.")
    p.add_argument("--log", default=None,
                   help="Run log. A --commit run WRITES it (default: "
                        "./household-backfill-<timestamp>.json); a --revert run READS "
                        "it and undoes exactly those changes. Without it, revert "
                        "refuses: reverting by inference is what would demote a "
                        "hand-set tier or delete somebody's own membership.")
    return p


def _validate(args: argparse.Namespace) -> MemberList:
    family = _ids(args.family)
    guests = _ids(args.guests)
    if args.admin not in family:
        raise SystemExit(
            f"--admin {args.admin} is not in --family {family}. The admin is a "
            "household member; listing them separately would leave them out of "
            "every family×family membership."
        )
    overlap = set(family) & set(guests)
    if overlap:
        raise SystemExit(
            f"Accounts in BOTH --family and --guests: {sorted(overlap)}. Guests get "
            "no memberships (D-2d) and family get tier-2 ones — an account cannot be "
            "both without the result depending on evaluation order."
        )
    if args.device_account is not None and args.device_account in guests:
        raise SystemExit(
            f"--device-account {args.device_account} is listed as a guest. The device "
            "account is an identity, not a person; it needs its own memberships."
        )
    return MemberList(
        admin_id=args.admin,
        family=family,
        guests=guests,
        device_account_id=args.device_account,
    )


async def _run(args: argparse.Namespace) -> int:
    members = _validate(args)
    only = [c.strip() for c in args.only.split(",")] if args.only else None
    mode = "PROBELAUF (keine Schreibvorgänge)" if args.dry_run else "SCHREIBLAUF"
    if args.revert:
        mode += " · RÜCKWEG"
    logger.info("%s | admin=%s family=%s guests=%s device=%s",
                mode, members.admin_id, members.family, members.guests,
                members.device_account_id)

    log = RunLog()
    log_path: Path | None = None
    if args.revert:
        if not args.log:
            logger.error(
                "--revert ohne --log. Der Rückweg darf nicht raten, was der "
                "Schreiblauf getan hat: er würde sonst handgesetzte Stufen "
                "senken und Mitgliedschaften löschen, die jemand selbst angelegt "
                "hat. Gib das Protokoll des Schreiblaufs an."
            )
            return 2
        log_path = Path(args.log)
        if not log_path.is_file():
            logger.error("Protokoll nicht gefunden: %s", log_path)
            return 2
        log = RunLog.from_json(json.loads(log_path.read_text()))
        logger.info("Protokoll gelesen: %d kg-Atome, %d Mitgliedschaften",
                    len(log.kg_atom_ids), len(log.membership_pairs))
    elif args.commit:
        log_path = Path(
            args.log
            or f"household-backfill-{datetime.now().strftime('%Y%m%d-%H%M%S')}.json"
        )

    async with AsyncSessionLocal() as session:
        try:
            results = await run_backfill(
                session, members, only=only,
                dry_run=bool(args.dry_run), revert=bool(args.revert), log=log,
            )
        except RevertWithoutLog as e:
            logger.error("%s", e)
            return 2
        except ValueError as e:
            logger.error("%s", e)
            return 2
        finally:
            # Written even on a failure part-way: what it already did is exactly
            # what a revert has to undo, and that is when the log matters most.
            if log_path is not None and args.commit:
                log_path.write_text(json.dumps(log.to_json(), indent=2))
                logger.info("Protokoll geschrieben: %s", log_path)

    for r in results:
        logger.info("  %s", r.line())
    total = sum(r.changed for r in results)
    logger.info("%s — %d Änderung(en) %s", mode, total,
                "vorgesehen" if args.dry_run else "geschrieben")
    if args.dry_run:
        logger.info(
            "Dieselben Zahlen müssen im Schreiblauf herauskommen (§11 Abnahme). "
            "Weichen sie ab, hat sich der Bestand zwischen den Läufen verändert — "
            "nicht weitermachen, neu prüfen."
        )
    return 0


def main() -> int:
    args = _build_parser().parse_args()
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
