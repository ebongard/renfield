"""Cross-meeting ANONYMOUS speaker fingerprints (§2 redesign Track A).

On a completed meeting, resolve each diarized cluster's ECAPA centroid to a
stable, owner+tier-scoped anonymous fingerprint ("Speaker A1B2"): match an
existing fingerprint by cosine ≥ threshold (with a margin over the runner-up),
else mint a new one. This gives *the same anonymous speaker across meetings*
WITHOUT claiming who they are — enrolling a real name onto a fingerprint
(merge-on-enroll) is a separate, later step.

Gated on ``settings.meeting_fingerprints_enabled`` (dark). The calibration
go/no-go PASSED on public AMI audio (16 speakers, margin 0.33, EER 0.0018,
threshold ~0.48); the default match threshold errs CONSERVATIVE — a false merge
silently conflates two people (the magnet-hub failure class), so we prefer a
split (two fingerprints for one person; a human can merge later).

Matching runs in Python over the owner+tier's fingerprints (a small set — the
distinct people across one owner's meetings), reading ``centroid_b64`` (the
dialect-agnostic source of truth, so this works identically on the sqlite test
harness and Postgres). The pgvector ``centroid`` HNSW column is kept in sync for
a future scale-out but is not the matching path yet.
"""
from __future__ import annotations

import secrets

import numpy as np
from loguru import logger
from sqlalchemy import select

from models.database import PGVECTOR_AVAILABLE, MeetingSpeakerFingerprint
from services.speaker_service import SpeakerService
from utils.config import settings

_DIM = 192


def _unit(vec: np.ndarray) -> np.ndarray | None:
    n = float(np.linalg.norm(vec))
    return (vec / n) if n > 1e-9 else None


def _cosine(a: np.ndarray, b: np.ndarray) -> float:
    return float(np.dot(a, b))  # inputs are pre-unit-normed


def _cluster_centroids(raw_segments: list[dict]) -> dict[str, np.ndarray]:
    """One unit-normed centroid per diarization cluster. The voice-server emits a
    single per-cluster ECAPA embedding duplicated across that cluster's segments,
    so the first non-empty embedding per cluster IS the centroid."""
    out: dict[str, np.ndarray] = {}
    for seg in raw_segments:
        key = str(seg.get("speaker", "SPEAKER_?"))
        if key in out:
            continue
        emb = seg.get("embedding")
        if not emb:
            continue
        arr = np.asarray(emb, dtype=np.float32)
        if arr.shape != (_DIM,):
            continue
        u = _unit(arr)
        if u is not None:
            out[key] = u
    return out


def _generate_label(existing: set[str]) -> str:
    """A short, human-distinguishable anonymous label unique among ``existing``
    (the owner's labels). "Speaker" + 4 hex chars (65 536 space; collisions are
    re-rolled)."""
    for _ in range(20):
        candidate = f"Speaker {secrets.token_hex(2).upper()}"
        if candidate not in existing:
            return candidate
    # Astronomically unlikely; fall back to a longer token.
    return f"Speaker {secrets.token_hex(4).upper()}"


def _set_centroid(fp: MeetingSpeakerFingerprint, unit_centroid: np.ndarray) -> None:
    fp.centroid_b64 = SpeakerService.embedding_to_base64(unit_centroid.astype(np.float32))
    if PGVECTOR_AVAILABLE:
        # Keep the HNSW search copy in sync (Postgres only; Text column on sqlite).
        fp.centroid = unit_centroid.astype(np.float32).tolist()


def _fold_centroid(fp: MeetingSpeakerFingerprint, new_unit: np.ndarray) -> None:
    """Running-mean update of the fingerprint centroid with a newly-matched
    cluster, re-unit-normed. Weighted by how many clusters already folded in."""
    old = SpeakerService.embedding_from_base64(fp.centroid_b64).astype(np.float32)
    n = max(1, int(fp.sample_count or 1))
    merged = _unit((old * n + new_unit) / (n + 1))
    if merged is not None:
        _set_centroid(fp, merged)
    fp.sample_count = n + 1


async def resolve_meeting_fingerprints(db, meeting, raw_segments: list[dict]) -> dict[str, dict]:
    """Resolve each diarized cluster to a cross-meeting anonymous fingerprint.

    Returns ``{cluster_key: {"fingerprint_id": int, "fingerprint_label": str}}``.
    Mutates/creates ``MeetingSpeakerFingerprint`` rows in ``db`` (the caller
    commits). No-op returning ``{}`` when disabled or no usable embeddings.

    Scope: owner (``meeting.owner_user_id``) AND ``meeting.circle_tier`` — a
    person appearing in two different-tier meetings gets two fingerprints (a
    visibility-safe split; cross-tier unification is deferred to merge-on-enroll).
    """
    if not settings.meeting_fingerprints_enabled:
        return {}
    centroids = _cluster_centroids(raw_segments)
    if not centroids:
        return {}

    owner_id = meeting.owner_user_id
    tier = int(getattr(meeting, "circle_tier", 2) or 2)
    threshold = float(settings.meeting_fingerprint_match_threshold)
    margin = float(settings.meeting_fingerprint_match_margin)

    existing: list[MeetingSpeakerFingerprint] = list(
        (
            await db.execute(
                select(MeetingSpeakerFingerprint).where(
                    MeetingSpeakerFingerprint.owner_user_id == owner_id,
                    MeetingSpeakerFingerprint.circle_tier == tier,
                )
            )
        )
        .scalars()
        .all()
    )
    # Decode once; keep (fp, unit_centroid). Skip corrupt rows defensively.
    candidates: list[tuple[MeetingSpeakerFingerprint, np.ndarray]] = []
    for fp in existing:
        try:
            u = _unit(SpeakerService.embedding_from_base64(fp.centroid_b64).astype(np.float32))
        except Exception:  # noqa: BLE001 — a corrupt centroid must not sink matching
            u = None
        if u is not None and u.shape == (_DIM,):
            candidates.append((fp, u))

    labels = {fp.label for fp in existing}
    resolved: dict[str, dict] = {}
    # Two clusters in ONE meeting are distinct people (diarization separated
    # them) — a fingerprint matched by one cluster is excluded for the others.
    used_ids: set[int] = set()

    # Deterministic order so a run is reproducible.
    for cluster_key in sorted(centroids):
        query = centroids[cluster_key]
        best = second = None  # (score, fp, unit)
        for fp, unit in candidates:
            if fp.id in used_ids:
                continue
            score = _cosine(query, unit)
            if best is None or score > best[0]:
                best, second = (score, fp, unit), best
            elif second is None or score > second[0]:
                second = (score, fp, unit)

        matched = (
            best is not None
            and best[0] >= threshold
            and (second is None or best[0] - second[0] >= margin)
        )
        if matched:
            fp = best[1]
            _fold_centroid(fp, query)
            used_ids.add(fp.id)
            resolved[cluster_key] = {"fingerprint_id": fp.id, "fingerprint_label": fp.label}
            logger.debug(
                "meeting %s cluster %s → fingerprint %s (%.3f)",
                meeting.id, cluster_key, fp.label, best[0],
            )
        else:
            label = _generate_label(labels)
            labels.add(label)
            fp = MeetingSpeakerFingerprint(
                owner_user_id=owner_id, label=label, sample_count=1, circle_tier=tier,
            )
            _set_centroid(fp, query)
            db.add(fp)
            await db.flush()  # assign fp.id for the segment annotation + used set
            used_ids.add(fp.id)
            candidates.append((fp, query))  # a later cluster can't re-match it (used_ids)
            resolved[cluster_key] = {"fingerprint_id": fp.id, "fingerprint_label": fp.label}
            logger.debug("meeting %s cluster %s → NEW fingerprint %s", meeting.id, cluster_key, label)

    return resolved


def annotate_segments(segments: list[dict], resolved: dict[str, dict]) -> None:
    """Ride the resolved fingerprint identity onto each segment (in place), keyed
    by its ``speaker_key`` (the diarization cluster id). The display ``speaker``
    pseudonym is untouched — the fingerprint is the cross-meeting identity."""
    for seg in segments:
        info = resolved.get(str(seg.get("speaker_key", "")))
        if info:
            seg["fingerprint_id"] = info["fingerprint_id"]
            seg["fingerprint_label"] = info["fingerprint_label"]
