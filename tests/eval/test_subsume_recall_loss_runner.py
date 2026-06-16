"""Unit tests for the subsume recall-loss eval runner (Phase 3-subsume watch).

Validates the PURE classify_case logic + the corpus schema, without an LLM (the
real two-extractor run in bin/run_subsume_recall_loss_eval.py needs Ollama and is
on-demand). Mirrors tests/eval/test_kg_extraction_eval_runner.py.
"""
import os
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_env_dir = os.environ.get("SUBSUME_EVAL_RUNNER_DIR")
_runner_dir = Path(_env_dir) if _env_dir else Path(__file__).resolve().parents[2] / "bin"
if not (_runner_dir / "run_subsume_recall_loss_eval.py").exists():
    raise RuntimeError(
        f"run_subsume_recall_loss_eval.py not found in {_runner_dir}. "
        f"Set SUBSUME_EVAL_RUNNER_DIR to a directory reachable from the test mount."
    )
if str(_runner_dir) not in sys.path:
    sys.path.insert(0, str(_runner_dir))

import run_subsume_recall_loss_eval as R  # noqa: E402

_CORPUS = Path(__file__).resolve().parent / "subsume_recall_loss_eval.yaml"


class TestClassifyCase:
    def test_subsumed_and_captured_is_not_loss(self):
        mem = [{"content": "Anna wohnt in Bonn", "category": "fact", "subject": "Anna"}]
        rels = [{"subject": "Anna", "predicate": "wohnt_in", "object": "Bonn"}]
        v = R.classify_case(mem, [], rels)
        assert v["subsumed"] is True
        assert v["captured"] is True
        assert v["lost"] is False
        assert v["lost_subjects"] == []

    def test_subsumed_but_not_captured_is_loss(self):
        # "Anna ist müde" — memory labels fact+subject, KG emits no relation
        mem = [{"content": "Anna ist müde", "category": "fact", "subject": "Anna"}]
        v = R.classify_case(mem, [], [])
        assert v["subsumed"] is True
        assert v["captured"] is False
        assert v["lost"] is True
        assert v["lost_subjects"] == ["anna"]

    def test_not_subsumed_when_no_subject(self):
        mem = [{"content": "es regnet", "category": "fact", "subject": None}]
        v = R.classify_case(mem, [], [])
        assert v["subsumed"] is False
        assert v["lost"] is False

    def test_not_subsumed_when_not_fact_category(self):
        mem = [{"content": "mag Jazz", "category": "preference", "subject": "Anna"}]
        v = R.classify_case(mem, [], [])
        assert v["subsumed"] is False
        assert v["lost"] is False

    def test_partial_capture_counts_as_loss(self):
        # two subsumed subjects, only one captured -> still a loss
        mem = [
            {"content": "Anna wohnt in Bonn", "category": "fact", "subject": "Anna"},
            {"content": "Tom ist müde", "category": "fact", "subject": "Tom"},
        ]
        rels = [{"subject": "Anna", "predicate": "wohnt_in", "object": "Bonn"}]
        v = R.classify_case(mem, [], rels)
        assert v["subsumed"] is True
        assert v["captured"] is False
        assert v["lost"] is True
        assert v["lost_subjects"] == ["tom"]

    def test_case_insensitive_subject_match(self):
        mem = [{"content": "x", "category": "fact", "subject": "ANNA"}]
        rels = [{"subject": "anna", "predicate": "p", "object": "o"}]
        v = R.classify_case(mem, [], rels)
        assert v["captured"] is True
        assert v["lost"] is False


class TestClassifyCasePerFact:
    """classify_case_perfact models the per-(subject,turn) gate, object-aware so
    the same-turn-same-subject residual is VISIBLE (not hidden). A single-fact
    uncaptured fact is KEPT FLAT (cross-turn loss closed); a same-turn mixed fact
    whose object isn't grounded by a relation is LOST (documented residual)."""

    def test_captured_grounded_fact_is_subsumed_not_lost(self):
        # relation object "Bonn" appears in the fact content -> truly grounded.
        mem = [{"content": "Anna wohnt in Bonn", "category": "fact", "subject": "Anna"}]
        rels = [{"subject": "Anna", "predicate": "wohnt_in", "object": "Bonn"}]
        v = R.classify_case_perfact(mem, [], rels)
        assert v["subsumed"] is True
        assert v["kept_flat"] is False
        assert v["lost"] is False

    def test_uncaptured_state_fact_is_kept_flat_not_lost(self):
        # "Anna ist müde" — no relation captured this turn -> KEPT FLAT, not lost.
        mem = [{"content": "Anna ist müde", "category": "fact", "subject": "Anna"}]
        v = R.classify_case_perfact(mem, [], [])
        assert v["subsumed"] is False
        assert v["kept_flat"] is True
        assert v["lost"] is False
        assert v["kept_flat_subjects"] == ["anna"]

    def test_already_related_state_fact_kept_flat_cross_turn(self):
        """Cross-turn residual CLOSED: a state fact whose subject wasn't captured
        THIS turn is kept flat (this turn only captured Tom, not Anna)."""
        mem = [{"content": "Anna ist müde", "category": "fact", "subject": "Anna"}]
        rels = [{"subject": "Tom", "predicate": "arbeitet_bei", "object": "Siemens"}]
        v = R.classify_case_perfact(mem, [], rels)
        assert v["kept_flat"] is True
        assert v["lost"] is False

    def test_mixed_turn_different_subjects_no_loss(self):
        mem = [
            {"content": "Anna wohnt in Bonn", "category": "fact", "subject": "Anna"},
            {"content": "Tom ist müde", "category": "fact", "subject": "Tom"},
        ]
        rels = [{"subject": "Anna", "predicate": "wohnt_in", "object": "Bonn"}]
        v = R.classify_case_perfact(mem, [], rels)
        assert v["subsumed"] is True          # Anna grounded -> dropped
        assert v["kept_flat"] is True         # Tom uncaptured -> kept flat
        assert v["lost"] is False             # different subjects -> no residual
        assert v["subsumed_subjects"] == ["anna"]
        assert v["kept_flat_subjects"] == ["tom"]

    def test_same_turn_same_subject_mixed_is_residual_loss(self):
        """THE DOCUMENTED RESIDUAL: same turn, same subject, mixed object types.
        Anna's entity-object fact captures "anna" → BOTH facts are subsumed, but
        the state fact ("müde") has no grounding relation → it is LOST."""
        mem = [
            {"content": "Anna wohnt in Berlin", "category": "fact", "subject": "Anna"},
            {"content": "Anna ist müde", "category": "fact", "subject": "Anna"},
        ]
        rels = [{"subject": "Anna", "predicate": "wohnt_in", "object": "Berlin"}]
        v = R.classify_case_perfact(mem, [], rels)
        assert v["subsumed"] is True           # subject captured -> both dropped
        assert v["lost"] is True               # state fact not grounded -> residual loss
        assert v["lost_subjects"] == ["anna"]

    def test_corpus_single_fact_zero_loss_residual_documented(self):
        """Across the corpus, simulate the per-(subject,turn) gate per shape.
        Single-fact cases (loss_expected, no residual_expected) must be loss-free
        under the gate; the one residual_expected case must surface a loss."""
        cases = R.load_cases(_CORPUS)
        single_fact_lost = 0
        residual_seen = 0
        for c in cases:
            expect = c["expect"]
            if expect.get("residual_expected"):
                # mixed same-subject: entity-object fact grounds the subject, the
                # co-stated state fact does not -> residual loss surfaces.
                mem = [
                    {"content": "Anna wohnt in Berlin", "category": "fact", "subject": "Anna"},
                    {"content": "Anna ist müde", "category": "fact", "subject": "Anna"},
                ]
                rels = [{"subject": "Anna", "predicate": "p", "object": "Berlin"}]
                v = R.classify_case_perfact(mem, [], rels)
                residual_seen += int(v["lost"])
                continue
            subj = "X"
            mem = [{"content": c["user"], "category": "fact", "subject": subj}]
            # loss_expected True -> KG captured NO relation; False -> grounded relation
            rels = [] if expect["loss_expected"] else [
                {"subject": subj, "predicate": "p", "object": c["user"]}
            ]
            v = R.classify_case_perfact(mem, [], rels)
            single_fact_lost += int(v["lost"])
        assert single_fact_lost == 0, "single-fact gate must not lose anything"
        assert residual_seen == 1, "the documented same-turn residual must surface"


class TestCorpusSchema:
    def test_corpus_loads_and_has_cases(self):
        cases = R.load_cases(_CORPUS)
        assert len(cases) >= 8
        ids = [c["id"] for c in cases]
        assert len(ids) == len(set(ids)), "duplicate case ids"
        for c in cases:
            assert "user" in c and c["user"].strip()
            assert "expect" in c and "loss_expected" in c["expect"]
            assert isinstance(c["expect"]["loss_expected"], bool)

    def test_corpus_covers_both_loss_and_control(self):
        cases = R.load_cases(_CORPUS)
        expected_loss = [c for c in cases if c["expect"]["loss_expected"]]
        control = [c for c in cases if not c["expect"]["loss_expected"]]
        assert expected_loss, "need danger-zone (loss_expected) cases"
        assert control, "need control (KG-safe) cases"
