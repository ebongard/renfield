"""Resolve a wire-supplied speaker embedding to a Speaker DB row.

Phase B (B.4.a) adds a `speaker_embedding[192]` field to the chat-WS
message envelope so the voice-server can ship the ECAPA embedding
alongside the transcribed text. The chat handler calls this resolver
to look up an existing Speaker (cosine match) or auto-enrol a new one
("Unbekannter Sprecher #N") — same policy as
`whisper_service.transcribe_bytes_with_speaker`'s in-process resolver,
which the voice-server now bypasses.

This module is the platform-level point of truth for the
embedding → Speaker lookup. `whisper_service` will simplify in B.4.c
to delegate here instead of carrying its own copy.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import numpy as np
from loguru import logger
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from models.database import Speaker, SpeakerCandidate, SpeakerEmbedding
from services.speaker_service import get_speaker_service
from utils.config import settings

MAX_EMBEDDINGS_PER_SPEAKER = 10


def _l2_normalize(vec: np.ndarray) -> np.ndarray:
    """Unit-normalize an embedding (ECAPA outputs are not unit-norm; averaging
    raw vectors lets larger-norm samples dominate the centroid)."""
    norm = float(np.linalg.norm(vec))
    return vec / norm if norm > 0 else vec


def _empty_speaker_info() -> dict[str, Any]:
    return {
        "speaker_id": None,
        "speaker_name": None,
        "speaker_alias": None,
        "speaker_confidence": 0.0,
        "is_new_speaker": False,
    }


def known_speaker_flags() -> tuple[bool, bool, bool]:
    """The two switches that set the comparison standard -- as a triple
    `(gating, controlled, quality_active)`, because the third follows from the
    first two.

    🛑 Both together, not separately. `quality_active` is `gating OR controlled`:
    the duration bar applies under phase-0 gating AND under the controlled
    recognition from phase 3. Check only `speaker_quality_gating_enabled` and in
    the household (controlled on, gating off) you compute a DIFFERENT centroid
    than the recognition does.
    """
    gating = settings.speaker_quality_gating_enabled
    controlled = settings.speaker_controlled_enrollment_enabled
    return gating, controlled, (gating or controlled)


def build_known_speaker_centroids(
    all_speakers: list[Speaker],
    *,
    controlled: bool,
    quality_active: bool,
) -> tuple[list[tuple[int, str, np.ndarray]], list[Speaker]]:
    """The reference profiles that are compared against -- ONE version for all.

    🛑 WHY THIS IS A SINGLE FUNCTION
    --------------------------------
    Until 2026-09-27 `GET /api/speakers/identify` built its own centroid
    (`routes/speakers.py:get_speaker_embeddings_averaged`) and diverged from the
    live path in THREE points:

      * it normalized only under `speaker_quality_gating_enabled`, the resolver
        also under `speaker_controlled_enrollment_enabled` -- exactly the case in
        the household, so different centroids;
      * it took ALL speakers, the resolver under `controlled` only the enrolled
        ones;
      * it averaged ALL embeddings, the resolver only the
        `MAX_EMBEDDINGS_PER_SPEAKER` most recent ones.

    So the diagnostic endpoint answered from a different model than the
    recognition -- and it was precisely the tool one uses to chase down a
    misidentification. A second copy of the same computation has no remedy other
    than: there is no second copy.
    """
    known: list[tuple[int, str, np.ndarray]] = []
    with_embeddings: list[Speaker] = []
    service = get_speaker_service()
    for speaker in all_speakers:
        if not speaker.embeddings:
            continue
        # Phase-3: identify against ENROLLED reference profiles ONLY.
        if controlled and not speaker.enrolled:
            continue
        with_embeddings.append(speaker)
        recent = sorted(
            speaker.embeddings,
            key=lambda e: e.created_at or datetime.min,
            reverse=True,
        )[:MAX_EMBEDDINGS_PER_SPEAKER]
        decoded = [service.embedding_from_base64(emb.embedding) for emb in recent]
        if decoded:
            # L2-normalize each embedding before averaging so the centroid
            # isn't dominated by larger-norm samples (raw ECAPA norms vary
            # ~250-410). Off = legacy raw mean.
            if quality_active:
                decoded = [_l2_normalize(d) for d in decoded]
            known.append((speaker.id, speaker.name, np.mean(decoded, axis=0)))
    return known, with_embeddings


def match_known_speaker(
    embedding: np.ndarray,
    known_speakers: list[tuple[int, str, np.ndarray]],
    *,
    controlled: bool,
) -> tuple[int | None, float, float]:
    """THE decision -- not only the profiles. One version for all paths.

    🛑 WHY THIS LIVES HERE AND NOT IN THE CALLER
    --------------------------------------------
    On 2026-09-27 `build_known_speaker_centroids` was pulled out so that login,
    diagnostics and recognition compare against THE SAME reference profiles --
    and it was then written down that they did. That was true and concealing for
    exactly that reason: what was shared were the PROFILES, not the DECISION.

    `SpeakerService.identify_speaker` is plain argmax with a threshold
    (`speaker_recognition_threshold`, default 0,25). Under
    `speaker_controlled_enrollment_enabled` the resolver additionally demands a
    distance to the runner-up (`speaker_match_min_margin`, 0,1), because cosine
    values close to the noise do not separate -- without the margin an
    assignment between two similarly good profiles is a coin toss.

    The consequence while only the builder was shared: audio that reaches 0,72
    against TWO household members is rejected by the recognition as a coin toss
    -- and would have LOGGED IN at the voice login as the closest profile. One of
    them can be the administrator account. Sharing half was worse than sharing
    nothing, because it looked like unity.

    Returns `(speaker_id | None, best_score, runner_up)`. The two values come
    along because the controlled recognition puts a failure into the review
    bucket and needs the numbers for it. A LOGIN path throws them away.
    """
    if not known_speakers:
        return None, 0.0, -1.0

    service = get_speaker_service()
    scored = sorted(
        (
            (service.compute_similarity(embedding, centroid), sid)
            for sid, _name, centroid in known_speakers
        ),
        key=lambda x: x[0], reverse=True,
    )
    best_score, best_id = scored[0]
    runner_up = scored[1][0] if len(scored) > 1 else -1.0

    if best_score < settings.speaker_recognition_threshold:
        return None, best_score, runner_up
    # The margin applies under the controlled recognition -- there the profiles
    # are enrolled and immutable, so a coin toss between two of them is a real
    # risk of confusing one person for another.
    if controlled and (best_score - runner_up) < settings.speaker_match_min_margin:
        return None, best_score, runner_up
    return best_id, best_score, runner_up


async def resolve_speaker_from_embedding(
    db_session: AsyncSession,
    embedding: list[float] | np.ndarray,
    *,
    audio_duration_s: float | None = None,
) -> dict[str, Any]:
    """Look up or create a Speaker for the given ECAPA embedding.

    Returns the same shape as `whisper_service.transcribe_bytes_with_speaker`'s
    `speaker_info` dict so downstream consumers can swap callers without
    code change. Best-effort — on any error returns the empty info dict
    and logs the cause; the caller treats the speaker as unknown.

    ``audio_duration_s`` (best-effort; callers that have it pass it) drives the
    Phase-0 quality gate: when ``speaker_quality_gating_enabled`` and the turn is
    shorter than ``speaker_recognition_min_duration_s``, the turn may still be
    IDENTIFIED (read-only) but must NOT auto-enrol a new speaker or reinforce an
    existing one — short/noisy turns are what pollute profiles. See
    docs/design/speaker-enrollment-redesign.md.
    """
    # Defense in depth behind the chat handler's gate: with speaker recognition
    # off NOTHING is read or written — no match, no auto-enrol, no reinforcement,
    # no review-bucket candidate. An ECAPA voiceprint is biometric data (Art. 9
    # GDPR); an instance that disabled recognition must never persist one, even
    # if a future caller forgets its own gate.
    if not settings.speaker_recognition_enabled:
        return _empty_speaker_info()

    # The switches come from ONE place, so the route cannot diverge again
    # (see `known_speaker_flags`).
    gating, controlled, quality_active = known_speaker_flags()
    too_short = (
        quality_active
        and audio_duration_s is not None
        and audio_duration_s < settings.speaker_recognition_min_duration_s
    )
    if embedding is None:
        return _empty_speaker_info()

    if isinstance(embedding, list):
        embedding_array = np.asarray(embedding, dtype=np.float32)
    else:
        embedding_array = embedding.astype(np.float32, copy=False)

    # A non-finite wire embedding (NaN/inf) would make every cosine NaN — the
    # match silently fails AND, under controlled mode, a NaN best_score lands in
    # the review bucket and later serializes as bare `NaN` (invalid JSON) from
    # GET /candidates. Reject it up front, same guard as the enrollment path.
    if embedding_array.size == 0 or not np.all(np.isfinite(embedding_array)):
        return _empty_speaker_info()

    speaker_info = _empty_speaker_info()
    service = get_speaker_service()

    try:
        result = await db_session.execute(
            select(Speaker).options(selectinload(Speaker.embeddings))
        )
        all_speakers = result.scalars().all()

        known_speakers, speakers_with_embeddings = build_known_speaker_centroids(
            all_speakers, controlled=controlled, quality_active=quality_active
        )

        identified: Speaker | None = None
        confidence = 0.0
        best_score = 0.0
        best_speaker_id: int | None = None
        if known_speakers:
            if controlled:
                # Margin-gated match: the best enrolled profile must clear the
                # threshold AND beat the runner-up by `speaker_match_min_margin`
                # (near-noise-floor cosines don't cleanly separate — the margin
                # guards against a coin-flip between two profiles).
                scored = sorted(
                    (
                        (service.compute_similarity(embedding_array, centroid), sid, name)
                        for sid, name, centroid in known_speakers
                    ),
                    key=lambda x: x[0], reverse=True,
                )
                best_score, best_speaker_id, _ = scored[0]
                runner_up = scored[1][0] if len(scored) > 1 else -1.0
                if (
                    best_score >= settings.speaker_recognition_threshold
                    and (best_score - runner_up) >= settings.speaker_match_min_margin
                ):
                    confidence = best_score
                    for s in speakers_with_embeddings:
                        if s.id == best_speaker_id:
                            identified = s
                            break
            else:
                match = service.identify_speaker(embedding_array, known_speakers)
                if match:
                    speaker_id, _name, confidence = match
                    for s in speakers_with_embeddings:
                        if s.id == speaker_id:
                            identified = s
                            break

        if identified:
            speaker_info = {
                "speaker_id": identified.id,
                "speaker_name": identified.name,
                "speaker_alias": identified.alias,
                "speaker_confidence": confidence,
                "is_new_speaker": False,
            }
            logger.info(f"🎤 Speaker identified from wire-embedding: {identified.name} ({confidence:.2f})")

            # Reinforce the profile only on a STRONG, long-enough match — a weak
            # (barely-over-threshold, possibly wrong) or too-short match appended
            # every turn is exactly what pollutes profiles (Phase-0 gate).
            strong_enough = (
                not gating
                or confidence >= settings.speaker_continuous_learning_min_confidence
            )
            # Phase-3 keeps reference profiles IMMUTABLE (no passive reinforcement).
            if (
                settings.speaker_continuous_learning
                and not controlled
                and not too_short
                and strong_enough
            ):
                await _append_embedding(db_session, identified.id, embedding_array, service)

        elif controlled:
            # Phase-3: do NOT auto-enrol on a miss (no more polluting "Unbekannter
            # Sprecher"). A quality-passing unknown goes to the review bucket for
            # the admin to promote or dismiss; a too-short one is dropped.
            if not too_short:
                await _capture_candidate(
                    db_session, embedding_array, best_score, best_speaker_id,
                    audio_duration_s, service,
                )
                logger.info(f"🎤 Unknown voice → review bucket (best match {best_score:.2f})")
            else:
                logger.info("🎤 Unknown voice, turn too short — skipped (Phase-3)")

        elif settings.speaker_auto_enroll and not too_short:
            unknown_count = sum(
                1 for s in all_speakers if s.name.startswith("Unbekannter Sprecher")
            )
            new_number = unknown_count + 1
            new_speaker = Speaker(
                name=f"Unbekannter Sprecher #{new_number}",
                alias=f"unknown_{new_number}",
                is_admin=False,
            )
            db_session.add(new_speaker)
            await db_session.flush()

            db_session.add(
                SpeakerEmbedding(
                    speaker_id=new_speaker.id,
                    embedding=service.embedding_to_base64(embedding_array),
                )
            )
            await db_session.commit()

            speaker_info = {
                "speaker_id": new_speaker.id,
                "speaker_name": new_speaker.name,
                "speaker_alias": new_speaker.alias,
                "speaker_confidence": 1.0,
                "is_new_speaker": True,
            }
            logger.info(
                f"🆕 New unknown speaker auto-enrolled from wire-embedding: "
                f"{new_speaker.name} (ID: {new_speaker.id})"
            )
        elif too_short:
            logger.info(
                f"🎤 Speaker not recognised — turn too short "
                f"({audio_duration_s:.2f}s < {settings.speaker_recognition_min_duration_s}s); "
                f"not auto-enrolling (quality gate)"
            )
        else:
            logger.info("🎤 Speaker not recognised (auto-enrol disabled)")

    except Exception as e:
        logger.warning(f"Speaker resolution from wire-embedding failed: {e}")
        return _empty_speaker_info()

    return speaker_info


async def _append_embedding(
    db_session: AsyncSession,
    speaker_id: int,
    embedding: np.ndarray,
    service,
) -> None:
    """Continuous-learning embedding append, capped at MAX_EMBEDDINGS_PER_SPEAKER."""
    try:
        from sqlalchemy import func

        count_stmt = select(func.count(SpeakerEmbedding.id)).where(
            SpeakerEmbedding.speaker_id == speaker_id
        )
        result = await db_session.execute(count_stmt)
        existing_count = result.scalar_one()

        if existing_count >= MAX_EMBEDDINGS_PER_SPEAKER:
            oldest_stmt = (
                select(SpeakerEmbedding)
                .where(SpeakerEmbedding.speaker_id == speaker_id)
                .order_by(SpeakerEmbedding.created_at.asc())
                .limit(existing_count - MAX_EMBEDDINGS_PER_SPEAKER + 1)
            )
            old_rows = (await db_session.execute(oldest_stmt)).scalars().all()
            for row in old_rows:
                await db_session.delete(row)

        db_session.add(
            SpeakerEmbedding(
                speaker_id=speaker_id,
                embedding=service.embedding_to_base64(embedding),
            )
        )
        await db_session.commit()
    except Exception as e:
        logger.warning(f"Continuous-learning append failed for speaker {speaker_id}: {e}")


async def _capture_candidate(
    db_session: AsyncSession,
    embedding: np.ndarray,
    best_score: float,
    best_speaker_id: int | None,
    audio_duration_s: float | None,
    service,
) -> None:
    """Store an unmatched voice in the review bucket (Phase-3), capped to the
    newest ``speaker_review_bucket_cap`` rows. Best-effort — never fails a turn."""
    try:
        db_session.add(SpeakerCandidate(
            embedding=service.embedding_to_base64(embedding),
            best_score=float(best_score),
            best_speaker_id=best_speaker_id,
            audio_duration_s=audio_duration_s,
        ))
        await db_session.flush()
        cap = settings.speaker_review_bucket_cap
        total = (await db_session.execute(
            select(func.count(SpeakerCandidate.id))
        )).scalar_one()
        if total > cap:
            stale = (await db_session.execute(
                select(SpeakerCandidate.id)
                .order_by(SpeakerCandidate.created_at.asc())
                .limit(total - cap)
            )).scalars().all()
            await db_session.execute(
                delete(SpeakerCandidate).where(SpeakerCandidate.id.in_(stale))
                .execution_options(synchronize_session=False)
            )
        await db_session.commit()
    except Exception as e:
        logger.warning(f"Review-bucket capture failed: {e}")
