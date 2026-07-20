"""
E2E Test Fixtures — Playwright browser fixtures for renfield.local.

Provides browser, context, page, and screenshot helpers for all E2E tests.
Target: https://renfield.local (production, self-signed certs).
"""

import json
import os
import sys
import pytest
from playwright.sync_api import sync_playwright

# Make `from tests.e2e.helpers import ...` work in every environment —
# pytest's rootdir varies (repo root when run via `make test-e2e-browser`,
# `/tests/e2e/areas` when invoked inside the backend container). This
# conftest is always imported before any test module, so inserting the
# parent directory of `tests/` onto sys.path guarantees the
# `tests.e2e.helpers` import path resolves.
_REPO_ROOT = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..")
)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)
# In the backend container the repo root isn't mounted — only `/tests`
# is. When that's the case, expose `/tests` so `import e2e.helpers...`
# works as a fallback. We re-export the same modules under that name.
_TESTS_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _TESTS_ROOT not in sys.path:
    sys.path.insert(0, _TESTS_ROOT)

# Target host is env-overridable so the same suite runs against the auth-off
# household (default) OR an auth-on instance (e.g. xidra) without code changes:
#   E2E_BASE_URL=https://x-ren.local
BASE_URL = os.environ.get("E2E_BASE_URL", "https://renfield.local").rstrip("/")
SCREENSHOTS_DIR = os.path.join(os.path.dirname(__file__), "screenshots")

# Auth credentials come ONLY from the environment — NEVER hard-coded, committed,
# echoed, or passed on a URL. Inject them at run time from the cluster secret, e.g.:
#   E2E_USERNAME=evdb \
#   E2E_PASSWORD="$(kubectl -n renfield-xidra get secret renfield-secrets \
#       -o jsonpath='{.data.default-admin-password}' | base64 -d)" \
#   E2E_BASE_URL=https://x-ren.local \
#   ./bin/run-e2e.sh tests/e2e/areas/test_notes.py
# The password reaches Playwright via the process env and is exchanged for a JWT
# through the real /api/auth/login flow; the token lives only in browser storage
# for the run. When the vars are unset (auth-off household) the fixtures below
# behave as an unauthenticated passthrough, so existing tests are unaffected.
E2E_USERNAME = os.environ.get("E2E_USERNAME")
E2E_PASSWORD = os.environ.get("E2E_PASSWORD")
# Alternative to a username/password: a pre-minted access token, injected via env.
# Use when the target's password is not recoverable (e.g. a human admin who
# changed it) — mint a SHORT-LIVED token server-side and pipe it straight into
# the env var so it never touches a URL, a committed file, or the shell history:
#   E2E_ACCESS_TOKEN="$(kubectl -n renfield-xidra exec -i deploy/backend -c backend \
#       -- python3 -c 'from datetime import timedelta; \
#       from services.auth_service import create_access_token; \
#       print(create_access_token({\"sub\":\"1\"}, expires_delta=timedelta(minutes=20)))')" \
#   E2E_BASE_URL=https://x-ren.local ./bin/run-e2e.sh tests/e2e/areas/test_notes.py
E2E_ACCESS_TOKEN = os.environ.get("E2E_ACCESS_TOKEN")
ACCESS_TOKEN_STORAGE_KEY = "renfield_access_token"


def e2e_auth_enabled() -> bool:
    """True when auth material is present — a pre-minted token, or a user+password."""
    return bool(E2E_ACCESS_TOKEN or (E2E_USERNAME and E2E_PASSWORD))


@pytest.fixture(scope="session")
def _playwright_instance():
    with sync_playwright() as p:
        yield p


@pytest.fixture(scope="session")
def browser(_playwright_instance):
    # --ignore-certificate-errors applies the cert bypass to WebSocket
    # upgrades too, which `ignore_https_errors=True` on the context
    # does NOT cover in headless Chromium. Without it, renfield.local's
    # self-signed cert silently kills the wss:// handshake and the
    # chat page sits on "Verbinde..." forever.
    browser = _playwright_instance.chromium.launch(
        headless=True,
        args=["--ignore-certificate-errors"],
    )
    yield browser
    browser.close()


@pytest.fixture(scope="session")
def browser_context(browser):
    context = browser.new_context(ignore_https_errors=True)
    yield context
    context.close()


@pytest.fixture
def page(browser_context, request):
    """New page per test with automatic screenshot on completion."""
    os.makedirs(SCREENSHOTS_DIR, exist_ok=True)
    pg = browser_context.new_page()
    pg.set_default_timeout(30_000)

    yield pg

    # Screenshot after every test (pass or fail)
    test_name = request.node.name
    pg.screenshot(
        path=os.path.join(SCREENSHOTS_DIR, f"{test_name}.png"),
        full_page=True,
    )
    pg.close()


@pytest.fixture
def knowledge_page(page):
    """Navigate to /knowledge and wait for stats to load."""
    page.goto(f"{BASE_URL}/knowledge", wait_until="networkidle", timeout=15_000)
    # Wait for stats grid to render (at least one stat card with a number)
    page.wait_for_selector(".text-2xl.font-bold", timeout=10_000)
    return page


@pytest.fixture
def chat_page(page):
    """Navigate to / and wait for chat to be ready."""
    page.goto(BASE_URL, wait_until="networkidle", timeout=15_000)
    page.wait_for_selector("#chat-input", timeout=10_000)
    return page


def _login_for_token(browser) -> str:
    """Exchange the env credentials for an access token via the real login flow.

    Uses Playwright's request API (not a browser form) so the token is obtained
    over the same `/api/auth/login` OAuth2-password endpoint the UI uses, without
    scraping brittle form selectors. The password is read from the process env
    only; it is never logged. Returns the JWT string (kept in-process)."""
    req = browser.new_context(ignore_https_errors=True).request
    resp = req.post(
        f"{BASE_URL}/api/auth/login",
        form={"username": E2E_USERNAME, "password": E2E_PASSWORD},
    )
    assert resp.ok, f"E2E login failed: HTTP {resp.status} (check E2E_USERNAME/E2E_PASSWORD)"
    token = resp.json().get("access_token")
    assert token, "login response had no access_token"
    return token


@pytest.fixture(scope="session")
def authenticated_context(browser):
    """A browser context pre-seeded with a real JWT (auth-on targets only).

    Skips cleanly when credentials are absent (auth-off household), so this
    fixture is safe to request from any test — an auth-required test should call
    `require_e2e_auth` (below) to skip itself, this just wires the token in.

    The token is injected via an init-script so EVERY page in the context boots
    authenticated (the frontend reads `renfield_access_token` from localStorage).
    Nothing sensitive is written to disk or the URL."""
    if not e2e_auth_enabled():
        ctx = browser.new_context(ignore_https_errors=True)
        yield ctx
        ctx.close()
        return
    # A pre-minted token wins (no password needed); else exchange creds via login.
    token = E2E_ACCESS_TOKEN or _login_for_token(browser)
    ctx = browser.new_context(ignore_https_errors=True)
    ctx.add_init_script(
        f"window.localStorage.setItem({json.dumps(ACCESS_TOKEN_STORAGE_KEY)}, {json.dumps(token)});"
    )
    yield ctx
    ctx.close()


@pytest.fixture
def authenticated_page(authenticated_context, request):
    """Per-test authenticated page (screenshot on completion, mirrors `page`)."""
    os.makedirs(SCREENSHOTS_DIR, exist_ok=True)
    pg = authenticated_context.new_page()
    pg.set_default_timeout(30_000)
    yield pg
    pg.screenshot(
        path=os.path.join(SCREENSHOTS_DIR, f"{request.node.name}.png"),
        full_page=True,
    )
    pg.close()


@pytest.fixture
def require_e2e_auth():
    """Skip a test unless run against an auth-on target with credentials set."""
    if not e2e_auth_enabled():
        pytest.skip("needs E2E_USERNAME/E2E_PASSWORD (auth-on target, e.g. E2E_BASE_URL=https://x-ren.local)")
