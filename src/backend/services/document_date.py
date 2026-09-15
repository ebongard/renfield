"""Derive a document's OWN date (invoice/letter date) for ``documents.document_date``.

Distinct from ``created_at`` (import time). The document date is the date the
document itself carries — never a deadline, a validity end or an instalment due
date. Sources, in order:

  1. an EXPLICIT document-date fact (``rechnungsdatum`` first, then e.g.
     ``belegdatum`` / ``dokumentdatum`` / ``ausstellungsdatum``) — trusted as-is;
  2. an EVENT/PERIOD fact the document reports (``leistungsdatum``,
     ``transaktionsdatum``, ``leistungszeitraum`` …) — a proxy, accepted only up
     to ``reference_date + FUTURE_TOLERANCE_DAYS``;
  3. a date parsed from the titles (generated_title → title) — same limit;
  4. None.

Why an allowlist and not "every fact that parses as a date" (the pre-2026-09-15
behaviour): Schicht-A facts are free LLM labels, and the ranking used to try ALL
of them. An obligation stores its excerpt ("zahlbar bis 15.04.2026") as
``value``; ``faelligkeitsdatum`` matched the ``"datum"`` substring;
``gueltigkeit`` / ``naechster_abschlag`` / a next year's ``leistungszeitraum``
were taken whenever nothing better parsed. The household documents this dated up
to a year ahead of their import — and the same path silently mis-dated documents
with a PAST deadline. Unknown kinds therefore count as nothing, and obligations
(``category == 'obligation'``) never count. The kind sets were built from the
kinds the extractor actually emits on both instances (2026-09-15, names and
counts only), including the kinds that the first cut left unranked.

Because the labels are free text, a kind is compared by a CANONICAL key (see
``_canonical_kind``), applied identically to the extracted kind and to every
list entry, so spelling variants (``Rechnungs-Datum``, ``rechnung_datum``,
``datum_rechnung``, ``rechnung_vom``) meet the same entry without a blanket
"contains datum" rule — that rule is exactly what admitted
``faelligkeitsdatum``.

Why the future limit applies only to weaker sources: an explicit invoice date
ahead of the import is a legitimate pre-dated invoice (measured: no explicit
document-date fact lay more than 7 days ahead of its import on either instance),
whereas an event date or a title date ahead of the import is exactly the
due-date / next-period shape. It is a limit on SOURCE trust, not a date cap: a
rejected candidate falls through to the next source, never to a clamped value.
The limit is relative to the import, so it cannot catch a deadline on an OLD
document imported late — which is why deadline-shaped kinds are kept off the
event list altogether (``zahlungsdatum``, bare ``stichtag``).

Shared by the Schicht-A ingest hook (facts in memory),
``bin/backfill_document_dates.py`` (facts from the DB) and the Simba booking
period (``simba_ingest_review._document_period``), so all three agree.
"""
from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from datetime import date, timedelta

# ISO YYYY-MM-DD and DD.MM.YYYY (also - or / separators).
_DATE_ISO = re.compile(r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b")
_DATE_DMY = re.compile(r"\b(\d{1,2})[.\-/](\d{1,2})[.\-/](\d{4})\b")

#: How far past the reference (import) date an event/period fact or a title date
#: may lie and still be taken as the document date. 7 days covers (a) the
#: UTC-vs-local day boundary of ``created_at`` and (b) value/delivery dates a few
#: banking days after the document was produced (a weekend plus a holiday), while
#: staying below the shortest common payment term (14 days) so a due date in
#: disguise is not re-admitted. Explicit document-date facts are not bound by it.
FUTURE_TOLERANCE_DAYS = 7

# Stored fact category of obligations (models.database.DOC_FACT_CATEGORY_OBLIGATION;
# not imported so this module stays dependency-free).
_CATEGORY_OBLIGATION = "obligation"

_UMLAUTS = str.maketrans({"ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss"})
# A trailing enumeration the extractor appends to repeated kinds (belegdatum_1).
# One or two digits only: a 4-digit tail is a year qualifier
# (rechnungsdatum_2024, leistungszeitraum_2027) and names a DIFFERENT fact.
_ENUMERATION = re.compile(r"^\d{1,2}$")
_DATE_WORDS = ("datum", "date")


def _canonical_kind(kind: str | None) -> str:
    """Free LLM label → comparison key, used for kinds AND list entries alike.

    1. lower-case, umlauts/ß folded;
    2. split into tokens on anything non-alphanumeric; a trailing 1-2 digit
       enumeration token is dropped;
    3. a leading date word moves to the end (``datum_rechnung`` → rechnung·datum);
    4. a trailing ``vom`` becomes ``datum`` — German "<Dokument> vom <Datum>"
       names exactly that document's date (``rechnung_vom``, ``bescheid_vom``).
       ``am`` is NOT folded: "<Partizip> am" is as often a deadline
       (``faellig_am``) as a document date, so those are explicit list entries;
    5. tokens are joined without separators, and a linking ``s`` directly before
       the trailing ``datum`` is dropped (Rechnungs·datum = Rechnung·datum).
    """
    k = (kind or "").strip().lower().translate(_UMLAUTS)
    tokens = [t for t in re.split(r"[^a-z0-9]+", k) if t]
    if len(tokens) > 1 and _ENUMERATION.match(tokens[-1]):
        tokens = tokens[:-1]
    if len(tokens) > 1 and tokens[0] in _DATE_WORDS:
        tokens = tokens[1:] + tokens[:1]
    if len(tokens) > 1 and tokens[-1] == "vom":
        tokens[-1] = "datum"
    key = "".join(tokens)
    if key.endswith("sdatum") and len(key) > len("sdatum"):
        key = key[: -len("sdatum")] + "datum"
    return key


def _kind_set(*kinds: str) -> frozenset[str]:
    return frozenset(_canonical_kind(k) for k in kinds)


# Explicit dates OF this document. rechnungsdatum / invoice_date rank first.
_PRIMARY_DOCUMENT_DATE_KINDS = _kind_set("rechnungsdatum", "invoice_date")
_DOCUMENT_DATE_KINDS = _PRIMARY_DOCUMENT_DATE_KINDS | _kind_set(
    "datum", "date", "document_date", "dokumentdatum",
    "belegdatum", "ausstellungsdatum", "issue_date", "ausgabedatum",
    "schreibdatum", "schreibensdatum", "briefdatum", "letter_date",
    "absenderdatum", "absenddatum", "sendedatum", "uebersandtdatum", "ortdatum",
    "bescheiddatum", "verfuegungsdatum", "verordnungsdatum",
    "erstelldatum", "erstellungsdatum", "creation_date",
    "abrechnungsdatum", "druckdatum", "ausdruckdatum", "rechnungsdruck",
    "mahnungsdatum", "erinnerungsdatum", "aufforderungsdatum", "gutschriftsdatum",
    "bescheinigungsdatum", "protokolldatum", "lieferscheindatum",
    "quittungsdatum", "kontoauszugsdatum", "receipt_date", "statement_date",
    "vollstreckungsanordnungsdatum", "bussgeldbescheiddatum",
    "belegausstellung", "ausstellungszeitpunkt", "belegzeitpunkt",
    # "<Partizip> am" that name the document's own date (see _canonical_kind).
    "ausgestellt_am", "erstellt_am", "datiert_am", "gedruckt_am",
)

# Events/periods a document reports — a proxy for its date, never trusted ahead
# of the import. Period START kinds only; a period's end is not the document's
# date (and is the typical next-year instalment-plan shape). Deliberately NOT
# listed: zahlungsdatum / payment_date (as often the due or direct-debit date as
# the date paid — and the import-relative limit cannot tell an old document's
# deadline from its payment) and bare stichtag (as often a submission deadline).
_EVENT_DATE_KINDS = _kind_set(
    "leistungsdatum", "lieferdatum", "transaktionsdatum", "buchungsdatum",
    "wertstellung", "kaufdatum", "bestelldatum", "bestellzeitpunkt", "auftragsdatum",
    "zustellungsdatum", "versanddatum", "uebergabedatum", "annahmedatum",
    "eingangsdatum", "ueberweisungsdatum", "pruefdatum",
    "vertragsdatum", "vertragsabschlussdatum",
    "leistungszeitraum", "leistungszeitraum_start", "leistungszeitraum_beginn",
    "leistungsbeginn", "abrechnungszeitraum", "abrechnungszeitpunkt",
    "tse_start", "zeitstempel", "timestamp", "transaction_time",
    "geliefert_am", "gebucht_am",
    # As-of / cut-off dates: the state the document reports ("Stand",
    # "Zahlungen berücksichtigt bis").
    "stand", "beruecksichtigungsdatum", "berechnungsstichtag",
    "kontostand_datum", "saldo_datum", "as_of_date",
    "service_date", "delivery_date", "transaction_date", "booking_date",
    "purchase_date", "order_date", "service_period", "billing_period",
)


def parse_full_date(s: str | None) -> date | None:
    """Parse the first ISO (YYYY-MM-DD) then DD.MM.YYYY date out of ``s``.

    Returns a real ``date`` or None. Range-guarded (year 2000-2100) to reject
    stray numbers.
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


def _source_rank(category: str | None, kind: str | None) -> int | None:
    """0/1 = explicit document date, 2 = event/period proxy, None = never."""
    if (category or "").lower() == _CATEGORY_OBLIGATION:
        return None
    k = _canonical_kind(kind)
    if k in _PRIMARY_DOCUMENT_DATE_KINDS:
        return 0
    if k in _DOCUMENT_DATE_KINDS:
        return 1
    if k in _EVENT_DATE_KINDS:
        return 2
    return None


def derive_document_date(
    facts: Iterable[Sequence[str | None]],
    titles: list[str | None] | None = None,
    *,
    reference_date: date | None = None,
) -> date | None:
    """Best document date from ``facts`` = [(category, kind, normalized_value, value)],
    falling back to a date parsed out of ``titles`` (generated_title → title).

    ``reference_date`` is the document's import date (``created_at``); None means
    today. Within a fact ``normalized_value`` is tried before ``value``; a
    candidate a source is not trusted for falls through to the next one.
    """
    ref = reference_date or date.today()
    latest_proxy = ref + timedelta(days=FUTURE_TOLERANCE_DAYS)

    ranked = []
    for category, kind, normalized_value, value in facts:
        rank = _source_rank(category, kind)
        if rank is not None:
            ranked.append((rank, normalized_value, value))
    ranked.sort(key=lambda r: r[0])  # stable: extraction order within a rank

    for rank, normalized_value, value in ranked:
        d = parse_full_date(normalized_value) or parse_full_date(value)
        if d is None:
            continue
        if rank == 2 and d > latest_proxy:
            continue
        return d
    for title in titles or []:
        d = parse_full_date(title)
        if d is not None and d <= latest_proxy:
            return d
    return None
