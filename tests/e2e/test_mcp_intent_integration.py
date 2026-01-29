"""
E2E Tests: MCP Intent Integration + Dynamic Examples

Tests that the full pipeline works:
1. MCP servers are connected and tools are registered (incl. Paperless)
2. Intent prompt includes YAML-configured examples
3. Intents status API returns MCP tools
4. Integrations page shows MCP servers with tools
5. Intents admin page shows MCP tools section
6. Chat page loads correctly
"""
from playwright.sync_api import sync_playwright
import requests
import os

BASE_URL = os.environ.get("BASE_URL", "http://localhost:3000")
API_URL = os.environ.get("API_URL", "http://localhost:8000")
SCREENSHOTS = os.path.join(os.path.dirname(__file__), "test-screenshots")

os.makedirs(SCREENSHOTS, exist_ok=True)


def test_api_mcp_tools_registered():
    """Test: MCP tools are registered in the backend API, including Paperless."""
    print("\n=== Test 1: MCP Tools registered via API ===")

    resp = requests.get(f"{API_URL}/api/mcp/tools")
    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}"

    data = resp.json()
    tools = data.get("tools", [])
    assert len(tools) > 0, "Expected at least 1 MCP tool"

    # Check that paperless tools exist
    paperless_tools = [t for t in tools if t.get("server") == "paperless"]
    servers = sorted(set(t["server"] for t in tools))
    print(f"  Total MCP tools: {len(tools)}")
    print(f"  Servers: {servers}")
    print(f"  Paperless tools: {len(paperless_tools)}")
    assert len(paperless_tools) > 0, f"Expected paperless tools. Connected servers: {servers}"

    print("  ✅ MCP tools registered (including Paperless)")


def test_api_mcp_status_all_connected():
    """Test: All 7 MCP servers are connected."""
    print("\n=== Test 2: All MCP servers connected ===")

    resp = requests.get(f"{API_URL}/api/mcp/status")
    assert resp.status_code == 200
    data = resp.json()

    servers = data.get("servers", [])
    connected = [s for s in servers if s.get("connected")]
    disconnected = [s for s in servers if not s.get("connected")]

    print(f"  Total servers: {len(servers)}")
    print(f"  Connected: {[s['name'] for s in connected]}")
    if disconnected:
        print(f"  Disconnected: {[s['name'] for s in disconnected]}")

    assert len(connected) >= 7, f"Expected >= 7 connected servers, got {len(connected)}"
    server_names = {s["name"] for s in connected}
    assert "paperless" in server_names, f"Expected paperless server. Got: {server_names}"

    print("  ✅ All MCP servers connected")


def test_api_intent_prompt_includes_mcp_examples():
    """Test: Intent prompt includes YAML-configured MCP examples (DE + EN)."""
    print("\n=== Test 3: Intent prompt includes MCP examples ===")

    # German prompt
    resp_de = requests.get(f"{API_URL}/api/intents/prompt", params={"lang": "de"})
    assert resp_de.status_code == 200
    data_de = resp_de.json()

    intent_types = data_de.get("intent_types", "")
    examples_de = data_de.get("examples", "")

    assert "MCP TOOLS" in intent_types, "Expected MCP TOOLS section in intent_types"
    assert "paperless" in intent_types.lower(), "Expected paperless in intent_types"
    assert "BEISPIELE:" in examples_de, "Expected BEISPIELE header in examples"

    # Check that YAML-configured examples appear (from mcp_servers.yaml)
    assert "Dokumente" in examples_de or "Paperless" in examples_de, \
        f"Expected Paperless example in German prompt. Got:\n{examples_de}"
    print(f"  German examples (first 300 chars):\n    {examples_de[:300]}")

    # English prompt
    resp_en = requests.get(f"{API_URL}/api/intents/prompt", params={"lang": "en"})
    assert resp_en.status_code == 200
    data_en = resp_en.json()
    examples_en = data_en.get("examples", "")
    assert "EXAMPLES:" in examples_en, "Expected EXAMPLES header in English"
    print(f"  English examples (first 300 chars):\n    {examples_en[:300]}")

    print("  ✅ Intent prompt includes MCP examples (DE + EN)")


def test_api_intents_status_has_paperless():
    """Test: /api/intents/status returns MCP tools including paperless."""
    print("\n=== Test 4: Intents status includes paperless tools ===")

    resp = requests.get(f"{API_URL}/api/intents/status")
    assert resp.status_code == 200
    data = resp.json()

    mcp_tools = data.get("mcp_tools", [])
    total = data.get("total_intents", 0)
    print(f"  Total intents: {total}")
    print(f"  MCP tools: {len(mcp_tools)}")

    paperless_tools = [t for t in mcp_tools if "paperless" in t.get("intent", "")]
    assert len(paperless_tools) > 0, \
        f"Expected paperless tools in intents status. Servers found: {set(t.get('server','') for t in mcp_tools)}"
    print(f"  Paperless intents: {[t['intent'] for t in paperless_tools[:3]]}...")

    print("  ✅ Intents status has paperless tools")


def test_ui_integrations_page():
    """Test: Integrations page renders MCP servers including Paperless."""
    print("\n=== Test 5: Integrations page shows MCP servers ===")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()

        try:
            page.goto(f"{BASE_URL}/admin/integrations", wait_until="networkidle")
            page.wait_for_timeout(3000)

            page.screenshot(path=f"{SCREENSHOTS}/integrations-page.png", full_page=True)

            content = page.text_content("body") or ""
            content_lower = content.lower()

            # Should show MCP servers section
            assert "mcp" in content_lower, "Expected MCP references on Integrations page"

            # Should show paperless server
            assert "paperless" in content_lower, \
                "Expected Paperless server on Integrations page"

            # Should show server count (7 servers)
            assert "7" in content, "Expected 7 servers count on page"

            print("  ✅ Integrations page shows MCP servers including Paperless")

        except Exception as e:
            page.screenshot(path=f"{SCREENSHOTS}/integrations-page-FAIL.png", full_page=True)
            print(f"  ❌ Failed: {e}")
            raise
        finally:
            browser.close()


def test_ui_intents_page():
    """Test: Admin intents page shows MCP tools section."""
    print("\n=== Test 6: Intents page shows MCP tools ===")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()

        try:
            page.goto(f"{BASE_URL}/admin/intents", wait_until="networkidle")
            page.wait_for_timeout(3000)

            page.screenshot(path=f"{SCREENSHOTS}/intents-page.png", full_page=True)

            content = page.text_content("body") or ""
            content_lower = content.lower()

            # Should show MCP tools section
            assert "mcp" in content_lower, "Expected MCP section on intents page"

            # Should show paperless tools
            assert "paperless" in content_lower, "Expected paperless on intents page"

            print("  ✅ Intents page shows MCP tools including Paperless")

        except Exception as e:
            page.screenshot(path=f"{SCREENSHOTS}/intents-page-FAIL.png", full_page=True)
            print(f"  ❌ Failed: {e}")
            raise
        finally:
            browser.close()


def test_ui_chat_page():
    """Test: Chat page loads and input is available."""
    print("\n=== Test 7: Chat page loads ===")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()

        try:
            page.goto(f"{BASE_URL}/chat", wait_until="networkidle")
            page.wait_for_timeout(2000)

            page.screenshot(path=f"{SCREENSHOTS}/chat-page.png", full_page=True)

            # Chat input should be present
            chat_input = page.locator("textarea, input[type='text']").first
            assert chat_input.is_visible(), "Expected chat input to be visible"

            print("  ✅ Chat page loads correctly")

        except Exception as e:
            page.screenshot(path=f"{SCREENSHOTS}/chat-page-FAIL.png", full_page=True)
            print(f"  ❌ Failed: {e}")
            raise
        finally:
            browser.close()


if __name__ == "__main__":
    tests = [
        test_api_mcp_tools_registered,
        test_api_mcp_status_all_connected,
        test_api_intent_prompt_includes_mcp_examples,
        test_api_intents_status_has_paperless,
        test_ui_integrations_page,
        test_ui_intents_page,
        test_ui_chat_page,
    ]

    passed = 0
    failed = 0

    for test in tests:
        try:
            test()
            passed += 1
        except Exception as e:
            failed += 1
            print(f"  ❌ FAILED: {e}")

    print(f"\n{'='*50}")
    print(f"Results: {passed} passed, {failed} failed out of {len(tests)}")
    print(f"Screenshots: {SCREENSHOTS}")
    if failed > 0:
        exit(1)
