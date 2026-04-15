"""Home-Assistant / smart-home glue layer for the Renfield platform.

This package contains all the Home Assistant, satellite, presence, media,
camera, and paperless-audit consumer features that currently ship with
Renfield as a monorepo but will be extracted into `ebongard/renfield`
(the home-automation product flavor) during Phase 1-3 of the open-source
extraction.

Target split (see `docs/architecture/renfield-platform-boundary.md` in
the parent Reva repo):

- `ha_glue.models` — HA-specific SQLAlchemy tables (Room, RoomDevice,
  PresenceEvent, PaperlessAuditResult, etc.)
- `ha_glue.services` — HA integrations (audio routing, presence, media
  follow, satellite management, Zeroconf discovery). *Not yet populated.*
- `ha_glue.api` — HA-specific REST + WebSocket routes. *Not yet populated.*

## Layering rule

`ha_glue.*` is allowed to import from `models.*`, `services.*`, `utils.*`,
`api.*` (platform side). The REVERSE is forbidden and will be enforced
by a CI lint in Phase 1 Week 4. Platform files that need data from
ha_glue should use the hook system (`utils.hooks`) instead of direct
imports.

## Current state (Phase 1 Week 1.2)

- `ha_glue.models.database` — 9 SQLAlchemy classes + constants. Compat
  re-export in `models/database.py` keeps legacy
  `from models.database import Room` working.
- `ha_glue.services.intent_fallback` — HA-keyword intent fallback,
  registered as an `intent_fallback_resolve` hook handler at import
  time of this package.

Service and route extractions for the rest of the ha-glue surface
happen in Week 2; Alembic migration cutover in Week 3; CI lint gate
in Week 4.

## Hook registration

This package's `__init__.py` registers all ha-glue hook handlers as a
side effect of import. Trigger the bootstrap by adding `import ha_glue`
to a platform startup file (currently `api/lifecycle.py` at the
appropriate point), wrapped in a try/except so platform-only
deployments (no ha_glue installed) degrade cleanly to "no HA fallback"
without crashing.
"""

from loguru import logger as _logger


def _register_hooks() -> None:
    """Register all ha_glue hook handlers with the platform hook system.

    Called once, at package import time. Failures are logged but never
    propagate — a broken handler must not break Renfield startup.
    """
    try:
        from utils.hooks import register_hook

        from ha_glue.services.intent_fallback import ha_intent_fallback

        register_hook("intent_fallback_resolve", ha_intent_fallback)
        _logger.info("ha_glue: registered intent_fallback_resolve handler")
    except Exception:  # noqa: BLE001 — startup must never break on plugin error
        _logger.opt(exception=True).warning(
            "ha_glue: hook registration failed — HA fallback disabled"
        )


_register_hooks()
