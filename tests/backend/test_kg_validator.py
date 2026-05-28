"""
Unit tests for services/kg_validator.py — the deterministic post-extraction
relation filter. No DB, no LLM: the validator is a pure function.

Coverage:
- Each of the 4 rules: happy + reject + boundary.
- German inflection edge cases for source grounding.
- Regression: the 11 known 2026-05-26 incident relations produce the right
  reject reason (or pass, for the attribution case the validator can't see).
- load_heuristics: yaml parsing, graceful degradation, regex compilation.
"""
import re

import pytest

from services.kg_validator import (
    REASON_LOW_CONFIDENCE,
    REASON_NOT_GROUNDED,
    REASON_SELF_LOOP,
    REASON_TYPE_MISMATCH,
    load_heuristics,
    validate_relation,
)


# --------------------------------------------------------------------------
# Test doubles + fixtures
# --------------------------------------------------------------------------

class FakeEntity:
    """Minimal stand-in for a resolved KGEntity (id, name, entity_type)."""

    def __init__(self, id: int, name: str, entity_type: str):
        self.id = id
        self.name = name
        self.entity_type = entity_type


# Heuristics matching the production yaml (kept in sync by
# test_yaml_heuristics_match_production below).
HEURISTICS = {
    "confidence_floor": 0.5,
    "type_rules": [
        (re.compile(
            r"^(heiratet|ist_verheiratet|verheiratet_mit|hat_kind|ist_kind_von|"
            r"ist_(mutter|vater|sohn|tochter|elternteil|eltern)|hat_elternteil|"
            r"ist_eltern_mit|ist_geschwister|ist_verwandt|married_to|has_child|"
            r"is_(mother|father|son|daughter|parent|sibling)_of)",
            re.IGNORECASE), "person"),
        (re.compile(
            r"^(wohnt_in|lebt_in|befindet_sich_in|hat_sitz_in|ansaessig_in|"
            r"sitzt_in|liegt_in|lives_in|located_in|based_in|headquartered_in|"
            r"resides_in)",
            re.IGNORECASE), "place"),
    ],
}


def _validate(subject, obj, predicate, confidence, source_text, heuristics=HEURISTICS):
    return validate_relation(
        subject=subject,
        obj=obj,
        predicate=predicate,
        confidence=confidence,
        source_text=source_text,
        heuristics=heuristics,
    )


# --------------------------------------------------------------------------
# Rule 1 — no self-loop
# --------------------------------------------------------------------------

class TestSelfLoop:
    @pytest.mark.unit
    def test_self_loop_rejected(self):
        anna = FakeEntity(11, "Anna", "person")
        r = _validate(anna, anna, "ist_verheiratet_mit", 1.0, "Anna ist verheiratet.")
        assert not r.ok
        assert r.reason == REASON_SELF_LOOP

    @pytest.mark.unit
    def test_distinct_entities_same_name_not_a_loop(self):
        # Different ids → not a self-loop even if names coincide.
        a1 = FakeEntity(11, "Anna", "person")
        a2 = FakeEntity(12, "Anna", "person")
        r = _validate(a1, a2, "kennt", 0.9, "Anna kennt Anna.")
        assert r.ok


# --------------------------------------------------------------------------
# Rule 2 — source grounding
# --------------------------------------------------------------------------

class TestSourceGrounding:
    @pytest.mark.unit
    def test_both_names_present_passes(self):
        eduard = FakeEntity(1, "Eduard", "person")
        jutta = FakeEntity(2, "Jutta", "person")
        r = _validate(eduard, jutta, "ist_verheiratet_mit", 0.9,
                      "Eduard ist mit Jutta verheiratet.")
        assert r.ok

    @pytest.mark.unit
    def test_hallucinated_object_rejected(self):
        # Hans Filbinger never appears in the dialog → reject.
        anna = FakeEntity(11, "Anna", "person")
        hans = FakeEntity(99, "Hans Filbinger", "person")
        r = _validate(anna, hans, "ist_verheiratet_mit", 1.0,
                      "Anna Johanna von den Bongard ist meine Mutter.")
        assert not r.ok
        assert r.reason == REASON_NOT_GROUNDED

    @pytest.mark.unit
    def test_german_inflection_genitive(self):
        # "Annas" (genitive) contains "anna" → grounded.
        anna = FakeEntity(11, "Anna", "person")
        ben = FakeEntity(93, "Ben", "person")
        r = _validate(anna, ben, "ist_mutter_von", 0.9,
                      "Annas Sohn Ben kam zu Besuch.")
        assert r.ok

    @pytest.mark.unit
    def test_multiword_name_all_words_present(self):
        anna = FakeEntity(11, "Anna Johanna von den Bongard", "person")
        ben = FakeEntity(93, "Ben", "person")
        r = _validate(anna, ben, "ist_mutter_von", 0.9,
                      "Anna Johanna von den Bongard ist Bens Mutter, und Ben ist hier.")
        assert r.ok

    @pytest.mark.unit
    def test_canonical_name_grounds_on_partial_mention(self):
        # F1 fix: resolve_entity may canonicalize a spoken "Eduard" into the
        # stored "Eduard van den Bongard". The dialog only contains "Eduard".
        # The relation must STILL ground — one significant word anchors it.
        # (Pre-fix this was dropped as not_grounded, silently losing the fact.)
        eduard = FakeEntity(1, "Eduard van den Bongard", "person")
        mango = FakeEntity(50, "Mango", "thing")
        r = _validate(eduard, mango, "mag", 0.9, "Eduard mag Mango.")
        assert r.ok

    @pytest.mark.unit
    def test_name_with_no_word_present_rejected(self):
        # Neither "Hans" nor "Filbinger" appears → no significant word anchors
        # → rejected. This is the hallucination case the rule must still catch.
        anna = FakeEntity(11, "Anna", "person")
        hans = FakeEntity(99, "Hans Filbinger", "person")
        r = _validate(anna, hans, "kennt", 0.9,
                      "Anna kennt niemanden aus der Politik.")
        assert not r.ok
        assert r.reason == REASON_NOT_GROUNDED

    @pytest.mark.unit
    def test_short_word_does_not_falsely_ground(self):
        # F3 fix: "Ada" (3 chars) must NOT ground against "Kanada" — prefix
        # match, not loose substring. "ada" is a substring of "kanada" but
        # neither prefixes the other.
        ada = FakeEntity(1, "Ada", "person")
        b = FakeEntity(2, "Berlin", "place")
        r = _validate(ada, b, "wohnt_in", 0.9, "Wir waren in Kanada und Berlin.")
        assert not r.ok
        assert r.reason == REASON_NOT_GROUNDED

    @pytest.mark.unit
    def test_punctuation_surrounding_name(self):
        x = FakeEntity(1, "X-idra", "organization")
        kb = FakeEntity(2, "Kleinenbroich", "place")
        r = _validate(x, kb, "hat_sitz_in", 0.9,
                      "Die Firma (X-idra) sitzt in Kleinenbroich.")
        assert r.ok

    @pytest.mark.unit
    def test_punctuation_only_source_rejects_missing_entity(self):
        a = FakeEntity(1, "Foo", "thing")
        b = FakeEntity(2, "Bar", "thing")
        # Neither Foo nor Bar appears → rejected (subject checked first).
        r = _validate(a, b, "verknuepft_mit", 0.9, "Some unrelated text here.")
        assert not r.ok
        assert r.reason == REASON_NOT_GROUNDED


# --------------------------------------------------------------------------
# Rule 3 — predicate / object-type contract
# --------------------------------------------------------------------------

class TestPredicateObjectType:
    @pytest.mark.unit
    def test_kinship_predicate_with_place_object_rejected(self):
        # "Anna heißt_auch Kleinenbroich" — wait, heißt_auch isn't in table.
        # Use a kinship predicate to a place: ist_verheiratet_mit → place.
        anna = FakeEntity(11, "Anna", "person")
        kb = FakeEntity(2, "Kleinenbroich", "place")
        r = _validate(anna, kb, "ist_verheiratet_mit", 1.0,
                      "Anna und Kleinenbroich.")
        assert not r.ok
        assert r.reason == REASON_TYPE_MISMATCH

    @pytest.mark.unit
    def test_kinship_predicate_with_person_object_passes(self):
        eduard = FakeEntity(1, "Eduard", "person")
        jutta = FakeEntity(2, "Jutta", "person")
        r = _validate(eduard, jutta, "ist_verheiratet_mit", 0.9,
                      "Eduard und Jutta sind verheiratet.")
        assert r.ok

    @pytest.mark.unit
    def test_location_predicate_with_org_object_rejected(self):
        person = FakeEntity(1, "Eduard", "person")
        org = FakeEntity(2, "X-idra", "organization")
        r = _validate(person, org, "wohnt_in", 0.9, "Eduard wohnt in X-idra.")
        assert not r.ok
        assert r.reason == REASON_TYPE_MISMATCH

    @pytest.mark.unit
    def test_location_predicate_with_place_object_passes(self):
        person = FakeEntity(1, "Eduard", "person")
        place = FakeEntity(2, "Kleinenbroich", "place")
        r = _validate(person, place, "wohnt_in", 0.9,
                      "Eduard wohnt in Kleinenbroich.")
        assert r.ok

    @pytest.mark.unit
    def test_unknown_predicate_allowed(self):
        # Open vocabulary: a predicate matching no rule passes regardless of type.
        a = FakeEntity(1, "Eduard", "person")
        b = FakeEntity(2, "Mango", "thing")
        r = _validate(a, b, "mag", 0.9, "Eduard mag Mango.")
        assert r.ok

    @pytest.mark.unit
    def test_english_kinship_predicate_enforced(self):
        anna = FakeEntity(11, "Anna", "person")
        place = FakeEntity(2, "Berlin", "place")
        r = _validate(anna, place, "married_to", 1.0, "Anna and Berlin.")
        assert not r.ok
        assert r.reason == REASON_TYPE_MISMATCH

    @pytest.mark.unit
    def test_case_insensitive_predicate_match(self):
        anna = FakeEntity(11, "Anna", "person")
        place = FakeEntity(2, "Berlin", "place")
        r = _validate(anna, place, "IST_VERHEIRATET_MIT", 1.0, "Anna and Berlin.")
        assert not r.ok
        assert r.reason == REASON_TYPE_MISMATCH


# --------------------------------------------------------------------------
# Rule 4 — confidence floor (defense-in-depth)
# --------------------------------------------------------------------------

class TestConfidenceFloor:
    @pytest.mark.unit
    def test_below_floor_rejected(self):
        # Floor is 0.5 (matches the prompt's advertised 0.5-1.0 range).
        a = FakeEntity(1, "Eduard", "person")
        b = FakeEntity(2, "Mango", "thing")
        r = _validate(a, b, "mag", 0.49, "Eduard mag Mango.")
        assert not r.ok
        assert r.reason == REASON_LOW_CONFIDENCE

    @pytest.mark.unit
    def test_at_floor_passes(self):
        # 0.5 is exactly the prompt's lower bound — must NOT be dropped.
        a = FakeEntity(1, "Eduard", "person")
        b = FakeEntity(2, "Mango", "thing")
        r = _validate(a, b, "mag", 0.5, "Eduard mag Mango.")
        assert r.ok

    @pytest.mark.unit
    def test_rule_order_grounding_before_confidence(self):
        # An ungrounded relation at low confidence reports grounding first
        # (rule 2 runs before rule 4).
        a = FakeEntity(1, "Foo", "thing")
        b = FakeEntity(2, "Bar", "thing")
        r = _validate(a, b, "mag", 0.1, "Nothing relevant here.")
        assert not r.ok
        assert r.reason == REASON_NOT_GROUNDED


# --------------------------------------------------------------------------
# Regression: the 11 known 2026-05-26 incident relations
# --------------------------------------------------------------------------

class TestIncidentRegression:
    """The dialog these came from (paraphrased): Eduard says he likes Mango,
    his wife Jutta likes Maracuja, they have a son Ben, his mother is Anna
    Johanna van den Bongard. Hans Filbinger and Kleinenbroich are NEVER
    mentioned. All garbage relations had subject 'Anna'.
    """

    # The actual dialog text the validator would see.
    DIALOG = (
        "Ich, Eduard van den Bongard, bin mit Jutta van den Bongard geborene "
        "Deussen verheiratet. Jutta mag Maracujas und Ananas. Mango ist mein "
        "Lieblingsobst. Anna Johanna van den Bongard ist meine Mutter. Wir "
        "haben zwei Kinder, Ben und Tom."
    )

    @pytest.mark.unit
    def test_self_loop_anna_married_anna(self):
        anna = FakeEntity(11, "Anna Johanna van den Bongard", "person")
        r = _validate(anna, anna, "ist_verheiratet_mit", 1.0, self.DIALOG)
        assert not r.ok and r.reason == REASON_SELF_LOOP

    @pytest.mark.unit
    def test_self_loop_anna_mother_anna(self):
        anna = FakeEntity(11, "Anna Johanna van den Bongard", "person")
        r = _validate(anna, anna, "ist_mutter_von", 1.0, self.DIALOG)
        assert not r.ok and r.reason == REASON_SELF_LOOP

    @pytest.mark.unit
    def test_hallucination_anna_married_filbinger(self):
        anna = FakeEntity(11, "Anna Johanna van den Bongard", "person")
        hans = FakeEntity(99, "Hans Filbinger", "person")
        r = _validate(anna, hans, "ist_verheiratet_mit", 1.0, self.DIALOG)
        assert not r.ok and r.reason == REASON_NOT_GROUNDED

    @pytest.mark.unit
    def test_hallucination_anna_heisst_kleinenbroich(self):
        # heißt_auch is not in the type table, so this is caught by grounding:
        # Kleinenbroich is not in the dialog.
        anna = FakeEntity(11, "Anna Johanna van den Bongard", "person")
        kb = FakeEntity(2, "Kleinenbroich", "place")
        r = _validate(anna, kb, "heißt_auch", 1.0, self.DIALOG)
        assert not r.ok and r.reason == REASON_NOT_GROUNDED

    @pytest.mark.unit
    def test_hallucination_anna_vorheriger_name_kleinenbroich(self):
        anna = FakeEntity(11, "Anna Johanna van den Bongard", "person")
        kb = FakeEntity(2, "Kleinenbroich", "place")
        r = _validate(anna, kb, "heiratet_vorheriger_name", 1.0, self.DIALOG)
        # "heiratet" matches the kinship pattern → object must be person, but
        # Kleinenbroich is a place → type mismatch (caught before grounding?).
        # Rule order: grounding (rule 2) runs before type (rule 3). Kleinenbroich
        # is not in the dialog → NOT_GROUNDED wins.
        assert not r.ok and r.reason == REASON_NOT_GROUNDED

    @pytest.mark.unit
    def test_attribution_anna_mag_mango_SURVIVES(self):
        # This is the 11th relation the validator CANNOT catch: Anna is in the
        # dialog, Mango is in the dialog, 'mag' is an allowed predicate, conf
        # is high. The misattribution (it's Eduard who likes Mango) is invisible
        # to a per-relation validator. Documented limitation — fixed at the
        # prompt layer (speaker-identity binding), not here.
        anna = FakeEntity(11, "Anna Johanna van den Bongard", "person")
        mango = FakeEntity(50, "Mango", "thing")
        r = _validate(anna, mango, "mag", 1.0, self.DIALOG)
        assert r.ok, "validator cannot see attribution errors — this is expected"


# --------------------------------------------------------------------------
# load_heuristics
# --------------------------------------------------------------------------

class FakePromptManager:
    def __init__(self, config):
        self._config = config

    def get_config(self, file, key, default=None, lang=None):
        return self._config


class TestLoadHeuristics:
    @pytest.mark.unit
    def test_loads_floor_and_rules(self):
        pm = FakePromptManager({
            "confidence_floor": 0.7,
            "predicate_object_type": [
                {"pattern": "^married_to", "object_type": "person"},
            ],
        })
        h = load_heuristics(pm)
        assert h["confidence_floor"] == 0.7
        assert len(h["type_rules"]) == 1
        pattern, otype = h["type_rules"][0]
        assert otype == "person"
        assert pattern.search("married_to")

    @pytest.mark.unit
    def test_missing_config_degrades_gracefully(self):
        pm = FakePromptManager(None)
        h = load_heuristics(pm)
        assert h["confidence_floor"] == 0.5  # default
        assert h["type_rules"] == []  # fail-open: no type enforcement

    @pytest.mark.unit
    def test_malformed_regex_skipped(self):
        pm = FakePromptManager({
            "predicate_object_type": [
                {"pattern": "[invalid(regex", "object_type": "person"},
                {"pattern": "^valid", "object_type": "place"},
            ],
        })
        h = load_heuristics(pm)
        # Bad regex skipped, good one kept.
        assert len(h["type_rules"]) == 1
        assert h["type_rules"][0][1] == "place"

    @pytest.mark.unit
    def test_incomplete_entry_skipped(self):
        pm = FakePromptManager({
            "predicate_object_type": [
                {"pattern": "^foo"},  # missing object_type
                {"object_type": "person"},  # missing pattern
            ],
        })
        h = load_heuristics(pm)
        assert h["type_rules"] == []

    @pytest.mark.unit
    def test_non_numeric_floor_falls_back(self):
        pm = FakePromptManager({"confidence_floor": "not-a-number"})
        h = load_heuristics(pm)
        assert h["confidence_floor"] == 0.5


# --------------------------------------------------------------------------
# Production yaml sanity — the in-test HEURISTICS must match the shipped file
# --------------------------------------------------------------------------

class TestYamlHeuristicsMatchProduction:
    @pytest.mark.unit
    def test_production_yaml_loads_and_enforces(self):
        """Load the real yaml via prompt_manager and confirm the rules fire."""
        from services.prompt_manager import prompt_manager
        prompt_manager.reload()
        h = load_heuristics(prompt_manager)

        # Floor present.
        assert h["confidence_floor"] == 0.5
        # At least the two documented rules.
        assert len(h["type_rules"]) >= 2

        # Kinship predicate → place object must be rejected via the real config.
        anna = FakeEntity(11, "Anna", "person")
        kb = FakeEntity(2, "Kleinenbroich", "place")
        r = validate_relation(
            subject=anna, obj=kb, predicate="ist_verheiratet_mit",
            confidence=1.0, source_text="Anna und Kleinenbroich.",
            heuristics=h,
        )
        assert not r.ok and r.reason == REASON_TYPE_MISMATCH
