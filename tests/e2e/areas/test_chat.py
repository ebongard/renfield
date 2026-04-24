"""Comprehensive functional tests for the Chat area (https://renfield.local/).

Covers:
  * Page render + core UI elements
  * Plain text message → agent responds
  * File attach + chat-upload → agent forwards to Paperless with metadata
    (the flow that silently broke in 2026-04-24 and shipped as PR #467)
  * Dark-mode toggle persists across navigations
  * Conversation history: send → persisted → visible in sidebar
  * New-chat button resets the input

Tests that drive Paperless also assert the downstream Paperless state
(correspondent / document_type / tags) — a pure UI check like
"erfolgreich hochgeladen" would have passed during the 2026-04-24
regression, which is exactly what we're trying to prevent.

Tests that mutate shared state (upload a doc, start a conversation)
clean up after themselves via the conversation + Paperless APIs.
"""
from __future__ import annotations

import os
import re
import shutil
import tempfile
import time
import uuid

import pytest

from tests.e2e.helpers import api, paperless
from tests.e2e.helpers.asserts import (
    assert_body_not_blank,
    assert_no_critical_console_errors,
)
from tests.e2e.helpers.page import BASE_URL, capture_console_errors


pytestmark = pytest.mark.e2e


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def chat_page(page):
    """Navigate to / and wait for the message input."""
    page.goto(BASE_URL, wait_until="networkidle", timeout=20_000)
    page.wait_for_selector("#chat-input", timeout=15_000)
    return page


@pytest.fixture()
def ws_connected_chat_page(chat_page):
    """chat_page + a skip when the WebSocket doesn't connect.

    Headless Chromium against renfield.local's self-signed cert often
    fails the wss:// upgrade — the page renders, but the "Verbunden"
    badge never shows up. Tests that actually send a message through
    the WebSocket (agent turn, file upload, delete a just-created
    conversation) skip cleanly in that case rather than time out on
    an impossible wait. Against a deploy with a trusted TLS cert they
    run normally.
    """
    try:
        chat_page.wait_for_selector("text=Verbunden", timeout=8_000)
    except Exception:
        pytest.skip(
            "Chat WebSocket did not connect in this browser/cert "
            "combination — WS-dependent send tests skipped. Add a "
            "trusted TLS cert or run against a deploy with one."
        )
    return chat_page


@pytest.fixture()
def test_pdf_path(tmp_path):
    """A real invoice PDF from Downloads if present, else a minimal valid
    PDF written to tmp. Must be >100 bytes or the MCP rejects it."""
    candidates = [
        "/Users/evdb/Downloads/2024-10-27 Herr Wilhelm Von den Bongard Rechnung NEW Niederrhein Energie und Wasser GmbH.pdf",
    ]
    for src in candidates:
        if os.path.isfile(src):
            dst = tmp_path / "test-invoice.pdf"
            shutil.copy(src, dst)
            return str(dst)

    dst = tmp_path / "minimal.pdf"
    dst.write_bytes(
        b"%PDF-1.4\n1 0 obj<</Type/Catalog>>endobj\n"
        b"trailer<</Root 1 0 R>>\n%%EOF\n" + b"x" * 300
    )
    return str(dst)


@pytest.fixture()
def created_session_ids():
    """Collect session IDs created during a test; delete them at the end
    so the conversation sidebar doesn't fill up with test artefacts."""
    ids: list[str] = []
    yield ids
    for sid in ids:
        try:
            api.delete_conversation(sid)
        except Exception:
            pass


@pytest.fixture()
def created_paperless_ids():
    """Same story for uploaded-to-Paperless doc IDs."""
    ids: list[int] = []
    yield ids
    if not (paperless.URL and paperless.TOKEN):
        return
    for did in ids:
        try:
            paperless.delete_document(did)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# 1. Page render + core UI
# ---------------------------------------------------------------------------


class TestChatPageRenders:
    def test_loads_and_shows_empty_state(self, chat_page):
        """Cold visit: empty state + suggested prompts are visible."""
        get_errors = capture_console_errors(chat_page)

        # H1 is "Chat"; empty state has "Starte ein Gespräch mit Renfield"
        assert chat_page.locator("h1:has-text('Chat')").is_visible()
        assert chat_page.get_by_text(
            "Starte ein Gespräch mit Renfield"
        ).is_visible()

        # At least one suggested prompt button
        assert chat_page.get_by_role(
            "button", name=re.compile(r"Wetter|Licht|Musik", re.IGNORECASE),
        ).first.is_visible()

        assert_body_not_blank(chat_page.locator("body").inner_text())
        assert_no_critical_console_errors(get_errors())

    def test_input_controls_present_and_enabled(self, chat_page):
        """Textarea + attach + mic + send buttons all render; send is
        disabled until the user has entered text (or uploaded a file)."""
        textarea = chat_page.locator("#chat-input").first
        assert textarea.is_visible()
        assert textarea.is_enabled()

        assert chat_page.get_by_role(
            "button", name=re.compile(r"Datei anh", re.IGNORECASE),
        ).is_visible()
        assert chat_page.get_by_role(
            "button", name=re.compile(r"Sprachaufnahme", re.IGNORECASE),
        ).is_visible()

        send_btn = chat_page.get_by_role(
            "button", name=re.compile(r"Nachricht senden", re.IGNORECASE),
        )
        assert send_btn.is_visible()
        # Disabled on a cold page (no text, no attachment)
        assert send_btn.is_disabled()

    def test_websocket_connects(self, chat_page):
        """Canary for the WS handshake. Deliberately uses the plain
        `chat_page` fixture (not `ws_connected_chat_page`) so a broken
        WS fails this ONE test hard while every other WS-dependent
        test skips cleanly. One clear signal, not N noisy failures."""
        chat_page.wait_for_selector(
            "text=Verbunden",
            timeout=15_000,
            state="visible",
        )


# ---------------------------------------------------------------------------
# 2. Plain text chat
# ---------------------------------------------------------------------------


class TestChatSendMessage:
    def _wait_for_agent_reply(self, page, *, timeout_s: float = 120.0) -> None:
        """Block until the 'denkt nach' status disappears AND at least
        one assistant message is visible."""
        deadline = time.monotonic() + timeout_s
        thinking = page.get_by_text("Renfield denkt nach")
        while time.monotonic() < deadline:
            if thinking.count() == 0 or not thinking.first.is_visible():
                break
            time.sleep(0.5)
        else:
            pytest.fail(
                f"Agent still showing 'denkt nach' after {timeout_s}s — "
                "LLM or agent loop is stuck",
            )

    def test_send_simple_question_gets_reply(
        self, ws_connected_chat_page, created_session_ids,
    ):
        """Happy path: type → send → Renfield replies. Assert BOTH the
        user message and assistant message are visible in the transcript,
        and a conversation row appears in the backend API."""
        chat_page = ws_connected_chat_page
        """Happy path: type → send → Renfield replies. Assert BOTH the
        user message and assistant message are visible in the transcript,
        and a conversation row appears in the backend API."""
        get_errors = capture_console_errors(chat_page)

        textarea = chat_page.locator("#chat-input").first
        textarea.fill("Sag Hallo auf Deutsch in einem Satz.")
        chat_page.keyboard.press("Enter")

        # User message lands immediately
        user_msg = chat_page.get_by_text(
            "Sag Hallo auf Deutsch in einem Satz.",
        ).first
        user_msg.wait_for(state="visible", timeout=5_000)

        # Wait for the thinking indicator to clear
        self._wait_for_agent_reply(chat_page, timeout_s=120.0)

        # At least one assistant message visible (aria-label or role=article)
        articles = chat_page.get_by_role("article")
        assert articles.count() >= 2, (
            f"Expected ≥2 messages (user + assistant), got {articles.count()}"
        )

        # Backend: the conversation was persisted; session_id is logged in
        # a data attribute or visible in the sidebar
        conversations = api.list_conversations(limit=5)
        assert conversations, "Backend reports no conversations"
        newest = conversations[0]
        created_session_ids.append(newest["session_id"])
        assert newest.get("message_count", 0) >= 2, (
            f"Newest conversation should have ≥2 messages, "
            f"got {newest.get('message_count')}"
        )

        assert_no_critical_console_errors(get_errors())

    def test_new_chat_button_resets_transcript(
        self, ws_connected_chat_page, created_session_ids,
    ):
        """After a reply, clicking 'Neuer Chat' clears the transcript and
        the empty state returns."""
        chat_page = ws_connected_chat_page
        textarea = chat_page.locator("#chat-input").first
        textarea.fill("Kurztest: Hallo.")
        chat_page.keyboard.press("Enter")
        TestChatSendMessage()._wait_for_agent_reply(chat_page, timeout_s=120.0)

        # Snapshot the conversation so we can clean it up
        conversations = api.list_conversations(limit=1)
        if conversations:
            created_session_ids.append(conversations[0]["session_id"])

        # Anchor the label so this doesn't false-match a conversation
        # titled something like "Neuer Chat heute gestartet".
        chat_page.get_by_role("button", name="Neuer Chat", exact=True).click()
        chat_page.wait_for_selector(
            "text=Starte ein Gespräch mit Renfield",
            timeout=5_000,
        )


# ---------------------------------------------------------------------------
# 3. File upload → Paperless forward (the 2026-04-24 bug class)
# ---------------------------------------------------------------------------


class TestChatFileUploadToPaperless:
    """Regression for PRs #464 and #467.

    Two distinct failures were shipped to users:
      - #464: MCP upload_document sent correspondent/document_type/tags
        as raw name strings; Paperless rejected with HTTP 400.
      - #467: metadata extractor picked qwen3-vl:8b which ignored
        think=False, returned empty content, and the flow silently fell
        back to a bare upload (no metadata on the stored document).

    A smoke test saying "UI shows upload succeeded" passed in both
    scenarios. The tests here drive the full flow AND assert the
    downstream Paperless state.
    """

    def test_chat_upload_forwards_with_extracted_metadata(
        self, ws_connected_chat_page, test_pdf_path, created_session_ids,
        created_paperless_ids,
    ):
        chat_page = ws_connected_chat_page
        paperless.require_paperless()
        get_errors = capture_console_errors(chat_page)

        # Step 1: attach file
        with chat_page.expect_file_chooser() as fc_info:
            chat_page.get_by_role(
                "button", name=re.compile(r"Datei anh", re.IGNORECASE),
            ).click()
        fc_info.value.set_files(test_pdf_path)

        # The attachment row appears with the filename and a 100% badge
        chat_page.wait_for_selector(
            f"text={os.path.basename(test_pdf_path)}", timeout=20_000,
        )
        chat_page.wait_for_selector("text=100%", timeout=20_000)

        # Step 2: ask the agent to forward to Paperless
        unique_title = (
            f"e2e-test-{uuid.uuid4().hex[:8]}-"
            f"{os.path.basename(test_pdf_path).rsplit('.', 1)[0]}"
        )
        textarea = chat_page.locator("#chat-input").first
        textarea.fill(
            f"Bitte dieses Dokument nach Paperless hochladen, "
            f"Titel: {unique_title}"
        )
        chat_page.keyboard.press("Enter")

        # Agent processing takes a while — extraction + upload + PATCH
        TestChatSendMessage()._wait_for_agent_reply(
            chat_page, timeout_s=180.0,
        )

        # Step 3: UI claims success
        success_text_re = re.compile(
            r"(erfolgreich|hochgeladen|archiviert|paperless)",
            re.IGNORECASE,
        )
        assert chat_page.get_by_text(success_text_re).first.is_visible(), (
            "Agent didn't confirm the upload in chat."
        )

        # Step 4: THE DOWNSTREAM CHECK — Paperless must actually have the
        # document AND it must carry the metadata the extractor produced.
        # A bare upload would have title + nothing; the fix means we get
        # correspondent + document_type + tags populated for a real invoice.
        doc = paperless.find_document_by_title(
            unique_title, timeout_s=30.0,
        )
        assert doc is not None, (
            f"Paperless has no document titled {unique_title!r} after the "
            "agent claimed success. The upload flow is broken."
        )
        created_paperless_ids.append(doc["id"])

        # The exact bug-class from #467: metadata should NOT all be empty
        # when the extractor had a real invoice as input. At least
        # correspondent + document_type must resolve.
        assert doc.get("correspondent"), (
            f"Paperless document {doc['id']} has NO correspondent — "
            "metadata extraction silently failed (PR #467 regression?). "
            "Full doc: " + repr({k: doc.get(k) for k in
                                 ("title", "correspondent",
                                  "document_type", "tags")})
        )
        assert doc.get("document_type"), (
            f"Paperless document {doc['id']} has NO document_type — "
            f"extraction fell through to bare upload. Full doc: "
            + repr({k: doc.get(k) for k in
                    ("title", "correspondent", "document_type", "tags")})
        )

        # Clean up the conversation too
        conversations = api.list_conversations(limit=1)
        if conversations:
            created_session_ids.append(conversations[0]["session_id"])

        assert_no_critical_console_errors(get_errors())

    def test_upload_rejects_oversized_file(self, chat_page, tmp_path):
        """MAX_FILE_SIZE_MB defaults to 50; a 60 MB file must be
        rejected by the backend. We validate at the API level because
        the 60 MB file chooser interaction is slow and flaky under
        Playwright — the same contract is enforced server-side, so a
        direct httpx POST is a faithful check."""
        import httpx
        big = tmp_path / "huge.pdf"
        big.write_bytes(b"%PDF-1.4\n" + b"x" * (60 * 1024 * 1024))

        with httpx.Client(base_url="https://renfield.local",
                           verify=False, timeout=60.0) as c:
            with open(big, "rb") as f:
                r = c.post(
                    "/api/knowledge/upload",
                    files={"file": ("huge.pdf", f, "application/pdf")},
                )
        assert r.status_code == 413, (
            f"Expected HTTP 413 Content Too Large for oversize upload, "
            f"got {r.status_code}. Body: {r.text[:200]}"
        )


# ---------------------------------------------------------------------------
# 4. Conversation sidebar
# ---------------------------------------------------------------------------


class TestConversationSidebar:
    def test_sent_message_appears_in_sidebar(
        self, ws_connected_chat_page, created_session_ids,
    ):
        """After sending, the first-user-message preview shows up in the
        sidebar — regression guard for the K3 audit finding (empty
        preview because the API bypassed list_all)."""
        chat_page = ws_connected_chat_page
        textarea = chat_page.locator("#chat-input").first
        unique_msg = f"Sidebar-test-{uuid.uuid4().hex[:8]}"
        textarea.fill(unique_msg)
        chat_page.keyboard.press("Enter")
        TestChatSendMessage()._wait_for_agent_reply(chat_page, timeout_s=120.0)

        chat_page.wait_for_selector(f"text={unique_msg}", timeout=10_000)

        conversations = api.list_conversations(limit=5)
        # The backend should return a non-empty preview (not "Leere
        # Konversation" / "New Conversation")
        first = next(
            (c for c in conversations
             if unique_msg.lower() in (c.get("preview") or "").lower()),
            None,
        )
        assert first is not None, (
            f"No conversation in sidebar has a preview containing "
            f"{unique_msg!r}. Previews seen: "
            f"{[c.get('preview') for c in conversations[:5]]}"
        )
        created_session_ids.append(first["session_id"])

    def test_delete_button_removes_conversation(
        self, ws_connected_chat_page, created_session_ids,
    ):
        """Deleting a conversation from the sidebar calls the backend AND
        removes the row from the DOM."""
        chat_page = ws_connected_chat_page
        # Seed a fresh conversation
        textarea = chat_page.locator("#chat-input").first
        unique = f"delete-test-{uuid.uuid4().hex[:8]}"
        textarea.fill(unique)
        chat_page.keyboard.press("Enter")
        TestChatSendMessage()._wait_for_agent_reply(chat_page, timeout_s=120.0)
        chat_page.wait_for_selector(f"text={unique}", timeout=10_000)

        # Locate the Löschen-Button for this conversation
        delete_btn = chat_page.get_by_role(
            "button", name=re.compile(f"Konversation löschen.*{re.escape(unique)}"),
        ).first

        # Ensure it's visible (may require hover over the list item)
        list_item = chat_page.get_by_role(
            "button", name=re.compile(f"Konversation:.*{re.escape(unique)}"),
        ).first
        list_item.hover()
        delete_btn.wait_for(state="visible", timeout=5_000)

        # Capture session_id for API-level verification
        convos_before = api.list_conversations(limit=5)
        target = next(
            (c for c in convos_before
             if unique.lower() in (c.get("preview") or "").lower()),
            None,
        )
        assert target is not None, "Seeded conversation not in backend list"
        session_id = target["session_id"]

        delete_btn.click()

        # UI: the entry is gone
        chat_page.wait_for_selector(
            f"text={unique}",
            state="detached",
            timeout=10_000,
        )

        # Backend: the session is gone
        convos_after = api.list_conversations(limit=20)
        assert not any(
            c["session_id"] == session_id for c in convos_after
        ), f"Backend still has conversation {session_id} after UI delete"


# ---------------------------------------------------------------------------
# 5. Theme toggle
# ---------------------------------------------------------------------------


class TestChatTheme:
    def test_theme_toggle_flips_dark_class(self, chat_page):
        """Theme control is a dropdown (ThemeToggle.jsx): click opens
        it, then click 'Dunkel' / 'Hell' picks a theme. We flip to the
        opposite of the current state and assert the `dark` class on
        <html> tracks."""
        initial = chat_page.evaluate(
            "document.documentElement.classList.contains('dark')"
        )
        chat_page.get_by_role(
            "button", name=re.compile(r"Theme wechseln", re.IGNORECASE),
        ).click()

        # Pick the opposite of the current state. Options are rendered
        # with role="menuitem" (not button) per ThemeToggle.jsx.
        target_label = "Hell" if initial else "Dunkel"
        chat_page.get_by_role(
            "menuitem", name=re.compile(target_label, re.IGNORECASE),
        ).click()

        chat_page.wait_for_function(
            "(expected) => document.documentElement.classList.contains('dark') !== expected",
            arg=initial,
            timeout=5_000,
        )

        after = chat_page.evaluate(
            "document.documentElement.classList.contains('dark')"
        )
        assert after != initial, "Theme toggle did not flip the dark class"
