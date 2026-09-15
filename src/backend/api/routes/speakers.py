"""
Speaker Management API Routes

Endpoints for speaker enrollment, identification, verification, and management.
Uses SpeechBrain ECAPA-TDNN for speaker embeddings.
"""

import numpy as np
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from loguru import logger
from pydantic import BaseModel
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from models.database import Conversation, Speaker, SpeakerCandidate, SpeakerEmbedding, User
from models.permissions import Permission
from services.auth_service import require_permission
from services.database import get_db
from services.speaker_service import SPEECHBRAIN_ERROR, get_speaker_service
from utils.config import settings

router = APIRouter()


def _require_inprocess_embeddings(path: str) -> None:
    """P0 fail-loud guard (docs/design/voice-identity-wakeword-verification.md).

    These legacy routes embed with the in-process SpeechBrain model, which
    does NOT share a representation space with the voice-server ONNX model
    that produced the stored wire-path embeddings. Comparing or storing
    across the two spaces silently corrupts speaker identity, so unless a
    dev environment explicitly opts in, refuse with a clear pointer to the
    supported flow.
    """
    if settings.speaker_inprocess_embeddings_enabled:
        return
    from utils.metrics import record_speaker_inprocess_embedding_blocked

    logger.warning(
        f"🚫 Refused in-process SpeechBrain embedding on {path}: "
        "speaker_inprocess_embeddings_enabled is off"
    )
    record_speaker_inprocess_embedding_blocked(path)
    raise HTTPException(
        status_code=503,
        detail=(
            "In-process (SpeechBrain) speaker embeddings are disabled: they are "
            "incompatible with the voice-server ONNX embedding space used by "
            "recognition. Use the guided enrollment (POST /api/speakers/enroll) "
            "instead, or set SPEAKER_INPROCESS_EMBEDDINGS_ENABLED=true in a "
            "dev environment without a voice-server."
        ),
    )


def _require_speaker_recognition(path: str) -> None:
    """Refuse every route that WRITES a voiceprint while speaker recognition is
    off. An ECAPA embedding is biometric data (Art. 9 GDPR); an instance that set
    ``SPEAKER_RECOGNITION_ENABLED=false`` must not store one through the admin
    enrollment paths either (the chat/voice/satellite paths are gated already).
    409: the request is valid, the instance configuration forbids it."""
    if settings.speaker_recognition_enabled:
        return
    logger.warning(f"🚫 Refused voiceprint write on {path}: speaker_recognition_enabled is off")
    raise HTTPException(
        status_code=409,
        detail=(
            "Speaker recognition is disabled on this instance "
            "(SPEAKER_RECOGNITION_ENABLED=false): no voiceprints are stored, so "
            "speaker enrollment is unavailable."
        ),
    )


# --- Pydantic Models ---

class SpeakerCreate(BaseModel):
    name: str
    alias: str
    is_admin: bool = False


class SpeakerUpdate(BaseModel):
    name: str | None = None
    alias: str | None = None
    is_admin: bool | None = None


class SpeakerResponse(BaseModel):
    id: int
    name: str
    alias: str
    is_admin: bool
    embedding_count: int

    class Config:
        from_attributes = True


class IdentifyResponse(BaseModel):
    speaker_id: int | None
    speaker_name: str | None
    speaker_alias: str | None
    confidence: float
    is_identified: bool


class EnrollResponse(BaseModel):
    speaker_id: int
    embedding_id: int
    embedding_count: int
    message: str


class VerifyResponse(BaseModel):
    is_verified: bool
    confidence: float
    speaker_name: str | None


class MergeSpeakersRequest(BaseModel):
    source_speaker_id: int
    target_speaker_id: int


class PromoteCandidatesRequest(BaseModel):
    candidate_ids: list[int]
    name: str
    user_id: int | None = None


class DismissCandidatesRequest(BaseModel):
    candidate_ids: list[int]


class MergeSpeakersResponse(BaseModel):
    target_speaker_id: int
    target_speaker_name: str
    merged_embedding_count: int
    total_embedding_count: int
    source_speaker_deleted: str
    message: str


class ServiceStatusResponse(BaseModel):
    available: bool
    model_loaded: bool
    message: str


# --- Helper Functions ---

async def get_speaker_embeddings_averaged(
    db: AsyncSession
) -> list[tuple]:
    """
    Load all speakers with their averaged embeddings.

    Returns:
        List of (speaker_id, speaker_name, averaged_embedding) tuples
    """
    service = get_speaker_service()

    # Get all speakers with embeddings (eagerly loaded)
    result = await db.execute(
        select(Speaker)
        .where(Speaker.embeddings.any())
        .options(selectinload(Speaker.embeddings))
    )
    speakers = result.scalars().all()

    speaker_data = []
    for speaker in speakers:
        if not speaker.embeddings:
            continue

        # Decode and average embeddings
        embeddings = [
            service.embedding_from_base64(emb.embedding)
            for emb in speaker.embeddings
        ]

        if embeddings:
            # Phase-0: L2-normalize before averaging (mirror speaker_resolver) so
            # /identify uses the same centroid as live recognition. Off = legacy.
            if settings.speaker_quality_gating_enabled:
                embeddings = [
                    (e / n if (n := float(np.linalg.norm(e))) > 0 else e)
                    for e in embeddings
                ]
            averaged = np.mean(embeddings, axis=0)
            speaker_data.append((speaker.id, speaker.name, averaged))

    return speaker_data


# --- Endpoints ---

@router.get("/status", response_model=ServiceStatusResponse)
async def get_service_status(_user: User = Depends(require_permission(Permission.SPEAKERS_ALL))):
    """Check if speaker recognition service is available"""
    service = get_speaker_service()

    if service.is_available():
        message = "Speaker recognition is available"
    elif SPEECHBRAIN_ERROR:
        message = f"SpeechBrain not available: {SPEECHBRAIN_ERROR}"
    else:
        message = "SpeechBrain not installed. Install with: pip install speechbrain torchaudio"

    return ServiceStatusResponse(
        available=service.is_available(),
        model_loaded=service._model_loaded,
        message=message
    )


@router.post("", response_model=SpeakerResponse)
async def create_speaker(
    speaker: SpeakerCreate,
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(require_permission(Permission.SPEAKERS_ALL)),
):
    """Create a new speaker profile"""
    # Check if alias already exists
    result = await db.execute(
        select(Speaker).where(Speaker.alias == speaker.alias)
    )
    existing = result.scalar_one_or_none()

    if existing:
        raise HTTPException(
            status_code=400,
            detail=f"Speaker with alias '{speaker.alias}' already exists"
        )

    # Create speaker
    new_speaker = Speaker(
        name=speaker.name,
        alias=speaker.alias,
        is_admin=speaker.is_admin
    )
    db.add(new_speaker)
    await db.commit()
    await db.refresh(new_speaker)

    logger.info(f"✅ Created speaker: {new_speaker.name} ({new_speaker.alias})")

    return SpeakerResponse(
        id=new_speaker.id,
        name=new_speaker.name,
        alias=new_speaker.alias,
        is_admin=new_speaker.is_admin,
        embedding_count=0
    )


@router.get("", response_model=list[SpeakerResponse])
async def list_speakers(db: AsyncSession = Depends(get_db), _user: User = Depends(require_permission(Permission.SPEAKERS_ALL))):
    """List all registered speakers"""
    # Use selectinload to eagerly load embeddings (avoids lazy-load in async context)
    result = await db.execute(
        select(Speaker).options(selectinload(Speaker.embeddings))
    )
    speakers = result.scalars().all()

    return [
        SpeakerResponse(
            id=s.id,
            name=s.name,
            alias=s.alias,
            is_admin=s.is_admin,
            embedding_count=len(s.embeddings)
        )
        for s in speakers
    ]


@router.get("/candidates")
async def list_candidates(
    limit: int = 100,
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(require_permission(Permission.SPEAKERS_ALL)),
):
    """Review bucket (Phase-3): unmatched unknown voices captured from passive
    turns, newest first, with the nearest enrolled profile for context.

    Declared BEFORE GET /{speaker_id} so the literal path isn't shadowed by the
    int path param (which would 422 on "candidates")."""
    rows = (await db.execute(
        select(
            SpeakerCandidate.id, SpeakerCandidate.best_score,
            SpeakerCandidate.best_speaker_id, SpeakerCandidate.audio_duration_s,
            SpeakerCandidate.created_at, Speaker.name.label("nearest_name"),
        )
        .select_from(SpeakerCandidate)
        .outerjoin(Speaker, Speaker.id == SpeakerCandidate.best_speaker_id)
        .order_by(SpeakerCandidate.created_at.desc())
        .limit(min(max(limit, 1), 500))
    )).all()
    total = (await db.execute(
        select(func.count(SpeakerCandidate.id))
    )).scalar_one()
    return {
        "total": total,
        "candidates": [
            {
                "id": r.id,
                "best_score": round(r.best_score, 3) if r.best_score is not None else None,
                "nearest_speaker": r.nearest_name,
                "audio_duration_s": r.audio_duration_s,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in rows
        ],
    }


@router.get("/{speaker_id}", response_model=SpeakerResponse)
async def get_speaker(
    speaker_id: int,
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(require_permission(Permission.SPEAKERS_ALL)),
):
    """Get a specific speaker"""
    result = await db.execute(
        select(Speaker)
        .where(Speaker.id == speaker_id)
        .options(selectinload(Speaker.embeddings))
    )
    speaker = result.scalar_one_or_none()

    if not speaker:
        raise HTTPException(status_code=404, detail="Speaker not found")

    return SpeakerResponse(
        id=speaker.id,
        name=speaker.name,
        alias=speaker.alias,
        is_admin=speaker.is_admin,
        embedding_count=len(speaker.embeddings)
    )


@router.patch("/{speaker_id}", response_model=SpeakerResponse)
async def update_speaker(
    speaker_id: int,
    update: SpeakerUpdate,
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(require_permission(Permission.SPEAKERS_ALL)),
):
    """Update a speaker's information"""
    result = await db.execute(
        select(Speaker)
        .where(Speaker.id == speaker_id)
        .options(selectinload(Speaker.embeddings))
    )
    speaker = result.scalar_one_or_none()

    if not speaker:
        raise HTTPException(status_code=404, detail="Speaker not found")

    # Update fields
    if update.name is not None:
        speaker.name = update.name
    if update.alias is not None:
        # Check if new alias is taken
        if update.alias != speaker.alias:
            alias_check = await db.execute(
                select(Speaker).where(Speaker.alias == update.alias)
            )
            if alias_check.scalar_one_or_none():
                raise HTTPException(
                    status_code=400,
                    detail=f"Alias '{update.alias}' is already taken"
                )
        speaker.alias = update.alias
    if update.is_admin is not None:
        speaker.is_admin = update.is_admin

    await db.commit()

    # Re-query with eager loading to avoid lazy load issue
    result = await db.execute(
        select(Speaker)
        .where(Speaker.id == speaker_id)
        .options(selectinload(Speaker.embeddings))
    )
    speaker = result.scalar_one()

    return SpeakerResponse(
        id=speaker.id,
        name=speaker.name,
        alias=speaker.alias,
        is_admin=speaker.is_admin,
        embedding_count=len(speaker.embeddings)
    )


@router.delete("/{speaker_id}")
async def delete_speaker(
    speaker_id: int,
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(require_permission(Permission.SPEAKERS_ALL)),
):
    """Delete a speaker.

    The DB-level ON DELETE rules (migration pc20260705) do the cleanup:
    embeddings CASCADE-delete with the speaker, and any conversation attribution
    / user link is SET NULL (the conversation + user account are kept, just
    unlinked). Previously the FKs were NO ACTION, so this 500'd on any speaker
    that had ever been used. To CONSOLIDATE rather than unlink, use /merge.
    """
    result = await db.execute(
        select(Speaker).where(Speaker.id == speaker_id)
    )
    speaker = result.scalar_one_or_none()

    if not speaker:
        raise HTTPException(status_code=404, detail="Speaker not found")

    speaker_name = speaker.name
    # Bulk DELETE (not ORM db.delete) so the DB-level ON DELETE rules do the
    # cleanup directly — no ORM cascade collection-load (MissingGreenlet-safe).
    await db.execute(
        delete(Speaker).where(Speaker.id == speaker_id)
        .execution_options(synchronize_session=False)
    )
    await db.commit()

    logger.info(f"🗑️ Deleted speaker: {speaker_name}")

    return {"message": f"Speaker '{speaker_name}' deleted"}


@router.post("/merge", response_model=MergeSpeakersResponse)
async def merge_speakers(
    request: MergeSpeakersRequest,
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(require_permission(Permission.SPEAKERS_ALL)),
):
    """
    Merge two speakers into one.

    Moves all voice embeddings from the source speaker to the target speaker,
    then deletes the source speaker. Use this when an unknown speaker was
    actually a known speaker and you want to combine their data.

    Args:
        source_speaker_id: The speaker to merge FROM (will be deleted)
        target_speaker_id: The speaker to merge INTO (will keep all embeddings)
    """
    if request.source_speaker_id == request.target_speaker_id:
        raise HTTPException(
            status_code=400,
            detail="Source and target speaker cannot be the same"
        )

    src_id, tgt_id = request.source_speaker_id, request.target_speaker_id

    # Load both speakers WITH their user link (for the UNIQUE-link decision).
    source_speaker = (await db.execute(
        select(Speaker).where(Speaker.id == src_id).options(selectinload(Speaker.user))
    )).scalar_one_or_none()
    if not source_speaker:
        raise HTTPException(
            status_code=404, detail=f"Source speaker (ID: {src_id}) not found")

    target_speaker = (await db.execute(
        select(Speaker).where(Speaker.id == tgt_id).options(selectinload(Speaker.user))
    )).scalar_one_or_none()
    if not target_speaker:
        raise HTTPException(
            status_code=404, detail=f"Target speaker (ID: {tgt_id}) not found")

    source_name = source_speaker.name
    target_name = target_speaker.name  # capture before commit (avoid post-commit refresh)
    source_has_user = source_speaker.user is not None
    target_has_user = target_speaker.user is not None

    source_embedding_count = (await db.execute(
        select(func.count()).select_from(SpeakerEmbedding)
        .where(SpeakerEmbedding.speaker_id == src_id)
    )).scalar() or 0

    # Reassign every reference source → target via bulk UPDATEs (not ORM
    # collection mutation — that leaves the rows in the source's delete-orphan
    # cascade, which the DB CASCADE would then delete). This PRESERVES the data;
    # only the plain-delete path lets the FK SET NULL / CASCADE fire.
    await db.execute(update(SpeakerEmbedding)
                     .where(SpeakerEmbedding.speaker_id == src_id)
                     .values(speaker_id=tgt_id)
                     .execution_options(synchronize_session=False))
    await db.execute(update(Conversation)
                     .where(Conversation.speaker_id == src_id)
                     .values(speaker_id=tgt_id)
                     .execution_options(synchronize_session=False))
    # users.speaker_id is UNIQUE: only move the link if the target isn't already
    # linked (else keep the target's — two users can't share one speaker; the
    # source's user is unlinked by SET NULL when the source is deleted).
    if source_has_user and not target_has_user:
        await db.execute(update(User)
                         .where(User.speaker_id == src_id)
                         .values(speaker_id=tgt_id)
                         .execution_options(synchronize_session=False))

    # Delete the now-dereferenced source via bulk DELETE (no ORM cascade).
    await db.execute(
        delete(Speaker).where(Speaker.id == src_id)
        .execution_options(synchronize_session=False)
    )
    await db.commit()

    total_embedding_count = (await db.execute(
        select(func.count()).select_from(SpeakerEmbedding)
        .where(SpeakerEmbedding.speaker_id == tgt_id)
    )).scalar() or 0

    # If BOTH speakers were linked to (different) users, the source's link could
    # not move (users.speaker_id is UNIQUE) and was severed when the source was
    # deleted — surface it so the admin knows that user must re-pair their voice.
    source_user_unlinked = source_has_user and target_has_user

    logger.info(
        f"🔗 Merged speaker '{source_name}' into '{target_name}': "
        f"{source_embedding_count} embeddings moved, "
        f"{total_embedding_count} total embeddings"
        + (" (source user unlinked — was linked to a different account)"
           if source_user_unlinked else "")
    )

    message = (f"Successfully merged '{source_name}' into '{target_name}'. "
               f"{source_embedding_count} embeddings transferred.")
    if source_user_unlinked:
        message += (" Note: the source speaker was linked to a different user "
                    "account; that link was removed (a speaker maps to one user).")

    return MergeSpeakersResponse(
        target_speaker_id=tgt_id,
        target_speaker_name=target_name,
        merged_embedding_count=source_embedding_count,
        total_embedding_count=total_embedding_count,
        source_speaker_deleted=source_name,
        message=message,
    )


@router.post("/enroll")
async def enroll_controlled(
    name: str = Form(...),
    audio: list[UploadFile] = File(...),
    user_id: int | None = Form(None),
    speaker_id: int | None = Form(None),
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(require_permission(Permission.SPEAKERS_ALL)),
):
    """Controlled multi-sample enrollment (docs/design/speaker-enrollment-redesign.md).

    Builds ONE trusted, named reference profile from several audio samples via the
    voice-server ONNX model (same as inference), quality- + cohesion-gated. Links
    the profile to a user. `speaker_id` re-enrols an existing speaker (replaces
    its embeddings). Returns the structured result (`ok` + reason on rejection —
    a rejection is a normal, actionable outcome of the guided flow, not a 500).
    """
    _require_speaker_recognition("route_enroll_controlled")
    samples: list[tuple[bytes, str]] = [
        (await f.read(), f.filename or "sample.wav") for f in audio
    ]
    from services.speaker_enrollment_service import enroll_speaker_controlled

    result = await enroll_speaker_controlled(
        db, name=name, samples=samples, user_id=user_id, speaker_id=speaker_id,
    )
    return result


@router.post("/candidates/promote")
async def promote_candidates_route(
    request: PromoteCandidatesRequest,
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(require_permission(Permission.SPEAKERS_ALL)),
):
    """Promote selected review-bucket candidates to a named enrolled speaker."""
    _require_speaker_recognition("route_promote_candidates")
    from services.speaker_enrollment_service import promote_candidates

    return await promote_candidates(
        db, candidate_ids=request.candidate_ids, name=request.name, user_id=request.user_id,
    )


@router.post("/candidates/dismiss")
async def dismiss_candidates(
    request: DismissCandidatesRequest,
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(require_permission(Permission.SPEAKERS_ALL)),
):
    """Discard review-bucket candidates (not a real voice / not worth enrolling)."""
    if request.candidate_ids:
        await db.execute(
            delete(SpeakerCandidate).where(SpeakerCandidate.id.in_(request.candidate_ids))
            .execution_options(synchronize_session=False)
        )
        await db.commit()
    return {"dismissed": len(request.candidate_ids)}


@router.post("/{speaker_id}/enroll", response_model=EnrollResponse)
async def enroll_speaker(
    speaker_id: int,
    audio: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(require_permission(Permission.SPEAKERS_ALL)),
):
    """
    Enroll a speaker with a voice sample.

    Upload an audio file (WAV, MP3, WebM, etc.) to create a voice embedding.
    Multiple enrollments (3-5 samples) improve recognition accuracy.

    Recommended: Use samples of 3-10 seconds with clear speech.
    """
    _require_speaker_recognition("route_enroll")
    _require_inprocess_embeddings("route_enroll")

    # Get speaker with embeddings eagerly loaded
    result = await db.execute(
        select(Speaker)
        .where(Speaker.id == speaker_id)
        .options(selectinload(Speaker.embeddings))
    )
    speaker = result.scalar_one_or_none()

    if not speaker:
        raise HTTPException(status_code=404, detail="Speaker not found")

    # Get speaker service
    service = get_speaker_service()

    if not service.is_available():
        raise HTTPException(
            status_code=503,
            detail="Speaker recognition not available. Install speechbrain."
        )

    # Extract embedding
    logger.info(f"📥 Enrolling voice sample for {speaker.name}: {audio.filename}")
    audio_bytes = await audio.read()

    embedding = service.extract_embedding_from_bytes(audio_bytes, audio.filename)

    if embedding is None:
        raise HTTPException(
            status_code=400,
            detail="Failed to extract voice embedding. Audio may be too short or unclear."
        )

    # Serialize and store
    embedding_b64 = service.embedding_to_base64(embedding)

    # Calculate duration (approximate from file size for PCM)
    duration_ms = int(len(audio_bytes) / 32)  # Rough estimate

    new_embedding = SpeakerEmbedding(
        speaker_id=speaker_id,
        embedding=embedding_b64,
        sample_duration=duration_ms
    )
    db.add(new_embedding)
    await db.commit()
    await db.refresh(new_embedding)

    # Get updated embedding count - count directly to avoid session issues
    from sqlalchemy import func
    count_result = await db.execute(
        select(func.count(SpeakerEmbedding.id))
        .where(SpeakerEmbedding.speaker_id == speaker_id)
    )
    embedding_count = count_result.scalar()

    logger.info(f"✅ Voice sample enrolled for {speaker.name} (total: {embedding_count})")

    return EnrollResponse(
        speaker_id=speaker_id,
        embedding_id=new_embedding.id,
        embedding_count=embedding_count,
        message=f"Voice sample enrolled for {speaker.name}. Total samples: {embedding_count}"
    )


@router.post("/identify", response_model=IdentifyResponse)
async def identify_speaker(
    audio: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(require_permission(Permission.SPEAKERS_ALL)),
):
    """
    Identify speaker from audio.

    Upload an audio file to identify which registered speaker is speaking.
    Returns the most likely speaker and confidence score.
    """
    _require_inprocess_embeddings("route_identify")

    service = get_speaker_service()

    if not service.is_available():
        raise HTTPException(
            status_code=503,
            detail="Speaker recognition not available"
        )

    # Extract embedding from query audio
    logger.info(f"🔍 Identifying speaker from: {audio.filename}")
    audio_bytes = await audio.read()

    query_embedding = service.extract_embedding_from_bytes(audio_bytes, audio.filename)

    if query_embedding is None:
        raise HTTPException(
            status_code=400,
            detail="Failed to extract voice embedding from audio"
        )

    # Load known speakers
    known_speakers = await get_speaker_embeddings_averaged(db)

    if not known_speakers:
        return IdentifyResponse(
            speaker_id=None,
            speaker_name=None,
            speaker_alias=None,
            confidence=0.0,
            is_identified=False
        )

    # Identify
    result = service.identify_speaker(query_embedding, known_speakers)

    if result:
        speaker_id, speaker_name, confidence = result

        # Get alias
        speaker_result = await db.execute(
            select(Speaker).where(Speaker.id == speaker_id)
        )
        speaker = speaker_result.scalar_one_or_none()

        logger.info(f"✅ Identified: {speaker_name} (confidence: {confidence:.2f})")

        return IdentifyResponse(
            speaker_id=speaker_id,
            speaker_name=speaker_name,
            speaker_alias=speaker.alias if speaker else None,
            confidence=confidence,
            is_identified=True
        )

    logger.info("❌ Speaker not identified (below threshold)")

    return IdentifyResponse(
        speaker_id=None,
        speaker_name=None,
        speaker_alias=None,
        confidence=0.0,
        is_identified=False
    )


@router.post("/{speaker_id}/verify", response_model=VerifyResponse)
async def verify_speaker(
    speaker_id: int,
    audio: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(require_permission(Permission.SPEAKERS_ALL)),
):
    """
    Verify if audio matches a specific speaker.

    Use this to verify that someone claiming to be a specific speaker
    actually matches their enrolled voice samples.
    """
    _require_inprocess_embeddings("route_verify")

    service = get_speaker_service()

    if not service.is_available():
        raise HTTPException(
            status_code=503,
            detail="Speaker recognition not available"
        )

    # Get speaker with embeddings eagerly loaded
    result = await db.execute(
        select(Speaker)
        .where(Speaker.id == speaker_id)
        .options(selectinload(Speaker.embeddings))
    )
    speaker = result.scalar_one_or_none()

    if not speaker:
        raise HTTPException(status_code=404, detail="Speaker not found")

    if not speaker.embeddings:
        raise HTTPException(
            status_code=400,
            detail=f"Speaker {speaker.name} has no enrolled voice samples"
        )

    # Extract query embedding
    audio_bytes = await audio.read()
    query_embedding = service.extract_embedding_from_bytes(audio_bytes, audio.filename)

    if query_embedding is None:
        raise HTTPException(
            status_code=400,
            detail="Failed to extract voice embedding from audio"
        )

    # Get speaker's embeddings (already loaded via selectinload)
    claimed_embeddings = [
        service.embedding_from_base64(emb.embedding)
        for emb in speaker.embeddings
    ]

    # Verify
    is_verified, confidence = service.verify_speaker(query_embedding, claimed_embeddings)

    logger.info(
        f"🔐 Verification for {speaker.name}: "
        f"{'✅ VERIFIED' if is_verified else '❌ NOT VERIFIED'} "
        f"(confidence: {confidence:.2f})"
    )

    return VerifyResponse(
        is_verified=is_verified,
        confidence=confidence,
        speaker_name=speaker.name
    )


@router.delete("/{speaker_id}/embeddings/{embedding_id}")
async def delete_embedding(
    speaker_id: int,
    embedding_id: int,
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(require_permission(Permission.SPEAKERS_ALL)),
):
    """Delete a specific voice embedding"""
    result = await db.execute(
        select(SpeakerEmbedding).where(
            SpeakerEmbedding.id == embedding_id,
            SpeakerEmbedding.speaker_id == speaker_id
        )
    )
    embedding = result.scalar_one_or_none()

    if not embedding:
        raise HTTPException(status_code=404, detail="Embedding not found")

    await db.delete(embedding)
    await db.commit()

    return {"message": "Embedding deleted"}
