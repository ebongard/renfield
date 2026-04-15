"""REST API routes owned by the ha_glue layer.

Currently minimal — only `ha_glue.api.admin` for the
`/admin/refresh-keywords` endpoint. Week 2 Phase C moves the full set
of HA-specific routes (rooms, presence, satellites, camera,
paperless_audit, homeassistant) into `ha_glue.api.routes.*`.

Route modules are registered with the platform FastAPI app via the
`register_routes` hook, which the platform fires at the end of
startup. ha_glue.bootstrap.register() wires a `register_routes`
handler that pulls in every router in this package and mounts them
on the app.
"""
