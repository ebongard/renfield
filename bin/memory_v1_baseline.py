#!/usr/bin/env python3
"""
Memory Extraction v1 — Baseline Runner

Runs the hand-curated baseline corpus
(`tests/eval/memory_v1_baseline_corpus.yaml`) against the current
`ConversationMemoryService.extract_and_save` v1 path and emits the
empirical numbers the Mem0 v2 upgrade must beat (Phase 0 of the
memory architecture plan — see
`docs/architecture/memory-architecture-plan.md`).

For each corpus turn the runner:
  1. Creates an isolated test user in a clean DB session.
  2. Seeds any `preexisting_memories` (backdated to `age_days`).
  3. Invokes the v1 extract path with the turn's user_message +
     assistant_response.
  4. Diffs the per-user memory rows before vs after to classify the
     actual outcome (NOOP / ADD / UPDATE / DELETE / FALLBACK).
  5. Records latency, parse failures, embedding failures.

Outputs:
  - JSON   `<output_dir>/memory_v1_baseline_<timestamp>.json` —
           per-turn results for the diff in shadow mode and for
           historical comparison.
  - MD     `<output_dir>/memory_v1_baseline_<timestamp>.md` —
           aggregated metrics in the four baseline categories the
           plan locks (NOOP rate on generic queries, duplicate rate,
           cross-session UPDATE detection, schema-validation rate).

Usage:
    # Requires DATABASE_URL + OLLAMA_BASE_URL + LLM_OPENAI_BASE_URL
    # (or the equivalent .env wiring the running cluster uses).
    python bin/memory_v1_baseline.py \\
        --corpus tests/eval/memory_v1_baseline_corpus.yaml \\
        --output-dir ./baseline-runs/

    # Run only a subset:
    python bin/memory_v1_baseline.py --category dedup --flavor reva

    # Dry-run aggregation against an existing JSON without re-running
    # the corpus (useful for tuning the report layout):
    python bin/memory_v1_baseline.py \\
        --aggregate-only baseline-runs/memory_v1_baseline_2026-05-14.json
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import hashlib
import json
import logging
import os
import re
import sys
import time
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Optional

# yaml is imported lazily inside `run_corpus`; unit tests that only
# exercise the pure aggregation paths must not require pyyaml.


# ---------------------------------------------------------------------------
# Data shapes — pure (no DB / LLM imports)
# ---------------------------------------------------------------------------

VALID_OUTCOMES = {"NOOP", "ADD", "UPDATE", "DELETE", "FALLBACK"}

# Corpus categories that REQUIRE v1 to extract something (ADD or
# UPDATE/DELETE on a pre-existing row). New "should-extract" categories
# go here.
EXTRACT_CATEGORIES = {"dedup", "pure_add", "cross_session_stale"}

# Corpus categories where v1 should NOOP (generic queries, contradictions,
# injection, decision-rationale, cross-tier probes). Derived from
# VALID_CATEGORIES so adding a new category forces an explicit decision
# in `aggregate_baseline_metrics`.
NOOP_CATEGORIES = {
    "within_turn_contradiction",
    "generic_query",
    "role_injection",
    "wrong_substrate",
    "circle_leakage",
}

VALID_CATEGORIES = EXTRACT_CATEGORIES | NOOP_CATEGORIES

# Reserved namespace for test user_ids. Real user IDs in Renfield are
# small positive integers (users.id is SERIAL starting at 1). The
# baseline runner stays out of that range entirely to guarantee no
# collision with real users — even if a misconfigured run somehow hits
# a populated DB, the test_user_id values never overlap with anyone
# real, and a single cleanup query
# `DELETE FROM conversation_memories WHERE user_id >= 2_000_000_000`
# reliably purges all baseline runs.
#
# Sizing: TEST_USER_ID_BASE + 2^24 = 2_016_777_215, which fits Postgres
# `INTEGER` (int4 max = 2_147_483_647) with ~130M of headroom.
# `models/database.py:895` confirms conversation_memories.user_id is
# Integer, NOT BigInteger — using a wider hash digest would overflow
# int4 on ~96.5% of corpus turns and crash every baseline run with
# DataError. See the adversarial review (F6) for the exact math.
TEST_USER_ID_BASE = 2_000_000_000
TEST_USER_ID_HASH_BYTES = 3  # 2^24 collision space = 16_777_216 buckets

# Owner-ID offset for cross-tier corpus seeds (when a turn references
# `owner_user_id: N` in the YAML to simulate another user's memory).
# Offset by half the hash space so test_user_id and owner_user_id can
# never collide — would silently invalidate circle_leakage turns by
# attributing the "other user's" row to the asker.
OWNER_ID_NAMESPACE_OFFSET = 1 << 23  # 8_388_608

# Static fake bcrypt-shaped password hash, mirroring the convention in
# `tests/backend/conftest.py::sample_user_data`. Baseline users never
# authenticate, so going through `get_password_hash()` would only burn
# bcrypt rounds (~150 per run) for no functional benefit. The hash is
# bcrypt-shaped solely so the column's VARCHAR(255) shape is realistic.
_BASELINE_FAKE_PASSWORD_HASH = "$2b$12$LQv3c1yqBWVHxkd0LHAkCOYz6TtxMQJqhN8/X4wPpY/ABCDEFGH"



# URL patterns that ALMOST CERTAINLY indicate prod infra. Substring
# match (lower-cased). The runner refuses to proceed if any match
# BASELINE_DATABASE_URL unless --i-know-this-is-prod is passed.
#
# This is project-specific: the real prod hostnames/subnets are loaded from a
# GITIGNORED file (bin/prod_url_patterns.private) so the real infra names stay
# out of the public repo. A generic substring list ("prod", ".cluster.local",
# etc.) is necessary but not sufficient — adversarial review F5 caught that the
# original list let a project-specific prod host through trivially, hence the
# private supplement.
def _load_private_prod_patterns() -> tuple[str, ...]:
    """Project-specific prod-infra substrings from an optional gitignored file
    (bin/prod_url_patterns.private), so real infra names are not committed. One
    lower-cased substring per line; blank / ``#`` lines ignored. Absent file →
    empty tuple (the generic patterns below still guard)."""
    from pathlib import Path
    p = Path(__file__).with_name("prod_url_patterns.private")
    if not p.exists():
        return ()
    return tuple(
        line.strip().lower()
        for line in p.read_text().splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    )


PROD_URL_PATTERNS = (
    "prod",
    "production",
    ".cluster.local",
    "k8s.local",
    "kubernetes.default",
    "renfield-private",
    "renfield-db",
    # Common managed-DB SaaS — would-be-prod URLs even if user named
    # the env var "BASELINE_..." by mistake
    ".rds.amazonaws.com",
    ".aiven.io",
    ".supabase.co",
    ".neon.tech",
) + _load_private_prod_patterns()


# NOTE: per-turn instrumentation lives on the InstrumentedConversationMemoryService
# subclass (defined later) — NOT as module-global state. The class-level
# monkey-patch approach used in the first /review fix pass mutated the
# real service class, leaking instrumented behavior into any other
# extraction running in the same process. Subclassing keeps the
# real service untouched and lets concurrent unrelated extractions
# behave normally. See adversarial review F1+F2+F15.


# Acceptable outcomes for a cross-session stale-fact contradiction.
# UPDATE or DELETE: v1 touched the stale row. A pure ADD outcome means
# the old row was abandoned and a new one created — that IS the
# duplicate-accumulation failure mode Mem0 v2 is designed to fix. The
# diff classifier in `run_one_turn` emits DELETE before ADD when both
# occur in one turn, so a DELETE+ADD pair surfaces as "DELETE" here.
# Single source of truth used by both `aggregate_baseline_metrics` and
# `matches_expected` (previously drift-prone — aggregator was strict,
# matches_expected was over-permissive).
STALE_DETECTION_OUTCOMES = {"UPDATE", "DELETE"}


# ---------------------------------------------------------------------------
# Safety helpers — pure (no DB / LLM imports)
# ---------------------------------------------------------------------------

def stable_test_user_id(turn_id: str) -> int:
    """Deterministic test user_id allocator.

    Returns an integer in [TEST_USER_ID_BASE, TEST_USER_ID_BASE + 2^24).
    The value is stable across processes (uses hashlib, not Python's
    randomized `hash()` builtin) so post-hoc cleanup queries can rebuild
    the test-id set from corpus turn_ids alone.

    Digest size is 3 bytes (24 bits) ON PURPOSE so the resulting user_id
    fits Postgres `INTEGER` (int4 max = 2_147_483_647). A 4-byte digest
    would land up to 2e9 + 2^32 ≈ 6.3e9 which overflows int4 and crashes
    the INSERT — every baseline run would fail. See adversarial review
    F6 + the TEST_USER_ID_HASH_BYTES module constant.

    Collision space at 2^24 = 16.7M buckets is plenty for a 150-turn
    corpus (birthday probability ~0.07%).

    Why this matters: real user IDs in Renfield are small positive
    integers. Using `hash(turn_id) % 1_000_000` (the original
    implementation) would land allocate test IDs in 1..999_999 —
    overlapping with real user accounts. A baseline run against a
    populated DB would have attributed role-injection corpus content
    to random real users.
    """
    h = hashlib.blake2b(turn_id.encode("utf-8"), digest_size=TEST_USER_ID_HASH_BYTES).hexdigest()
    return TEST_USER_ID_BASE + int(h, 16)


def stable_owner_user_id(raw_owner: int) -> int:
    """Deterministic test owner_user_id allocator for cross-tier corpus seeds.

    Maps a small positive int from the corpus YAML (e.g. `owner_user_id: 99`)
    into the test namespace at a fixed offset that does NOT collide with
    `stable_test_user_id` outputs.

    Sizing: stable_test_user_id outputs land in
    [TEST_USER_ID_BASE, TEST_USER_ID_BASE + 2^24). Owners live in
    [TEST_USER_ID_BASE + 2^23, TEST_USER_ID_BASE + 2^23 + small),
    so they're inside the test namespace (purged by the standard
    cleanup query) but in a non-overlapping band when `raw_owner`
    stays small (corpus YAML uses single/double-digit values).
    """
    return TEST_USER_ID_BASE + OWNER_ID_NAMESPACE_OFFSET + int(raw_owner)


def check_database_url_safety(db_url: str, allow_prod: bool = False) -> Optional[str]:
    """Refuse to proceed against a production-pointing DB URL.

    Returns:
        None if the URL is safe to use, OR a refusal message naming
        the matched pattern. The runner exits with the message
        non-zero when this returns non-None and `allow_prod` is False.

    Production-ness is fuzzy by design. We err on the side of refusing
    rather than accidentally writing. Operators who really do want to
    point at prod (e.g. for forensic re-runs against a snapshot) pass
    `--i-know-this-is-prod`.

    Match scope: hostname only. The earlier implementation lower-cased
    the entire URL and ran substring matches — that produced both
    false-positives (a password containing a prod-host substring refused
    a safe URL) and false-negatives (a credentials-free 'prod-alias.local'
    URL passed when the pattern list didn't include that exact alias).
    Parsing the URL and matching `urlparse(...).hostname` removes
    both classes of bug.
    """
    from urllib.parse import urlparse

    # SQLAlchemy URLs have the form `dialect+driver://...`. urlparse
    # handles the embedded `+` correctly because it sees `dialect+driver`
    # as the scheme; `.hostname` returns the post-`@` host part regardless.
    try:
        host = (urlparse(db_url).hostname or "").lower()
    except ValueError:
        # Malformed URL — refuse rather than try to interpret it.
        return (
            f"Refusing to run: BASELINE_DATABASE_URL is not a parseable URL. "
            f"Set a valid postgres connection string."
        )

    if not host:
        # No hostname at all (e.g. unix-socket DSNs). Don't trip on this —
        # if it's local enough to lack a hostname, it's not prod.
        return None

    for pattern in PROD_URL_PATTERNS:
        if pattern in host:
            if allow_prod:
                return None
            return (
                f"Refusing to run: BASELINE_DATABASE_URL hostname {host!r} "
                f"matches production pattern {pattern!r}. Set a dedicated "
                f"test-DB URL, or pass --i-know-this-is-prod if you "
                f"genuinely want to write to this database (DESTRUCTIVE "
                f"for real users)."
            )
    return None


def _make_instrumented_service_class(base_cls):
    """Build a subclass of ConversationMemoryService that counts the
    silent error-swallowing v1 does.

    Why a subclass instead of monkey-patching the base class (the first
    /review fix used module-global counters + class-level patches —
    adversarial review F1+F2+F15 flagged it):

      - The base service class is shared across the whole process.
        Patching it leaks instrumented behavior into ANY concurrent
        ConversationMemoryService caller (background extraction
        workers, hooks, other tests importing the runner module).
      - A subclass leaves the real class untouched. Each turn
        instantiates a fresh InstrumentedConversationMemoryService;
        counters are per-instance, not module-global; nothing leaks.
      - Cleanup is automatic when the instance is dropped — no
        try/finally teardown to leak on exception.

    v1's `_parse_extraction_response` swallows json.JSONDecodeError
    and returns []. v1's contradiction-resolution flow swallows
    embedding failures and falls through. From the outside neither is
    visible — `schema_validation_rate` and `embedding_error_rate`
    would be permanently 0% if measured from extract_and_save's
    return value alone. The subclass overrides catch both.
    """

    class InstrumentedConversationMemoryService(base_cls):

        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self._parse_failures = 0
            self._embedding_failures = 0

        @staticmethod
        def _parse_extraction_response(raw_text):  # type: ignore[override]
            # We can't access self from a staticmethod, so we set a
            # thread/task-local counter via the contextvar below. The
            # subclass overrides the staticmethod to invoke the parent
            # parser and capture parse-failure signal via re-parsing
            # the same input ourselves (heuristic: non-empty input that
            # produced empty output = JSONDecodeError swallowed by v1).
            result = base_cls._parse_extraction_response(raw_text)
            if result or not raw_text or not raw_text.strip():
                return result
            # Re-parse to disambiguate "legitimate empty array" vs
            # "JSON decode failed and v1 returned []".
            text = raw_text.strip()
            if "```" in text:
                m = re.search(r"```(?:json)?\s*(\[.*?\])\s*```", text, re.DOTALL)
                if m:
                    text = m.group(1)
            first_bracket = text.find("[")
            if first_bracket >= 0:
                text = text[first_bracket:]
            try:
                json.loads(text)
            except json.JSONDecodeError:
                _current_instrumentation_counters.get()["parse_failures"] += 1
            return result

        async def _get_embedding(self, content):  # type: ignore[override]
            try:
                return await super()._get_embedding(content)
            except Exception:
                self._embedding_failures += 1
                _current_instrumentation_counters.get()["embedding_failures"] += 1
                raise

    return InstrumentedConversationMemoryService


# Per-turn counters. A ContextVar (not a module-global dict) so multiple
# concurrent runs in the same interpreter would not stomp on each other.
# In practice the runner is sequential, but this defense survives a
# future refactor that parallelizes turns.
import contextvars  # noqa: E402

_current_instrumentation_counters: contextvars.ContextVar[dict] = contextvars.ContextVar(
    "baseline_instrumentation_counters",
    default={"parse_failures": 0, "embedding_failures": 0},
)


@dataclasses.dataclass
class BaselineResult:
    """One corpus-turn run against v1 extract_and_save."""

    turn_id: str
    category: str
    flavor: str
    expected_outcome: str
    actual_outcome: str
    extracted_count: int          # rows added/updated/deleted by this turn
    latency_seconds: float
    parse_error: bool             # extraction LLM JSON unparseable
    embedding_error: bool         # embedding call failed
    notes: str = ""

    def matches_expected(self) -> bool:
        # NOOP is the strictest; everything else is graded permissively
        # because UPDATE vs DELETE+ADD can both be acceptable outcomes
        # for the same input (see corpus notes). The set
        # `STALE_DETECTION_OUTCOMES` is the single source of truth for
        # "UPDATE is satisfied by" and is also used in
        # `aggregate_baseline_metrics`.
        if self.expected_outcome == "NOOP":
            return self.actual_outcome == "NOOP"
        if self.expected_outcome == "UPDATE":
            return self.actual_outcome in STALE_DETECTION_OUTCOMES
        return self.actual_outcome == self.expected_outcome


@dataclasses.dataclass
class BaselineReport:
    """Aggregated metrics across a corpus run."""

    total_turns: int
    turns_by_category: dict[str, int]
    outcome_distribution: dict[str, int]
    parse_error_rate: float                       # fraction of turns with parse_error
    embedding_error_rate: float
    latency_p50: float
    latency_p95: float
    latency_p99: float

    # The four locked baseline metrics (plan §test-plan-artifact baselines)
    noop_rate_on_generic_queries: float           # higher = better; target ≥0.95
    duplicate_rate: float                          # lower = better; target ≤0.01
    cross_session_update_detection: float          # higher = better; target ≥0.80
    schema_validation_rate: float                  # higher = better; target ≥0.95


# ---------------------------------------------------------------------------
# Pure aggregation (covered by unit test — no DB / LLM dependency)
# ---------------------------------------------------------------------------

def aggregate_baseline_metrics(results: list[BaselineResult]) -> BaselineReport:
    """Pure aggregation. Same input → same output. Covered by unit test.

    Definitions of the four locked baselines (plan §test-plan-artifact):

    - noop_rate_on_generic_queries: among turns with category in
      {generic_query, within_turn_contradiction, role_injection,
      wrong_substrate, circle_leakage}, the fraction that v1
      correctly answered with NOOP. Target ≥0.95.

    - duplicate_rate: among turns with category=dedup, the fraction
      where v1 incorrectly produced a NEW row (actual=ADD when
      expected=NOOP). Target ≤0.01.

    - cross_session_update_detection: among turns with
      category=cross_session_stale, the fraction where v1 correctly
      emitted UPDATE or DELETE+ADD on the stale row. Target ≥0.80.

    - schema_validation_rate: 1.0 minus the parse_error_rate. Target ≥0.95.
    """
    if not results:
        return BaselineReport(
            total_turns=0,
            turns_by_category={},
            outcome_distribution={},
            parse_error_rate=0.0,
            embedding_error_rate=0.0,
            latency_p50=0.0,
            latency_p95=0.0,
            latency_p99=0.0,
            noop_rate_on_generic_queries=0.0,
            duplicate_rate=0.0,
            cross_session_update_detection=0.0,
            schema_validation_rate=1.0,
        )

    total = len(results)
    by_cat = Counter(r.category for r in results)
    outcomes = Counter(r.actual_outcome for r in results)

    parse_errors = sum(1 for r in results if r.parse_error)
    embed_errors = sum(1 for r in results if r.embedding_error)

    latencies = sorted(r.latency_seconds for r in results)
    p50 = latencies[len(latencies) // 2]
    p95 = latencies[max(0, int(len(latencies) * 0.95) - 1)]
    p99 = latencies[max(0, int(len(latencies) * 0.99) - 1)]

    # NOOP rate on "should not extract" categories. Sourced from
    # NOOP_CATEGORIES module constant so future corpus additions force
    # explicit routing (the constant is checked in run_one_turn).
    noop_candidates = [r for r in results if r.category in NOOP_CATEGORIES]
    noop_correct = sum(1 for r in noop_candidates if r.actual_outcome == "NOOP")
    noop_rate = noop_correct / len(noop_candidates) if noop_candidates else 0.0

    # Duplicate rate among dedup cases: v1 wrongly emits ADD instead of NOOP
    dedup_results = [r for r in results if r.category == "dedup"]
    dup_count = sum(1 for r in dedup_results if r.actual_outcome == "ADD")
    dup_rate = dup_count / len(dedup_results) if dedup_results else 0.0

    # Cross-session UPDATE detection. Uses STALE_DETECTION_OUTCOMES so
    # the metric and matches_expected stay in lock-step.
    stale_results = [r for r in results if r.category == "cross_session_stale"]
    stale_detected = sum(
        1 for r in stale_results
        if r.actual_outcome in STALE_DETECTION_OUTCOMES
    )
    stale_rate = stale_detected / len(stale_results) if stale_results else 0.0

    return BaselineReport(
        total_turns=total,
        turns_by_category=dict(by_cat),
        outcome_distribution=dict(outcomes),
        parse_error_rate=parse_errors / total,
        embedding_error_rate=embed_errors / total,
        latency_p50=p50,
        latency_p95=p95,
        latency_p99=p99,
        noop_rate_on_generic_queries=noop_rate,
        duplicate_rate=dup_rate,
        cross_session_update_detection=stale_rate,
        schema_validation_rate=1.0 - (parse_errors / total),
    )


# ---------------------------------------------------------------------------
# Report rendering — pure
# ---------------------------------------------------------------------------

def render_markdown_report(report: BaselineReport, corpus_path: Path, run_started: datetime) -> str:
    """Render the baseline report as the markdown the plan asks operators to publish."""
    lines: list[str] = []
    lines.append(f"# Memory v1 Baseline — {run_started.strftime('%Y-%m-%d %H:%M UTC')}")
    lines.append("")
    lines.append(f"Corpus: `{corpus_path}`  ")
    lines.append(f"Total turns: **{report.total_turns}**")
    lines.append("")
    lines.append("## The four locked baselines (v2 must beat these)")
    lines.append("")
    lines.append("| Metric | v1 measured | Target for v2 | Pass? |")
    lines.append("|---|---|---|---|")
    lines.append(
        f"| NOOP rate on generic queries | {report.noop_rate_on_generic_queries:.3f} | ≥ 0.950 | "
        f"{'✓' if report.noop_rate_on_generic_queries >= 0.95 else '✗ (gap to close in v2)'} |"
    )
    lines.append(
        f"| Duplicate rate (dedup turns ADD-ing wrongly) | {report.duplicate_rate:.3f} | ≤ 0.010 | "
        f"{'✓' if report.duplicate_rate <= 0.01 else '✗ (gap to close in v2)'} |"
    )
    lines.append(
        f"| Cross-session UPDATE detection | {report.cross_session_update_detection:.3f} | ≥ 0.800 | "
        f"{'✓' if report.cross_session_update_detection >= 0.80 else '✗ (gap to close in v2)'} |"
    )
    lines.append(
        f"| Schema-validation rate (JSON parseable) | {report.schema_validation_rate:.3f} | ≥ 0.950 | "
        f"{'✓' if report.schema_validation_rate >= 0.95 else '✗ (gap to close in v2)'} |"
    )
    lines.append("")
    lines.append("## Latency")
    lines.append("")
    lines.append(f"- p50: {report.latency_p50:.2f}s")
    lines.append(f"- p95: {report.latency_p95:.2f}s")
    lines.append(f"- p99: {report.latency_p99:.2f}s")
    lines.append("")
    lines.append("## Outcome distribution")
    lines.append("")
    for outcome, count in sorted(report.outcome_distribution.items()):
        lines.append(f"- {outcome}: {count}")
    lines.append("")
    lines.append("## Turns by category")
    lines.append("")
    for cat, count in sorted(report.turns_by_category.items()):
        lines.append(f"- {cat}: {count}")
    lines.append("")
    lines.append("## v2 quality bar")
    lines.append("")
    lines.append(
        "v2 ships only when ALL four baselines are at least met (≥ on improvement, "
        "≤ on regression) AND v2 is strictly better on at least one. See "
        "`docs/architecture/memory-architecture-plan.md` → Eng-review-modifications → "
        "v1 disposition for the Phase A shadow-mode protocol."
    )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Integration runner — requires DB + LLM access (NOT exercised by unit tests)
# ---------------------------------------------------------------------------

async def run_one_turn(
    turn: dict[str, Any],
    db_session,
    service,
    allow_cross_tier: bool = False,
    use_v2: bool = False,
) -> BaselineResult:
    """Execute one corpus turn against the v1 or v2 extract path.

    Caller is responsible for transaction management: the runner
    operates entirely inside `db_session.begin()`, so the caller wraps
    each turn and commits-or-rolls-back per its --commit flag.

    Imports are inside the function so the module remains importable
    without a backend env (the pure aggregation paths covered by unit
    tests do not exercise this function).
    """
    from models.database import ConversationMemory

    expected = turn.get("expected_v1_outcome", "NOOP")
    if expected not in VALID_OUTCOMES:
        raise ValueError(
            f"Turn {turn['id']}: expected_v1_outcome={expected!r} not in {VALID_OUTCOMES}"
        )
    if turn.get("category") not in VALID_CATEGORIES:
        raise ValueError(
            f"Turn {turn['id']}: category={turn.get('category')!r} not in {VALID_CATEGORIES}"
        )

    # 1. Allocate test user_id in the reserved namespace (>= 2_000_000_000).
    #    Stable across processes via hashlib — see stable_test_user_id docstring.
    test_user_id = stable_test_user_id(turn["id"])

    # 2. Seed preexisting memories via the canonical service.save() so the
    #    Wissensbasis longitudinal atom_id wiring + embeddings are handled
    #    automatically (the WIP branch has conversation_memories.atom_id
    #    NOT NULL with a FK to atoms.atom_id; direct INSERTs fail).
    #    Backdate last_accessed_at + created_at via a raw UPDATE after
    #    save() commits.
    #
    #    Restrict circle_tier to 0 unless --allow-cross-tier passed —
    #    a malicious or careless YAML edit could otherwise seed at tier 4
    #    (global) and expose to every household member via Circles v1.
    from sqlalchemy import update
    seeded_ids_to_backdate = []
    for pre in turn.get("preexisting_memories") or []:
        seed_tier = pre.get("circle_tier", 0)
        if seed_tier != 0 and not allow_cross_tier:
            raise ValueError(
                f"Turn {turn['id']}: preexisting memory has circle_tier={seed_tier} "
                f"but --allow-cross-tier was not passed. Cross-tier seeding is a "
                f"security-sensitive operation."
            )
        # Re-namespace owner_user_id — circle_leakage corpus turns reference
        # an "other user" via owner_user_id=99 in the YAML, which collides
        # with real user 99. stable_owner_user_id puts owners in a separate
        # band inside the test namespace.
        raw_owner = pre.get("owner_user_id")
        if raw_owner is None:
            owner_id = test_user_id
        else:
            owner_id = stable_owner_user_id(raw_owner)
        # Use the canonical save path. service.save() commits internally,
        # creates the atom row, generates the embedding, and writes the
        # MemoryHistory entry. We capture the resulting id for backdating.
        saved = await service.save(
            content=pre["content"],
            category=pre["category"],
            user_id=owner_id,
            importance=pre.get("importance", 0.5),
            source_session_id=f"baseline-seed-{turn['id']}",
        )
        if saved is None:
            raise RuntimeError(
                f"Turn {turn['id']}: service.save() returned None for seed "
                f"({pre['content']!r}, category={pre['category']!r}). "
                f"Likely an embedding failure or invalid category."
            )
        age_days = pre.get("age_days", 0)
        if age_days > 0:
            seeded_ids_to_backdate.append((saved.id, age_days))
        # circle_tier override (service.save defaults to tier=0; we need
        # tier>0 for some circle_leakage cases). Apply per-row via UPDATE.
        if seed_tier != 0:
            await db_session.execute(
                update(ConversationMemory)
                .where(ConversationMemory.id == saved.id)
                .values(circle_tier=seed_tier)
            )
            await db_session.commit()

    # Backdate seed timestamps so cross_session_stale + dedup turns
    # exercise the recency-decay path the way real prod data would.
    for seed_id, age_days in seeded_ids_to_backdate:
        seeded_at = datetime.now(UTC).replace(tzinfo=None) - timedelta(days=age_days)
        await db_session.execute(
            update(ConversationMemory)
            .where(ConversationMemory.id == seed_id)
            .values(last_accessed_at=seeded_at, created_at=seeded_at)
        )
    if seeded_ids_to_backdate:
        await db_session.commit()

    # 3. Snapshot existing rows BEFORE the extract call.
    from sqlalchemy import select
    before = (
        await db_session.execute(
            select(ConversationMemory.id, ConversationMemory.content)
            .where(ConversationMemory.user_id == test_user_id)
        )
    ).all()
    before_ids = {row.id for row in before}
    before_contents = {row.id: row.content for row in before}

    # 4. Reset per-turn instrumentation counters. Stored in a ContextVar
    # rather than a module-global dict so a future refactor that
    # parallelizes turns doesn't get cross-task counter writes.
    _current_instrumentation_counters.set({"parse_failures": 0, "embedding_failures": 0})

    # 5. Invoke extraction (v1 or v2 depending on use_v2).
    fallback = False
    started = time.monotonic()
    extract_method = service.extract_and_save_v2 if use_v2 else service.extract_and_save
    try:
        await extract_method(
            user_message=turn["user_message"],
            assistant_response=turn["assistant_response"],
            user_id=test_user_id,
            session_id=f"baseline-{turn['id']}",
            lang=turn.get("lang", "de"),
        )
    except Exception as e:
        fallback = True
        # Log only the exception type, not str(e) — exception messages
        # may embed user_message content (corpus role-injection strings,
        # potential PII) and we don't want that in long-term log storage.
        logging.warning("Turn %s raised: %s", turn["id"], type(e).__name__)
    elapsed = time.monotonic() - started

    # 6. Snapshot the instrumentation counters set by the
    # InstrumentedConversationMemoryService subclass.
    counters = _current_instrumentation_counters.get()
    parse_error = counters["parse_failures"] > 0
    embedding_error = counters["embedding_failures"] > 0

    # 7. Snapshot AFTER and diff.
    after = (
        await db_session.execute(
            select(ConversationMemory.id, ConversationMemory.content, ConversationMemory.is_active)
            .where(ConversationMemory.user_id == test_user_id)
        )
    ).all()
    after_active = {row.id for row in after if row.is_active}
    after_inactive = {row.id for row in after if not row.is_active}
    new_ids = after_active - before_ids
    # Soft-delete only: rows missing from `after_active` AND present
    # in `after_inactive`. A row that disappeared from `after` entirely
    # (hard-deleted) is NOT a v1 outcome we expect — flag it via the
    # extracted_count delta in the result so reviewers notice.
    soft_deleted_ids = (before_ids - after_active) & after_inactive
    hard_deleted_ids = (before_ids - after_active) - after_inactive
    if hard_deleted_ids:
        logging.warning(
            "Turn %s: %d row(s) hard-deleted (expected soft-delete via is_active=false). "
            "Indicates v1 misbehavior or a fixture inconsistency.",
            turn["id"], len(hard_deleted_ids),
        )
    changed_content = {
        row.id for row in after
        if row.id in before_ids and row.content != before_contents.get(row.id)
    }

    # 8. Classify outcome.
    if fallback:
        actual = "FALLBACK"
    elif soft_deleted_ids:
        actual = "DELETE"
    elif changed_content:
        actual = "UPDATE"
    elif new_ids:
        actual = "ADD"
    else:
        actual = "NOOP"

    return BaselineResult(
        turn_id=turn["id"],
        category=turn["category"],
        flavor=turn["flavor"],
        expected_outcome=expected,
        actual_outcome=actual,
        extracted_count=len(new_ids) + len(soft_deleted_ids) + len(changed_content),
        latency_seconds=elapsed,
        parse_error=parse_error,
        embedding_error=embedding_error,
        notes=turn.get("notes", ""),
    )


async def ensure_baseline_users(db_session, corpus: list[dict]) -> None:
    """Create the User rows the corpus will reference.

    The runner allocates IDs via stable_test_user_id() /
    stable_owner_user_id() in the >= 2_000_000_000 namespace. Those IDs
    used to live only on memory rows where the FK to users.id didn't
    exist. Since `atoms.owner_user_id` became `FK NOT NULL → users.id`,
    every referenced ID must resolve to a real users row before any
    extract path can write an atom.

    Mirrors the `tests/backend/conftest.py::test_user` pattern: direct
    `User(...)` ORM write with a static fake bcrypt hash, role pulled
    from `auth_service.ensure_default_roles` (the same seeder the
    backend lifecycle uses at startup). Pre-check via `WHERE id IN (...)`
    makes the call idempotent across re-runs against the same DB.

    Defense in depth:
      - `is_active=False` — baseline users must never authenticate.
        The static fake hash is also structurally invalid so
        `verify_password` returns False; setting is_active=False makes
        the intent explicit and removes the dependency on PassLib's
        behavior for malformed hashes.
      - `role_id = Gast` — least-privilege role. A leaked baseline user
        carries no admin / settings / MCP grants even if the
        is_active guard ever drifted.

    Caller note: pass the FULL corpus (unfiltered) so seeding stays
    idempotent across filter combinations. Seeding only the
    filter-narrowed subset would cause FK violations on a later
    unfiltered run against the same DB.
    """
    from models.database import User
    from services.auth_service import ensure_default_roles
    from sqlalchemy import select

    roles = await ensure_default_roles(db_session)
    gast_role_id = next(r.id for r in roles if r.name == "Gast")

    needed_ids: set[int] = set()
    for turn in corpus:
        needed_ids.add(stable_test_user_id(turn["id"]))
        for pre in turn.get("preexisting_memories") or []:
            raw_owner = pre.get("owner_user_id")
            if raw_owner is not None:
                needed_ids.add(stable_owner_user_id(raw_owner))

    existing = set(
        (
            await db_session.execute(
                select(User.id).where(User.id.in_(needed_ids))
            )
        ).scalars()
    )
    to_create = needed_ids - existing

    for uid in sorted(to_create):
        db_session.add(
            User(
                id=uid,
                username=f"baseline-test-{uid}",
                password_hash=_BASELINE_FAKE_PASSWORD_HASH,
                role_id=gast_role_id,
                is_active=False,
                preferred_language="de",
            )
        )
    await db_session.commit()
    logging.info(
        "Baseline users ensured: %d referenced, %d existed, %d created.",
        len(needed_ids), len(existing), len(to_create),
    )


async def run_corpus(
    corpus_path: Path,
    category_filter: Optional[str],
    flavor_filter: Optional[str],
    commit: bool = False,
    allow_cross_tier: bool = False,
    allow_prod: bool = False,
    use_v2: bool = False,
) -> list[BaselineResult]:
    """Run the full corpus end-to-end. Requires backend env (DB + LLM).

    Safety contract — gated on BASELINE_DATABASE_URL env var:
      - Refuses to run if BASELINE_DATABASE_URL is unset (no fallback
        to DATABASE_URL: too dangerous; the runner must be operated
        with intent).
      - Refuses to run if BASELINE_DATABASE_URL matches a prod URL
        pattern, unless `allow_prod` is True.
      - Default mode is ROLLBACK per turn: writes are NOT persisted.
        Pass `commit=True` only for runs against a fully dedicated
        test DB.
      - Default refuses circle_tier>0 seeds in the corpus YAML to
        prevent a malicious or careless YAML edit from leaking
        cross-tier content. Pass `allow_cross_tier=True` to override.
    """
    # Import backend modules lazily so this script can be unit-tested
    # without a full backend env.
    BACKEND_ROOT = Path(__file__).resolve().parent.parent / "src" / "backend"
    if str(BACKEND_ROOT) not in sys.path:
        sys.path.insert(0, str(BACKEND_ROOT))

    try:
        import yaml
    except ImportError:
        sys.exit("pyyaml not installed (only needed for `run_corpus`). pip install pyyaml")

    from services.conversation_memory_service import ConversationMemoryService
    InstrumentedService = _make_instrumented_service_class(ConversationMemoryService)

    # Read the dedicated baseline DB URL. We deliberately do NOT fall
    # back to settings.database_url — the operator must opt in via
    # BASELINE_DATABASE_URL so a stray invocation on a developer shell
    # with prod credentials sourced cannot write to prod.
    db_url = os.environ.get("BASELINE_DATABASE_URL")
    if not db_url:
        sys.exit(
            "BASELINE_DATABASE_URL is not set. The baseline runner requires a "
            "dedicated test-DB URL distinct from the production DATABASE_URL. "
            "Set it to your test-postgres connection string before retrying."
        )
    # Defense against the operator copy-pasting their prod URL into
    # BASELINE_DATABASE_URL "for quick testing" — adversarial review F14.
    prod_url = os.environ.get("DATABASE_URL")
    if prod_url and db_url == prod_url and not allow_prod:
        sys.exit(
            "Refusing to run: BASELINE_DATABASE_URL is identical to DATABASE_URL. "
            "The two env vars exist precisely to be different. Set BASELINE_DATABASE_URL "
            "to your test-postgres URL (NOT your production one)."
        )
    refusal = check_database_url_safety(db_url, allow_prod=allow_prod)
    if refusal:
        sys.exit(refusal)

    from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
    engine = create_async_engine(db_url, echo=False)
    Session = async_sessionmaker(engine, expire_on_commit=False)

    with open(corpus_path) as f:
        full_corpus = yaml.safe_load(f)["corpus"]

    # Setup phase: seed users for the FULL corpus, NOT the filter-narrowed
    # subset. A `--category foo` run followed by an unfiltered run on the
    # same DB would otherwise FK-fail on every turn whose user wasn't in
    # the first filter — `ensure_baseline_users` is idempotent only
    # across the same set of IDs. Seeding the full corpus keeps the
    # contract "any subsequent run against this DB just works."
    async with Session() as setup_session:
        await ensure_baseline_users(setup_session, full_corpus)

    corpus = full_corpus
    if category_filter:
        corpus = [t for t in corpus if t["category"] == category_filter]
    if flavor_filter:
        corpus = [t for t in corpus if t["flavor"] == flavor_filter]

    results: list[BaselineResult] = []
    for turn in corpus:
        # Each turn runs in its own session AND its own transaction.
        # Default is rollback (commit=False): seeded rows + any v1
        # writes are abandoned at end-of-turn. The LLM call still
        # happens for real (it's outside the DB transaction), so
        # latency/extract behavior is measured correctly — we just
        # don't persist the side effects.
        async with Session() as session:
            # InstrumentedConversationMemoryService is a per-turn instance.
            # No class-level monkey patches; nothing leaks across turns.
            service = InstrumentedService(session)
            # Transaction-management pattern: DO NOT use
            # `async with session.begin():` here. SQLAlchemy 2 async's
            # AsyncSessionTransaction.__aexit__ will commit/rollback on
            # its own at context exit; calling `session.rollback()`
            # explicitly inside that context closes the transaction and
            # the __aexit__ then raises InvalidRequestError on the
            # closed transaction. Adversarial review F4 caught this in
            # theory; an actual cuda.local run surfaced it in practice.
            # Pattern: manual commit/rollback at the right moments.
            try:
                result = await run_one_turn(
                    turn, session, service,
                    allow_cross_tier=allow_cross_tier,
                    use_v2=use_v2,
                )
                if commit:
                    await session.commit()
                else:
                    await session.rollback()
            except Exception as e:
                # Any exception from run_one_turn (DB constraint, LLM
                # failure, etc.) rolls back this turn. Other turns
                # proceed normally with fresh sessions.
                logging.exception(
                    "Turn %s failed catastrophically: %s",
                    turn["id"], type(e).__name__,
                )
                try:
                    await session.rollback()
                except Exception:
                    pass  # session may already be in error state
                result = BaselineResult(
                    turn_id=turn["id"],
                    category=turn.get("category", "unknown"),
                    flavor=turn.get("flavor", "unknown"),
                    expected_outcome=turn.get("expected_v1_outcome", "NOOP"),
                    actual_outcome="FALLBACK",
                    extracted_count=0,
                    latency_seconds=0.0,
                    parse_error=False,
                    embedding_error=False,
                    notes=f"runner exception: {type(e).__name__}",
                )
            results.append(result)

    await engine.dispose()
    return results


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _serialize_results(results: list[BaselineResult]) -> list[dict]:
    return [dataclasses.asdict(r) for r in results]


def _deserialize_results(raw: list[dict]) -> list[BaselineResult]:
    return [BaselineResult(**r) for r in raw]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--corpus",
        type=Path,
        default=Path(__file__).resolve().parent.parent / "tests" / "eval" / "memory_v1_baseline_corpus.yaml",
        help="Path to the corpus YAML (default: the in-repo baseline corpus).",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("./baseline-runs"),
        help="Where to write the JSON + markdown report (default: ./baseline-runs/).",
    )
    parser.add_argument("--category", help="Run only turns in this category.")
    parser.add_argument("--flavor", choices=["reva", "renfield"], help="Run only turns of this flavor.")
    parser.add_argument(
        "--aggregate-only",
        type=Path,
        help="Skip the corpus run; read an existing JSON and regenerate the markdown report only.",
    )
    parser.add_argument(
        "--commit",
        action="store_true",
        help=(
            "Persist seeded rows + any v1 writes. Default is ROLLBACK per turn — "
            "the LLM call happens for real (so latency/extract behavior is measured) "
            "but no rows are committed. Pass --commit ONLY against a fully dedicated "
            "test DB where leaking rows is acceptable."
        ),
    )
    parser.add_argument(
        "--allow-cross-tier",
        action="store_true",
        help=(
            "Permit preexisting_memories with circle_tier > 0 in the corpus. "
            "Default refuses (a malicious / careless YAML edit could otherwise "
            "seed at tier 4 / global and expose to all household members)."
        ),
    )
    parser.add_argument(
        "--i-know-this-is-prod",
        action="store_true",
        dest="allow_prod",
        help=(
            "Override the BASELINE_DATABASE_URL prod-pattern check. The runner "
            "refuses to run against URLs containing 'prod', '.cluster.local', "
            "etc. — use this flag only for deliberate forensic re-runs."
        ),
    )
    parser.add_argument(
        "--use-v2",
        action="store_true",
        help=(
            "Run the Mem0 v2 extractor (extract_and_save_v2) instead of v1. "
            "Lets you produce a v2-vs-v1 side-by-side report against the same "
            "corpus. v2 falls back to v1 on any LLM / schema / drift failure, "
            "so partial-v2 results show through as v1 outcomes."
        ),
    )
    args = parser.parse_args()

    args.output_dir.mkdir(parents=True, exist_ok=True)
    run_started = datetime.now(UTC)
    timestamp = run_started.strftime("%Y%m%d-%H%M%S")

    if args.aggregate_only:
        with open(args.aggregate_only) as f:
            results = _deserialize_results(json.load(f)["results"])
    else:
        results = asyncio.run(
            run_corpus(
                args.corpus,
                args.category,
                args.flavor,
                commit=args.commit,
                allow_cross_tier=args.allow_cross_tier,
                allow_prod=args.allow_prod,
                use_v2=args.use_v2,
            )
        )

    report = aggregate_baseline_metrics(results)
    md = render_markdown_report(report, args.corpus, run_started)

    json_path = args.output_dir / f"memory_v1_baseline_{timestamp}.json"
    md_path = args.output_dir / f"memory_v1_baseline_{timestamp}.md"

    with open(json_path, "w") as f:
        # No `default=str` — let json.dump raise on unexpected types so
        # schema mismatches (e.g. a future dataclass field becoming
        # datetime / Path) surface loudly instead of silently coercing.
        json.dump(
            {
                "run_started": run_started.isoformat(),
                "corpus_path": str(args.corpus),
                "report": dataclasses.asdict(report),
                "results": _serialize_results(results),
            },
            f,
            indent=2,
        )

    with open(md_path, "w") as f:
        f.write(md)

    print(f"Wrote {json_path}")
    print(f"Wrote {md_path}")
    print()
    print(md)
    return 0


if __name__ == "__main__":
    sys.exit(main())
