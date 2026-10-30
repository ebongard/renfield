"""Lexical tokenisation survives German umlauts.

Why this file exists, 2026-10-29: while converting my own comments to ASCII I
transliterated the character class in `_significant_tokens` along with them.
`[A-Za-zÄÖÜäöüß0-9]` became a class that no
longer matches the very letters it was there for -- a blanket search-and-replace
over a file without looking at what it hits.

I found it by looking, not by a test: NO test covered umlauts in this function.
A German query for "Mietvertrag fuer die Wohnung in Koeln" would have quietly
got worse -- no exception, just fewer hits.

This file closes that gap. It tests the function's behaviour, not the spelling
of the character class: rewrite it however you like, as long as umlauts still
get through.
"""
from __future__ import annotations

import pytest

from services.lexical_retrieval import _significant_tokens

pytestmark = pytest.mark.unit


class TestUmlautsSurviveTokenisation:
    @pytest.mark.parametrize("word", [
        "Koeln", "Köln", "Grundstück", "Straße", "Übergabe", "Ärztin",
        "Öffnungszeiten",
    ])
    def test_a_word_with_an_umlaut_is_one_token(self, word):
        """Not merely "some token": the word has to come through WHOLE.

        Drop the umlaut from the class and `Grundstück` splits into
        `Grundst` and `ck` -- both clear the minimum length, so a test that
        only checks "not empty" would stay green.

        The function does NOT lower-case its output; it only lowers for the
        stop-word comparison and returns the token unchanged. My first version
        asserted `[word.lower()]` and failed for EVERY word, plain ASCII
        included -- the expectation was wrong, not the code.
        """
        assert _significant_tokens(word) == [word]

    def test_a_real_german_query_keeps_every_word(self):
        hits = _significant_tokens("Mietvertrag für die Wohnung in Köln")
        assert "Mietvertrag" in hits
        assert "Köln" in hits, "the umlaut must not split the word"
        assert "Wohnung" in hits

    def test_the_eszett_is_not_a_separator(self):
        assert _significant_tokens("Hauptstraße") == ["Hauptstraße"]

    def test_plain_ASCII_still_works(self):
        """Negative control: the check must not only measure umlauts."""
        assert _significant_tokens("rental contract") == ["rental", "contract"]

    def test_short_tokens_are_STILL_dropped(self):
        """Negative control against an over-wide character class: the minimum
        length stays in force."""
        assert "in" not in _significant_tokens("Vertrag in Köln")
