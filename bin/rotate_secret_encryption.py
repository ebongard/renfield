#!/usr/bin/env python3
"""Re-encrypt stored secrets (BLE IRKs) under the current SECRET_KEY (BL-0357).

Step 2 of a SECRET_KEY rotation. The backend must already run with
SECRET_KEY=<new> and SECRET_KEY_PREVIOUS=<old> (step 1); this rewrites every
token that still carries the old key so step 3 can drop SECRET_KEY_PREVIOUS.
Idempotent, counts only — never prints a plaintext or a token.

ALWAYS --dry-run first:

    python bin/rotate_secret_encryption.py --dry-run   # what would change
    python bin/rotate_secret_encryption.py --commit    # rewrite the tokens

Run inside the backend pod (kubectl exec) or on a host with RENFIELD_BACKEND_DIR
and the backend's DATABASE_URL / SECRET_KEY / SECRET_KEY_PREVIOUS in the env.
A row no configured key can decrypt is reported as `undecryptable` and left
alone: that IRK must be re-entered via the pairing flow.
"""
from __future__ import annotations

import argparse
import asyncio
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

from ha_glue.services.secret_rotation import rotate_irks  # noqa: E402
from services.database import AsyncSessionLocal  # noqa: E402
from services.secret_encryption import previous_key_count  # noqa: E402


async def main(commit: bool) -> int:
    # Same parsing as the decryptor: a blank or comma-only value is NO previous
    # key — otherwise every old row would read as "undecryptable" with the wrong
    # advice (re-enter) instead of the right one (set the former key first).
    if previous_key_count() == 0:
        print(
            "SECRET_KEY_PREVIOUS is empty — nothing to rotate FROM. Set it to the "
            "former key (step 1 of the runbook) before running this script.",
            file=sys.stderr,
        )
        return 2
    async with AsyncSessionLocal() as session:
        report = await rotate_irks(session, commit=commit)
    mode = "COMMIT" if commit else "DRY RUN"
    print(f"[{mode}] user_ble_irks: {report.as_dict()}")
    if report.undecryptable:
        print(
            f"{report.undecryptable} row(s) decryptable by no configured key — re-enter "
            "those IRKs via the pairing flow.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--dry-run", action="store_true", help="report only, no writes")
    g.add_argument("--commit", action="store_true", help="rewrite tokens under the current key")
    args = ap.parse_args()
    raise SystemExit(asyncio.run(main(commit=args.commit)))
