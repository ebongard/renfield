"""Derive a document's OWN date (invoice/letter date) for ``documents.document_date``.

Distinct from ``created_at`` (import time). Mirrors the fact-ranking in
``simba_ingest_review._document_period`` but returns a FULL ``date`` (day too, for
sorting) instead of just (month, year):

  rechnungsdatum fact → other date fact (datum/date/leistung) → the generated
  title's date → None.

Shared by the Schicht-A ingest hook (facts in memory) and
``bin/backfill_document_dates.py`` (facts from the DB), so both agree.
"""
from __future__ import annotations

import re
from datetime import date

# ISO YYYY-MM-DD and DD.MM.YYYY (also - or / separators) — mirrors
# simba_ingest_review._DATE_ISO / _DATE_DMY.
_DATE_ISO = re.compile(r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b")
_DATE_DMY = re.compile(r"\b(\d{1,2})[.\-/](\d{1,2})[.\-/](\d{4})\b")


def parse_full_date(s: str | None) -> date | None:
    """Parse the first ISO (YYYY-MM-DD) then DD.MM.YYYY date out of ``s``.

    Returns a real ``date`` or None. Range-guarded (year 2000-2100) to reject
    stray numbers, matching the Simba period parser.
    """
    if not s:
        return None
    m = _DATE_ISO.search(s)
    if m:
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
    else:
        m = _DATE_DMY.search(s)
        if not m:
            return None
        d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
    if not (2000 <= y <= 2100 and 1 <= mo <= 12 and 1 <= d <= 31):
        return None
    try:
        return date(y, mo, d)
    except ValueError:  # e.g. 31.02.2025
        return None


def _rank(kind: str | None) -> int:
    """Prefer the invoice date, then generic date facts, then anything else."""
    k = (kind or "").lower()
    if "rechnungsdatum" in k:
        return 0
    if "datum" in k or "date" in k or "leistung" in k:
        return 1
    return 2


def derive_document_date(
    facts: list[tuple[str | None, str | None, str | None]],
    titles: list[str | None] | None = None,
) -> date | None:
    """Best document date from ``facts`` = [(kind, normalized_value, value)],
    falling back to a date parsed out of any ``titles`` (generated_title → title).

    Returns a ``date`` or None. Facts are tried in rank order; within a fact,
    ``normalized_value`` before ``value``.
    """
    for kind, normalized_value, value in sorted(facts, key=lambda f: _rank(f[0])):
        d = parse_full_date(normalized_value) or parse_full_date(value)
        if d is not None:
            return d
    for title in titles or []:
        d = parse_full_date(title)
        if d is not None:
            return d
    return None
