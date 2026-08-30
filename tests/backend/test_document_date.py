"""Unit tests for services.document_date (document's own date derivation)."""
from datetime import date

import pytest

from services.document_date import derive_document_date, parse_full_date

pytestmark = [pytest.mark.unit]


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


def test_derive_prefers_rechnungsdatum():
    facts = [
        ("leistungsdatum", "2025-11-01", "01.11.2025"),
        ("rechnungsdatum", "2025-12-16", "16.12.2025"),
    ]
    assert derive_document_date(facts) == date(2025, 12, 16)


def test_derive_falls_back_to_other_date_fact():
    facts = [("lieferdatum", None, "irrelevant"), ("belegdatum", "2025-03-05", None)]
    assert derive_document_date(facts) == date(2025, 3, 5)


def test_derive_falls_back_to_title():
    facts = [("issuer", "ACME", "ACME GmbH")]
    assert derive_document_date(facts, ["Rechnung ACME 2025-07-09"]) == date(2025, 7, 9)


def test_derive_none_when_no_date_anywhere():
    facts = [("issuer", "ACME", "ACME GmbH"), ("steuernummer", "12345", "123/45")]
    assert derive_document_date(facts, ["Rechnung ACME"]) is None


def test_derive_normalized_value_before_value():
    # normalized_value is tried before value within a fact
    facts = [("rechnungsdatum", "2025-12-16", "garbled 99")]
    assert derive_document_date(facts) == date(2025, 12, 16)
