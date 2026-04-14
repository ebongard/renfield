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

## Current state (Phase 1 Week 1 — models split only)

As of 2026-04-14, only `ha_glue.models.database` is populated. The
service and route extractions happen in Week 2; Alembic migration
cutover in Week 3; CI lint gate in Week 4.

Consumers can already import from the new location:

    from ha_glue.models.database import Room, RoomDevice, PresenceEvent

The legacy `from models.database import Room` path also still works
via a compat re-export in `models/database.py`, which will be removed
in Week 4 once every consumer has migrated.
"""
