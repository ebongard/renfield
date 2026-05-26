"""Unit tests for the OCR-garbage detection heuristic.

Regression for the v2.10.2 brain-quality fix: the production corpus
contained Paperless OCR chunks like
``- r . : ■ { - n ; ; : t » - , : :' r ' ● r : ; '`` that were
out-ranking real memories in the polymorphic atom store's RRF merge.
"""
from __future__ import annotations

import pytest

from utils.content_quality import filter_low_quality, is_low_quality_text


# Real garbage chunks pulled from the production document_chunks table
# (Paperless failed-OCR output) on 2026-05-26.
# Note: the heuristic intentionally targets the truly-broken cases
# (dominated by single-char runs and glyphs). Partially-corrupted text
# like "qedQcht _gemocht klar mon, bei. Luohnnonn bouelemente ■ Im"
# is allowed through — we'd rather a fuzzy chunk surface than a real
# answer get dropped.
GARBAGE_SAMPLES = [
    "- r . : ■ { - n ; ; : t » - , : :' r ' ● r : ; '\nydl .'-Ti'",
    ".\n;\n>\n■\nj J i\n-\n'\n●\n11-\n. <\nV,\n.\n▶\n■\n..C'j\n●\n●\n■■\n1\n.<\n,'r\n-",
]

# Realistic medium-to-long text that MUST NOT trip the heuristic.
CLEAN_SAMPLES = [
    "Jutta mag Maracujas und Ananas. Sie isst diese Früchte fast jeden Morgen zum Frühstück.",
    "Invoice number 1SOGUR2D0010, Date of issue April 17, 2026, Amount due 142.50 EUR.",
    "The agent loop dispatches MCP tools sequentially until either the budget exhausts or the model emits a final_answer step.",
    "Der Benutzer und Jutta sind seit 26 Jahren verheiratet und feiern ihren Hochzeitstag am 27. Mai.",
]


@pytest.mark.parametrize("text", GARBAGE_SAMPLES)
def test_garbage_flagged(text: str):
    assert is_low_quality_text(text) is True, f"Should flag garbage: {text!r}"


@pytest.mark.parametrize("text", CLEAN_SAMPLES)
def test_clean_text_passes(text: str):
    assert is_low_quality_text(text) is False, f"Should NOT flag clean text: {text!r}"


def test_none_and_empty_pass():
    assert is_low_quality_text(None) is False
    assert is_low_quality_text("") is False
    assert is_low_quality_text("   \n\t  ") is False


def test_short_text_always_passes():
    """Length floor: <40 char inputs are never flagged. A short fragment
    might be all punctuation but isn't OCR garbage at the scale that
    matters (single-token names, dates, etc.)."""
    assert is_low_quality_text("Jutta") is False
    assert is_low_quality_text("?!?!?!") is False
    assert is_low_quality_text("$$$") is False


def test_filter_low_quality_drops_garbage_dicts():
    items = [
        {"id": 1, "content": GARBAGE_SAMPLES[0]},
        {"id": 2, "content": CLEAN_SAMPLES[0]},
        {"id": 3, "content": GARBAGE_SAMPLES[1]},
        {"id": 4, "content": CLEAN_SAMPLES[1]},
    ]
    kept = filter_low_quality(items, text_key="content")
    assert [i["id"] for i in kept] == [2, 4]


def test_filter_low_quality_keeps_missing_text_items():
    """An item with no text under ``text_key`` is NOT garbage by the
    heuristic's lights — None falls through ``is_low_quality_text``
    as False (we can't judge what we can't read). This is intentional:
    a missing snippet upstream is a different concern than OCR garbage,
    and silently dropping rows here would mask the real upstream bug."""
    items = [{"id": 1}, {"id": 2, "content": CLEAN_SAMPLES[0]}]
    kept = filter_low_quality(items, text_key="content")
    assert [i["id"] for i in kept] == [1, 2]


def test_filter_low_quality_plain_strings():
    items = [GARBAGE_SAMPLES[0], CLEAN_SAMPLES[0], GARBAGE_SAMPLES[1]]
    kept = filter_low_quality(items)
    assert kept == [CLEAN_SAMPLES[0]]
