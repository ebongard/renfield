"""Low-signal auto-merge brake of the KG reconciler (pure functions, no DB).

#1329 field data (xidra): "Product A - 1.2.4" vs "Product A - 1.2.3", both
`thing`, both described "Ein Release mit Status IN_PROGRESS", cosine >= 0.95 —
auto-merged two different releases. The old brake only fired on EQUAL names.
"""
import pytest

from services.kg_reconciler_service import (
    _embedding_match_low_signal,
    _names_differ_only_in_digit_tokens,
)

GENERIC = "Ein Release mit Status IN_PROGRESS"


@pytest.mark.unit
class TestNamesDifferOnlyInDigitTokens:
    def test_version_token_is_one_whitespace_token(self):
        assert _names_differ_only_in_digit_tokens("Product A - 1.2.4", "Product A - 1.2.3")

    @pytest.mark.parametrize("a,b", [
        ("Konto 01", "Konto 02"),
        ("Bericht 2025", "Bericht 2024"),
        ("Bericht 2025", "Bericht"),            # added / dropped number
        ("Raum 1", "Raum 12"),
        ("Produkt-1.2.3", "Produkt-1.2.4"),     # digit glued to the word
        ("Release v2", "release V3"),           # case-insensitive like _norm
    ])
    def test_identifier_differences(self, a, b):
        assert _names_differ_only_in_digit_tokens(a, b)

    @pytest.mark.parametrize("a,b", [
        ("Product A - 1.2.3", "Product A - 1.2.3"),   # equal: no difference at all
        ("Product A - 1.2.3", "product a -  1.2.3"),  # equal after _norm
        ("1.2.3 Product", "Product 1.2.3"),           # reordering only
        ("Acme GmbH", "Acme GmbG"),                   # differing token has no digit
        ("Product A 1.2.3", "Product B 1.2.4"),       # a non-digit token differs too
    ])
    def test_not_an_identifier_difference(self, a, b):
        assert not _names_differ_only_in_digit_tokens(a, b)

    def test_empty_both(self):
        assert not _names_differ_only_in_digit_tokens(None, "")


@pytest.mark.unit
class TestEmbeddingMatchLowSignal:
    def test_field_case_identical_generic_descriptions(self):
        assert _embedding_match_low_signal(
            "Product A - 1.2.4", "Product A - 1.2.3", GENERIC, GENERIC)

    def test_field_case_distinct_descriptions_still_blocked_by_version(self):
        assert _embedding_match_low_signal(
            "Product A - 1.2.4", "Product A - 1.2.3",
            "Bugfix-Release", "Feature-Release mit neuer Oberfläche")

    @pytest.mark.parametrize("da,db", [(None, "Lieferant"), ("Lieferant", ""), (None, None)])
    def test_differing_names_one_description_empty(self, da, db):
        assert _embedding_match_low_signal("Fa. Müller", "Müller GmbH", da, db)

    def test_identical_descriptions_normalized(self):
        assert _embedding_match_low_signal("Acme", "Acme Corp", " Kunde ", "kunde")

    def test_negative_control_distinct_names_and_descriptions(self):
        # Not "block everything": real signal on both sides, no identifier
        # difference -> the similarity is meaningful, auto-merge stays allowed.
        assert not _embedding_match_low_signal(
            "Fa. Müller", "Müller GmbH", "Lieferant für Holz", "Schreinerei in Bonn")

    # The pre-#1329 same-name cases, unchanged.
    @pytest.mark.parametrize("da,db", [(None, None), ("Mutter", None), ("Mutter", "mutter")])
    def test_same_name_without_signal_blocks(self, da, db):
        assert _embedding_match_low_signal("Anna", "Anna", da, db)

    def test_same_name_distinct_descriptions_allowed(self):
        assert not _embedding_match_low_signal(
            "Anna", "anna", "meine Mutter, wohnt in Bonn", "Kollegin im Büro")
