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
- `ha_glue.services.intent_fallback` — HA-keyword intent fallback
  handler. Registered with the platform hook system by an explicit
  call to `ha_glue.bootstrap.register()` from a platform startup file.
- `ha_glue.bootstrap` — explicit hook-registration entry point.

Service and route extractions for the rest of the ha-glue surface
happen in Week 2; Alembic migration cutover in Week 3; CI lint gate
in Week 4.

## Hook registration — explicit, NOT side-effect-on-import

Importing this package is side-effect-free. Hook registration only
happens when something explicitly calls `ha_glue.bootstrap.register()`.
This is deliberate: the legacy compat re-export in
`models/database.py::__getattr__` does `from ha_glue.models import
database`, which imports the `ha_glue` package as part of attribute
resolution. If `ha_glue/__init__.py` registered hooks as a side effect
of import, every platform service that touched an ha-glue model
through the compat shim would unconditionally activate HA behavior —
even on `RENFIELD_EDITION=pro` deployments where the smart_home
feature flag is False.

Trigger the bootstrap explicitly from `api/lifecycle.py`, gated on
`settings.features["smart_home"]`, and wrap in try/except so a
missing `ha_glue` package degrades cleanly.
"""
