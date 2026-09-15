"""Unit tests for services.document_date (document's own date derivation).

The document date is the date the document itself carries (invoice/letter/
issue date) — never a deadline, a validity end or an instalment due date. The
2026-09-15 Paperless created-date backfill found household documents dated up
to a year AHEAD of their import: the derivation took the first parsable date
out of ANY fact (an obligation's excerpt, ``gueltigkeit``, a future
``leistungszeitraum``). All fixtures below are synthetic.
"""
from datetime import date, timedelta

import pytest

from services.document_date import (
    FUTURE_TOLERANCE_DAYS,
    derive_document_date,
    parse_full_date,
)

pytestmark = [pytest.mark.unit]

IMPORT = date(2026, 3, 1)  # the document's import (created_at) date


def _fact(kind, value=None, normalized=None, category="universal"):
    return (category, kind, normalized, value)


# --------------------------------------------------------------------------
# parse_full_date
# --------------------------------------------------------------------------


def test_parse_iso():
    assert parse_full_date("2025-12-16") == date(2025, 12, 16)
    assert parse_full_date("Rechnung vom 2025-12-16, fällig") == date(2025, 12, 16)


def test_parse_dmy():
    assert parse_full_date("16.12.2025") == date(2025, 12, 16)
    assert parse_full_date("16/12/2025") == date(2025, 12, 16)
    assert parse_full_date("16-12-2025") == date(2025, 12, 16)


def test_parse_none_and_invalid():
    assert parse_full_date(None) is None
    assert parse_full_date("") is None
    assert parse_full_date("kein Datum") is None
    assert parse_full_date("31.02.2025") is None  # invalid calendar date
    assert parse_full_date("16.12.1850") is None  # out of range year


# --------------------------------------------------------------------------
# Deadlines are never the document date
# --------------------------------------------------------------------------


def test_obligation_fact_is_never_the_document_date():
    """An obligation stores its excerpt as ``value`` — a due date inside it
    must not become the document date, not even when it lies in the past."""
    facts = [_fact("zahlung", "Bitte zahlen Sie bis 15.04.2026", category="obligation")]
    assert derive_document_date(facts, reference_date=IMPORT) is None
    past = [_fact("zahlung", "zahlbar bis 20.02.2026", category="obligation")]
    assert derive_document_date(past, reference_date=IMPORT) is None


def test_deadline_kinds_in_universal_facts_are_never_chosen():
    """Deadline / validity kinds stored as universal facts (``faelligkeit``,
    ``faelligkeitsdatum`` — which the old ``"datum"`` substring rank admitted —
    ``naechster_abschlag``, ``gueltigkeit``) do not count as document dates."""
    for kind in ("faelligkeit", "faelligkeitsdatum", "fälligkeit_1", "naechster_abschlag",
                 "gueltigkeit", "gueltig_bis", "ablauf", "termin", "zahlungsziel"):
        facts = [_fact(kind, "20.02.2026")]
        assert derive_document_date(facts, reference_date=IMPORT) is None, kind


def test_deadline_does_not_shadow_the_real_document_date():
    facts = [
        _fact("zahlung", "zahlbar bis 15.04.2026", category="obligation"),
        _fact("faelligkeitsdatum", "15.04.2026"),
        _fact("belegdatum", "20.02.2026"),
    ]
    assert derive_document_date(facts, reference_date=IMPORT) == date(2026, 2, 20)


# --------------------------------------------------------------------------
# Explicit document-date kinds
# --------------------------------------------------------------------------


def test_derive_prefers_rechnungsdatum():
    facts = [
        _fact("leistungsdatum", "01.11.2025", "2025-11-01"),
        _fact("dokumentdatum", "10.12.2025"),
        _fact("rechnungsdatum", "16.12.2025", "2025-12-16"),
    ]
    assert derive_document_date(facts, reference_date=IMPORT) == date(2025, 12, 16)


def test_explicit_document_date_kinds_are_recognised():
    for kind in ("rechnungsdatum", "Rechnungsdatum", "invoice_date", "dokumentdatum",
                 "belegdatum", "ausstellungsdatum", "schreibdatum", "bescheiddatum",
                 "verfuegungsdatum", "datum", "briefdatum", "belegdatum_1"):
        facts = [_fact(kind, "05.02.2026")]
        assert derive_document_date(facts, reference_date=IMPORT) == date(2026, 2, 5), kind


def test_explicit_document_date_is_trusted_even_when_ahead_of_import():
    """A pre-dated invoice carries its own date — an explicit document-date fact
    is accepted beyond the tolerance; only weaker sources are held to it."""
    ahead = IMPORT + timedelta(days=FUTURE_TOLERANCE_DAYS + 30)
    facts = [_fact("rechnungsdatum", ahead.strftime("%d.%m.%Y"))]
    assert derive_document_date(facts, reference_date=IMPORT) == ahead


def test_legitimate_past_date_unchanged():
    facts = [_fact("issuer", "ACME GmbH"), _fact("schreibdatum", "18.02.2026")]
    assert derive_document_date(facts, ["Schreiben ACME 18.02.2026"], reference_date=IMPORT) == date(2026, 2, 18)


def test_derive_normalized_value_before_value():
    facts = [_fact("rechnungsdatum", "garbled 99", "2025-12-16")]
    assert derive_document_date(facts, reference_date=IMPORT) == date(2025, 12, 16)


# --------------------------------------------------------------------------
# Event/period dates: accepted only up to import + tolerance
# --------------------------------------------------------------------------


def test_event_date_used_when_no_explicit_document_date():
    facts = [_fact("lieferdatum", None), _fact("leistungsdatum", "05.02.2026")]
    assert derive_document_date(facts, reference_date=IMPORT) == date(2026, 2, 5)


def test_as_of_dates_are_guarded_proxies():
    """An as-of / cut-off date ("Stand", "Zahlungen berücksichtigt bis") stands in
    for the document date; an ambiguous bare ``stichtag`` does not."""
    for kind in ("stand", "beruecksichtigungsdatum", "kontostand_datum"):
        assert derive_document_date([_fact(kind, "25.02.2026")], reference_date=IMPORT) == date(2026, 2, 25), kind
        assert derive_document_date([_fact(kind, "25.06.2026")], reference_date=IMPORT) is None, kind
    assert derive_document_date([_fact("stichtag", "25.02.2026")], reference_date=IMPORT) is None


def test_future_period_is_not_the_document_date():
    """Structural reconstruction of the household case: an instalment plan whose
    only date fact is a ``leistungszeitraum`` for the coming year."""
    facts = [_fact("leistungszeitraum", "01.01.2027 - 31.12.2027")]
    assert derive_document_date(facts, reference_date=IMPORT) is None


def test_future_transaction_date_falls_through_to_next_source():
    """Household case (one future ``transaktionsdatum`` as the only date fact,
    a title that carries the same future date) → no document date. With a past
    explicit fact present, that one wins instead."""
    future = "10.12.2026"
    facts = [_fact("transaktionsdatum", future)]
    assert derive_document_date(facts, ["Abbuchung ACME 10.12.2026"], reference_date=IMPORT) is None
    facts.append(_fact("dokumentdatum", "27.02.2026"))
    assert derive_document_date(facts, reference_date=IMPORT) == date(2026, 2, 27)


def test_tolerance_edge_for_event_dates():
    at_edge = IMPORT + timedelta(days=FUTURE_TOLERANCE_DAYS)
    past_edge = at_edge + timedelta(days=1)
    assert derive_document_date(
        [_fact("wertstellung", at_edge.isoformat())], reference_date=IMPORT
    ) == at_edge
    assert derive_document_date(
        [_fact("wertstellung", past_edge.isoformat())], reference_date=IMPORT
    ) is None


def test_tolerance_is_below_a_typical_payment_term():
    """The tolerance must stay below the shortest common payment term (14 days),
    or a Zahlungsziel-shaped event date would slip back in."""
    assert 0 < FUTURE_TOLERANCE_DAYS < 14


# --------------------------------------------------------------------------
# Title fallback
# --------------------------------------------------------------------------


def test_derive_falls_back_to_title():
    facts = [_fact("issuer", "ACME GmbH")]
    assert derive_document_date(facts, ["Rechnung ACME 2026-02-09"], reference_date=IMPORT) == date(2026, 2, 9)


def test_title_with_deadline_date_does_not_win():
    """The generated title is synthesised from the facts INCLUDING obligation
    Fristen, so its date may be the deadline — held to the same tolerance."""
    facts = [_fact("zahlung", "zahlbar bis 15.04.2026", category="obligation")]
    assert derive_document_date(
        facts, ["Zahlungsaufforderung ACME fällig 15.04.2026"], reference_date=IMPORT
    ) is None


def test_title_tolerance_edge():
    edge = IMPORT + timedelta(days=FUTURE_TOLERANCE_DAYS)
    assert derive_document_date([], [f"Brief {edge.isoformat()}"], reference_date=IMPORT) == edge
    beyond = edge + timedelta(days=1)
    assert derive_document_date([], [f"Brief {beyond.isoformat()}"], reference_date=IMPORT) is None


def test_later_title_candidate_used_when_first_is_rejected():
    titles = ["Mahnung fällig 2026-06-30", "Mahnung vom 2026-02-20"]
    assert derive_document_date([], titles, reference_date=IMPORT) == date(2026, 2, 20)


# --------------------------------------------------------------------------
# No date / defaults
# --------------------------------------------------------------------------


def test_derive_none_when_no_date_anywhere():
    facts = [_fact("issuer", "ACME GmbH"), _fact("steuernummer", "123/45", "12345", category="identifier")]
    assert derive_document_date(facts, ["Rechnung ACME"], reference_date=IMPORT) is None


def test_reference_date_defaults_to_today():
    far_future = date.today() + timedelta(days=200)
    assert derive_document_date([_fact("leistungsdatum", far_future.isoformat())]) is None
    past = date.today() - timedelta(days=10)
    assert derive_document_date([_fact("leistungsdatum", past.isoformat())]) == past
