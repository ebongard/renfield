#!/usr/bin/env python3
"""Post-deploy browser smoke for the live Renfield cluster.

Verifies that the rolled bundle hydrates correctly in a real Chromium
and that each page on the v2.10+ admin surface renders its localized
marker. Catches the class of failure that curl /health cannot:

  - PWA service worker serving a stale pre-rollout bundle
  - JS bundle 404 because the frontend image didn't actually move
  - Build-time env-var bug that nukes a feature path
  - SPA route mounted but the page component throws on hydration
  - Backend route reachable but returning a shape the client can't parse
    (the 422 on /api/trajectories/stats from #617 was caught this way)

How to run
----------

Two paths, both equivalent. The Mac dev machine usually can't reach the
cluster network directly, so prefer running from the build box.

(a) Locally, against any reachable host:

    pip install --user playwright
    playwright install chromium
    python3 bin/post-deploy-smoke.py
    # override the target:
    RENFIELD_SMOKE_URL=https://staging.example python3 bin/post-deploy-smoke.py

(b) From the .159 build box via the official Playwright image (no host
    Python deps; reaches the renfield-private cluster directly):

    scp bin/post-deploy-smoke.py evdb@192.168.1.159:/tmp/
    ssh evdb@192.168.1.159 'docker run --rm --network host \
        -v /tmp/post-deploy-smoke.py:/smoke.py -v /tmp:/tmp \
        mcr.microsoft.com/playwright/python:v1.45.0-jammy \
        sh -c "pip3 install -q playwright==1.45.0 && python3 /smoke.py"'

Exit code is 0 on full pass, 1 on any page failure. Screenshots are
written to ``/tmp/renfield-smoke-*.png`` (or ``SMOKE_SCREENSHOT_DIR``
when set) so a failed run leaves a visual artefact behind for triage.

Assumptions
-----------

* ``auth_enabled = false`` in the deployment under test (single-user
  mode — the home cluster's default). For an auth-required deployment,
  use ``tests/e2e/test_self_learning_admin_console.py`` with
  ``RENFIELD_E2E_ADMIN_PASSWORD`` set instead.
* SSL cert errors are ignored (self-signed cluster cert).
* Only the v2.10 admin-console pages are smoked today. Add to
  ``PAGES`` as new admin surfaces ship.
"""
from __future__ import annotations

import os
import sys

from playwright.sync_api import sync_playwright


BASE = os.environ.get("RENFIELD_SMOKE_URL", "https://renfield.local")
SCREENSHOT_DIR = os.environ.get("SMOKE_SCREENSHOT_DIR", "/tmp")

# Each page must contain at least one of the markers. Localized variants
# (DE + EN) cover both browser locales without pinning either.
PAGES: list[tuple[str, list[str]]] = [
    ("/admin/skills",       ["Fähigkeiten-Inbox", "Skills Inbox"]),
    ("/admin/tool-health",  ["Werkzeug-Gesundheit", "Tool Health"]),
    ("/admin/trajectories", ["Verlaufstrajektorien", "Agent Trajectories"]),
    ("/admin/curator",      ["Kurator-Runbook", "Curator Runbook"]),
    ("/brain/skills",       ["Meine Fähigkeiten", "My Skills"]),
]

# XHR failures we know about and explicitly accept. The Reva
# wissensbasis probe is supposed to 404 on Renfield (the hook hides
# the nav entry on absence). Anything else is suspect.
EXPECTED_XHR_FAILS: list[str] = [
    "/api/wissensbasis/me/mix",
]


def _is_expected(url: str) -> bool:
    return any(needle in url for needle in EXPECTED_XHR_FAILS)


def main() -> int:
    failures: list[str] = []

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=[
                # Container-cluster cert is self-signed; ignore at the
                # browser-process level so sub-resource fetches (not just
                # the top-level navigation) pass too.
                "--ignore-certificate-errors",
                "--ignore-certificate-errors-spki-list",
                "--allow-running-insecure-content",
            ],
        )
        ctx = browser.new_context(ignore_https_errors=True)
        page = ctx.new_page()

        console_errors: dict[str, list[str]] = {}
        failed_xhrs: dict[str, list[tuple[int, str, str]]] = {}

        def _on_console(msg) -> None:
            if msg.type == "error":
                console_errors.setdefault(page.url, []).append(msg.text)

        def _on_response(resp) -> None:
            if resp.status >= 400:
                failed_xhrs.setdefault(page.url, []).append(
                    (resp.status, resp.request.method, resp.url)
                )

        page.on("console", _on_console)
        page.on("response", _on_response)

        for path, markers in PAGES:
            url = BASE + path
            console_errors.pop(url, None)
            failed_xhrs.pop(url, None)

            try:
                resp = page.goto(url, wait_until="networkidle", timeout=20000)
                page.wait_for_load_state("networkidle", timeout=10000)
                # Brief idle so React renders the page body after the
                # initial XHRs settle.
                page.wait_for_timeout(800)

                status = resp.status if resp else "?"
                body_text = page.locator("body").inner_text()
                found = next((m for m in markers if m in body_text), None)

                screenshot = (
                    f"{SCREENSHOT_DIR}/renfield-smoke-"
                    f"{path.strip('/').replace('/', '_') or 'root'}.png"
                )
                page.screenshot(path=screenshot, full_page=False)

                xhrs = failed_xhrs.get(url, [])
                unexpected_xhrs = [
                    (code, method, u) for (code, method, u) in xhrs
                    if not _is_expected(u)
                ]
                critical_console = [
                    e for e in console_errors.get(url, [])
                    if "status of 5" in e  # surface only 5xx-driven errors
                ]

                ok = (
                    status == 200
                    and found is not None
                    and not unexpected_xhrs
                    and not critical_console
                )
                tag = "OK" if ok else "FAIL"
                print(
                    f"[{tag}] {path}  http={status}  "
                    f"marker={found or 'MISSING'}  "
                    f"screenshot={screenshot}"
                )
                for code, method, u in unexpected_xhrs:
                    print(f"    unexpected xhr: {code} {method} {u}")
                for e in critical_console:
                    print(f"    console err: {e[:140]}")
                if not ok:
                    failures.append(path)
            except Exception as e:
                print(f"[FAIL] {path}  exception: {e}")
                failures.append(path)

        browser.close()

    print()
    if failures:
        print(f"FAILED: {len(failures)}/{len(PAGES)} pages — {failures}")
        return 1
    print(f"PASS: {len(PAGES)}/{len(PAGES)} pages rendered cleanly")
    return 0


if __name__ == "__main__":
    sys.exit(main())
