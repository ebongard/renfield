"""
Speaker Management API Routes

Endpoints for speaker enrollment, identification, verification, and management.
Uses SpeechBrain ECAPA-TDNN for speaker embeddings.
"""
from fastapi import APIRouter, HTTPException, UploadFile, File, Depends
from pydantic import BaseModel
from typing import Optional, List
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from loguru import logger
import numpy as np

from services.database import get_db
from services.speaker_service import get_speaker_service, SpeakerService
from models.database import Speaker, SpeakerEmbedding

router = APIRouter()


# --- Pydantic Models ---

class SpeakerCreate(BaseModel):
    name: str
    alias: str
    is_admin: bool = False


class SpeakerUpdate(BaseModel):
    name: Optional[str] = None
    alias: Optional[str] = None
    is_admin: Optional[bool] = None


class SpeakerResponse(BaseModel):
    id: int
    name: str
    alias: str
    is_admin: bool
    embedding_count: int

    class Config:
        from_attributes = True


class IdentifyResponse(BaseModel):
    speaker_id: Optional[int]
    speaker_name: Optional[str]
    speaker_alias: Optional[str]
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
    speaker_name: Optional[str]


class ServiceStatusResponse(BaseModel):
    available: bool
    model_loaded: bool
    message: str


# --- Helper Functions ---

async def get_speaker_embeddings_averaged(
    db: AsyncSession
) -> List[tuple]:
    """
    Load all speakers with their averaged embeddings.

    Returns:
        List of (speaker_id, speaker_name, averaged_embedding) tuples
    """
    service = get_speaker_service()

    # Get all speakers with embeddings
    result = await db.execute(
        select(Speaker).where(Speaker.embeddings.any())
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
            averaged = np.mean(embeddings, axis=0)
            speaker_data.append((speaker.id, speaker.name, averaged))

    return speaker_data


# --- Endpoints ---

@router.get("/status", response_model=ServiceStatusResponse)
async def get_service_status():
    """Check if speaker recognition service is available"""
    service = get_speaker_service()

    return ServiceStatusResponse(
        available=service.is_available(),
        model_loaded=service._model_loaded,
        message="Speaker recognition is available" if service.is_available()
                else "SpeechBrain not installed. Install with: pip install speechbrain torchaudio"
    )


@router.post("", response_model=SpeakerResponse)
async def create_speaker(
    speaker: SpeakerCreate,
    db: AsyncSession = Depends(get_db)
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


@router.get("", response_model=List[SpeakerResponse])
async def list_speakers(db: AsyncSession = Depends(get_db)):
    """List all registered speakers"""
    result = await db.execute(select(Speaker))
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


@router.get("/{speaker_id}", response_model=SpeakerResponse)
async def get_speaker(
    speaker_id: int,
    db: AsyncSession = Depends(get_db)
):
    """Get a specific speaker"""
    result = await db.execute(
        select(Speaker).where(Speaker.id == speaker_id)
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
    db: AsyncSession = Depends(get_db)
):
    """Update a speaker's information"""
    result = await db.execute(
        select(Speaker).where(Speaker.id == speaker_id)
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
    await db.refresh(speaker)

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
    db: AsyncSession = Depends(get_db)
):
    """Delete a speaker and all their embeddings"""
    result = await db.execute(
        select(Speaker).where(Speaker.id == speaker_id)
    )
    speaker = result.scalar_one_or_none()

    if not speaker:
        raise HTTPException(status_code=404, detail="Speaker not found")

    speaker_name = speaker.name
    await db.delete(speaker)
    await db.commit()

    logger.info(f"🗑️ Deleted speaker: {speaker_name}")

    return {"message": f"Speaker '{speaker_name}' deleted"}


@router.post("/{speaker_id}/enroll", response_model=EnrollResponse)
async def enroll_speaker(
    speaker_id: int,
    audio: UploadFile = File(...),
    db: AsyncSession = Depends(get_db)
):
    """
    Enroll a speaker with a voice sample.

    Upload an audio file (WAV, MP3, WebM, etc.) to create a voice embedding.
    Multiple enrollments (3-5 samples) improve recognition accuracy.

    Recommended: Use samples of 3-10 seconds with clear speech.
    """
    # Get speaker
    result = await db.execute(
        select(Speaker).where(Speaker.id == speaker_id)
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

    # Get updated embedding count
    await db.refresh(speaker)
    embedding_count = len(speaker.embeddings)

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
    db: AsyncSession = Depends(get_db)
):
    """
    Identify speaker from audio.

    Upload an audio file to identify which registered speaker is speaking.
    Returns the most likely speaker and confidence score.
    """
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
    db: AsyncSession = Depends(get_db)
):
    """
    Verify if audio matches a specific speaker.

    Use this to verify that someone claiming to be a specific speaker
    actually matches their enrolled voice samples.
    """
    service = get_speaker_service()

    if not service.is_available():
        raise HTTPException(
            status_code=503,
            detail="Speaker recognition not available"
        )

    # Get speaker
    result = await db.execute(
        select(Speaker).where(Speaker.id == speaker_id)
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

    # Get speaker's embeddings
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
    db: AsyncSession = Depends(get_db)
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
