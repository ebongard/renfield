"""Person-guard name tests for the KG reconciler (pure functions, no DB).

The guard drops person pairs with UNRELATED names (distinct people cluster in
embedding space by their names alone). The #876 field data (2026-09-21, a live
auth-on graph) showed the gap: the single most common duplicate cause was two
characters transposed INSIDE one token of a 4-token name — a subset in neither
direction, so the pair had no path at all (not merged, not proposed, and the
conflation monitor excludes persons). ``_names_near_typo`` closes that gap as
a REVIEW-only candidate. The same data supplies the negative shape: numbered
test accounts that differ only in a trailing two-character ordinal must stay
apart.
"""
import pytest

from services.kg_reconciler_service import (
    _names_near_typo,
    _names_related,
    _osa_distance_is_one,
    _person_pair_names_unrelated,
)


@pytest.mark.unit
class TestOsaDistanceOne:
    @pytest.mark.parametrize("a,b", [
        ("lastname", "lastnmae"),    # adjacent transposition
        ("lastname", "lastnrame"),   # insertion
        ("schmidt", "schmitt"),      # substitution
        ("meier", "meir"),           # deletion
        ("ab", "ba"),                # transposition of the whole token
    ])
    def test_single_edits_in_both_directions(self, a, b):
        assert _osa_distance_is_one(a, b) is True
        assert _osa_distance_is_one(b, a) is True

    @pytest.mark.parametrize("a,b", [
        ("anna", "anna"),            # identical → distance 0, not 1
        ("müller", "meyer"),         # several edits
        ("abc", "cba"),              # two transpositions
        ("schmidt", "schmidtxx"),    # length differs by 2
    ])
    def test_zero_or_more_than_one_edit(self, a, b):
        assert _osa_distance_is_one(a, b) is False


@pytest.mark.unit
class TestNamesNearTypo:
    FIELD_A = "Firstname von der Lastname"
    FIELD_B = "Firstname von der Lastnrame"

    def test_field_shape_transposition_inside_last_token(self):
        # The measured gap: not related by the subset test …
        assert _names_related(self.FIELD_A, self.FIELD_B) is False
        # … but a typo pair, hence a review candidate.
        assert _names_near_typo(self.FIELD_A, self.FIELD_B) is True
        assert _names_near_typo(self.FIELD_B, self.FIELD_A) is True

    def test_bare_first_name_is_related_not_typo(self):
        # "Firstname" ⊆ "Firstname von der Lastname" — the subset test owns it.
        assert _names_related("Firstname", self.FIELD_A) is True
        assert _names_near_typo("Firstname", self.FIELD_A) is False

    def test_numbered_test_accounts_stay_distinct(self):
        # Six of the seven measured pairs: distinct identities differing only in
        # a trailing two-character ordinal. Below the token minimum → not a typo.
        assert _names_near_typo("Testkonto Alpha 01", "Testkonto Alpha 02") is False
        assert _names_related("Testkonto Alpha 01", "Testkonto Alpha 02") is False

    def test_distinct_people_are_not_typos(self):
        assert _names_near_typo("Jutta", "Anna") is False
        assert _names_near_typo("Anna Schmidt", "Anna Müller") is False
        assert _names_near_typo("Anna Schmidt", "Anna Maria Schmidt") is False  # token count
        assert _names_near_typo("Anna Schmidt", "Schmidt Anna") is False        # order

    def test_one_edit_surname_is_a_review_candidate(self):
        # Legitimately ambiguous ("Schmidt"/"Schmitt" may be two people) — the
        # function says "near"; the caller must route it to review, never merge.
        assert _names_near_typo("Anna Schmidt", "Anna Schmitt") is True

    def test_short_token_edit_is_not_a_typo(self):
        assert _names_near_typo("Jan Berg", "Jen Berg") is False

    def test_case_and_whitespace_insensitive(self):
        assert _names_near_typo("  anna  SCHMIDT ", "Anna schmitt") is True

    def test_empty_never_matches(self):
        assert _names_near_typo("", "Anna") is False
        assert _names_near_typo(None, None) is False


@pytest.mark.unit
class TestGuardStillDropsUnrelated:
    def test_person_pair_unrelated_is_unchanged_by_typo_logic(self):
        # The drop decision in find_duplicate_pairs is: unrelated AND not typo.
        # _person_pair_names_unrelated itself keeps its contract (typo pairs are
        # still "unrelated" — that is what keeps them out of auto-merge).
        assert _person_pair_names_unrelated(
            "person", None, TestNamesNearTypo.FIELD_A,
            "person", None, TestNamesNearTypo.FIELD_B,
        ) is True
        assert _person_pair_names_unrelated(
            "organization", None, "Acme", "organization", None, "Globex",
        ) is False
