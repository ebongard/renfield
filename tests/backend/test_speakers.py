"""
Tests für Speakers API

Testet:
- Speaker CRUD Operations
- Speaker Enrollment
- Speaker Identification
- Speaker Verification
- Speaker Merge
"""

import pytest
from io import BytesIO
from unittest.mock import AsyncMock, MagicMock, patch
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from sqlalchemy.orm import selectinload
from httpx import AsyncClient

from models.database import Speaker, SpeakerEmbedding


# ============================================================================
# Fixtures
# ============================================================================

@pytest.fixture
def mock_audio_file():
    """Create a mock audio file for enrollment"""
    wav_header = b'RIFF' + b'\x24\x00\x00\x00' + b'WAVE'
    wav_header += b'fmt ' + b'\x10\x00\x00\x00'
    wav_header += b'\x01\x00\x01\x00'
    wav_header += b'\x44\xac\x00\x00'
    wav_header += b'\x88\x58\x01\x00'
    wav_header += b'\x02\x00\x10\x00'
    wav_header += b'data' + b'\x00\x00\x00\x00'
    return wav_header + b'\x00' * 1000


@pytest.fixture
async def speaker_with_embeddings(db_session: AsyncSession) -> Speaker:
    """Create a speaker with voice embeddings"""
    speaker = Speaker(
        name="Test Speaker",
        alias="testspeaker",
        is_admin=False
    )
    db_session.add(speaker)
    await db_session.commit()
    await db_session.refresh(speaker)

    # Add embeddings
    for i in range(3):
        embedding = SpeakerEmbedding(
            speaker_id=speaker.id,
            embedding="base64encodedembedding==",
            sample_duration=5000
        )
        db_session.add(embedding)

    await db_session.commit()
    return speaker


@pytest.fixture
def mock_speaker_service():
    """Mock speaker recognition service"""
    with patch('api.routes.speakers.get_speaker_service') as mock:
        service = MagicMock()
        service.is_available.return_value = True
        service._model_loaded = True
        service.extract_embedding_from_bytes.return_value = [0.1] * 192
        service.embedding_to_base64.return_value = "base64embedding=="
        service.embedding_from_base64.return_value = [0.1] * 192
        service.identify_speaker.return_value = (1, "Test Speaker", 0.85)
        service.verify_speaker.return_value = (True, 0.90)
        mock.return_value = service
        yield service


# ============================================================================
# Model Tests
# ============================================================================

class TestSpeakerModel:
    """Tests für das Speaker Model"""

    @pytest.mark.database
    async def test_create_speaker(self, db_session: AsyncSession):
        """Testet das Erstellen eines Speakers"""
        speaker = Speaker(
            name="New Speaker",
            alias="newspeaker",
            is_admin=False
        )
        db_session.add(speaker)
        await db_session.commit()
        await db_session.refresh(speaker)

        assert speaker.id is not None
        assert speaker.name == "New Speaker"
        assert speaker.alias == "newspeaker"

    @pytest.mark.database
    async def test_speaker_unique_alias(self, db_session: AsyncSession, test_speaker):
        """Testet, dass Alias eindeutig sein muss"""
        from sqlalchemy.exc import IntegrityError

        duplicate = Speaker(
            name="Duplicate",
            alias=test_speaker.alias
        )
        db_session.add(duplicate)

        with pytest.raises(IntegrityError):
            await db_session.commit()


class TestSpeakerEmbeddingModel:
    """Tests für das SpeakerEmbedding Model"""

    @pytest.mark.database
    async def test_create_embedding(
        self,
        db_session: AsyncSession,
        test_speaker: Speaker
    ):
        """Testet das Erstellen eines Embeddings"""
        embedding = SpeakerEmbedding(
            speaker_id=test_speaker.id,
            embedding="testembedding==",
            sample_duration=3000
        )
        db_session.add(embedding)
        await db_session.commit()
        await db_session.refresh(embedding)

        assert embedding.id is not None
        assert embedding.speaker_id == test_speaker.id

    @pytest.mark.database
    async def test_speaker_embedding_relationship(
        self,
        db_session: AsyncSession,
        speaker_with_embeddings: Speaker
    ):
        """Testet die Beziehung zwischen Speaker und Embeddings"""
        result = await db_session.execute(
            select(Speaker)
            .where(Speaker.id == speaker_with_embeddings.id)
            .options(selectinload(Speaker.embeddings))
        )
        speaker = result.scalar_one()

        assert len(speaker.embeddings) == 3


# ============================================================================
# CRUD API Tests
# ============================================================================

class TestSpeakerCRUDAPI:
    """Tests für Speaker CRUD API"""

    @pytest.mark.integration
    async def test_create_speaker(self, async_client: AsyncClient):
        """Testet POST /api/speakers"""
        response = await async_client.post(
            "/api/speakers",
            json={
                "name": "API Speaker",
                "alias": "apispeaker",
                "is_admin": False
            }
        )

        assert response.status_code == 200
        data = response.json()
        assert data["name"] == "API Speaker"
        assert data["alias"] == "apispeaker"
        assert data["embedding_count"] == 0

    @pytest.mark.integration
    async def test_create_speaker_duplicate_alias(
        self,
        async_client: AsyncClient,
        test_speaker: Speaker
    ):
        """Testet Erstellung mit doppeltem Alias"""
        response = await async_client.post(
            "/api/speakers",
            json={
                "name": "Duplicate",
                "alias": test_speaker.alias,
                "is_admin": False
            }
        )

        assert response.status_code == 400

    @pytest.mark.integration
    async def test_list_speakers(
        self,
        async_client: AsyncClient,
        test_speaker: Speaker
    ):
        """Testet GET /api/speakers"""
        response = await async_client.get("/api/speakers")

        assert response.status_code == 200
        data = response.json()
        assert isinstance(data, list)
        assert len(data) >= 1

    @pytest.mark.integration
    async def test_get_speaker(
        self,
        async_client: AsyncClient,
        test_speaker: Speaker
    ):
        """Testet GET /api/speakers/{id}"""
        response = await async_client.get(f"/api/speakers/{test_speaker.id}")

        assert response.status_code == 200
        data = response.json()
        assert data["id"] == test_speaker.id
        assert data["name"] == test_speaker.name

    @pytest.mark.integration
    async def test_get_nonexistent_speaker(self, async_client: AsyncClient):
        """Testet GET für nicht-existenten Speaker"""
        response = await async_client.get("/api/speakers/99999")

        assert response.status_code == 404

    @pytest.mark.integration
    async def test_update_speaker(
        self,
        async_client: AsyncClient,
        test_speaker: Speaker
    ):
        """Testet PATCH /api/speakers/{id}"""
        response = await async_client.patch(
            f"/api/speakers/{test_speaker.id}",
            json={"name": "Updated Name"}
        )

        assert response.status_code == 200
        data = response.json()
        assert data["name"] == "Updated Name"

    @pytest.mark.integration
    async def test_delete_speaker(
        self,
        async_client: AsyncClient,
        test_speaker: Speaker
    ):
        """Testet DELETE /api/speakers/{id}"""
        response = await async_client.delete(f"/api/speakers/{test_speaker.id}")

        assert response.status_code == 200

        # Verify deleted
        response = await async_client.get(f"/api/speakers/{test_speaker.id}")
        assert response.status_code == 404


# ============================================================================
# Enrollment API Tests
# ============================================================================

class TestSpeakerEnrollmentAPI:
    """Tests für Speaker Enrollment API"""

    @pytest.mark.integration
    async def test_enroll_speaker(
        self,
        async_client: AsyncClient,
        test_speaker: Speaker,
        mock_audio_file,
        mock_speaker_service
    ):
        """Testet POST /api/speakers/{id}/enroll"""
        files = {"audio": ("test.wav", BytesIO(mock_audio_file), "audio/wav")}
        response = await async_client.post(
            f"/api/speakers/{test_speaker.id}/enroll",
            files=files
        )

        assert response.status_code == 200
        data = response.json()
        assert data["speaker_id"] == test_speaker.id
        assert "embedding_id" in data
        assert data["embedding_count"] >= 1

    @pytest.mark.integration
    async def test_enroll_nonexistent_speaker(
        self,
        async_client: AsyncClient,
        mock_audio_file,
        mock_speaker_service
    ):
        """Testet Enrollment für nicht-existenten Speaker"""
        files = {"audio": ("test.wav", BytesIO(mock_audio_file), "audio/wav")}
        response = await async_client.post(
            "/api/speakers/99999/enroll",
            files=files
        )

        assert response.status_code == 404

    @pytest.mark.integration
    async def test_enroll_service_unavailable(
        self,
        async_client: AsyncClient,
        test_speaker: Speaker,
        mock_audio_file
    ):
        """Testet Enrollment wenn Service nicht verfügbar"""
        with patch('api.routes.speakers.get_speaker_service') as mock:
            service = MagicMock()
            service.is_available.return_value = False
            mock.return_value = service

            files = {"audio": ("test.wav", BytesIO(mock_audio_file), "audio/wav")}
            response = await async_client.post(
                f"/api/speakers/{test_speaker.id}/enroll",
                files=files
            )

        assert response.status_code == 503


# ============================================================================
# Identification API Tests
# ============================================================================

class TestSpeakerIdentificationAPI:
    """Tests für Speaker Identification API"""

    @pytest.mark.integration
    async def test_identify_speaker(
        self,
        async_client: AsyncClient,
        speaker_with_embeddings: Speaker,
        mock_audio_file,
        mock_speaker_service
    ):
        """Testet POST /api/speakers/identify"""
        files = {"audio": ("test.wav", BytesIO(mock_audio_file), "audio/wav")}
        response = await async_client.post("/api/speakers/identify", files=files)

        assert response.status_code == 200
        data = response.json()
        assert "speaker_id" in data
        assert "confidence" in data
        assert "is_identified" in data

    @pytest.mark.integration
    async def test_identify_no_speakers(
        self,
        async_client: AsyncClient,
        mock_audio_file
    ):
        """Testet Identification ohne registrierte Sprecher"""
        with patch('api.routes.speakers.get_speaker_service') as mock:
            service = MagicMock()
            service.is_available.return_value = True
            service.extract_embedding_from_bytes.return_value = [0.1] * 192
            mock.return_value = service

            with patch('api.routes.speakers.get_speaker_embeddings_averaged') as mock_avg:
                mock_avg.return_value = []

                files = {"audio": ("test.wav", BytesIO(mock_audio_file), "audio/wav")}
                response = await async_client.post("/api/speakers/identify", files=files)

        assert response.status_code == 200
        data = response.json()
        assert data["is_identified"] is False


# ============================================================================
# Verification API Tests
# ============================================================================

class TestSpeakerVerificationAPI:
    """Tests für Speaker Verification API"""

    @pytest.mark.integration
    async def test_verify_speaker(
        self,
        async_client: AsyncClient,
        speaker_with_embeddings: Speaker,
        mock_audio_file,
        mock_speaker_service
    ):
        """Testet POST /api/speakers/{id}/verify"""
        files = {"audio": ("test.wav", BytesIO(mock_audio_file), "audio/wav")}
        response = await async_client.post(
            f"/api/speakers/{speaker_with_embeddings.id}/verify",
            files=files
        )

        assert response.status_code == 200
        data = response.json()
        assert "is_verified" in data
        assert "confidence" in data

    @pytest.mark.integration
    async def test_verify_nonexistent_speaker(
        self,
        async_client: AsyncClient,
        mock_audio_file,
        mock_speaker_service
    ):
        """Testet Verification für nicht-existenten Speaker"""
        files = {"audio": ("test.wav", BytesIO(mock_audio_file), "audio/wav")}
        response = await async_client.post(
            "/api/speakers/99999/verify",
            files=files
        )

        assert response.status_code == 404


# ============================================================================
# Merge API Tests
# ============================================================================

class TestSpeakerMergeAPI:
    """Tests für Speaker Merge API"""

    @pytest.mark.integration
    async def test_merge_speakers(
        self,
        async_client: AsyncClient,
        db_session: AsyncSession
    ):
        """Testet POST /api/speakers/merge"""
        # Create two speakers
        source = Speaker(name="Source", alias="source")
        target = Speaker(name="Target", alias="target")
        db_session.add(source)
        db_session.add(target)
        await db_session.commit()
        await db_session.refresh(source)
        await db_session.refresh(target)

        # Add embedding to source
        embedding = SpeakerEmbedding(
            speaker_id=source.id,
            embedding="sourceembedding==",
            sample_duration=3000
        )
        db_session.add(embedding)
        await db_session.commit()

        response = await async_client.post(
            "/api/speakers/merge",
            json={
                "source_speaker_id": source.id,
                "target_speaker_id": target.id
            }
        )

        assert response.status_code == 200
        data = response.json()
        assert data["target_speaker_id"] == target.id
        assert data["merged_embedding_count"] == 1

    @pytest.mark.integration
    async def test_merge_same_speaker(
        self,
        async_client: AsyncClient,
        test_speaker: Speaker
    ):
        """Testet Merge mit gleichem Source und Target"""
        response = await async_client.post(
            "/api/speakers/merge",
            json={
                "source_speaker_id": test_speaker.id,
                "target_speaker_id": test_speaker.id
            }
        )

        assert response.status_code == 400


# ============================================================================
# Status API Tests
# ============================================================================

class TestSpeakerServiceStatusAPI:
    """Tests für Service Status API"""

    @pytest.mark.integration
    async def test_service_status_available(
        self,
        async_client: AsyncClient,
        mock_speaker_service
    ):
        """Testet GET /api/speakers/status"""
        response = await async_client.get("/api/speakers/status")

        assert response.status_code == 200
        data = response.json()
        assert data["available"] is True
        assert data["model_loaded"] is True

    @pytest.mark.integration
    async def test_service_status_unavailable(self, async_client: AsyncClient):
        """Testet Status wenn Service nicht verfügbar"""
        with patch('api.routes.speakers.get_speaker_service') as mock:
            service = MagicMock()
            service.is_available.return_value = False
            service._model_loaded = False
            mock.return_value = service

            with patch('api.routes.speakers.SPEECHBRAIN_ERROR', "Not installed"):
                response = await async_client.get("/api/speakers/status")

        assert response.status_code == 200
        data = response.json()
        assert data["available"] is False
