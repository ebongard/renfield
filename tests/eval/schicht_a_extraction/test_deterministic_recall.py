"""Schicht A — deterministic-identifier recall on synthetic cases (CI-runnable).

Recall is the safety axis (a MISSED identifier is the real harm). This harness
scores the deterministic layer against the committed synthetic cases; the LLM
obligation eval runs locally against the real golden set (see synthetic_cases.yaml
header). No model, no private data — safe in CI.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import yaml

for _mod in ("asyncpg", "whisper", "piper", "piper.voice", "speechbrain",
             "speechbrain.inference", "speechbrain.inference.speaker",
             "openwakeword", "openwakeword.model"):
    if _mod not in sys.modules:
        try:
            __import__(_mod)
        except Exception:  # noqa: BLE001
            sys.modules[_mod] = MagicMock()

from services.schicht_a_extractor import extract_identifiers  # noqa: E402

_CASES = yaml.safe_load(
    (Path(__file__).parent / "synthetic_cases.yaml").read_text(encoding="utf-8")
)["cases"]


@pytest.mark.parametrize("case", _CASES, ids=[c["id"] for c in _CASES])
def test_identifier_recall(case):
    """Every expected identifier must be recovered (recall == 1.0), and the
    no-identifier case must stay empty (no false positives on clean prose)."""
    got = extract_identifiers(case["field_text"])
    got_pairs = {(f.kind, f.normalized_value) for f in got}
    for want in case["expect_identifiers"]:
        assert (want["kind"], want["normalized_value"]) in got_pairs, (
            f"{case['id']}: missed {want['kind']}={want['normalized_value']!r}; "
            f"got {got_pairs}"
        )
    if not case["expect_identifiers"]:
        assert not [f for f in got if f.kind in ("steuernummer", "iban")]


def test_aggregate_recall_meets_bar():
    """Aggregate deterministic recall across the synthetic set must be perfect —
    these are the clean cases; the real-fixture bar (>=0.90) lives local."""
    expected = matched = 0
    for case in _CASES:
        got = {(f.kind, f.normalized_value) for f in extract_identifiers(case["field_text"])}
        for want in case["expect_identifiers"]:
            expected += 1
            if (want["kind"], want["normalized_value"]) in got:
                matched += 1
    recall = matched / expected if expected else 1.0
    assert recall == 1.0, f"deterministic identifier recall {recall:.2f} < 1.0"
