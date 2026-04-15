"""HA-specific FastAPI route modules.

Mounted on the platform FastAPI app via the `register_routes` hook
from `ha_glue.bootstrap.ha_glue_register_routes`. Platform-only
deploys (no ha_glue) do not mount any of these — the endpoints
simply do not exist and a client call returns 404.

Phase 1 W2 Phase C moved these from `api/routes/`:

- camera.py           (Frigate NVR event stream)
- homeassistant.py    (HA entity state push/pull)
- paperless_audit.py  (Paperless-NGX audit admin API)
- presence.py         (Presence query + user location)
- rooms.py            (Room CRUD, with rooms_schemas.py)
- satellites.py       (Satellite registration/status)
"""

