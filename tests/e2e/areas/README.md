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

| Area                    | Depth                  |
|-------------------------|------------------------|
| chat                    | **Full** — covers upload → Paperless assertion |
| knowledge               | **Full** — upload + ingest + search + delete |
| settings_circles        | **Full** — tier + member CRUD via API |
| admin_users             | **Full** — CRUD template |
| _everything else_       | Stub (page-render guard + TODO list) |

Stub files carry a TODO block listing what each area's full suite
should cover. As bugs hit a given area, port the repro into that
file before closing the bug out.

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
