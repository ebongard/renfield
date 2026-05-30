"""Schicht A field extractor — deterministic layer, payload mapping, hybrid merge.

The deterministic identifier layer is the load-bearing piece (whitespace
normalization recovers poppler ``-layout`` letter-spaced Steuernummern). The LLM
layer is exercised with a mocked client — we test our parsing/merge, not the model.
"""
from __future__ import annotations

import sys
from datetime import date
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

# Stub modules unavailable in a bare test env (mirrors sibling test files).
_missing_stubs = [
    "asyncpg", "whisper", "piper", "piper.voice", "speechbrain",
    "speechbrain.inference", "speechbrain.inference.speaker",
    "openwakeword", "openwakeword.model",
]
import importlib as _importlib
for _mod in _missing_stubs:
    if _mod in sys.modules:
        continue
    try:
        _importlib.import_module(_mod)
    except Exception:  # noqa: BLE001
        sys.modules[_mod] = MagicMock()

from services.schicht_a_extractor import (  # noqa: E402
    SchichtAExtractor,
    _facts_from_payload,
    _parse_amount,
    _parse_date,
    extract_identifiers,
    normalize_field_text,
)

# The doc-44 reality: poppler -layout letter-spaces the wide-tracked line, both
# the keyword AND the value.
LETTER_SPACED_STEUER = (
    "Finanzverwaltung NRW Muenster\n"
    "*6871*0001086*  I h r e   S t e u e r n u m m e r :   11 4 / 5 8 7 6 / 5 2 9 3\n"
    "Frist zur Aktivierung: 23.07.2026"
)


# ============================================================ normalization
class TestNormalize:
    def test_collapses_intra_number_spacing(self):
        assert "114/5876/5293" in normalize_field_text(
            "Steuernummer: 11 4 / 5 8 7 6 / 5 2 9 3"
        )

    def test_preserves_prose_spacing(self):
        # Only digit/slash-adjacent spaces collapse — words stay separated.
        out = normalize_field_text("Ihre Frist endet am 23.07.2026 endgueltig")
        assert "Ihre Frist endet am" in out
        assert "23.07.2026" in out

    def test_empty(self):
        assert normalize_field_text("") == ""


# ====================================================== deterministic identifiers
class TestExtractIdentifiers:
    def test_letter_spaced_steuernummer_recovered(self):
        """The headline case: raw exact-match fails, the extractor recovers it."""
        assert "114/5876/5293" not in LETTER_SPACED_STEUER          # raw: defeated
        facts = extract_identifiers(LETTER_SPACED_STEUER)
        steuer = [f for f in facts if f.kind == "steuernummer"]
        assert len(steuer) == 1
        assert steuer[0].normalized_value == "114/5876/5293"
        assert steuer[0].category == "identifier"
        assert steuer[0].source == "deterministic"

    def test_steuernummer_requires_keyword_gate(self):
        """A NN/NNNN/NNNN run with no 'steuernummer' anywhere is not emitted as one."""
        facts = extract_identifiers("Rechnung 114/5876/5293 vom Lieferanten")
        assert not any(f.kind == "steuernummer" for f in facts)

    def test_steuernummer_keyword_must_be_colocated(self):
        """The keyword must be NEAR the number, not merely somewhere in the doc —
        a footer mention of 'Steuernummer' must not tag an unrelated slashed
        number 200 chars away as one."""
        far = "Steuernummer im Briefkopf. " + "x " * 120 + " Bestellnummer 114/5876/5293"
        facts = extract_identifiers(far)
        assert not any(f.kind == "steuernummer" for f in facts)

    def test_german_iban_group_spaced(self):
        facts = extract_identifiers("Konto IBAN DE89 3704 0044 0532 0130 00 bei der Bank")
        iban = [f for f in facts if f.kind == "iban"]
        assert len(iban) == 1
        assert iban[0].normalized_value == "DE89370400440532013000"

    def test_dedup_repeated_steuernummer(self):
        text = "Steuernummer 114/5876/5293 ... siehe oben 114/5876/5293"
        steuer = [f for f in extract_identifiers(text) if f.kind == "steuernummer"]
        assert len(steuer) == 1

    def test_empty_returns_nothing(self):
        assert extract_identifiers("") == []


# ============================================================ helper parsers
class TestParsers:
    @pytest.mark.parametrize("raw,expected", [
        ("2026-07-23", date(2026, 7, 23)),
        ("23.07.2026", date(2026, 7, 23)),
        ("7.4.2026", date(2026, 4, 7)),
        ("not a date", None),
        ("2026-13-01", None),
        (None, None),
    ])
    def test_parse_date(self, raw, expected):
        assert _parse_date(raw) == expected

    @pytest.mark.parametrize("raw,expected", [
        ("107.10", Decimal("107.10")),
        ("1.234,56", Decimal("1234.56")),
        ("1234,56", Decimal("1234.56")),
        (107.10, Decimal("107.1")),
        ("€ 99,00", Decimal("99.00")),
        (None, None),
        ("", None),
    ])
    def test_parse_amount(self, raw, expected):
        assert _parse_amount(raw) == expected


# ============================================================ LLM payload mapping
class TestFactsFromPayload:
    def test_obligation_mapped(self):
        facts = _facts_from_payload({
            "obligations": [{
                "kind": "zahlung", "date": "2026-04-17",
                "amount": {"value": 107.10, "currency": "eur"},
                "excerpt": "Date due April 17, 2026", "payment_method": "manual",
            }],
        })
        ob = [f for f in facts if f.category == "obligation"][0]
        assert ob.kind == "zahlung"
        assert ob.obligation_date == date(2026, 4, 17)
        assert ob.amount_value == Decimal("107.10")
        assert ob.amount_currency == "EUR"
        assert ob.payment_method == "manual"
        assert ob.legal_gate is False
        assert ob.source == "llm"

    def test_widerspruch_forces_legal_gate(self):
        """Statutory kinds are ALWAYS human-confirmed even if the LLM says false."""
        facts = _facts_from_payload({
            "obligations": [{"kind": "widerspruch", "legal_gate": False,
                             "excerpt": "Einspruch binnen eines Monats"}],
        })
        assert facts[0].legal_gate is True

    def test_universal_issuer_total_identifier(self):
        facts = _facts_from_payload({
            "universal_facts": {
                "issuer": "Anthropic, PBC",
                "total": {"value": 107.10, "currency": "EUR"},
                "identifiers": [{"kind": "rechnungsnummer", "value": "1SOGUR2D0010"}],
            },
        })
        kinds = {f.kind for f in facts}
        assert {"issuer", "total", "rechnungsnummer"} <= kinds
        total = [f for f in facts if f.kind == "total"][0]
        assert total.amount_value == Decimal("107.10")

    def test_malformed_entries_skipped_not_fatal(self):
        facts = _facts_from_payload({
            "obligations": ["garbage", {"no_kind": 1}, {"kind": "termin", "date": "2026-01-01"}],
        })
        assert [f.kind for f in facts] == ["termin"]


# ============================================================ hybrid extract()
@pytest.mark.asyncio
class TestExtract:
    def _client(self, content: str):
        client = MagicMock()
        client.chat = AsyncMock(return_value=MagicMock(message=MagicMock(content=content)))
        return client

    async def test_merges_deterministic_and_llm(self, monkeypatch):
        monkeypatch.setattr(
            "services.schicht_a_extractor.settings.schicht_a_extraction_model", "x"
        )
        client = self._client(
            '{"obligations": [{"kind": "termin", "date": "2026-07-23", '
            '"excerpt": "Frist zur Aktivierung: 23.07.2026"}], "universal_facts": {}}'
        )
        result = await SchichtAExtractor(llm_client=client).extract(
            LETTER_SPACED_STEUER, lang="de"
        )
        kinds = {f.kind for f in result.facts}
        assert "steuernummer" in kinds      # deterministic
        assert "termin" in kinds            # LLM
        assert result.error is None

    async def test_llm_failure_keeps_deterministic_facts(self, monkeypatch):
        monkeypatch.setattr(
            "services.schicht_a_extractor.settings.schicht_a_extraction_model", "x"
        )
        client = self._client("not json at all")
        result = await SchichtAExtractor(llm_client=client).extract(
            LETTER_SPACED_STEUER, lang="de"
        )
        assert any(f.kind == "steuernummer" for f in result.facts)
        assert result.error == "llm_extraction_failed"

    async def test_llm_does_not_duplicate_deterministic_identifier(self, monkeypatch):
        monkeypatch.setattr(
            "services.schicht_a_extractor.settings.schicht_a_extraction_model", "x"
        )
        # LLM also surfaces the Steuernummer under identifiers — must be dropped.
        client = self._client(
            '{"obligations": [], "universal_facts": {"identifiers": '
            '[{"kind": "steuernummer", "value": "114/5876/5293"}]}}'
        )
        result = await SchichtAExtractor(llm_client=client).extract(
            LETTER_SPACED_STEUER, lang="de"
        )
        steuer = [f for f in result.facts if f.kind == "steuernummer"]
        assert len(steuer) == 1
        assert steuer[0].source == "deterministic"

    async def test_llm_spaced_identifier_deduped_against_deterministic(self, monkeypatch):
        """The deterministic IBAN is stored normalized; the LLM echoes it
        space-grouped. Dedup must key on the normalized form so the spaced LLM
        copy is dropped (regression: keying on raw value missed it)."""
        monkeypatch.setattr(
            "services.schicht_a_extractor.settings.schicht_a_extraction_model", "x"
        )
        client = self._client(
            '{"obligations": [], "universal_facts": {"identifiers": '
            '[{"kind": "iban", "value": "DE89 3704 0044 0532 0130 00"}]}}'
        )
        result = await SchichtAExtractor(llm_client=client).extract(
            "IBAN DE89 3704 0044 0532 0130 00 bei der Bank", lang="de"
        )
        ibans = [f for f in result.facts if f.kind == "iban"]
        assert len(ibans) == 1
        assert ibans[0].source == "deterministic"

    async def test_empty_field_text_no_llm_call(self):
        client = self._client("{}")
        result = await SchichtAExtractor(llm_client=client).extract("   ", lang="de")
        assert result.facts == []
        client.chat.assert_not_called()
