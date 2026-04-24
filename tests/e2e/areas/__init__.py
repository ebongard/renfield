"""Per-area functional E2E tests for the Renfield frontend.

One file per navigation area. Each file exercises every user-visible
flow in that area against the live k8s production stack at
https://renfield.local. Tests are expected to be comprehensive, not
smoke-only — we want the suite to catch regressions before a user does.

Convention:
  * Filenames match tests/e2e/helpers/routes.py::Area.key
    (e.g. areas/test_chat.py → Area("chat", ...)).
  * Every test either reads an existing stable entity OR creates its
    own temporary resource and deletes it at the end — never leaks
    into the shared Brain/Knowledge DB.
  * Each test captures a screenshot on failure (handled by the
    session-scoped conftest fixtures).
"""
