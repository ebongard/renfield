# Per-Area E2E Tests

One test file per navigation area in the Renfield frontend. Each file
drives the full user-facing flow (UI actions + downstream state
assertions) against `https://renfield.local`.

## Why "functional", not "smoke"

PR #464 and PR #467 both shipped bugs that a smoke test would have
passed: the UI rendered "upload erfolgreich", but Paperless either
rejected the document (HTTP 400 — #464) or accepted it stripped of
every piece of metadata the extractor was supposed to produce (#467).

Smoke tests — "page loads, element visible" — verify nothing about
whether the feature actually works. A test file in this directory is
expected to assert the downstream effect: DB row created, MCP called,
Paperless document carries the right correspondent/document_type/tags,
circle tier change cascades to the retrieval filter, etc.

## File naming

`test_<area_key>.py` where `<area_key>` matches the `key` field in
`tests/e2e/helpers/routes.py::AREAS`. Adding a new page to the nav
means adding:

1. An `Area(...)` entry in `routes.py`.
2. A matching `test_<area_key>.py` in this directory.

## Depth status

Every area ships with real browser tests that drive the UI AND assert
the backend state — no stubs. Each file covers the core user flows
for that area plus at least one end-to-end mutation round-trip where
the UI exposes one.

| Area                    | Key coverage |
|-------------------------|--------------|
| chat                    | render, send/reply, **attach → forward to Paperless with metadata assertion**, sidebar preview, delete, theme |
| knowledge               | render, upload PDF → assert chunks > 0, delete, KB list |
| settings_circles        | render, tier API, member CRUD, UI controls present |
| admin_users             | render, list endpoint, user CRUD + UI verify |
| tasks                   | render, list endpoint, create/delete round-trip, UI verify |
| memory                  | render, list, create/delete round-trip, UI verify |
| brain                   | render, /api/atoms list, search input fires request |
| brain_review            | render, review-queue endpoint, empty-state |
| federation_audit        | render, audit endpoint, fetch on load |
| knowledge_graph         | render, entities/relations/stats/tiers, fetch on load |
| camera                  | render, cameras/events list, fetch on load |
| rooms                   | render, list, room CRUD + UI verify |
| speakers                | render, list, status, speaker CRUD + UI verify |
| smart_home              | render, states endpoint, fetch on load |
| admin_integrations      | render, MCP status/tools, fetch on load, refresh button |
| admin_intents           | render, intents status, fetch on load, prompt endpoint |
| admin_routing           | render, traces/stats, fetch on load |
| admin_roles             | render, list, role CRUD + UI verify |
| admin_satellites        | render, list, versions, fetch on load |
| admin_presence          | render, status/rooms/devices, fetch on load |
| admin_paperless_audit   | render, status/stats, fetch on load |
| admin_maintenance       | render, action buttons present, reindex button fires POST |
| admin_settings          | render, wakeword settings/models, fetch on load |

Tests that need auth (list users, create role, etc.) skip cleanly
when the endpoint returns 401/403/404 in a single-user dev deploy —
but the test itself is real; flip `AUTH_ENABLED=true` and they run.

## Running

```bash
./bin/run-e2e.sh                             # full suite + HTML report
./bin/run-e2e.sh tests/e2e/areas/test_chat.py    # one area
./bin/run-e2e.sh -k circles                  # pytest -k filter
make test-e2e-browser                        # Makefile alias
```

Reports land in `tests/e2e/reports/e2e-report-<timestamp>.html`
(self-contained — can be emailed / attached to an issue). Screenshots
of every test page land in `tests/e2e/screenshots/`.

### Environment

| Var                     | Purpose                               |
|-------------------------|---------------------------------------|
| `PAPERLESS_API_URL`     | Enables the Paperless-state assertions in `test_chat.py`. If unset, those tests skip cleanly. |
| `PAPERLESS_API_TOKEN`   | Same.                                 |
| `RENFIELD_TEST_TOKEN`   | Bearer token for authenticated backend calls (when `AUTH_ENABLED=true`). |

### Dependencies

Browser tests add `playwright`, `pytest-html`, and `httpx` to
`src/backend/requirements-test.txt`. First-time setup:

```bash
pip install -r src/backend/requirements-test.txt
python -m playwright install chromium
```
