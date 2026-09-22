"""No voiceprint is persisted outside an explicitly enabled speaker feature.

Two gaps in the "speaker recognition off → no biometric persistence" promise:

1. Meetings: ``process_meeting`` stored the voice-server's per-cluster ECAPA
   ``embedding`` on every segment of ``Meeting.segments`` — regardless of any
   flag — and ``GET /api/meetings/{id}/segments`` returned it. Fingerprint
   matching (which persists a centroid) only checked
   ``meeting_fingerprints_enabled``.
2. Admin enrollment (``POST /api/speakers/enroll``, ``/candidates/promote``,
   ``/{id}/enroll``) wrote ``SpeakerEmbedding`` rows with recognition off.
"""
from __future__ import annotations

import importlib.util
from io import BytesIO
from pathlib import Path
from unittest.mock import AsyncMock, patch

import numpy as np
import pytest
from sqlalchemy import func, select

from models.database import (
    Document,
    Meeting,
    MeetingSpeakerFingerprint,
    Speaker,
    SpeakerCandidate,
    SpeakerEmbedding,
)
from utils.config import settings

_DIM = 192


def _unit(seed: int) -> list[float]:
    v = np.random.default_rng(seed).standard_normal(_DIM)
    return (v / np.linalg.norm(v)).astype(np.float32).tolist()


def _raw_response() -> dict:
    """The /transcribe-meeting shape: one ECAPA embedding per cluster, duplicated
    across that cluster's segments."""
    a, b = _unit(1), _unit(2)
    return {"segments": [
        {"speaker": "SPEAKER_00", "start_s": 0.0, "end_s": 1.0, "text": "hi", "embedding": a},
        {"speaker": "SPEAKER_01", "start_s": 1.0, "end_s": 2.0, "text": "yo", "embedding": b},
        {"speaker": "SPEAKER_00", "start_s": 2.0, "end_s": 3.0, "text": "ok", "embedding": a},
    ]}


class _SessionCtx:
    def __init__(self, sess):
        self.sess = sess

    async def __aenter__(self):
        return self.sess

    async def __aexit__(self, *a):
        return None


def _no_voiceprint(segments) -> bool:
    return all(
        not ({"embedding", "speaker_embedding", "centroid", "voiceprint"} & set(s))
        for s in segments
    )


async def _count(db, model) -> int:
    return (await db.execute(select(func.count(model.id)))).scalar()


# --- pure helper --------------------------------------------------------------


class TestStripBiometricFields:
    def test_strips_all_voiceprint_keys_keeps_the_rest(self):
        from services.meeting_pipeline import strip_biometric_fields

        seg = {
            "speaker": "Sprecher 1", "speaker_key": "SPEAKER_00", "text": "hi",
            "start_s": 0.0, "end_s": 1.0, "fingerprint_id": 3, "fingerprint_label": "Speaker AB",
            "embedding": [0.1], "speaker_embedding": [0.2], "centroid": [0.3], "voiceprint": "x",
        }
        out = strip_biometric_fields([seg])
        assert out == [{
            "speaker": "Sprecher 1", "speaker_key": "SPEAKER_00", "text": "hi",
            "start_s": 0.0, "end_s": 1.0, "fingerprint_id": 3, "fingerprint_label": "Speaker AB",
        }]
        assert "embedding" in seg  # input not mutated

    def test_none_and_malformed(self):
        from services.meeting_pipeline import strip_biometric_fields

        assert strip_biometric_fields(None) == []
        assert strip_biometric_fields([{"text": "a"}, "junk", None]) == [{"text": "a"}]

    def test_pseudonyms_drop_embedding(self):
        from services.meeting_pipeline import apply_pseudonyms

        out = apply_pseudonyms(_raw_response()["segments"])
        assert _no_voiceprint(out)
        assert [s["speaker"] for s in out] == ["Sprecher 1", "Sprecher 2", "Sprecher 1"]


# --- process_meeting ------------------------------------------------------------


@pytest.mark.database
@pytest.mark.asyncio
class TestMeetingPipelinePersistence:
    async def _run(self, db_session, monkeypatch, *, doc_id=None):
        from models.database import Document
        from services import meeting_pipeline as mp

        if doc_id is None:
            # A real transcript Document: `meetings.transcript_document_id` is a
            # foreign key, so the id the fake ingest returns has to exist.
            doc = Document(filename="t.md", file_path="/tmp/t.md", status="completed")
            db_session.add(doc)
            await db_session.flush()
            doc_id = doc.id

        monkeypatch.setattr(mp, "AsyncSessionLocal", lambda: _SessionCtx(db_session))

        async def _fake_transcribe(path, **kw):
            return _raw_response()

        async def _fake_ingest(db, meeting, markdown):
            assert "0.1" not in markdown  # sanity: rendered text is not the vector dump
            return doc_id

        monkeypatch.setattr(mp, "transcribe_meeting", _fake_transcribe)
        monkeypatch.setattr(mp, "_ingest_transcript", _fake_ingest)
        from tests.backend.dbrows import ensure_user

        await ensure_user(db_session, 1)
        m = Meeting(status="processing", title="Sync", owner_user_id=1, circle_tier=2,
                    consent_confirmed=True)
        db_session.add(m)
        await db_session.commit()
        await mp.process_meeting(m.id, "/x/meeting.wav")
        await db_session.refresh(m)
        return m

    async def test_embedding_not_persisted_features_off(self, db_session, monkeypatch):
        monkeypatch.setattr(settings, "meeting_fingerprints_enabled", False)
        m = await self._run(db_session, monkeypatch)
        assert m.status == "completed"
        assert len(m.segments) == 3
        assert _no_voiceprint(m.segments)
        assert await _count(db_session, MeetingSpeakerFingerprint) == 0

    async def test_fingerprints_still_resolve_when_enabled(self, db_session, monkeypatch):
        monkeypatch.setattr(settings, "meeting_fingerprints_enabled", True)
        monkeypatch.setattr(settings, "speaker_recognition_enabled", True)
        m = await self._run(db_session, monkeypatch)
        # matching read the RAW embeddings: two clusters → two fingerprints …
        assert await _count(db_session, MeetingSpeakerFingerprint) == 2
        by_key = {s["speaker_key"]: s.get("fingerprint_id") for s in m.segments}
        assert by_key["SPEAKER_00"] and by_key["SPEAKER_01"]
        assert by_key["SPEAKER_00"] != by_key["SPEAKER_01"]
        # … yet the persisted segments carry no vector
        assert _no_voiceprint(m.segments)

    async def test_fingerprints_off_when_recognition_off(self, db_session, monkeypatch):
        monkeypatch.setattr(settings, "meeting_fingerprints_enabled", True)
        monkeypatch.setattr(settings, "speaker_recognition_enabled", False)
        m = await self._run(db_session, monkeypatch)
        assert await _count(db_session, MeetingSpeakerFingerprint) == 0
        assert all("fingerprint_id" not in s for s in m.segments)
        assert _no_voiceprint(m.segments)

    async def test_resolver_itself_refuses_when_recognition_off(self, db_session, monkeypatch):
        from services.meeting_fingerprint_service import resolve_meeting_fingerprints

        monkeypatch.setattr(settings, "meeting_fingerprints_enabled", True)
        monkeypatch.setattr(settings, "speaker_recognition_enabled", False)
        from tests.backend.dbrows import ensure_user

        await ensure_user(db_session, 1)
        m = Meeting(status="completed", owner_user_id=1, circle_tier=2, consent_confirmed=True)
        db_session.add(m)
        await db_session.flush()
        assert await resolve_meeting_fingerprints(db_session, m, _raw_response()["segments"]) == {}
        assert await _count(db_session, MeetingSpeakerFingerprint) == 0


# --- re-render paths clean legacy rows --------------------------------------------


def _legacy_segments():
    a = _unit(5)
    return [
        {"speaker": "Sprecher 1", "speaker_key": "SPEAKER_00", "text": "hi", "embedding": a,
         "fingerprint_id": 9},
        {"speaker": "Sprecher 2", "speaker_key": "SPEAKER_01", "text": "yo", "embedding": _unit(6)},
    ]


@pytest.mark.database
@pytest.mark.asyncio
class TestRerenderPaths:
    async def test_reattribute_strips_legacy_embedding(self, db_session, monkeypatch, tmp_path):
        from services import meeting_pipeline as mp

        doc_file = tmp_path / "meeting.md"
        doc_file.write_text("old")
        doc = Document(filename="meeting.md", file_path=str(doc_file), status="completed")
        db_session.add(doc)
        await db_session.commit()
        m = Meeting(status="completed", title="T", transcript_document_id=doc.id,
                    segments=_legacy_segments())
        db_session.add(m)
        await db_session.commit()

        class _FakeQ:
            def __init__(self, *a, **k):
                pass

            async def enqueue(self, params):
                return "1-0"

        import services.task_queue as tq
        monkeypatch.setattr(tq, "DocumentTaskQueue", _FakeQ)
        monkeypatch.setattr("services.redis_client.get_redis", lambda: None)

        assert await mp.reattribute(db_session, m, "SPEAKER_00", "Alice") is True
        await db_session.refresh(m)
        assert _no_voiceprint(m.segments)
        assert {s["speaker_key"]: s["speaker"] for s in m.segments}["SPEAKER_00"] == "Alice"
        assert m.segments[0]["fingerprint_id"] == 9  # anonymous id kept
        assert "Alice" in doc_file.read_text()  # transcript re-rendered in place

    async def test_relabel_by_fingerprint_strips_legacy_embedding(self, db_session):
        from services.meeting_pipeline import _relabel_by_fingerprint

        m = Meeting(status="completed", segments=_legacy_segments())
        db_session.add(m)
        await db_session.commit()
        assert _relabel_by_fingerprint(m, 9, "Anna") is True
        await db_session.commit()
        await db_session.refresh(m)
        assert _no_voiceprint(m.segments)
        assert m.segments[0]["speaker"] == "Anna"


# --- segments API -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_segments_api_never_returns_embedding(async_client, db_session, monkeypatch):
    monkeypatch.setattr(settings, "meeting_transcription_enabled", True)
    monkeypatch.setattr(settings, "auth_enabled", False)
    m = Meeting(status="completed", segments=_legacy_segments())
    db_session.add(m)
    await db_session.commit()
    r = await async_client.get(f"/api/meetings/{m.id}/segments")
    assert r.status_code == 200
    segs = r.json()["segments"]
    assert len(segs) == 2
    assert _no_voiceprint(segs)


# --- purge script (not run against prod; logic only) ------------------------------


def _load_purge_script():
    path = Path(__file__).resolve().parents[2] / "bin" / "purge_meeting_segment_embeddings.py"
    spec = importlib.util.spec_from_file_location("purge_meeting_segment_embeddings", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_purge_script_counts_voiceprint_segments():
    mod = _load_purge_script()
    assert mod.count_biometric_segments(None) == 0
    assert mod.count_biometric_segments([{"text": "a"}]) == 0
    assert mod.count_biometric_segments(_legacy_segments()) == 2


@pytest.mark.database
@pytest.mark.asyncio
async def test_purge_script_dry_run_then_commit(db_session, monkeypatch):
    mod = _load_purge_script()
    monkeypatch.setattr(mod, "AsyncSessionLocal", lambda: _SessionCtx(db_session))
    dirty = Meeting(status="completed", segments=_legacy_segments())
    clean = Meeting(status="completed", segments=[{"speaker": "Sprecher 1", "text": "x"}])
    db_session.add_all([dirty, clean])
    await db_session.commit()

    assert await mod.run(commit=False, limit=None) == (1, 2)
    await db_session.refresh(dirty)
    assert not _no_voiceprint(dirty.segments)  # dry run wrote nothing

    assert await mod.run(commit=True, limit=None) == (1, 2)
    await db_session.refresh(dirty)
    assert _no_voiceprint(dirty.segments)
    assert await mod.run(commit=False, limit=None) == (0, 0)  # idempotent


# --- admin speaker enrollment -----------------------------------------------------


def _wav() -> bytes:
    return b"RIFF\x24\x00\x00\x00WAVEfmt " + b"\x00" * 1000


@pytest.mark.asyncio
class TestEnrollmentRoutesRefuseWhenRecognitionOff:
    @pytest.fixture(autouse=True)
    def _off(self, monkeypatch):
        monkeypatch.setattr(settings, "speaker_recognition_enabled", False)
        # Make sure the recognition gate — not the in-process P0 guard — refuses.
        monkeypatch.setattr(settings, "speaker_inprocess_embeddings_enabled", True)

    async def test_controlled_enroll_409(self, async_client, db_session):
        with patch("services.voice_server_client.stt", AsyncMock()) as stt:
            r = await async_client.post(
                "/api/speakers/enroll",
                data={"name": "Anna"},
                files=[("audio", ("a.wav", BytesIO(_wav()), "audio/wav"))],
            )
        assert r.status_code == 409
        assert "SPEAKER_RECOGNITION_ENABLED" in r.json()["detail"]
        stt.assert_not_called()
        assert await _count(db_session, Speaker) == 0
        assert await _count(db_session, SpeakerEmbedding) == 0

    async def test_promote_candidates_409(self, async_client, db_session):
        c = SpeakerCandidate(embedding="AAAA", best_score=0.4)
        db_session.add(c)
        await db_session.commit()
        r = await async_client.post(
            "/api/speakers/candidates/promote",
            json={"candidate_ids": [c.id], "name": "Anna"},
        )
        assert r.status_code == 409
        assert await _count(db_session, SpeakerEmbedding) == 0
        assert await _count(db_session, SpeakerCandidate) == 1  # not consumed

    async def test_legacy_enroll_409(self, async_client, db_session):
        sp = Speaker(name="Anna", alias="anna")
        db_session.add(sp)
        await db_session.commit()
        with patch("api.routes.speakers.get_speaker_service") as svc:
            r = await async_client.post(
                f"/api/speakers/{sp.id}/enroll",
                files={"audio": ("a.wav", BytesIO(_wav()), "audio/wav")},
            )
        assert r.status_code == 409
        svc.assert_not_called()
        assert await _count(db_session, SpeakerEmbedding) == 0


@pytest.mark.database
@pytest.mark.asyncio
class TestEnrollmentServiceRefusesWhenRecognitionOff:
    async def test_enroll_speaker_controlled(self, db_session, monkeypatch):
        from services.speaker_enrollment_service import enroll_speaker_controlled

        monkeypatch.setattr(settings, "speaker_recognition_enabled", False)
        monkeypatch.setattr(settings, "voice_server_url", "http://vs")
        with patch("services.voice_server_client.stt", AsyncMock()) as stt:
            res = await enroll_speaker_controlled(
                db_session, name="Anna", samples=[(b"a", "a.wav")] * 3,
            )
        assert res["ok"] is False
        assert "SPEAKER_RECOGNITION_ENABLED" in res["reason"]
        stt.assert_not_called()
        assert await _count(db_session, SpeakerEmbedding) == 0

    async def test_promote_candidates(self, db_session, monkeypatch):
        from services.speaker_enrollment_service import promote_candidates

        monkeypatch.setattr(settings, "speaker_recognition_enabled", False)
        c = SpeakerCandidate(embedding="AAAA", best_score=0.4)
        db_session.add(c)
        await db_session.commit()
        res = await promote_candidates(db_session, candidate_ids=[c.id], name="Anna")
        assert res["ok"] is False
        assert await _count(db_session, SpeakerEmbedding) == 0
        assert await _count(db_session, SpeakerCandidate) == 1
