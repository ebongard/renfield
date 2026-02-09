"""
E2E Browser Test: WakeWord (Ear Icon) on renfield.local
Troubleshoots the error that occurs when clicking the ear icon.
"""
from playwright.sync_api import sync_playwright
import os
import json
import time

SCREENSHOTS_DIR = "/Users/evdb/projects.ai/renfield/test-screenshots"
BASE_URL = "https://renfield.local"


def test_wakeword_click():
    os.makedirs(SCREENSHOTS_DIR, exist_ok=True)

    console_logs = []
    network_errors = []
    failed_requests = []

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--ignore-certificate-errors"],
        )
        context = browser.new_context(
            ignore_https_errors=True,
            permissions=["microphone"],
            viewport={"width": 1280, "height": 800},
        )
        page = context.new_page()

        # Capture ALL console messages
        page.on("console", lambda msg: console_logs.append(
            f"[{msg.type}] {msg.text}"
        ))

        # Capture page errors (uncaught exceptions)
        page.on("pageerror", lambda err: console_logs.append(
            f"[PAGE_ERROR] {err.message}"
        ))

        # Capture failed network requests
        page.on("requestfailed", lambda req: failed_requests.append(
            f"{req.method} {req.url} -> {req.failure}"
        ))

        # Track network responses for wakeword-related resources
        def on_response(response):
            url = response.url
            if any(k in url for k in ["wakeword", "ort/", ".onnx", ".wasm"]):
                status = response.status
                if status >= 400:
                    network_errors.append(f"{status} {url}")
                else:
                    console_logs.append(f"[NETWORK OK] {status} {url}")

        page.on("response", on_response)

        # 1. Load page
        print(f"Loading {BASE_URL}...")
        page.goto(BASE_URL, wait_until="networkidle", timeout=30000)
        page.screenshot(path=f"{SCREENSHOTS_DIR}/01_initial_load.png")
        print(f"Page loaded: {page.title()}")

        # 2. Find the ear/wakeword button
        print("\nSearching for WakeWord button (ear icon)...")

        # Try multiple selectors for the ear button
        ear_selectors = [
            'button[aria-label*="wake" i]',
            'button[aria-label*="Wake" i]',
            'button[title*="wake" i]',
            'button[title*="Wake" i]',
            '[data-testid*="wake"]',
            'button:has(svg)',  # buttons with SVG icons
        ]

        ear_button = None
        for sel in ear_selectors:
            buttons = page.locator(sel)
            count = buttons.count()
            if count > 0:
                print(f"  Found {count} match(es) for: {sel}")
                for i in range(count):
                    btn = buttons.nth(i)
                    aria = btn.get_attribute("aria-label") or ""
                    title = btn.get_attribute("title") or ""
                    text = btn.text_content() or ""
                    inner = btn.inner_html()[:100]
                    print(f"    [{i}] aria='{aria}' title='{title}' text='{text}' html={inner}...")
                    if "wake" in aria.lower() or "wake" in title.lower() or "ohr" in aria.lower() or "ear" in aria.lower():
                        ear_button = btn
                        print(f"    -> MATCH!")
                        break
            if ear_button:
                break

        # If still not found, search more broadly
        if not ear_button:
            print("\n  Trying broader search...")
            all_buttons = page.locator("button")
            for i in range(all_buttons.count()):
                btn = all_buttons.nth(i)
                aria = btn.get_attribute("aria-label") or ""
                title = btn.get_attribute("title") or ""
                classes = btn.get_attribute("class") or ""
                inner = btn.inner_html()[:200]
                # Look for ear-related content
                combined = f"{aria} {title} {classes} {inner}".lower()
                if any(k in combined for k in ["wake", "ear", "ohr", "listen", "micro", "hör"]):
                    print(f"    Found candidate [{i}]: aria='{aria}' title='{title}'")
                    print(f"      html: {inner[:150]}")
                    ear_button = btn
                    break

        if not ear_button:
            # Last resort: dump all buttons for debugging
            print("\n  WARNING: No ear button found. All buttons on page:")
            all_buttons = page.locator("button")
            for i in range(min(all_buttons.count(), 30)):
                btn = all_buttons.nth(i)
                aria = btn.get_attribute("aria-label") or ""
                title = btn.get_attribute("title") or ""
                text = (btn.text_content() or "").strip()[:50]
                visible = btn.is_visible()
                print(f"    [{i}] visible={visible} aria='{aria}' title='{title}' text='{text}'")

            page.screenshot(path=f"{SCREENSHOTS_DIR}/02_no_ear_button.png")
            print("\nFAILED: Could not find WakeWord/ear button")
            browser.close()
            return

        # 3. Screenshot before click
        page.screenshot(path=f"{SCREENSHOTS_DIR}/02_before_ear_click.png")

        # 4. Clear console logs and click
        console_logs.clear()
        print(f"\nClicking ear button...")
        ear_button.click()

        # 5. Wait for any error dialog/toast/modal to appear
        time.sleep(3)

        # 6. Screenshot after click
        page.screenshot(path=f"{SCREENSHOTS_DIR}/03_after_ear_click.png")

        # 7. Check for visible error messages
        print("\nChecking for error messages...")
        error_selectors = [
            ".error", ".alert", ".toast", ".notification",
            "[role='alert']", "[role='dialog']",
            ".Toastify", ".react-hot-toast",
            ".snackbar", ".modal",
        ]
        for sel in error_selectors:
            elements = page.locator(sel)
            if elements.count() > 0:
                for i in range(elements.count()):
                    el = elements.nth(i)
                    if el.is_visible():
                        text = el.text_content()
                        print(f"  VISIBLE ERROR [{sel}]: {text}")
                        el.screenshot(path=f"{SCREENSHOTS_DIR}/04_error_element_{sel.replace('.','').replace('[','').replace(']','')}.png")

        # 8. Check for any modals or overlays
        modals = page.locator("[class*='modal'], [class*='Modal'], [class*='dialog'], [class*='Dialog'], [class*='overlay']")
        if modals.count() > 0:
            for i in range(modals.count()):
                m = modals.nth(i)
                if m.is_visible():
                    text = m.text_content()[:500]
                    print(f"  MODAL/OVERLAY: {text}")

        # 9. Print console logs
        print(f"\n=== CONSOLE LOGS ({len(console_logs)} entries) ===")
        for log in console_logs:
            print(f"  {log}")

        # 10. Print network errors
        if network_errors:
            print(f"\n=== NETWORK ERRORS ({len(network_errors)}) ===")
            for err in network_errors:
                print(f"  {err}")

        if failed_requests:
            print(f"\n=== FAILED REQUESTS ({len(failed_requests)}) ===")
            for req in failed_requests:
                print(f"  {req}")

        # 11. Final full-page screenshot
        page.screenshot(path=f"{SCREENSHOTS_DIR}/05_final_state.png", full_page=True)

        print(f"\nScreenshots saved to {SCREENSHOTS_DIR}/")
        browser.close()


if __name__ == "__main__":
    test_wakeword_click()
