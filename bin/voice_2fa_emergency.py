#!/usr/bin/env python3
"""Emergency exit: switch the voice off as a second login factor from the shell.

🛑 THIS IS NOT THE NORMAL WAY
==============================
The normal way is **administration -> users -> the shield button**, or
``POST /api/users/{id}/voice-second-factor`` with ``{"enabled": false}``.
Whoever can get there should stay there: that route logs, checks permissions
and knows the asymmetry between switching on and switching off.

This script exists for **exactly one** case the route cannot serve (review
finding F5):

    The only administrator switched the factor on for herself and cannot speak
    -- a broken microphone, a cold, a broken browser.

Then the chain closes against her without a gap: ``/auth/login`` withholds the
tokens, ``/auth/voice`` fails on the voice, and switching off requires
``users.manage`` -- which on a default installation ONLY the admin role has
(``models/permissions.py``, ``DEFAULT_ROLES``). There is no second
administrator who could help. **No supported API call brings the instance
back.** But the operator does have shell access, and that is the emergency exit
such systems have at this point.

The alternative would have been to refuse switching on for the last
administrator -- then of all people the one with the greatest need for
protection would be the only one who could never use the factor.

🛑 What this script CANNOT do: switch on. That is a consent to biometric
processing (Art. 9 GDPR), and a person gives it for themselves through the
interface, not an operator on the command line.

🛑 The script is NOT in the backend image: the build context is
``src/backend/``, and ``COPY . .`` does not see the repo's ``bin/``. It is
copied into the pod at runtime -- like every ``bin/backfill_*.py``. That is why
it carries the same ``_find_backend_dir`` block: that finds ``/app`` in the
image by itself, no matter which directory it is started from.

Usage:

    # locally / on the build box
    python bin/voice_2fa_emergency.py --list

    # in production
    POD=$(kubectl -n renfield get pod -l app.kubernetes.io/name=backend -o name | head -1)
    kubectl -n renfield cp bin/voice_2fa_emergency.py "${POD#pod/}":/tmp/voice_2fa_emergency.py
    kubectl -n renfield exec "$POD" -- python /tmp/voice_2fa_emergency.py --username admin --off
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

from sqlalchemy import select  # noqa: E402

from models.database import User  # noqa: E402
from services.database import AsyncSessionLocal  # noqa: E402


async def list_armed() -> int:
    """Who has the factor armed? Username and id only -- no further data."""
    async with AsyncSessionLocal() as db:
        rows = (await db.execute(
            select(User.id, User.username, User.speaker_id)
            .where(User.voice_second_factor_enabled.is_(True))
            .order_by(User.username)
        )).all()
    if not rows:
        print("No account has the voice enabled as a second factor.")
        return 0
    print(f"{len(rows)} account(s) with an armed second factor:")
    for uid, username, speaker_id in rows:
        profile_label = f"profile {speaker_id}" if speaker_id else "NO profile (hurdle at rest)"
        print(f"  id={uid:<5} {username:<24} {profile_label}")
    return 0


async def switch_off(username: str, dry_run: bool) -> int:
    async with AsyncSessionLocal() as db:
        user = (await db.execute(
            select(User).where(User.username == username)
        )).scalar_one_or_none()

        if user is None:
            print(f"No account with the username '{username}'.", file=sys.stderr)
            return 1
        if not user.voice_second_factor_enabled:
            print(f"'{username}' (id={user.id}): the factor is already off — nothing to do.")
            return 0

        if dry_run:
            print(f"[dry-run] '{username}' (id={user.id}): "
                  f"voice_second_factor_enabled True -> False")
            return 0

        user.voice_second_factor_enabled = False
        await db.commit()

    print(f"'{username}' (id={user.id}): voice_second_factor_enabled True -> False")
    print("WARNING: that was the emergency exit. The normal way is")
    print("  Verwaltung -> Benutzer -> shield button.")
    print("The consent is thereby withdrawn; the person can grant it again there")
    print("themselves at any time.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Notausgang: zweiten Anmeldefaktor (Stimme) abschalten.",
    )
    ap.add_argument("--username", help="Konto, dessen Faktor abgeschaltet wird")
    ap.add_argument("--off", action="store_true",
                    help="den Faktor abschalten (die einzige Richtung, die dieses Skript kann)")
    ap.add_argument("--list", action="store_true",
                    help="zeigen, welche Konten den Faktor scharf haben")
    ap.add_argument("--dry-run", action="store_true", help="nur zeigen, nichts schreiben")
    args = ap.parse_args()

    if args.list:
        return asyncio.run(list_armed())
    if not (args.username and args.off):
        ap.error("either --list, or --username NAME --off")
    return asyncio.run(switch_off(args.username, args.dry_run))


if __name__ == "__main__":
    raise SystemExit(main())
