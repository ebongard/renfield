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
        ("QA", "QAEngineer"),
        ("Security", "SecurityEngineer"),
        ("backend", "BackendEngineer"),
        ("Management", "DocumentManagement"),
    ])
    def test_the_measured_role_shapes(self, a, b):
        assert _names_related(a, b) is False
        assert _names_related_after_split(a, b) is True
        assert _names_related_after_split(b, a) is True

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
