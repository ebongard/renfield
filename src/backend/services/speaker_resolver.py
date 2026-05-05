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
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from models.database import Speaker, SpeakerEmbedding
from services.speaker_service import get_speaker_service
from utils.config import settings

MAX_EMBEDDINGS_PER_SPEAKER = 10


def _empty_speaker_info() -> dict[str, Any]:
    return {
        "speaker_id": None,
        "speaker_name": None,
        "speaker_alias": None,
        "speaker_confidence": 0.0,
        "is_new_speaker": False,
    }


async def resolve_speaker_from_embedding(
    db_session: AsyncSession,
    embedding: list[float] | np.ndarray,
) -> dict[str, Any]:
    """Look up or create a Speaker for the given ECAPA embedding.

    Returns the same shape as `whisper_service.transcribe_bytes_with_speaker`'s
    `speaker_info` dict so downstream consumers can swap callers without
    code change. Best-effort — on any error returns the empty info dict
    and logs the cause; the caller treats the speaker as unknown.
    """
    if embedding is None:
        return _empty_speaker_info()

    if isinstance(embedding, list):
        embedding_array = np.asarray(embedding, dtype=np.float32)
    else:
        embedding_array = embedding.astype(np.float32, copy=False)

    if embedding_array.size == 0:
        return _empty_speaker_info()

    speaker_info = _empty_speaker_info()
    service = get_speaker_service()

    try:
        result = await db_session.execute(
            select(Speaker).options(selectinload(Speaker.embeddings))
        )
        all_speakers = result.scalars().all()

        known_speakers: list[tuple[int, str, np.ndarray]] = []
        speakers_with_embeddings: list[Speaker] = []
        for speaker in all_speakers:
            if not speaker.embeddings:
                continue
            speakers_with_embeddings.append(speaker)
            recent = sorted(
                speaker.embeddings,
                key=lambda e: e.created_at or datetime.min,
                reverse=True,
            )[:MAX_EMBEDDINGS_PER_SPEAKER]
            decoded = [service.embedding_from_base64(emb.embedding) for emb in recent]
            if decoded:
                averaged = np.mean(decoded, axis=0)
                known_speakers.append((speaker.id, speaker.name, averaged))

        identified: Speaker | None = None
        confidence = 0.0
        if known_speakers:
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

            if settings.speaker_continuous_learning:
                await _append_embedding(db_session, identified.id, embedding_array, service)

        elif settings.speaker_auto_enroll:
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
