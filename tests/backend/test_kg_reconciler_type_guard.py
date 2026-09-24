"""Type-guard tests for the KG reconciler (pure functions, no DB).

The guard drops pairs whose PRIMARY entity types disagree unless the names are
related. The scalar `entity_type` is deliberate: the `entity_types` superset only
ever grows (merge unions it, every re-mention folds into it), so an overlap test
would disarm itself with exactly the usage the guard exists for. Field evidence, xidra graph 2026-09-24: the place "Korschenbroich"
and the organization "X-Idra Systems GmbH" embed at 0.895 — well over the 0.85
candidate bar — because both are described out of the same letterhead. Four such
pairs sat pending, and one cross-type edge inside a cluster drags the whole
component across the type boundary.

The exception is what keeps the guard honest: a disjoint type with an EQUAL or
subset name is a mis-TYPED duplicate, not two different things. Both live graphs
hold exactly one such fold (person "Pontresina" -> place "Pontresina";
organization "Publikationsplattform" -> thing "Publikationsplattform der ...").
Those must survive — as review candidates only.
"""
import pytest

from services.kg_reconciler_service import _UNTYPED, _types_compatible


@pytest.mark.unit
class TestTypesCompatible:
    def test_the_field_case_place_vs_organization_is_incompatible(self):
        assert _types_compatible("place", "organization") is False

    def test_same_type_is_compatible(self):
        assert _types_compatible("organization", "organization") is True

    def test_case_and_whitespace_are_normalized(self):
        assert _types_compatible(" Place ", "place") is True

    @pytest.mark.parametrize("a,b", [(None, "place"), ("place", None), (None, None)])
    def test_unknown_type_is_not_evidence_of_a_mismatch(self, a, b):
        """The guard accuses, it never guesses: no type -> no verdict."""
        assert _types_compatible(a, b) is True

    def test_symmetric(self):
        assert (_types_compatible("place", "organization")
                == _types_compatible("organization", "place"))

@pytest.mark.unit
class TestThingIsNotATypeClaim:
    """`thing` is the extraction's fallback bucket, not a type.

    ``_build_entities`` assigns it when the model named no type at all
    (knowledge_graph_service.py, ``raw_types[0] if raw_types else "thing"``), so
    treating it as a claim would make the guard DELETE the most common duplicate
    shape in an LLM-extracted graph: the same real thing extracted once as
    `thing` and once as `organization`. Those names are often not token-related
    ("Fa. Müller" / "Müller GmbH"), so the pair would be dropped with no merge,
    no proposal and no row the owner could ever find — strictly worse than the
    `gray_zone` proposal it used to get.
    """

    def test_thing_is_the_only_wildcard(self):
        assert _UNTYPED == {"thing"}

    @pytest.mark.parametrize("other", ["organization", "place", "person", "concept", "event"])
    def test_thing_is_compatible_with_everything(self, other):
        assert _types_compatible("thing", other) is True
        assert _types_compatible(other, "thing") is True

    def test_concept_is_a_real_claim_not_a_bucket(self):
        # Only the no-type fallback is a wildcard; `concept` is assigned because
        # the model SAID concept, so it still separates.
        assert _types_compatible("concept", "organization") is False
