"""Post-deployment UI tests for Renfield on renfield.local.

Tests all major pages and core chat functionality after Phase 1-4 fixes.
"""
from playwright.sync_api import sync_playwright, expect
import os
import time

BASE_URL = "https://renfield.local"
SCREENSHOTS = os.path.join(os.path.dirname(__file__), "test-screenshots")
os.makedirs(SCREENSHOTS, exist_ok=True)

results = []


def record(name, passed, detail=""):
    status = "PASS" if passed else "FAIL"
    results.append((name, status, detail))
    print(f"  {'✅' if passed else '❌'} {name}" + (f" — {detail}" if detail else ""))


def test_all():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(ignore_https_errors=True)
        page = context.new_page()

        # Collect console errors
        console_errors = []
        page.on("console", lambda msg: console_errors.append(msg.text) if msg.type == "error" else None)

        # ============================================================
        # 1. Homepage / Chat
        # ============================================================
        print("\n📄 1. Homepage / Chat")
        try:
            page.goto(BASE_URL, wait_until="networkidle", timeout=15000)
            page.screenshot(path=f"{SCREENSHOTS}/01_homepage.png", full_page=True)

            # Check page loaded
            title = page.title()
            record("Homepage loads", bool(title), f"title='{title}'")

            # Check chat input exists
            chat_input = page.locator("textarea, input[type='text']").first
            record("Chat input visible", chat_input.is_visible())

            # Check no crash / white screen
            body_text = page.locator("body").inner_text()
            record("No blank page", len(body_text) > 50, f"{len(body_text)} chars")

        except Exception as e:
            page.screenshot(path=f"{SCREENSHOTS}/01_homepage_error.png")
            record("Homepage loads", False, str(e))

        # ============================================================
        # 2. Send a chat message and get streaming response
        # ============================================================
        print("\n💬 2. Chat Functionality")
        try:
            page.goto(BASE_URL, wait_until="networkidle", timeout=15000)
            time.sleep(1)

            # Type a message
            chat_input = page.locator("textarea, input[type='text']").first
            chat_input.fill("Hallo, wie geht es dir?")
            record("Can type in chat", True)

            # Send message (Enter or send button)
            send_btn = page.locator("button[type='submit'], button:has(svg)").last
            if send_btn.is_visible():
                send_btn.click()
            else:
                chat_input.press("Enter")

            # Wait for response (streaming)
            time.sleep(8)
            page.screenshot(path=f"{SCREENSHOTS}/02_chat_response.png", full_page=True)

            # Check that a response appeared
            messages = page.locator("[class*='message'], [class*='Message'], [class*='chat'], [class*='bubble']").all()
            record("Chat response received", len(messages) >= 2, f"{len(messages)} messages visible")

        except Exception as e:
            page.screenshot(path=f"{SCREENSHOTS}/02_chat_error.png")
            record("Chat functionality", False, str(e))

        # ============================================================
        # 3. Navigation — check all main pages
        # ============================================================
        print("\n🧭 3. Navigation / Pages")
        pages_to_test = [
            ("/knowledge", "Knowledge Base", "03_knowledge"),
            ("/rooms", "Rooms", "03_rooms"),
            ("/devices", "Devices", "03_devices"),
            ("/settings", "Settings", "03_settings"),
            ("/cameras", "Cameras", "03_cameras"),
        ]

        for path, name, screenshot_prefix in pages_to_test:
            try:
                page.goto(f"{BASE_URL}{path}", wait_until="networkidle", timeout=10000)
                page.screenshot(path=f"{SCREENSHOTS}/{screenshot_prefix}.png", full_page=True)

                # Check page rendered (not blank, no error)
                body = page.locator("body").inner_text()
                has_content = len(body) > 30
                record(f"{name} page loads", has_content, f"{len(body)} chars")

            except Exception as e:
                page.screenshot(path=f"{SCREENSHOTS}/{screenshot_prefix}_error.png")
                record(f"{name} page loads", False, str(e))

        # ============================================================
        # 4. Dark mode toggle
        # ============================================================
        print("\n🌙 4. Dark Mode")
        try:
            page.goto(BASE_URL, wait_until="networkidle", timeout=10000)

            # Find theme toggle button
            theme_btn = page.locator("button:has(svg[class*='moon']), button:has(svg[class*='sun']), button[aria-label*='theme'], button[aria-label*='Theme'], button[aria-label*='dark'], button[aria-label*='Dark']").first
            if theme_btn.is_visible():
                theme_btn.click()
                time.sleep(0.5)
                page.screenshot(path=f"{SCREENSHOTS}/04_dark_mode.png", full_page=True)
                record("Dark mode toggle works", True)
            else:
                # Try clicking any theme-related element
                page.screenshot(path=f"{SCREENSHOTS}/04_dark_mode_nobutton.png")
                record("Dark mode toggle works", False, "Toggle button not found")

        except Exception as e:
            page.screenshot(path=f"{SCREENSHOTS}/04_dark_mode_error.png")
            record("Dark mode toggle", False, str(e))

        # ============================================================
        # 5. Mobile viewport
        # ============================================================
        print("\n📱 5. Mobile Viewport")
        try:
            page.set_viewport_size({"width": 375, "height": 667})
            page.goto(BASE_URL, wait_until="networkidle", timeout=10000)
            time.sleep(1)
            page.screenshot(path=f"{SCREENSHOTS}/05_mobile.png", full_page=True)

            body = page.locator("body").inner_text()
            record("Mobile viewport renders", len(body) > 50, f"{len(body)} chars")

            # Reset viewport
            page.set_viewport_size({"width": 1280, "height": 720})

        except Exception as e:
            page.screenshot(path=f"{SCREENSHOTS}/05_mobile_error.png")
            record("Mobile viewport", False, str(e))

        # ============================================================
        # 6. Health check endpoint (via browser)
        # ============================================================
        print("\n🏥 6. Health Check (sanitized errors)")
        try:
            page.goto(f"{BASE_URL}/api/health/ready", wait_until="networkidle", timeout=10000)
            body = page.locator("body").inner_text()
            page.screenshot(path=f"{SCREENSHOTS}/06_health.png")

            # Verify no internal error details leaked
            has_status = "healthy" in body or "unhealthy" in body or "degraded" in body
            no_traceback = "Traceback" not in body and "Exception" not in body
            record("Health endpoint responds", has_status, body[:100])
            record("No error details leaked", no_traceback)

        except Exception as e:
            record("Health check", False, str(e))

        # ============================================================
        # 7. Console errors check
        # ============================================================
        print("\n🔍 7. Console Errors")
        critical_errors = [e for e in console_errors if "FATAL" in e or "Uncaught" in e or "chunk" in e.lower()]
        record("No critical JS errors", len(critical_errors) == 0,
               f"{len(console_errors)} total, {len(critical_errors)} critical")

        # ============================================================
        # Summary
        # ============================================================
        browser.close()

        print("\n" + "=" * 50)
        passed = sum(1 for _, s, _ in results if s == "PASS")
        failed = sum(1 for _, s, _ in results if s == "FAIL")
        print(f"📊 Ergebnis: {passed} bestanden, {failed} fehlgeschlagen")

        if failed > 0:
            print("\nFehlgeschlagene Tests:")
            for name, status, detail in results:
                if status == "FAIL":
                    print(f"  ❌ {name}: {detail}")

        print("=" * 50)


if __name__ == "__main__":
    test_all()
