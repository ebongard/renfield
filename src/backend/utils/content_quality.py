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

# A MEANINGFUL token carries real content: a word OR a structured/numeric datum.
# The garbage we filter is glyph noise — single-char runs, punctuation, control
# glyphs from broken OCR (``- r . : ■ { - n ; ;``). A token is meaningful when it
# holds ≥3 alphanumeric characters (letters incl. ä/ö/ü/ß, or digits): this counts
# real words (``Rechnung``), amounts (``1.250,00``), dates (``01.02.2026``), IBANs
# (``DE12345678``), and codes — legitimate financial/tabular content that a business
# archive is full of — while glyph noise (0-1 alphanumerics per token) is not counted.
# (Prior versions counted ONLY letters-only ≥3-char tokens, which FALSELY flagged
# numeric-heavy financial documents as garbage and dropped them at ingest.)
_MIN_MEANINGFUL_ALNUM = 3


def _is_meaningful(token: str) -> bool:
    """A token with real content (word / number / amount / date / code), not glyph noise."""
    return sum(1 for c in token if c.isalnum()) >= _MIN_MEANINGFUL_ALNUM

# Length floor: shorter inputs are dominated by noise statistics. A
# 12-char chunk of valid text could trivially trip a ratio check that
# works fine at 200 chars. We treat short inputs as ALWAYS high-quality
# — the wins come on the medium-to-long garbage chunks.
_MIN_LEN_FOR_QUALITY_CHECK = 40

# Meaningful-token ratio floor: below this, the text is dominated by
# single-character runs / glyphs / punctuation. Calibrated empirically
# against the production corpus: real text (German + technical English,
# and numeric/financial content) clears 0.4 by a wide margin; OCR garbage
# from Paperless lands 0.0-0.2.
#
# THRESHOLD-VERSIONING NOTE (v2.10.4): this constant is shared between
# two call sites:
#
#   1. Retrieval-time filtering — `services/rag_retrieval.py` drops
#      garbage chunks from query results. Best-effort; lowering the
#      threshold here later surfaces previously-filtered chunks
#      retroactively at zero cost.
#   2. Ingestion-time gating — `services/document_processor.py` AND
#      `services/rag_service.py` (defense in depth). DESTRUCTIVE: a
#      chunk dropped here never enters the table. Raising the threshold
#      later does NOT retroactively scan or remove chunks already in
#      the corpus — the corpus reflects whatever threshold was in
#      effect at ingest time of each document.
#
# Recovery from a bad calibration: re-ingest affected docs via the
# (deferred) bin/purge_low_quality_chunks.py once it lands. Until then,
# change cautiously and note the date in CHANGELOG.
_MIN_WORDLIKE_RATIO = 0.30


def is_low_quality_text(text: str | None) -> bool:
    """True if the text reads as OCR garbage / glyph noise.

    Returns False for None, empty, short (<40 char), or text that clears
    the meaningful-token ratio threshold (words OR numeric/structured data).
    Only flags the medium-to-long chunks dominated by single-char runs and
    punctuation glyphs that failed Paperless OCR produces — NOT legitimate
    numeric/financial content.
    """
    if not text:
        return False
    stripped = text.strip()
    if len(stripped) < _MIN_LEN_FOR_QUALITY_CHECK:
        return False

    tokens = stripped.split()
    if not tokens:
        return False

    meaningful = sum(1 for t in tokens if _is_meaningful(t))
    ratio = meaningful / len(tokens)
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
