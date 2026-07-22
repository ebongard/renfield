#!/usr/bin/env python3
"""§2 Track A calibration spike — the speaker-fingerprint go/no-go gate.

Cross-meeting speaker identity (docs/design/meeting-kg-and-speaker-identity.md,
Track A) hinges on ONE empirical question: do a person's ECAPA centroids from
DIFFERENT meetings sit closer (cosine) to each other than to OTHER people's
centroids, by a usable margin? The prior spike stalled on SYNTHETIC audio
(insufficient separation). This harness answers it on REAL, human-relabeled
meeting data before any of the fingerprint table / matching / merge code is
trusted.

It is READ-ONLY: it reads completed meetings' ``segments`` (each carries a
per-segment ECAPA ``embedding[192]`` + the human ``speaker`` label after
relabel), builds one centroid per (meeting, person), and reports the
same-person-across-meetings vs different-person cosine distributions plus a
suggested threshold at the equal-error operating point.

GO/NO-GO criterion (design D-A1): a usable regime needs a clear gap between the
intra-person and inter-person cosine distributions — concretely
``margin = intra_p05 - inter_p95 >= --min-margin`` (default 0.05) AND at least
``--min-pairs`` intra-person cross-meeting pairs to be statistically meaningful.
PASS => build Track A on the printed threshold; FAIL/INSUFFICIENT => keep
attribution unattributed (the design's graceful-degrade escape hatch, open-q #1).

Usage (run inside the backend pod / .159 container, real Postgres):
    PYTHONPATH=src/backend python bin/run_fingerprint_calibration.py \
        [--owner-id N] [--min-margin 0.05] [--min-pairs 20] [--json]

No audio files needed — it works off already-transcribed + relabeled meetings.
If too few people appear in ≥2 meetings it reports INSUFFICIENT (not FAIL): the
answer is "collect more relabeled meetings", not "abandon Track A".
"""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import sys
from collections import defaultdict
from itertools import combinations

# Pseudonym labels are NOT identities — a person is only usable ground truth
# once a human has relabeled the cluster to a real name.
_PSEUDONYM_PREFIXES = ("Sprecher ", "Speaker ")


def _is_relabeled(label: str | None) -> bool:
    if not label:
        return False
    return not any(label.startswith(p) for p in _PSEUDONYM_PREFIXES)


def _unit(vec: list[float]) -> list[float] | None:
    n = math.sqrt(sum(x * x for x in vec))
    if n <= 1e-9:
        return None
    return [x / n for x in vec]


def _mean_centroid(embeddings: list[list[float]]) -> list[float] | None:
    """Unit-norm each embedding, average, re-normalize (raw ECAPA norms vary, so
    averaging raw would let loud/long turns dominate — mirrors speaker_resolver)."""
    units = [u for e in embeddings if (u := _unit(e)) is not None]
    if not units:
        return None
    dim = len(units[0])
    acc = [0.0] * dim
    for u in units:
        if len(u) != dim:
            continue
        for i, x in enumerate(u):
            acc[i] += x
    return _unit(acc)


def _cosine(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b))  # both unit-norm => dot = cosine


def _percentile(sorted_vals: list[float], p: float) -> float:
    if not sorted_vals:
        return float("nan")
    k = (len(sorted_vals) - 1) * p
    lo, hi = math.floor(k), math.ceil(k)
    if lo == hi:
        return sorted_vals[int(k)]
    return sorted_vals[lo] * (hi - k) + sorted_vals[hi] * (k - lo)


async def _load_person_centroids(owner_id: int | None):
    """Return {person_label: [(meeting_id, centroid), ...]} from completed,
    human-relabeled meetings. Owner-scoped when owner_id is given."""
    from sqlalchemy import select

    from models.database import Meeting
    from services.database import AsyncSessionLocal

    per_meeting_person: dict[tuple[int, str], list[list[float]]] = defaultdict(list)
    async with AsyncSessionLocal() as db:
        stmt = select(Meeting).where(Meeting.status == "completed")
        if owner_id is not None:
            stmt = stmt.where(Meeting.owner_user_id == owner_id)
        for mtg in (await db.execute(stmt)).scalars():
            for seg in (mtg.segments or []):
                label = seg.get("speaker")
                emb = seg.get("embedding")
                if not _is_relabeled(label) or not emb:
                    continue
                per_meeting_person[(mtg.id, label)].append(emb)

    centroids: dict[str, list[tuple[int, list[float]]]] = defaultdict(list)
    for (meeting_id, label), embs in per_meeting_person.items():
        c = _mean_centroid(embs)
        if c is not None:
            centroids[label].append((meeting_id, c))
    return centroids


def _evaluate(centroids, min_margin: float, min_pairs: int) -> dict:
    # Intra: same person, DIFFERENT meetings. Inter: different people (any meeting).
    intra: list[float] = []
    for _label, items in centroids.items():
        for (m1, c1), (m2, c2) in combinations(items, 2):
            if m1 != m2:
                intra.append(_cosine(c1, c2))

    flat = [(label, c) for label, items in centroids.items() for (_m, c) in items]
    inter: list[float] = []
    for (l1, c1), (l2, c2) in combinations(flat, 2):
        if l1 != l2:
            inter.append(_cosine(c1, c2))

    intra.sort()
    inter.sort()
    result = {
        "people": len(centroids),
        "people_multi_meeting": sum(1 for v in centroids.values() if len({m for m, _ in v}) >= 2),
        "intra_pairs": len(intra),
        "inter_pairs": len(inter),
    }
    if len(intra) < min_pairs or not inter:
        result["verdict"] = "INSUFFICIENT"
        result["reason"] = (
            f"need >= {min_pairs} same-person cross-meeting pairs (have {len(intra)}) "
            f"and >=1 inter-person pair (have {len(inter)}); collect more relabeled meetings"
        )
        return result

    intra_p05 = _percentile(intra, 0.05)
    inter_p95 = _percentile(inter, 0.95)
    margin = intra_p05 - inter_p95
    # Equal-error threshold: sweep candidate thresholds, pick where the
    # false-accept rate (inter above t) ≈ false-reject rate (intra below t).
    cands = sorted(set(intra + inter))
    best_t, best_gap = float("nan"), float("inf")
    for t in cands:
        far = sum(1 for v in inter if v >= t) / len(inter)
        frr = sum(1 for v in intra if v < t) / len(intra)
        if abs(far - frr) < best_gap:
            best_gap, best_t, eer = abs(far - frr), t, (far + frr) / 2

    result.update({
        "intra_mean": sum(intra) / len(intra),
        "intra_p05": intra_p05,
        "inter_mean": sum(inter) / len(inter),
        "inter_p95": inter_p95,
        "margin": margin,
        "suggested_threshold": best_t,
        "eer": eer,
        "verdict": "PASS" if margin >= min_margin else "FAIL",
        "criterion": f"margin(intra_p05 - inter_p95) >= {min_margin}",
    })
    return result


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--owner-id", type=int, default=None, help="scope to one owner (else all)")
    ap.add_argument("--min-margin", type=float, default=0.05)
    ap.add_argument("--min-pairs", type=int, default=20)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    centroids = await _load_person_centroids(args.owner_id)
    result = _evaluate(centroids, args.min_margin, args.min_pairs)

    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print("=== §2 Track A fingerprint calibration spike ===")
        for k, v in result.items():
            print(f"  {k}: {round(v, 4) if isinstance(v, float) else v}")
        print(f"\nGO/NO-GO: {result['verdict']}")
    # Non-zero exit only on a hard FAIL, so CI/automation can gate on it.
    return 1 if result.get("verdict") == "FAIL" else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
