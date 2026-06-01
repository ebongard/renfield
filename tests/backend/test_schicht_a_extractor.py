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
    _MAX_OPEN_FACTS,
    SchichtAExtractor,
    _facts_from_payload,
    _parse_amount,
    _parse_date,
    _parse_llm_json,
    _salvage_truncated_json,
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

    def test_open_obligation_kind_free_label_kept(self):
        """The obligation kind is now open — a free label like 'klagefrist' must
        be kept, not dropped against a fixed enum."""
        facts = _facts_from_payload({
            "obligations": [{"kind": "klagefrist", "date": "2026-03-01",
                             "excerpt": "Klage binnen eines Monats"}],
        })
        assert facts[0].kind == "klagefrist"
        assert facts[0].legal_gate is True  # 'klage' keyword

    def test_legal_gate_keyword_in_excerpt(self):
        """A non-statutory-looking kind whose excerpt cites a statutory remedy
        still forces the gate (keyword matched across kind AND excerpt)."""
        facts = _facts_from_payload({
            "obligations": [{"kind": "termin", "legal_gate": False,
                             "excerpt": "Widerspruch binnen eines Monats moeglich"}],
        })
        assert facts[0].legal_gate is True

    def test_ordinary_obligation_not_gated(self):
        facts = _facts_from_payload({
            "obligations": [{"kind": "zahlung", "excerpt": "Bitte ueberweisen Sie den Betrag"}],
        })
        assert facts[0].legal_gate is False

    def test_open_facts_list_mapped(self):
        """The open facts[] schema: rich category collapses to the 2 stored
        buckets; identifier/reference get a normalized value, amounts parse."""
        facts = _facts_from_payload({
            "facts": [
                {"category": "party", "kind": "aussteller", "value": "Stadtwerke",
                 "excerpt": "Stadtwerke Musterstadt"},
                {"category": "date", "kind": "rechnungsdatum", "value": "01.01.2020",
                 "excerpt": "Rechnungsdatum: 01.01.2020"},
                {"category": "identifier", "kind": "vertragskonto", "value": "20 1234 5678",
                 "excerpt": "Vertragskonto 20 1234 5678"},
                {"category": "amount", "kind": "gesamtbetrag", "value": "12,34",
                 "amount": {"value": 12.34, "currency": "EUR"}, "excerpt": "12,34 EUR"},
            ],
        })
        by_kind = {f.kind: f for f in facts}
        assert by_kind["aussteller"].category == "universal"
        assert by_kind["rechnungsdatum"].category == "universal"
        assert by_kind["rechnungsdatum"].value == "01.01.2020"
        # identifier-category fact lands in the IDENTIFIER bucket + normalized.
        assert by_kind["vertragskonto"].category == "identifier"
        assert by_kind["vertragskonto"].normalized_value == "2012345678"
        # amount-category fact stays in the UNIVERSAL bucket (not identifier).
        assert by_kind["gesamtbetrag"].category == "universal"
        assert by_kind["gesamtbetrag"].amount_value == Decimal("12.34")
        assert by_kind["gesamtbetrag"].amount_currency == "EUR"

    def test_open_facts_malformed_entries_skipped(self):
        facts = _facts_from_payload({
            "facts": ["garbage", {"category": "date"}, {"value": "no kind"},
                      {"category": "date", "kind": "rechnungsdatum", "value": "2024-01-01"}],
        })
        assert [f.kind for f in facts] == ["rechnungsdatum"]

    def test_reference_category_maps_to_identifier_bucket(self):
        """identifier AND reference both collapse to the IDENTIFIER bucket and
        get a whitespace-stripped normalized_value."""
        facts = _facts_from_payload({
            "facts": [{"category": "reference", "kind": "aktenzeichen",
                       "value": "AZ 12 3", "excerpt": "Aktenzeichen AZ 12 3"}],
        })
        assert facts[0].category == "identifier"
        assert facts[0].normalized_value == "AZ123"

    def test_open_facts_nonstr_category_not_fatal(self):
        """A hostile non-str category must NOT raise out of _facts_from_payload
        (which would discard the whole batch) — it falls back to UNIVERSAL."""
        facts = _facts_from_payload({
            "facts": [{"category": ["identifier"], "kind": "x", "value": "y"},
                      {"category": 7, "kind": "z", "value": "w"}],
        })
        assert {f.kind for f in facts} == {"x", "z"}
        assert all(f.category == "universal" for f in facts)

    def test_nonlist_obligations_and_facts_not_fatal(self):
        """A truthy non-list for obligations/facts must coerce to empty, not
        raise on slice/iteration."""
        assert _facts_from_payload({"obligations": {"k": "v"}, "facts": "nope"}) == []
        assert _facts_from_payload({"facts": 5, "obligations": True}) == []

    def test_open_facts_cap_enforced(self):
        """The per-source open-facts cap bounds write amplification."""
        facts = _facts_from_payload({
            "facts": [{"category": "other", "kind": f"k{i}", "value": f"v{i}"}
                      for i in range(_MAX_OPEN_FACTS + 25)],
        })
        assert len(facts) == _MAX_OPEN_FACTS

    def test_legal_gate_substring_false_positive_not_gated(self):
        """Prefix-boundary match: a routine bill whose excerpt merely contains a
        legal keyword as a suffix/infix (Anklage, Preisrevision) is NOT gated."""
        facts = _facts_from_payload({
            "obligations": [{"kind": "zahlung", "legal_gate": False,
                             "excerpt": "Preisrevision; siehe Beklagtenanschrift"}],
        })
        assert facts[0].legal_gate is False

    def test_legal_gate_german_compound_still_gated(self):
        """Open right edge: a compound like Widerspruchsfrist still gates."""
        facts = _facts_from_payload({
            "obligations": [{"kind": "termin", "legal_gate": False,
                             "excerpt": "Die Widerspruchsfrist endet bald"}],
        })
        assert facts[0].legal_gate is True


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

    async def test_open_facts_identifier_deduped_against_deterministic(self, monkeypatch):
        """Current schema: the LLM echoes the IBAN via the open facts[] list
        (category=identifier). It must dedup against the deterministic copy the
        same way the legacy universal_facts path does."""
        monkeypatch.setattr(
            "services.schicht_a_extractor.settings.schicht_a_extraction_model", "x"
        )
        client = self._client(
            '{"obligations": [], "facts": [{"category": "identifier", '
            '"kind": "iban", "value": "DE89 3704 0044 0532 0130 00"}]}'
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


# ===================================================== truncated-JSON salvage
class TestSalvageTruncatedJson:
    """num_predict truncation cut the LLM JSON mid-object → strict parse failed
    → the whole batch was discarded (doc 43 lost all 14 facts). The salvage
    recovers the complete leading entries instead."""

    # Mirrors the real doc-43 failure: two complete obligations, then a third
    # cut off mid-value with an unterminated string (the token cap hit).
    TRUNCATED = (
        '{\n  "obligations": [\n'
        '    {"kind": "zahlung", "date": "2024-11-15", '
        '"amount": {"value": 33.82, "currency": "EUR"}},\n'
        '    {"kind": "abschlag", "date": "2024-12-01", '
        '"amount": {"value": 51.0, "currency": "EUR"}},\n'
        '    {"kind": "zahlung", "date": "2025-10-15", "amount": {"value": 51.0, '
        '"excerpt": "15.05.2025, 5 = 15.10'
    )

    def test_strict_parse_fails_on_truncation(self):
        import json
        with pytest.raises(json.JSONDecodeError):
            json.loads(self.TRUNCATED)

    def test_salvage_recovers_complete_entries(self):
        payload = _salvage_truncated_json(self.TRUNCATED)
        assert payload is not None
        # The two complete obligations survive; the half-written third is dropped.
        obs = payload["obligations"]
        assert len(obs) == 2
        assert obs[0]["kind"] == "zahlung" and obs[0]["amount"]["value"] == 33.82
        assert obs[1]["kind"] == "abschlag"

    def test_parse_llm_json_uses_salvage(self):
        payload = _parse_llm_json(self.TRUNCATED)
        assert payload is not None
        facts = _facts_from_payload(payload)
        assert len(facts) >= 2  # both complete obligations became facts

    def test_complete_json_unaffected(self):
        good = '{"obligations": [{"kind": "zahlung", "date": "2024-11-15"}], "universal_facts": {}}'
        assert _parse_llm_json(good)["obligations"][0]["kind"] == "zahlung"

    def test_garbage_returns_none(self):
        assert _salvage_truncated_json("not json at all") is None
        assert _parse_llm_json("not json at all") is None

    def test_truncated_nested_array_drops_element_not_completes_it(self):
        """/review finding 4: cutting at a comma inside a truncated nested array
        would present it as complete (e.g. [10,20,30,40<cut> → [10,20,30]) — a
        plausible-but-wrong value. The close-only cut rule must DROP the element
        with the truncated array, not silently complete it."""
        s = (
            '{"obligations": ['
            '{"kind": "a", "date": "2024-01-01"}, '
            '{"kind": "b", "items": [10, 20, 30, 40'
        )
        payload = _salvage_truncated_json(s)
        assert payload is not None
        obs = payload["obligations"]
        assert len(obs) == 1          # only the fully-complete first element
        assert obs[0]["kind"] == "a"
        assert all("items" not in o for o in obs)  # no half-array smuggled in

    def test_top_level_array_comma_cut_still_recovers_scalars(self):
        """The comma branch is KEPT (not dropped) but depth-gated: it still
        recovers complete scalars from a truncated OUTERMOST array — the case
        the container-close rule alone can't handle (scalars don't bracket-close).
        Proves we didn't take the shortcut of removing the branch."""
        assert _salvage_truncated_json('{"vals": [1, 2, 3, 4') == {"vals": [1, 2, 3]}

    @pytest.mark.parametrize("bad", [
        "{",
        "}}}}",
        '{"x": "ends with backslash \\',     # unterminated string + trailing escape
        '{"obligations": "not a list"',       # wrong-typed, truncated
        '{"obligations": [{"kind": "trunc',   # first element incomplete → None
        "",
    ])
    def test_no_crash_on_adversarial_input(self, bad):
        """Untrusted LLM text must never raise — returns None or a dict, and a
        returned dict must survive _facts_from_payload."""
        out = _salvage_truncated_json(bad)
        assert out is None or isinstance(out, dict)
        if isinstance(out, dict):
            _facts_from_payload(out)  # must not raise
