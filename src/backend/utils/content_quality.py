"""Content-quality heuristics for retrieval-time filtering.

Failed Paperless OCR runs produce chunks like::

    - r . : ■ { - n ; ; : t » - , : : ' r ' ● r : ; '
    ydl .'-Ti'

These chunks embed into vector space with low information content and
match *every* query at the same low cosine score. In the polymorphic
atom store's RRF (Reciprocal Rank Fusion) merge they snag rank=1 in the
RAG list and outrank actual answers — the user sees garbage at the top
of the brain page even when a real memory contains the answer.

This module provides a single heuristic, ``is_low_quality_text``,
that retrieval modules call to suppress such chunks before they enter
the merge.

The heuristic intentionally tolerates short well-formed text: an empty
string or a 5-char input is "low information" by length but is not OCR
garbage. The garbage case is: medium-to-long text dominated by
punctuation, glyphs, and 1-2 char runs with very few real words.
"""
from __future__ import annotations

import re

# A word-like token: ≥3 letters from a German-Latin alphabet. The bar is
# deliberately language-aware (ä/ö/ü/ß) so German OCR with diacritics
# isn't mis-classified as garbage. Anything outside this charset (digits,
# punctuation, control glyphs from broken OCR) is NOT counted.
_WORD_LIKE_RE = re.compile(r"^[A-Za-zÄÖÜäöüß]{3,}$")

# Length floor: shorter inputs are dominated by noise statistics. A
# 12-char chunk of valid text could trivially trip a ratio check that
# works fine at 200 chars. We treat short inputs as ALWAYS high-quality
# — the wins come on the medium-to-long garbage chunks.
_MIN_LEN_FOR_QUALITY_CHECK = 40

# Word-like-token ratio floor: below this, the text is dominated by
# single-character runs / glyphs / punctuation. Calibrated empirically
# against the production corpus: real text (German + technical English)
# clears 0.4 by a wide margin; OCR garbage from Paperless lands 0.0-0.2.
_MIN_WORDLIKE_RATIO = 0.30


def is_low_quality_text(text: str | None) -> bool:
    """True if the text reads as OCR garbage / glyph noise.

    Returns False for None, empty, short (<40 char), or text that clears
    the word-like-token ratio threshold. Only flags the medium-to-long
    chunks dominated by single-char runs and punctuation glyphs that
    failed Paperless OCR produces.
    """
    if not text:
        return False
    stripped = text.strip()
    if len(stripped) < _MIN_LEN_FOR_QUALITY_CHECK:
        return False

    tokens = stripped.split()
    if not tokens:
        return False

    wordlike = sum(1 for t in tokens if _WORD_LIKE_RE.fullmatch(t))
    ratio = wordlike / len(tokens)
    return ratio < _MIN_WORDLIKE_RATIO


def filter_low_quality(items: list, *, text_key: str | None = None) -> list:
    """Drop items whose text fails the quality check.

    Pass dicts with ``text_key='content'`` (or whichever attribute
    carries the body), or pass plain strings. The original list is not
    mutated.
    """
    out = []
    for item in items:
        if text_key is None:
            text = item if isinstance(item, str) else None
        elif isinstance(item, dict):
            text = item.get(text_key)
        else:
            text = getattr(item, text_key, None)
        if not is_low_quality_text(text):
            out.append(item)
    return out
