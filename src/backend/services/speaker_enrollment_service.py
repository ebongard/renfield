"""Controlled speaker enrollment (Phase 1).

A DELIBERATE, quality-gated enrollment that builds one trusted reference profile
per person — the antidote to ambient auto-enroll polluting profiles from noisy
far-field turns (see docs/design/speaker-enrollment-redesign.md, and the noise-
floor diagnosis: same-speaker cosine ~0.28 near the different-speaker floor).

Load-bearing: embeddings are computed by the **voice-server ONNX** model (the same
one used at inference), NOT the backend SpeechBrain path — enrolling via a
different ECAPA model would put the reference in a space that never matches live
turns.

Gates (all must pass, else the enrollment is REJECTED, nothing stored):
  1. each sample >= ``speaker_enroll_min_duration_s`` of audio AND yields an
     embedding,
  2. >= ``speaker_enroll_min_samples`` usable samples,
  3. the samples mutually COHERE — mean pairwise cosine >= ``speaker_enroll_min_
     cohesion`` (a low value means noisy captures or multiple speakers → a
     polluted profile at the source; reject and re-record).
"""
from __future__ import annotations

import re
from typing import Any

import numpy as np
from loguru import logger
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from models.database import Speaker, SpeakerCandidate, SpeakerEmbedding, User
from services.speaker_service import get_speaker_service
from utils.config import settings


# Defense in depth behind the route gate: with speaker recognition off no
# voiceprint (SpeakerEmbedding) is ever written, whoever calls these functions.
_RECOGNITION_OFF_REASON = (
    "speaker recognition is disabled on this instance "
    "(SPEAKER_RECOGNITION_ENABLED=false) — no voiceprints are stored"
)


def _slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", name.strip().lower()).strip("_")
    return slug or "speaker"


def _l2(v: np.ndarray) -> np.ndarray:
    n = float(np.linalg.norm(v))
    return v / n if n > 0 else v


def _cohesion(embeddings: list[np.ndarray]) -> float:
    """Mean pairwise cosine of the (L2-normalized) sample embeddings.

    1 sample → 1.0 (trivially coherent); the ``min_samples`` gate is what
    actually requires enough evidence.
    """
    us = [_l2(e) for e in embeddings]
    if len(us) < 2:
        return 1.0
    sims = [
        float(np.dot(us[i], us[j]))
        for i in range(len(us))
        for j in range(i + 1, len(us))
    ]
    return float(np.mean(sims))


def _service_token() -> str:
    from services.auth_service import create_access_token

    return create_access_token({"sub": "service:enrollment", "scope": "voice"})


async def _embed_sample(audio_bytes: bytes, filename: str, token: str) -> tuple[np.ndarray | None, float | None, str | None]:
    """Return (embedding, duration_s, reject_reason) for one sample via the
    voice-server ONNX model. reject_reason is set when the sample is unusable."""
    from services.voice_server_client import VoiceServerError, stt

    try:
        result = await stt(audio_bytes, filename=filename, auth_token=token)
    except VoiceServerError as e:
        return None, None, f"voice-server error: {e}"

    emb = result.get("speaker_embedding")
    dur = result.get("audio_duration_s")
    if not emb:
        return None, dur, "no embedding (audio too short / silent?)"
    if dur is not None and dur < settings.speaker_enroll_min_duration_s:
        return None, dur, f"too short ({dur:.2f}s < {settings.speaker_enroll_min_duration_s}s)"
    arr = np.asarray(emb, dtype=np.float32)
    if not np.all(np.isfinite(arr)):
        # A NaN/inf embedding would slip past the cohesion gate (NaN < x is False)
        # and store a poisoned "trusted" profile — reject it here.
        return None, dur, "non-finite embedding"
    return arr, dur, None


async def enroll_speaker_controlled(
    db: AsyncSession,
    *,
    name: str,
    samples: list[tuple[bytes, str]],
    user_id: int | None = None,
    speaker_id: int | None = None,
) -> dict[str, Any]:
    """Enroll a named reference profile from multiple audio samples.

    ``samples`` = list of (audio_bytes, filename). ``speaker_id`` re-enrols into
    an EXISTING speaker (replaces its embeddings); otherwise a new enrolled
    speaker is created. ``user_id`` links the speaker to a user account (moving
    the link off any other speaker — a user maps to one speaker).

    Returns a result dict (never raises for a gate failure):
        {ok, reason?, speaker_id?, name, cohesion, accepted, rejected, sample_reasons}
    """
    if not settings.speaker_recognition_enabled:
        return {"ok": False, "reason": _RECOGNITION_OFF_REASON, "accepted": 0}
    name = (name or "").strip()
    if not name:
        return {"ok": False, "reason": "name is required", "accepted": 0}
    if not settings.voice_server_url:
        return {"ok": False, "reason": "voice-server not configured (enrollment needs the ONNX model)", "accepted": 0}

    # Validate the target user upfront (an unvalidated Form int would otherwise
    # no-op the link UPDATE and report a "successful" but unlinked enrollment).
    if user_id is not None:
        if (await db.execute(
            select(User.id).where(User.id == user_id)
        )).scalar_one_or_none() is None:
            return {"ok": False, "reason": f"user {user_id} not found", "accepted": 0}

    token = _service_token()
    embeddings: list[np.ndarray] = []
    durations: list[float | None] = []
    sample_reasons: list[str] = []
    for audio_bytes, filename in samples:
        emb, dur, reason = await _embed_sample(audio_bytes, filename or "sample.wav", token)
        if emb is None:
            sample_reasons.append(reason or "rejected")
        else:
            embeddings.append(emb)
            durations.append(dur)

    accepted = len(embeddings)
    rejected = len(samples) - accepted
    if accepted < settings.speaker_enroll_min_samples:
        return {
            "ok": False,
            "reason": (
                f"only {accepted} usable sample(s); need "
                f">= {settings.speaker_enroll_min_samples} (each "
                f">= {settings.speaker_enroll_min_duration_s}s of clear speech)"
            ),
            "accepted": accepted, "rejected": rejected, "sample_reasons": sample_reasons,
        }

    cohesion = _cohesion(embeddings)
    if not np.isfinite(cohesion) or cohesion < settings.speaker_enroll_min_cohesion:
        return {
            "ok": False,
            "reason": (
                f"samples don't cohere (mean cosine {cohesion:.2f} < "
                f"{settings.speaker_enroll_min_cohesion}) — likely noisy captures "
                f"or more than one voice; please re-record in a quieter setting"
            ),
            "accepted": accepted, "rejected": rejected, "cohesion": round(cohesion, 3),
            "sample_reasons": sample_reasons,
        }

    svc = get_speaker_service()

    # Create or reuse the target speaker.
    if speaker_id is not None:
        speaker = (await db.execute(
            select(Speaker).where(Speaker.id == speaker_id)
        )).scalar_one_or_none()
        if speaker is None:
            return {"ok": False, "reason": f"speaker {speaker_id} not found", "accepted": accepted}
        speaker.name = name
        speaker.enrolled = True
        # Replace prior embeddings on a re-enroll (bulk delete; FK-safe).
        from sqlalchemy import delete
        await db.execute(
            delete(SpeakerEmbedding).where(SpeakerEmbedding.speaker_id == speaker_id)
            .execution_options(synchronize_session=False)
        )
    else:
        speaker = Speaker(name=name, alias=await _unique_alias(db, name), enrolled=True)
        db.add(speaker)
    await db.flush()

    for emb, dur in zip(embeddings, durations):
        db.add(SpeakerEmbedding(
            speaker_id=speaker.id,
            embedding=svc.embedding_to_base64(emb),
            sample_duration=int(dur * 1000) if dur is not None else None,
        ))

    # Link the user (a user maps to exactly one speaker — move the link).
    displaced: list[int] = []
    if user_id is not None:
        # A re-enroll (speaker_id) may already be linked to a DIFFERENT user;
        # unlinking them below is silent otherwise — surface it, like /merge does.
        displaced = list((await db.execute(
            select(User.id).where(User.speaker_id == speaker.id, User.id != user_id)
        )).scalars().all())
        await db.execute(
            update(User).where(User.speaker_id == speaker.id)
            .values(speaker_id=None).execution_options(synchronize_session=False)
        )
        await db.execute(
            update(User).where(User.id == user_id)
            .values(speaker_id=speaker.id).execution_options(synchronize_session=False)
        )

    await db.commit()
    logger.info(
        f"🎙️ Enrolled speaker '{name}' (id={speaker.id}, {accepted} samples, "
        f"cohesion {cohesion:.2f}, user_id={user_id})"
    )
    result = {
        "ok": True, "speaker_id": speaker.id, "name": name,
        "cohesion": round(cohesion, 3), "accepted": accepted, "rejected": rejected,
        "sample_reasons": sample_reasons,
    }
    if displaced:
        result["displaced_user_ids"] = displaced  # a prior user↔speaker link was moved
    return result


async def promote_candidates(
    db: AsyncSession,
    *,
    candidate_ids: list[int],
    name: str,
    user_id: int | None = None,
) -> dict[str, Any]:
    """Promote review-bucket candidates (Phase-3) to a named enrolled speaker.

    Same cohesion/count gates as a fresh enrollment (the candidates are already
    voice-server ONNX embeddings, so no re-embed). On success the promoted
    candidates are consumed (deleted). Returns the same result shape as
    ``enroll_speaker_controlled`` (never raises for a gate failure).
    """
    if not settings.speaker_recognition_enabled:
        return {"ok": False, "reason": _RECOGNITION_OFF_REASON, "accepted": 0}
    name = (name or "").strip()
    if not name:
        return {"ok": False, "reason": "name is required", "accepted": 0}
    if user_id is not None:
        if (await db.execute(
            select(User.id).where(User.id == user_id)
        )).scalar_one_or_none() is None:
            return {"ok": False, "reason": f"user {user_id} not found", "accepted": 0}

    # FOR UPDATE: lock the candidate rows so two concurrent promotes of the same
    # ids can't each build a full speaker from them (the second would find them
    # already consumed → too-few → rejected, instead of a duplicate profile).
    cands = (await db.execute(
        select(SpeakerCandidate).where(SpeakerCandidate.id.in_(candidate_ids))
        .with_for_update()
    )).scalars().all()
    svc = get_speaker_service()
    embeddings = [
        np.asarray(svc.embedding_from_base64(c.embedding), dtype=np.float32) for c in cands
    ]
    embeddings = [e for e in embeddings if e.size and np.all(np.isfinite(e))]
    if len(embeddings) < settings.speaker_enroll_min_samples:
        return {
            "ok": False,
            "reason": (
                f"select >= {settings.speaker_enroll_min_samples} candidates "
                f"(got {len(embeddings)} usable)"
            ),
            "accepted": len(embeddings),
        }

    cohesion = _cohesion(embeddings)
    if not np.isfinite(cohesion) or cohesion < settings.speaker_enroll_min_cohesion:
        return {
            "ok": False,
            "reason": (
                f"candidates don't cohere (mean cosine {cohesion:.2f} < "
                f"{settings.speaker_enroll_min_cohesion}) — likely different voices; "
                f"select ones that belong together"
            ),
            "accepted": len(embeddings), "cohesion": round(cohesion, 3),
        }

    speaker = Speaker(name=name, alias=await _unique_alias(db, name), enrolled=True)
    db.add(speaker)
    await db.flush()
    for emb in embeddings:
        db.add(SpeakerEmbedding(speaker_id=speaker.id, embedding=svc.embedding_to_base64(emb)))

    # Link the user to the brand-new speaker (a user maps to exactly one speaker —
    # this moves the link off any prior one). No displaced-user report here: the
    # speaker is freshly created, so nobody else is linked to IT (unlike the
    # re-enroll path in enroll_speaker_controlled).
    if user_id is not None:
        await db.execute(update(User).where(User.id == user_id)
                         .values(speaker_id=speaker.id).execution_options(synchronize_session=False))

    # consume the promoted candidates
    await db.execute(
        delete(SpeakerCandidate).where(SpeakerCandidate.id.in_([c.id for c in cands]))
        .execution_options(synchronize_session=False)
    )
    await db.commit()
    logger.info(
        f"🎙️ Promoted {len(embeddings)} candidate(s) → enrolled speaker '{name}' "
        f"(id={speaker.id}, cohesion {cohesion:.2f}, user_id={user_id})"
    )
    return {
        "ok": True, "speaker_id": speaker.id, "name": name,
        "cohesion": round(cohesion, 3), "accepted": len(embeddings),
    }


async def _unique_alias(db: AsyncSession, name: str) -> str:
    base = _slugify(name)
    alias = base
    i = 1
    while (await db.execute(
        select(func.count()).select_from(Speaker).where(Speaker.alias == alias)
    )).scalar():
        i += 1
        alias = f"{base}_{i}"
    return alias
