"""Shared OCR-quality heuristics.

Two consumers, two questions — kept in one module so the definition of
"garbled" can never drift between them again:

* ``is_text_garbled`` — the binary gate the *ingest* pipeline uses to decide
  whether to throw away an embedded PDF text layer and re-run full-page OCR
  (``DocumentProcessor``). Space-ratio only; deliberately narrow so a clean
  text layer is never needlessly re-rasterized.
* ``score_ocr_quality`` — the 1..5 advisory score the *Paperless audit* shows
  per document. Multi-signal, tuned for already-OCR'd content, and
  intentionally does NOT penalize ordinary table formatting.

Both read the same ``rag_ocr_space_threshold``.
"""
from __future__ import annotations

import logging

from utils.config import settings

logger = logging.getLogger(__name__)

# Below this we don't judge — too little signal either way.
_MIN_JUDGEABLE_CHARS = 50

# NB: there is intentionally NO "repeated characters" rule. The original
# ``(.)\1{5,}`` heuristic was a net negative: measured against the real audit
# corpus it produced ~19 flags and ZERO confirmed OCR defects — first on column
# padding / dotted leaders, then (even restricted to long same-alphanumeric
# runs) on legitimate content: redaction masks (``XXXXXXXX``) and zero-padded
# numbers (``00000000``). Genuine garbled OCR is caught by the space-ratio,
# special-char-ratio, and fragmentation signals below, which co-fire on real
# failures; an isolated same-char run is not a reliable predictor on its own.


def is_text_garbled(text: str) -> bool:
    """True if an embedded text layer looks mojibake'd (too few spaces).

    PDFs with a broken text layer run words together
    ('UmschauMarktplatz13Wiesbaden'); normal prose is ~15-25% spaces. Below
    ``rag_ocr_space_threshold`` (default 3%) the ingest pipeline re-runs
    full-page OCR. Short inputs (<50 chars) are never judged garbled.
    """
    if not text or len(text) < _MIN_JUDGEABLE_CHARS:
        return False
    space_ratio = text.count(" ") / len(text)
    garbled = space_ratio < settings.rag_ocr_space_threshold
    if garbled:
        logger.warning(
            "Garbled embedded text detected (space ratio=%.1f%% < threshold=%.1f%%) "
            "— re-running with force_full_page_ocr",
            space_ratio * 100,
            settings.rag_ocr_space_threshold * 100,
        )
    return garbled


# Character-level garbling thresholds (see ``_garbled_token_ratio``). A rotated /
# poor scan OCRs to real-looking spacing but wrong letters ("Bez:-ihl unq Maa
# torCa rd"), which the space / special-char / fragmentation signals all miss.
# Calibrated so ordinary German/English prose stays ~0 while such garble is high.
_GARBLE_RATIO_THRESHOLD = 0.25   # add an issue at/above this corrupt-token ratio
_GARBLE_RATIO_SEVERE = 0.40      # dominant failure → cap the score at 2
#   Measured: heavily-garbled scan (rotated MasterCard receipt) = 0.46; clean
#   German prose = 0.00 — wide margin, so a clean code/abbreviation-heavy receipt
#   stays well under 0.40. A false positive only wastes a re-OCR pass (never a
#   write-back, since the VLM/OCR result can't beat already-clean text).
_VOWELS = frozenset("aeiouyäöüàáâãéèêëíìîïóòôõúùûAEIOUYÄÖÜ")


def _garbled_token_ratio(text: str) -> float:
    """Fraction of word-like tokens that look OCR-corrupted at the character level.

    A token counts as corrupt when its alphabetic core (≥3 chars) either carries
    **internal punctuation** ("Bez:-ihl", "K(i", "Ni)") or is an **all-consonant
    run** with no vowel ("KIJ", "KKN"). Numbers, initials, and short tokens are
    ignored. Returns 0.0 when there's too little to judge (< 5 considered tokens).
    """
    import string

    punct = set(string.punctuation)
    considered = corrupt = 0
    for tok in text.split():
        core = tok.strip(string.punctuation)
        if len(core) < 3 or not any(c.isalpha() for c in core):
            continue
        considered += 1
        if any(c in punct for c in core):  # punctuation glued inside a word
            corrupt += 1
            continue
        alpha = [c for c in core if c.isalpha()]
        if alpha and not any(c in _VOWELS for c in alpha):  # implausible consonant run
            corrupt += 1
    if considered < 5:
        return 0.0
    return corrupt / considered


def score_ocr_quality(text: str) -> tuple[int, str]:
    """Rate already-OCR'd document content 1 (worst) .. 5 (clean).

    Returns ``(score, reason)`` where ``reason`` is "OK" or a "; "-joined list
    of detected issues; each issue costs one point (floor 1). Calibrated for
    Paperless content, NOT raw PDF text layers.
    """
    if not text or len(text.strip()) < 20:
        return 1, "No/minimal OCR text"

    issues: list[str] = []
    n = len(text)

    # Garbled mojibake: words run together, almost no spaces.
    if text.count(" ") / n < settings.rag_ocr_space_threshold:
        issues.append("Very few spaces (garbled)")

    # Mostly non-text (symbols/control chars) => bad recognition.
    alnum_or_space = sum(c.isalnum() or c.isspace() for c in text)
    if alnum_or_space / n < 0.6:
        issues.append("High special char ratio")

    # Many very short lines => fragmented OCR.
    lines = [ln for ln in text.split("\n") if ln.strip()]
    if lines:
        avg_line_len = sum(len(ln) for ln in lines) / len(lines)
        if avg_line_len < 10 and len(lines) > 5:
            issues.append("Fragmented text (very short lines)")

    # Character-level garbling with normal spacing (rotated / poor scan) — the case
    # the three signals above miss (they need run-together words or short lines).
    garble = _garbled_token_ratio(text)
    if garble >= _GARBLE_RATIO_THRESHOLD:
        issues.append("Implausible tokens (garbled OCR)")

    score = max(1, 5 - len(issues))
    # Severe garble is a dominant failure: the text is unreadable regardless of the
    # other signals, so make sure it registers as low-quality (≤ 2) so the audit
    # offers a re-OCR instead of scoring it OK.
    if garble >= _GARBLE_RATIO_SEVERE:
        score = min(score, 2)
    return score, "; ".join(issues) or "OK"
