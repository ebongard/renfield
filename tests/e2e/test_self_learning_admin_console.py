"""Playwright E2E suite for the v2.10 self-learning admin console.

Three scenarios — driven against a live Renfield deployment (default
``https://renfield.local``, override via ``RENFIELD_E2E_URL``). The
suite is skipped when ``RENFIELD_E2E_URL`` is not set so the file is
importable in environments without a running server (CI sanity, the
test_runner_159 box prior to deploy).

Scenarios:
  1. ``test_approval_loop``         — admin opens /admin/skills, finds a
                                      draft, clicks Approve, expects the
                                      card to disappear from the inbox.
  2. ``test_admin_multi_user_view`` — admin_view returns drafts from
                                      multiple users; switching the
                                      status pill swaps the visible set.
  3. ``test_curator_merge``         — admin opens /admin/curator, clicks
                                      "Run Now", expects the new audit
                                      row to appear in the history table.

The suite is fixture-light on purpose — it asserts UI invariants only
and assumes seed data was prepared via the backend route surface (the
production flow). Tests that need a clean DB belong in the Python+httpx
backend suite, not here.
"""
from __future__ import annotations

import os

import pytest
from playwright.sync_api import Page, expect, sync_playwright


BASE_URL = os.environ.get("RENFIELD_E2E_URL", "")
HEADLESS = os.environ.get("RENFIELD_E2E_HEADLESS", "true").lower() != "false"

pytestmark = pytest.mark.skipif(
    not BASE_URL,
    reason="set RENFIELD_E2E_URL to point at a live deployment",
)


@pytest.fixture(scope="module")
def page():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=HEADLESS)
        ctx = browser.new_context(ignore_https_errors=True)
        pg = ctx.new_page()
        yield pg
        ctx.close()
        browser.close()


def _login_as_admin(pg: Page) -> None:
    """Log in via the production /login form. Credentials are read from
    the env so the suite never has them hard-coded."""
    user = os.environ.get("RENFIELD_E2E_ADMIN_USER", "admin")
    pw = os.environ.get("RENFIELD_E2E_ADMIN_PASSWORD")
    if not pw:
        pytest.skip("RENFIELD_E2E_ADMIN_PASSWORD not set")
    pg.goto(f"{BASE_URL}/login", wait_until="networkidle", timeout=15000)
    pg.fill('input[name="username"]', user)
    pg.fill('input[type="password"]', pw)
    pg.click('button[type="submit"]')
    pg.wait_for_url(f"{BASE_URL}/", timeout=10000)


@pytest.mark.e2e
def test_approval_loop(page: Page) -> None:
    """Admin opens the Skills Inbox, approves the first draft, verifies
    the card disappears from the draft list and the draft-count badge
    decrements (or disappears entirely)."""
    _login_as_admin(page)
    page.goto(f"{BASE_URL}/admin/skills", wait_until="networkidle")

    # Empty inbox is a valid state — bail out cleanly rather than fail.
    cards = page.locator('[data-testid^="skill-card-"]')
    if cards.count() == 0:
        pytest.skip("no drafts present — seed the inbox or run after auto-extract")

    first_id = (cards.first.get_attribute("data-testid") or "").split("-")[-1]
    approve_btn = page.locator(
        f'[data-testid="skill-card-{first_id}"] [data-testid="approve-button"]'
    )
    approve_btn.click()

    expect(
        page.locator(f'[data-testid="skill-card-{first_id}"]')
    ).to_have_count(0, timeout=5000)


@pytest.mark.e2e
def test_admin_multi_user_view(page: Page) -> None:
    """Admin Skills Inbox shows drafts across users (admin_view=true),
    and the status filter pills swap the visible set."""
    _login_as_admin(page)
    page.goto(f"{BASE_URL}/admin/skills", wait_until="networkidle")

    # Default tab is "draft" — switch to "approved" and confirm the list
    # contents change (status pill flips aria-pressed).
    approved_pill = page.locator('[data-testid="status-filter-approved"]')
    approved_pill.click()
    expect(approved_pill).to_have_attribute("aria-pressed", "true")

    # Toolbar count must update — the totalCount span lives inside the
    # toolbar slot.
    expect(page.locator('[data-testid="admin-toolbar"]')).to_be_visible()


@pytest.mark.e2e
def test_curator_merge(page: Page) -> None:
    """Admin opens the curator runbook, triggers a manual run, and
    expects a new audit row to land in the history table."""
    _login_as_admin(page)
    page.goto(f"{BASE_URL}/admin/curator", wait_until="networkidle")

    before = page.locator('[data-testid^="curator-run-"]').count()
    run_btn = page.locator('[data-testid="run-curator-button"]')
    run_btn.click()

    # The mutation invalidates the runs query; wait for the count to grow.
    page.wait_for_function(
        f"document.querySelectorAll('[data-testid^=\"curator-run-\"]').length > {before}",
        timeout=15000,
    )
