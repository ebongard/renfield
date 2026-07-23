"""
Unit tests for the Phase-0 memory v1 baseline runner — covers the pure
aggregation paths (no DB / LLM needed). The integration paths that hit
Postgres + Ollama / llama-server are covered by running the script
itself against the .159 build box per CLAUDE.md.

These tests exist to:
  1. Prove `aggregate_baseline_metrics` computes the four locked
     baselines correctly across the categories the plan asks about.
  2. Prove the report renderer produces a markdown string with the
     v1-vs-v2 quality bar header expected by Phase 0.
  3. Prevent the next engineer from quietly changing the metric
     definitions and silently moving the v2 quality bar.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

# The runner script lives at <repo>/bin/memory_v1_baseline.py, which is
# NOT on Python's default sys.path. Discover it via:
#   1. MEMORY_BASELINE_RUNNER_DIR env var (explicit override for CI / the
#      .159 docker container, where /opt/renfield/bin is not mounted)
#   2. Standard local-dev layout: <test_file>/../../../bin/
_env_dir = os.environ.get("MEMORY_BASELINE_RUNNER_DIR")
if _env_dir:
    _runner_dir = Path(_env_dir)
else:
    _runner_dir = Path(__file__).resolve().parents[2] / "bin"
if not (_runner_dir / "memory_v1_baseline.py").exists():
    raise RuntimeError(
        f"memory_v1_baseline.py not found in {_runner_dir}. "
        f"Set MEMORY_BASELINE_RUNNER_DIR to override, or copy the runner "
        f"into a directory reachable from the test's mount."
    )
if str(_runner_dir) not in sys.path:
    sys.path.insert(0, str(_runner_dir))

import memory_v1_baseline as runner  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _result(
    turn_id: str,
    category: str,
    expected: str,
    actual: str,
    *,
    flavor: str = "reva",
    parse_error: bool = False,
    embedding_error: bool = False,
    latency: float = 1.0,
    extracted: int = 0,
) -> runner.BaselineResult:
    return runner.BaselineResult(
        turn_id=turn_id,
        category=category,
        flavor=flavor,
        expected_outcome=expected,
        actual_outcome=actual,
        extracted_count=extracted,
        latency_seconds=latency,
        parse_error=parse_error,
        embedding_error=embedding_error,
    )


# ---------------------------------------------------------------------------
# aggregate_baseline_metrics
# ---------------------------------------------------------------------------

class TestAggregateBaselineMetrics:
    """The four locked baselines, computed per the plan's definitions."""

    @pytest.mark.unit
    def test_empty_input_returns_zeroed_report(self):
        report = runner.aggregate_baseline_metrics([])
        assert report.total_turns == 0
        # No turns means perfect schema rate by definition (no failures observed)
        assert report.schema_validation_rate == 1.0
        assert report.noop_rate_on_generic_queries == 0.0
        assert report.duplicate_rate == 0.0
        assert report.cross_session_update_detection == 0.0

    @pytest.mark.unit
    def test_noop_rate_only_counts_should_not_extract_categories(self):
        """Plan: NOOP rate measured across {generic_query,
        within_turn_contradiction, role_injection, wrong_substrate,
        circle_leakage}. pure_add and dedup turns NEVER count toward
        this metric."""
        results = [
            # 3 generic_query turns: 2 NOOP, 1 wrongly ADD
            _result("g1", "generic_query", "NOOP", "NOOP"),
            _result("g2", "generic_query", "NOOP", "NOOP"),
            _result("g3", "generic_query", "NOOP", "ADD"),
            # 2 pure_add turns — must NOT affect the NOOP rate
            _result("a1", "pure_add", "ADD", "ADD"),
            _result("a2", "pure_add", "ADD", "ADD"),
        ]
        report = runner.aggregate_baseline_metrics(results)
        # 2 of 3 generic_query turns NOOP'd correctly = 0.6667
        assert report.noop_rate_on_generic_queries == pytest.approx(2 / 3, abs=0.001)

    @pytest.mark.unit
    def test_noop_rate_spans_all_should_not_extract_categories(self):
        results = [
            _result("g1", "generic_query", "NOOP", "NOOP"),
            _result("w1", "within_turn_contradiction", "NOOP", "NOOP"),
            _result("i1", "role_injection", "NOOP", "ADD"),  # wrong
            _result("s1", "wrong_substrate", "NOOP", "NOOP"),
            _result("c1", "circle_leakage", "NOOP", "NOOP"),
        ]
        report = runner.aggregate_baseline_metrics(results)
        # 4 of 5 correct
        assert report.noop_rate_on_generic_queries == 0.8

    @pytest.mark.unit
    def test_duplicate_rate_counts_dedup_turns_wrongly_adding(self):
        results = [
            # 4 dedup turns: 1 wrongly ADD, 3 correctly NOOP
            _result("d1", "dedup", "NOOP", "ADD"),  # duplicate created
            _result("d2", "dedup", "NOOP", "NOOP"),
            _result("d3", "dedup", "NOOP", "NOOP"),
            _result("d4", "dedup", "NOOP", "NOOP"),
            # NON-dedup ADD turns must NOT count toward duplicate rate
            _result("a1", "pure_add", "ADD", "ADD"),
        ]
        report = runner.aggregate_baseline_metrics(results)
        assert report.duplicate_rate == 0.25

    @pytest.mark.unit
    def test_cross_session_update_detection_accepts_update_or_delete(self):
        """The plan treats UPDATE and DELETE+ADD as equivalent acceptable
        outcomes for a stale-fact contradiction. The metric counts both."""
        results = [
            _result("s1", "cross_session_stale", "UPDATE", "UPDATE"),
            _result("s2", "cross_session_stale", "UPDATE", "DELETE"),
            _result("s3", "cross_session_stale", "UPDATE", "ADD"),  # missed: stale row not touched
            _result("s4", "cross_session_stale", "UPDATE", "NOOP"),  # missed
        ]
        report = runner.aggregate_baseline_metrics(results)
        # 2 of 4 stale rows actually touched (UPDATE or DELETE)
        assert report.cross_session_update_detection == 0.5

    @pytest.mark.unit
    def test_schema_validation_rate_is_one_minus_parse_error_rate(self):
        results = [
            _result("a1", "pure_add", "ADD", "ADD", parse_error=False),
            _result("a2", "pure_add", "ADD", "ADD", parse_error=False),
            _result("a3", "pure_add", "ADD", "FALLBACK", parse_error=True),
            _result("a4", "pure_add", "ADD", "FALLBACK", parse_error=True),
        ]
        report = runner.aggregate_baseline_metrics(results)
        assert report.parse_error_rate == 0.5
        assert report.schema_validation_rate == 0.5

    @pytest.mark.unit
    def test_latency_percentiles_compute_correctly(self):
        # 20 latencies, evenly spaced 1.0..20.0
        results = [
            _result(f"t{i}", "pure_add", "ADD", "ADD", latency=float(i))
            for i in range(1, 21)
        ]
        report = runner.aggregate_baseline_metrics(results)
        assert report.latency_p50 == 11.0  # middle of 1..20 = index 10 (zero-indexed)
        # p95 of 20 entries: index 18 (int(20*0.95)-1 == 18) → value 19
        assert report.latency_p95 == 19.0
        assert report.latency_p99 == 19.0

    @pytest.mark.unit
    def test_outcome_distribution_counts_all_turns(self):
        results = [
            _result("a1", "pure_add", "ADD", "ADD"),
            _result("a2", "pure_add", "ADD", "ADD"),
            _result("n1", "generic_query", "NOOP", "NOOP"),
            _result("u1", "cross_session_stale", "UPDATE", "UPDATE"),
        ]
        report = runner.aggregate_baseline_metrics(results)
        assert report.outcome_distribution == {"ADD": 2, "NOOP": 1, "UPDATE": 1}
        assert report.turns_by_category == {
            "pure_add": 2,
            "generic_query": 1,
            "cross_session_stale": 1,
        }


# ---------------------------------------------------------------------------
# matches_expected — the per-turn pass/fail used in the JSON report
# ---------------------------------------------------------------------------

class TestMatchesExpected:

    @pytest.mark.unit
    def test_noop_expected_is_strict(self):
        # NOOP-expected turns ONLY pass if actual is also NOOP
        assert _result("t", "generic_query", "NOOP", "NOOP").matches_expected()
        assert not _result("t", "generic_query", "NOOP", "ADD").matches_expected()
        assert not _result("t", "generic_query", "NOOP", "UPDATE").matches_expected()

    @pytest.mark.unit
    def test_update_expected_accepts_update_or_delete(self):
        # UPDATE expected — UPDATE or DELETE (DELETE+ADD pair surfaces as
        # DELETE in the classifier because soft_deleted_ids check fires
        # before new_ids). See TestMatchesExpectedDeleteFallback below for
        # the post-review tightening that excludes ADD.
        for actual in ("UPDATE", "DELETE"):
            assert _result("t", "cross_session_stale", "UPDATE", actual).matches_expected()
        assert not _result("t", "cross_session_stale", "UPDATE", "NOOP").matches_expected()

    @pytest.mark.unit
    def test_add_expected_strict(self):
        assert _result("t", "pure_add", "ADD", "ADD").matches_expected()
        assert not _result("t", "pure_add", "ADD", "NOOP").matches_expected()


# ---------------------------------------------------------------------------
# render_markdown_report — shape of the published artifact
# ---------------------------------------------------------------------------

class TestRenderMarkdownReport:

    @pytest.mark.unit
    def test_report_includes_four_baselines_and_target_columns(self):
        results = [
            _result("g1", "generic_query", "NOOP", "NOOP"),
            _result("d1", "dedup", "NOOP", "NOOP"),
            _result("s1", "cross_session_stale", "UPDATE", "UPDATE"),
        ]
        report = runner.aggregate_baseline_metrics(results)
        md = runner.render_markdown_report(
            report,
            corpus_path=Path("tests/eval/memory_v1_baseline_corpus.yaml"),
            run_started=datetime(2026, 5, 14, 12, 0, 0, tzinfo=UTC),
        )
        # Four locked baselines all present
        assert "NOOP rate on generic queries" in md
        assert "Duplicate rate" in md
        assert "Cross-session UPDATE detection" in md
        assert "Schema-validation rate" in md
        # Target column (so future engineers can't drop the bars)
        assert "≥ 0.950" in md
        assert "≤ 0.010" in md
        assert "≥ 0.800" in md
        # Pointer to the plan
        assert "memory-architecture-plan.md" in md

    @pytest.mark.unit
    def test_report_marks_pass_or_gap_per_metric(self):
        # Construct a report where 3 of 4 baselines fail
        bad_results = [
            _result("g1", "generic_query", "NOOP", "ADD"),  # NOOP rate 0.0 (fails)
            _result("d1", "dedup", "NOOP", "ADD"),           # dup rate 1.0 (fails)
            _result("s1", "cross_session_stale", "UPDATE", "NOOP"),  # detection 0.0 (fails)
            _result("a1", "pure_add", "ADD", "ADD"),
        ]
        report = runner.aggregate_baseline_metrics(bad_results)
        md = runner.render_markdown_report(
            report,
            corpus_path=Path("tests/eval/memory_v1_baseline_corpus.yaml"),
            run_started=datetime(2026, 5, 14, tzinfo=UTC),
        )
        # Three gap markers expected
        assert md.count("gap to close in v2") >= 3


# ---------------------------------------------------------------------------
# matches_expected — DELETE / FALLBACK paths (gaps surfaced by /review)
# ---------------------------------------------------------------------------

class TestMatchesExpectedDeleteFallback:

    @pytest.mark.unit
    def test_delete_expected_strict(self):
        # DELETE-expected turns ONLY pass if actual is also DELETE
        assert _result("t", "cross_session_stale", "DELETE", "DELETE").matches_expected()
        assert not _result("t", "cross_session_stale", "DELETE", "NOOP").matches_expected()
        assert not _result("t", "cross_session_stale", "DELETE", "ADD").matches_expected()
        assert not _result("t", "cross_session_stale", "DELETE", "UPDATE").matches_expected()

    @pytest.mark.unit
    def test_fallback_expected_strict(self):
        # FALLBACK-expected turns require the runner to have observed FALLBACK
        assert _result("t", "pure_add", "FALLBACK", "FALLBACK").matches_expected()
        assert not _result("t", "pure_add", "FALLBACK", "ADD").matches_expected()
        assert not _result("t", "pure_add", "FALLBACK", "NOOP").matches_expected()

    @pytest.mark.unit
    def test_update_expected_rejects_add_after_review_fix(self):
        # POST-REVIEW: STALE_DETECTION_OUTCOMES = {UPDATE, DELETE} — ADD is
        # NO LONGER acceptable. A pure ADD on a stale turn means v1
        # abandoned the old row (duplicate-accumulation, the bug v2 fixes).
        # This locks the corrected definition.
        assert _result("t", "cross_session_stale", "UPDATE", "UPDATE").matches_expected()
        assert _result("t", "cross_session_stale", "UPDATE", "DELETE").matches_expected()
        assert not _result("t", "cross_session_stale", "UPDATE", "ADD").matches_expected()
        assert not _result("t", "cross_session_stale", "UPDATE", "NOOP").matches_expected()


# ---------------------------------------------------------------------------
# Helper functions exposed for safety (added in /review hardening)
# ---------------------------------------------------------------------------

class TestSafetyHelpers:

    @pytest.mark.unit
    def test_stable_test_user_id_is_deterministic(self):
        # Same turn_id must produce the same user_id across calls — and
        # across processes, by virtue of using hashlib (not hash()).
        a1 = runner.stable_test_user_id("reva-dedup-01")
        a2 = runner.stable_test_user_id("reva-dedup-01")
        assert a1 == a2

    @pytest.mark.unit
    def test_stable_test_user_id_is_in_reserved_range(self):
        # All allocated IDs must be in [TEST_USER_ID_BASE, TEST_USER_ID_BASE + 2^24).
        # The 2^24 ceiling is critical: TEST_USER_ID_BASE + 2^24 ≈ 2.017e9
        # which fits Postgres INTEGER (int4, max 2_147_483_647) with
        # ~130M of headroom. A 4-byte digest (the original implementation)
        # would land up to 2e9 + 2^32 ≈ 6.3e9 → DataError on INSERT.
        # See adversarial review F6.
        for turn_id in ["a", "reva-dedup-01", "renfield-add-02", "zzz" * 50]:
            uid = runner.stable_test_user_id(turn_id)
            assert uid >= runner.TEST_USER_ID_BASE
            assert uid < runner.TEST_USER_ID_BASE + (1 << 24)
            # And — critically — fits int4:
            assert uid <= 2_147_483_647, (
                f"user_id {uid} overflows Postgres INTEGER (int4 max). "
                f"TEST_USER_ID_HASH_BYTES must stay <= 3."
            )

    @pytest.mark.unit
    def test_stable_owner_user_id_in_reserved_range_and_disjoint_from_test_ids(self):
        # Owner IDs live in a separate band inside the test namespace so
        # they cannot collide with stable_test_user_id outputs.
        # See adversarial review F7.
        for raw in [0, 1, 99, 1_000, 1_000_000]:
            oid = runner.stable_owner_user_id(raw)
            assert oid >= runner.TEST_USER_ID_BASE + runner.OWNER_ID_NAMESPACE_OFFSET
            # Owners use small corpus values (single/double digit) so
            # the band stays narrow and well under int4 max.
            assert oid <= 2_147_483_647

    @pytest.mark.unit
    def test_stable_test_user_id_differs_per_turn(self):
        # Different turn_ids should produce different user_ids (collision
        # at 4-byte hash is ~1 in 4 billion).
        ids = {runner.stable_test_user_id(f"turn-{i}") for i in range(100)}
        assert len(ids) == 100

    @pytest.mark.unit
    def test_check_database_url_safety_rejects_prod_patterns(self):
        cases = [
            "postgresql://user:pass@renfield-private.cluster.local/db",
            "postgresql://user@prod-db.k8s.local/renfield",
            "postgresql://u@host/production_db",
            "postgresql://u@host/renfield-db",
            # Project-specific prod hostnames (the /review adversarial-F5 additions)
            # are now loaded from the gitignored bin/prod_url_patterns.private so real
            # infra names stay out of the repo — covered by the sentinel test below.
            # Common managed-DB SaaS:
            "postgresql://u@db.abc123.us-west-2.rds.amazonaws.com/renfield",
            "postgresql://u@pg-xyz.aiven.io/defaultdb",
            "postgresql://u@db.proj.supabase.co/postgres",
        ]
        for url in cases:
            refusal = runner.check_database_url_safety(url, allow_prod=False)
            assert refusal is not None, f"Should have refused: {url}"
            assert "Refusing to run" in refusal

    @pytest.mark.unit
    def test_check_database_url_safety_honors_loaded_private_patterns(self, monkeypatch):
        # Project-specific prod hostnames are loaded from the gitignored
        # bin/prod_url_patterns.private (real infra names never enter the repo).
        # Prove the guard enforces a loaded pattern using a SENTINEL — so no real
        # infra name is hardcoded in this public test.
        monkeypatch.setattr(
            runner, "PROD_URL_PATTERNS",
            tuple(runner.PROD_URL_PATTERNS) + ("sentinel-prod-host",),
        )
        refusal = runner.check_database_url_safety(
            "postgresql://u@sentinel-prod-host/db", allow_prod=False
        )
        assert refusal is not None and "Refusing to run" in refusal

    @pytest.mark.unit
    def test_check_database_url_safety_accepts_dev_urls(self):
        cases = [
            "postgresql://user@localhost:5432/renfield_test",
            "postgresql://user@127.0.0.1/renfield_dev",
            "postgresql://user@db.test/renfield_baseline",
        ]
        for url in cases:
            assert runner.check_database_url_safety(url, allow_prod=False) is None

    @pytest.mark.unit
    def test_check_database_url_safety_allow_prod_override(self):
        # With allow_prod=True even a prod-looking URL is permitted (operator
        # explicitly opted in via --i-know-this-is-prod).
        url = "postgresql://user@prod.cluster.local/db"
        assert runner.check_database_url_safety(url, allow_prod=True) is None


# ---------------------------------------------------------------------------
# Corpus lint — surface YAML drift before .159 fires the runner
# ---------------------------------------------------------------------------

class TestCorpusLint:
    """Validates the in-repo corpus YAML against the runner's contract."""

    CORPUS_PATH = Path(__file__).resolve().parents[2] / "tests" / "eval" / "memory_v1_baseline_corpus.yaml"

    @pytest.fixture
    def corpus(self):
        # pyyaml is a runtime dep of the runner — these tests need it too.
        # Skip cleanly if running on a dev shell without it (Renfield's
        # canonical test runner is the .159 build box which has yaml).
        yaml = pytest.importorskip("yaml")
        with open(self.CORPUS_PATH) as f:
            return yaml.safe_load(f)["corpus"]

    @pytest.mark.unit
    def test_corpus_is_nonempty(self, corpus):
        assert len(corpus) > 0, "Corpus YAML must contain at least one turn"

    @pytest.mark.unit
    def test_every_turn_has_required_fields(self, corpus):
        required = {"id", "category", "flavor", "lang", "user_message", "assistant_response", "expected_v1_outcome"}
        for turn in corpus:
            missing = required - set(turn.keys())
            assert not missing, f"Turn {turn.get('id')} missing fields: {missing}"

    @pytest.mark.unit
    def test_every_category_is_valid(self, corpus):
        for turn in corpus:
            assert turn["category"] in runner.VALID_CATEGORIES, (
                f"Turn {turn['id']}: category {turn['category']!r} not in VALID_CATEGORIES "
                f"({runner.VALID_CATEGORIES})"
            )

    @pytest.mark.unit
    def test_every_flavor_is_valid(self, corpus):
        for turn in corpus:
            assert turn["flavor"] in {"reva", "renfield"}, (
                f"Turn {turn['id']}: flavor {turn['flavor']!r} not in {{reva, renfield}}"
            )

    @pytest.mark.unit
    def test_every_expected_outcome_is_valid(self, corpus):
        for turn in corpus:
            assert turn["expected_v1_outcome"] in runner.VALID_OUTCOMES, (
                f"Turn {turn['id']}: expected_v1_outcome {turn['expected_v1_outcome']!r} "
                f"not in {runner.VALID_OUTCOMES}"
            )

    @pytest.mark.unit
    def test_ids_are_unique(self, corpus):
        ids = [turn["id"] for turn in corpus]
        duplicates = {x for x in ids if ids.count(x) > 1}
        assert not duplicates, f"Duplicate turn IDs: {duplicates}"

    @pytest.mark.unit
    def test_preexisting_memories_well_formed(self, corpus):
        for turn in corpus:
            for i, pre in enumerate(turn.get("preexisting_memories") or []):
                assert "content" in pre, f"Turn {turn['id']} pre[{i}] missing content"
                assert "category" in pre, f"Turn {turn['id']} pre[{i}] missing category"
                if "age_days" in pre:
                    assert isinstance(pre["age_days"], (int, float)) and pre["age_days"] >= 0

    @pytest.mark.unit
    def test_extraction_eval_fixture_well_formed(self):
        """Lint the Lane B/2 CI eval fixture (memory_extraction_eval.yaml).

        Catches structural drift at PR time: every case must declare
        an id, user_message, assistant_response, candidates list, and
        an expect block with at least one of the supported assertion keys.
        """
        yaml = pytest.importorskip("yaml")
        path = Path(__file__).resolve().parents[2] / "tests" / "eval" / "memory_extraction_eval.yaml"
        with open(path) as f:
            doc = yaml.safe_load(f)
        cases = doc.get("cases") or []
        assert cases, "eval fixture must declare at least one case"
        required_keys = {"id", "user_message", "assistant_response", "candidates", "expect"}
        valid_expect_keys = {
            "ops",
            "ops_count_at_most",
            "ops_must_contain_op_types",
            "ops_must_not_contain_op_types",
            "ops_must_target_id_in_candidates",
        }
        ids_seen = set()
        for case in cases:
            missing = required_keys - set(case.keys())
            assert not missing, f"case {case.get('id')} missing fields: {missing}"
            assert case["id"] not in ids_seen, f"duplicate case id: {case['id']}"
            ids_seen.add(case["id"])
            assert isinstance(case["candidates"], list)
            expect = case["expect"]
            assert set(expect.keys()) & valid_expect_keys, (
                f"case {case['id']}: expect block has no supported assertion keys "
                f"({sorted(valid_expect_keys)})"
            )

    @pytest.mark.unit
    def test_wtc_subcategories_well_formed(self, corpus):
        """Every within_turn_contradiction turn must declare a subcategory.

        Added in the wtc analysis (docs/MEMORY_V1_WTC_ANALYSIS.md):
        - blunt_retraction: "eigentlich/actually/ach nein" flips → must NOOP
        - hedged_refinement: "naja/das hängt/eher" fuzzy → NOOP or nuanced
                             ADD both acceptable

        Lane B/2's eval YAML splits its assertion logic on this field.
        """
        wtc = [t for t in corpus if t["category"] == "within_turn_contradiction"]
        assert wtc, "Corpus should contain within_turn_contradiction turns"
        for turn in wtc:
            assert "subcategory" in turn, (
                f"Turn {turn['id']} (within_turn_contradiction) is missing "
                f"required `subcategory` field. Pick blunt_retraction or "
                f"hedged_refinement."
            )
            assert turn["subcategory"] in {"blunt_retraction", "hedged_refinement"}, (
                f"Turn {turn['id']}: subcategory must be one of "
                f"blunt_retraction / hedged_refinement, got "
                f"{turn['subcategory']!r}"
            )


# ---------------------------------------------------------------------------
# JSON round-trip for the --aggregate-only path
# ---------------------------------------------------------------------------

class TestJsonRoundTrip:

    @pytest.mark.unit
    def test_serialize_deserialize_round_trip(self):
        original = [
            _result("a1", "pure_add", "ADD", "ADD", latency=1.2, extracted=1),
            _result("g1", "generic_query", "NOOP", "NOOP", latency=0.8),
            _result("s1", "cross_session_stale", "UPDATE", "DELETE", latency=2.5, extracted=2),
            _result("f1", "pure_add", "ADD", "FALLBACK", latency=0.0, parse_error=True),
        ]
        serialized = runner._serialize_results(original)
        # Force through json so we catch any non-JSON-serializable fields
        # (default=str was removed from the runner — we want this to raise
        # cleanly if a field drifts to a non-primitive type).
        round_tripped = runner._deserialize_results(json.loads(json.dumps(serialized)))
        assert round_tripped == original

    @pytest.mark.unit
    def test_aggregation_invariant_under_round_trip(self):
        original = [
            _result(f"t{i}", "pure_add", "ADD", "ADD", latency=float(i))
            for i in range(1, 11)
        ]
        report_before = runner.aggregate_baseline_metrics(original)
        round_tripped = runner._deserialize_results(
            json.loads(json.dumps(runner._serialize_results(original)))
        )
        report_after = runner.aggregate_baseline_metrics(round_tripped)
        assert report_before == report_after


