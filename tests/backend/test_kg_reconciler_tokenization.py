"""CamelCase-tokenization rescue for the KG reconciler (pure functions, no DB).

Both find-time guards tokenize names on whitespace, so neither can see across a
tokenization difference: `concept "Product Owner"` vs `person "ProductOwner"`
embeds at 0.9030 and is dropped by the PERSON guard before the type guard can
look at it. In the field that shape is almost always a mis-typed ROLE, which is
exactly what the `cross_type` exception was built to collect.

The rescue is a SEPARATE test, never a loosening of `_names_related`: that one
feeds `person_ok`, which opens the AUTO-MERGE gate. Rule and the field data
agreed with the reva instance 2026-09-24, measured on its production graph.
"""
import pytest

from services.kg_reconciler_service import (
    _names_related,
    _names_related_after_split,
    _split_camel,
)


@pytest.mark.unit
class TestSplitCamel:
    @pytest.mark.parametrize("raw,split", [
        ("ProductOwner", "Product Owner"),
        ("SecurityEngineer", "Security Engineer"),
        ("BackendEngineer", "Backend Engineer"),
        ("DocumentManagement", "Document Management"),
        # The acronym boundary — the half that is easy to forget. Without it
        # "XMLHttpRequest" would come out as "XMLHttp Request".
        ("QAEngineer", "QA Engineer"),
        ("IBMWatson", "IBM Watson"),
        ("XMLHttpRequest", "XML Http Request"),
    ])
    def test_boundaries(self, raw, split):
        assert _split_camel(raw) == split

    @pytest.mark.parametrize("raw", [
        "Product Owner",     # already spaced
        "productowner",      # no boundary at all
        "PRODUCTOWNER",      # all caps, no lower to end an acronym
        "Product-Owner",     # `-` is deliberately NOT a separator …
        "Product_Owner",     # … nor is `_`
        "RM27-10",           # a hyphen carries meaning in this data
        "Product A - 1.2.4", # splitting digits would take version numbers apart
        "E2E",               # digits are deliberately not a boundary
        "",
    ])
    def test_left_alone(self, raw):
        assert _split_camel(raw) == raw

    @pytest.mark.parametrize("raw,split", [
        # KNOWN LIMITATION, pinned on purpose. A run-together German legal form
        # comes apart wrongly: the lowercase "b" before the final "H" triggers
        # rule 1, so "…GmbH" becomes "…Gmb H" and never matches the spaced
        # spelling. The pair is then simply not rescued — it fails CLOSED, it is
        # never wrongly merged.
        ("XidraSystemsGmbH", "Xidra Systems Gmb H"),
        ("MüllerGmbH", "Müller Gmb H"),
        # The other half of the trade, and why this is not "fixed": tightening
        # rule 1 to `(?=[A-Z][a-z])` would repair GmbH and break these two.
        ("BeispielAG", "Beispiel AG"),
        ("StadtwerkeKG", "Stadtwerke KG"),
    ])
    def test_german_legal_forms_are_a_known_trade(self, raw, split):
        """No positional rule separates "Gmb|H" from "Beispiel|AG" — that needs a
        lexicon. Measured 2026-09-24: 7 such names on the household graph, 4 on
        xidra, and neither variant rescues a single pair on either. Change this
        only with a measurement that beats both halves."""
        assert _split_camel(raw) == split

    @pytest.mark.parametrize("raw,split", [
        # The reason this is `str.isupper()` and not `[A-Z]`: the ASCII ranges
        # never split a German umlaut, on the two German graphs this ships to.
        ("ProjektÜbersicht", "Projekt Übersicht"),
        ("MüllerÖko", "Müller Öko"),
        ("DatenÜbertragung", "Daten Übertragung"),
        ("ИванПетров", "Иван Петров"),     # and any other cased script, for free
    ])
    def test_case_is_tested_per_script_not_per_ascii_range(self, raw, split):
        assert _split_camel(raw) == split

    def test_an_uncased_script_has_no_boundary(self):
        """CJK has no case at all — nothing to split, so nothing is rescued."""
        assert _split_camel("東京Tower") == "東京Tower"

    def test_none_is_empty_not_a_crash(self):
        assert _split_camel(None) == ""

    def test_only_inserts_spaces_never_alters_characters(self):
        for raw in ("ProductOwner", "QAEngineer", "XMLHttpRequest", "RM27-10"):
            assert _split_camel(raw).replace(" ", "") == raw.replace(" ", "")


@pytest.mark.unit
class TestNamesRelatedAfterSplit:
    def test_the_field_case(self):
        # Dropped today: a subset in neither direction …
        assert _names_related("Product Owner", "ProductOwner") is False
        # … and rescued by the split.
        assert _names_related_after_split("Product Owner", "ProductOwner") is True

    @pytest.mark.parametrize("a,b", [
        ("Billing Engine", "BillingEngine"),
        ("XML Http Request", "XMLHttpRequest"),
        ("Document Management", "DocumentManagement"),
    ])
    def test_equal_token_sets_after_splitting(self, a, b):
        """A tokenization variant is the SAME name written differently."""
        assert _names_related(a, b) is False
        assert _names_related_after_split(a, b) is True
        assert _names_related_after_split(b, a) is True

    @pytest.mark.parametrize("a,b", [
        ("Anna", "AnnaLena"),
        ("Lena", "AnnaLena"),
        ("Hans", "HansPeter"),
        ("Karl", "KarlHeinz"),
        ("Marie", "MarieLuise"),
    ])
    def test_a_compound_first_name_is_two_people_not_a_variant(self, a, b):
        """THE regression this guard exists to refuse, found in adversarial review.

        A German compound first name written without its hyphen is a strict
        SUPERSET of one of its halves. The first version of this rescue reused
        `_names_related` wholesale, which accepts subsets, and thereby re-opened
        the person guard for two genuinely different people — the exact thing it
        exists to prevent. The household graph holds 7 person rows of this shape,
        xidra 3. Equal token sets, never subset.
        """
        assert _names_related(a, b) is False
        assert _names_related_after_split(a, b) is False
        assert _names_related_after_split(b, a) is False

    @pytest.mark.parametrize("a,b", [
        ("QA", "QAEngineer"),
        ("Security", "SecurityEngineer"),
        ("backend", "BackendEngineer"),
        ("Management", "DocumentManagement"),
    ])
    def test_a_subset_role_name_is_not_rescued_either(self, a, b):
        """Same rule, and the cost is accepted knowingly.

        These four are real mis-typed roles on the reva graph and they are NOT
        rescued: "QA" and "QAEngineer" is the same subset shape as "Anna" and
        "AnnaLena", and no test can tell a role from a person by name alone. All
        four sit below the 0.85 candidate threshold there anyway (0.5242-0.6025),
        so nothing measurable is lost; the one pair above it — "Product Owner" /
        "ProductOwner", 0.9030 — has equal token sets and survives.
        """
        assert _names_related_after_split(a, b) is False

    @pytest.mark.parametrize("a,b", [
        ("Alice", "Alice B."),          # already related — nothing to rescue
        ("Product Owner", "Product Owner"),
    ])
    def test_already_related_is_not_a_tokenization_variant(self, a, b):
        assert _names_related(a, b) is True
        assert _names_related_after_split(a, b) is False

    @pytest.mark.parametrize("a,b", [
        ("Anna Schmidt", "Anna Müller"),   # two people
        ("ProductOwner", "ProjectOwner"),  # different first token
        ("Jutta", "Anna"),
    ])
    def test_unrelated_stays_unrelated(self, a, b):
        assert _names_related_after_split(a, b) is False

    def test_a_hyphen_does_not_rescue(self):
        """`-` is not a separator on purpose — else version numbers come apart."""
        assert _names_related_after_split("Product Owner", "Product-Owner") is False
