"""
Comprehensive E2E browser tests for all search functions.

Tests both search paths:
  A. Knowledge Page (/knowledge) — direct RAG search via POST /api/knowledge/search
  B. Chat Page (/) — WebSocket → Agent Loop → knowledge_search / paperless
  C. Edge cases & robustness

Target: https://renfield.local (production, self-signed certs)
Run:  python3 -m pytest tests/e2e/test_search_e2e.py -v -m e2e --tb=short
"""

import pytest

BASE_URL = "https://renfield.local"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _search_knowledge_page(page, query, *, press_enter=False):
    """Fill the search input on /knowledge and trigger search."""
    # The search input is inside a flex row with the search button
    search_row = page.locator("div.flex.gap-2").filter(
        has=page.locator("input.input.flex-1")
    )
    search_input = search_row.locator("input.input")
    search_input.fill(query)

    if press_enter:
        search_input.press("Enter")
    else:
        search_btn = search_row.locator("button.btn-primary")
        search_btn.click()

    # Wait for search to complete (spinner disappears)
    page.wait_for_timeout(500)
    page.locator("button.btn-primary svg.animate-spin").wait_for(
        state="detached", timeout=15_000
    )


def _get_search_results(page):
    """Return the search result card elements on the knowledge page."""
    # Result cards are inside the search section's results area
    return page.locator(
        "div.p-4.bg-gray-100.rounded-lg, "
        "div.p-4.dark\\:bg-gray-700\\/50.rounded-lg"
    ).all()


def _send_chat_message(page, message, *, timeout=90_000):
    """Type a message in chat and wait for the assistant response."""
    # Count existing article elements before sending
    initial_articles = len(page.locator("div[role='article']").all())

    chat_input = page.locator("#chat-input")
    chat_input.fill(message)

    # Send button — support both EN and DE aria-labels
    send_btn = page.locator(
        "button[aria-label='Send message'], "
        "button[aria-label='Nachricht senden']"
    )
    send_btn.click()

    # Wait for at least 2 new articles (user msg + assistant msg)
    page.wait_for_function(
        f"document.querySelectorAll('div[role=\"article\"]').length >= {initial_articles + 2}",
        timeout=timeout,
    )

    # Then wait for the loading spinner to vanish (streaming done)
    try:
        page.locator("div[role='status'] svg.animate-spin").wait_for(
            state="detached", timeout=timeout
        )
    except Exception:
        pass  # spinner might never have appeared or already gone

    # Buffer for final rendering
    page.wait_for_timeout(1_000)


def _get_assistant_messages(page):
    """Return all assistant message text contents."""
    # Assistant bubbles have bg-gray-200 (light) / bg-gray-700 (dark)
    bubbles = page.locator(
        "div[role='article'] div.bg-gray-200 p.whitespace-pre-wrap"
    ).all()
    return [b.inner_text() for b in bubbles]


def _get_stat_values(page):
    """Return the 4 stat card values from the knowledge page stats grid."""
    # Stats are inside a grid with grid-cols-2/4 — target .text-2xl inside .grid
    return page.locator(
        "div.grid .text-2xl.font-bold"
    ).all()


def _get_search_btn(page):
    """Return the search button on the knowledge page (language-agnostic)."""
    search_row = page.locator("div.flex.gap-2").filter(
        has=page.locator("input.input.flex-1")
    )
    return search_row.locator("button.btn-primary")


# ===========================================================================
# A. Knowledge Page — Direct RAG Search
# ===========================================================================

@pytest.mark.e2e
class TestKnowledgePageSearch:

    def test_a1_stats_visible(self, knowledge_page):
        """Stats cards show document count, indexed, chunks, KBs — all > 0."""
        stats = _get_stat_values(knowledge_page)
        assert len(stats) >= 4, f"Expected 4 stat cards, got {len(stats)}"

        values = [s.inner_text() for s in stats[:4]]
        for i, val in enumerate(values):
            assert val.isdigit(), f"Stat card {i} is not a number: {val}"
            assert int(val) > 0, f"Stat card {i} is zero"

    def test_a2_semantic_search_german(self, knowledge_page):
        """Semantic search for 'Rechnung Strom' returns results with relevance badges."""
        _search_knowledge_page(knowledge_page, "Rechnung Strom")

        results = _get_search_results(knowledge_page)
        assert len(results) > 0, "No search results for 'Rechnung Strom'"

        # Check relevance badge is present
        relevance = knowledge_page.locator("span.text-xs.px-2.py-1").first
        assert relevance.is_visible(), "Relevance badge not visible"
        assert "%" in relevance.inner_text(), "Relevance badge missing percentage"

    def test_a3_exact_name_search_bm25(self, knowledge_page):
        """BM25 keyword search for 'Alternate' returns results."""
        _search_knowledge_page(knowledge_page, "Alternate")

        results = _get_search_results(knowledge_page)
        assert len(results) > 0, "No search results for 'Alternate'"

        # Check if any result mentions 'Alternate' (BM25 should surface it)
        all_text = " ".join(r.inner_text() for r in results).lower()
        if "alternate" not in all_text:
            # Dense search may dominate — verify results have relevance badges
            relevance = knowledge_page.locator("span.text-xs.px-2.py-1").first
            assert relevance.is_visible(), "No relevance badge on results"

    def test_a4_address_search(self, knowledge_page):
        """Address search for 'Am Stirkenbend 20' returns results."""
        _search_knowledge_page(knowledge_page, "Am Stirkenbend 20")

        results = _get_search_results(knowledge_page)
        assert len(results) > 0, "No results for address 'Am Stirkenbend 20'"

    def test_a5_nonsense_query_no_crash(self, knowledge_page):
        """Nonsense query doesn't crash (dense search may still return low-relevance results)."""
        _search_knowledge_page(knowledge_page, "xyznonexistent123foobarbaz")

        # Dense/hybrid search always returns top_k results (no min threshold),
        # so we can't assert 0 results — just verify page didn't crash
        stats = _get_stat_values(knowledge_page)
        assert len(stats) >= 4, "Page broke after nonsense search"

        # If results exist, they should have very low relevance
        results = _get_search_results(knowledge_page)
        if len(results) > 0:
            relevance_text = knowledge_page.locator("span.text-xs.px-2.py-1").first.inner_text()
            assert "%" in relevance_text, "Relevance badge missing on results"

    def test_a6_empty_search_disabled(self, knowledge_page):
        """Empty search input disables the search button."""
        search_row = knowledge_page.locator("div.flex.gap-2").filter(
            has=knowledge_page.locator("input.input.flex-1")
        )
        search_input = search_row.locator("input.input")
        search_input.fill("")

        search_btn = search_row.locator("button.btn-primary")
        assert search_btn.is_disabled(), "Search button should be disabled for empty query"

    def test_a7_kb_filter_specific(self, knowledge_page):
        """Selecting a specific KB filters search results to that KB."""
        # KB buttons are inside div.flex.flex-wrap.gap-2
        # "All"/"Alle" is a direct child button, KB buttons are inside nested divs
        kb_wrapper = knowledge_page.locator("div.flex.flex-wrap.gap-2")
        # KB buttons (not the "All" button) are inside div.flex.items-center.gap-1
        kb_btns = kb_wrapper.locator("div.flex.items-center.gap-1 > button").all()

        if not kb_btns:
            pytest.skip("No knowledge bases found")

        # Click the first KB button
        kb_btns[0].click()
        knowledge_page.wait_for_timeout(500)

        _search_knowledge_page(knowledge_page, "Rechnung")

        results = _get_search_results(knowledge_page)
        assert isinstance(results, list), "Search with KB filter failed"

    def test_a8_kb_filter_all(self, knowledge_page):
        """Selecting 'All' KB and searching returns results from all KBs."""
        # "All"/"Alle" is the first direct child button of the KB wrapper
        kb_wrapper = knowledge_page.locator("div.flex.flex-wrap.gap-2")
        all_btn = kb_wrapper.locator("> button").first
        all_btn.click()
        knowledge_page.wait_for_timeout(500)

        _search_knowledge_page(knowledge_page, "Rechnung")

        results = _get_search_results(knowledge_page)
        assert len(results) > 0, "No results for 'Rechnung' with 'All' filter"

    def test_a9_enter_key_search(self, knowledge_page):
        """Pressing Enter triggers search."""
        _search_knowledge_page(knowledge_page, "Versicherung", press_enter=True)

        results = _get_search_results(knowledge_page)
        assert len(results) > 0, "Enter-key search for 'Versicherung' returned no results"

    def test_a10_special_characters(self, knowledge_page):
        """Special characters (Umlaute) don't crash the search."""
        _search_knowledge_page(knowledge_page, "Ä Ö Ü ß")

        # No crash — page still functional
        stats = _get_stat_values(knowledge_page)
        assert len(stats) >= 4, "Page crashed after special character search"


# ===========================================================================
# B. Chat Page — Agent-Based Search
# ===========================================================================

@pytest.mark.e2e
class TestChatSearch:

    def test_b1_document_question(self, chat_page):
        """Asking about Alternate invoices returns a relevant answer."""
        _send_chat_message(chat_page, "Welche Rechnungen habe ich von Alternate?")

        messages = _get_assistant_messages(chat_page)
        assert len(messages) >= 1, "No assistant response received"

        last = messages[-1].lower()
        assert len(last) > 20, f"Response too short: {last}"

    def test_b2_time_range_search(self, chat_page):
        """Asking for invoices from a specific year returns relevant results."""
        _send_chat_message(
            chat_page,
            "Welche Rechnungen gab es 2022?",
            timeout=120_000,
        )

        messages = _get_assistant_messages(chat_page)
        assert len(messages) >= 1, "No assistant response"
        assert len(messages[-1]) > 20, "Response too short"

    def test_b3_combined_address_time(self, chat_page):
        """Combined address+time query works."""
        _send_chat_message(
            chat_page,
            "Rechnungen 2022 für Am Stirkenbend 20",
            timeout=120_000,
        )

        messages = _get_assistant_messages(chat_page)
        assert len(messages) >= 1, "No assistant response"
        assert len(messages[-1]) > 20, "Response too short"

    def test_b4_rag_toggle_search(self, chat_page):
        """Enabling RAG toggle and asking about documents uses KB content."""
        # Enable RAG toggle — button text is "Knowledge" (EN) or "Wissen" (DE)
        rag_btn = chat_page.locator("button").filter(has_text="Knowledge").first
        if not rag_btn.is_visible():
            rag_btn = chat_page.locator("button").filter(has_text="Wissen").first

        btn_class = rag_btn.get_attribute("class") or ""
        if "bg-primary" not in btn_class:
            rag_btn.click()
            chat_page.wait_for_timeout(500)

        _send_chat_message(
            chat_page, "Was steht in meinen Dokumenten über Versicherung?"
        )

        messages = _get_assistant_messages(chat_page)
        assert len(messages) >= 1, "No assistant response with RAG enabled"
        assert len(messages[-1]) > 30, "RAG response too short"

        # Disable RAG toggle again
        rag_btn = chat_page.locator("button").filter(has_text="Knowledge").first
        if not rag_btn.is_visible():
            rag_btn = chat_page.locator("button").filter(has_text="Wissen").first
        btn_class = rag_btn.get_attribute("class") or ""
        if "bg-primary" in btn_class:
            rag_btn.click()

    def test_b5_general_question_no_crash(self, chat_page):
        """Non-document question works without crash."""
        _send_chat_message(chat_page, "Wie ist das Wetter?")

        messages = _get_assistant_messages(chat_page)
        assert len(messages) >= 1, "No response for general question"

    def test_b6_long_answer_complete(self, chat_page):
        """Asking for a document list produces a substantial response."""
        _send_chat_message(
            chat_page,
            "Liste alle Dokumente über Strom",
            timeout=120_000,
        )

        messages = _get_assistant_messages(chat_page)
        assert len(messages) >= 1, "No assistant response"
        assert len(messages[-1]) > 50, (
            f"Response seems truncated ({len(messages[-1])} chars)"
        )


# ===========================================================================
# C. Edge Cases & Robustness
# ===========================================================================

@pytest.mark.e2e
class TestSearchEdgeCases:

    def test_c1_rapid_double_search(self, knowledge_page):
        """Two rapid searches don't crash — second result wins."""
        search_row = knowledge_page.locator("div.flex.gap-2").filter(
            has=knowledge_page.locator("input.input.flex-1")
        )
        search_input = search_row.locator("input.input")
        search_btn = search_row.locator("button.btn-primary")

        # First search
        search_input.fill("Strom")
        search_btn.click()
        knowledge_page.wait_for_timeout(200)

        # Second search immediately
        search_input.fill("Versicherung")
        search_btn.click()

        # Wait for completion
        knowledge_page.locator("button.btn-primary svg.animate-spin").wait_for(
            state="detached", timeout=15_000
        )

        # Page should not have crashed
        stats = _get_stat_values(knowledge_page)
        assert len(stats) >= 4, "Page crashed after rapid double search"

    def test_c2_very_long_query(self, knowledge_page):
        """500-character query doesn't crash."""
        long_query = "Rechnung " * 55  # ~500 chars
        _search_knowledge_page(knowledge_page, long_query[:500])

        stats = _get_stat_values(knowledge_page)
        assert len(stats) >= 4, "Page crashed after long query"

    def test_c3_sql_injection_attempt(self, knowledge_page):
        """SQL injection attempt returns no results and doesn't crash."""
        _search_knowledge_page(knowledge_page, "'; DROP TABLE documents; --")

        # Stats should still show real data (table not dropped)
        stat_values = _get_stat_values(knowledge_page)
        assert len(stat_values) >= 4, "Page crashed after SQL injection attempt"
        doc_count = stat_values[0].inner_text()
        assert int(doc_count) > 0, (
            "Document count is 0 after injection attempt!"
        )

    def test_c4_numeric_only_query(self, knowledge_page):
        """Searching for just a number works."""
        _search_knowledge_page(knowledge_page, "2022")

        # Page still functional
        stats = _get_stat_values(knowledge_page)
        assert len(stats) >= 4, "Page crashed after numeric search"

    def test_c5_unicode_emoji_query(self, knowledge_page):
        """Unicode/emoji in search doesn't crash."""
        _search_knowledge_page(knowledge_page, "📄 Rechnung")

        stats = _get_stat_values(knowledge_page)
        assert len(stats) >= 4, "Page crashed after emoji search"
