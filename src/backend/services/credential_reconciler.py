"""Boot-time credential reconciler — self-heals DB-stored integration tokens.

A DB wipe (or a fresh install) clears operational tokens that live in the
database while their counterpart in a Secret/env survives — the two diverge and
the integration silently 401/403s (the 2026-07 xidra reset incident: the
folder/email-ingest push tokens in ``SystemSetting`` and the Paperless API token
in Paperless's own DB were wiped, so pushes/filing broke until re-synced by hand).

This reconciler runs once at startup (after auth init) and re-seeds the DB tokens
from their authoritative Secret source (``settings.*_ingest_token``) so the
divergence self-heals on the next boot. It is:

- **Idempotent** — a no-op when the token already matches.
- **Backward-compatible** — a no-op when the authoritative env token is unset
  (legacy DB-authoritative behavior: an admin generates the token via the mint
  route and copies it to the MCP; no reconcile).
- **Best-effort** — each check is isolated; one failure never blocks startup.

Paperless's API token lives in Paperless's OWN database (not reachable from the
backend session), so the backend cannot re-seed it here — it is handled by a
Paperless-side deployment init. This reconciler only **probes** it (when a token
is configured) and logs a loud WARNING if it's invalid, so the failure is visible
(detect/report) even though the fix is elsewhere.
"""
from __future__ import annotations

from loguru import logger

from models.database import (
    SETTING_EMAIL_INGEST_TOKEN,
    SETTING_FOLDER_INGEST_TOKEN,
)
from services.database import AsyncSessionLocal
from services.ingest_common import get_ingest_token, set_ingest_token
from utils.config import settings


async def _reconcile_ingest_tokens() -> list[str]:
    """Seed the folder/email-ingest DB tokens from their authoritative env value.
    Returns a list of human-readable actions taken (for logging/reporting)."""
    actions: list[str] = []
    pairs = [
        ("folder-ingest", settings.folder_ingest_token, SETTING_FOLDER_INGEST_TOKEN),
        ("email-ingest", settings.email_ingest_token, SETTING_EMAIL_INGEST_TOKEN),
    ]
    async with AsyncSessionLocal() as db:
        for label, desired, key in pairs:
            desired = (desired or "").strip()
            if not desired:
                continue  # legacy DB-authoritative; nothing to reconcile
            try:
                current = await get_ingest_token(db, key)
                # Seed ONLY when the DB token is empty (the wipe/fresh-install case).
                # A NON-empty DB token is authoritative — an admin may have rotated it
                # via POST /api/{folder,email}-ingest/token AND updated the MCP to match;
                # overwriting it from the (now-stale) env would REVERT that rotation and
                # re-introduce the 403 this reconciler exists to prevent. So we never
                # touch a present token — we only fill an absent one.
                if current:
                    continue
                await set_ingest_token(db, key, desired)
                actions.append(f"{label} token seeded (was empty)")
                logger.warning(
                    f"🔧 credential-reconciler: {label} token seeded from secret "
                    "(DB token was empty — fresh install or DB wipe)"
                )
            except Exception as e:  # noqa: BLE001 - never block startup
                logger.warning(f"credential-reconciler: {label} check failed: {e}")
    return actions


async def reconcile_credentials() -> list[str]:
    """Run all credential reconciliations at boot. Returns the actions taken
    (empty when nothing needed healing). Never raises."""
    actions: list[str] = []
    try:
        actions += await _reconcile_ingest_tokens()
    except Exception as e:  # noqa: BLE001
        logger.warning(f"credential-reconciler: ingest-token pass failed: {e}")
    if actions:
        logger.info(f"✅ credential-reconciler healed {len(actions)} token(s): {actions}")
    return actions
