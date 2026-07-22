"""§2 Track A — cross-meeting anonymous fingerprint matching.

Unit tests for services.meeting_fingerprint_service (sqlite db_session; matching
reads centroid_b64, so no pgvector needed). Embeddings are crafted so cosine is
deterministic: identical → match, orthogonal → new, ambiguous → prefer split.
"""
import numpy as np
import pytest

from models.database import Meeting, MeetingSpeakerFingerprint
from services.meeting_fingerprint_service import (
    annotate_segments,
    resolve_meeting_fingerprints,
)
from services.speaker_service import SpeakerService
from utils.config import settings

pytestmark = [pytest.mark.backend, pytest.mark.database]

_DIM = 192


def _unit(v):
    return (v / np.linalg.norm(v)).astype(np.float32)


def _rand_unit(seed):
    return _unit(np.random.default_rng(seed).standard_normal(_DIM))


def _raw_seg(cluster, emb, text="hi"):
    return {"speaker": cluster, "start_s": 0.0, "end_s": 1.0, "text": text,
            "embedding": np.asarray(emb, dtype=np.float32).tolist()}


async def _meeting(db, owner_user_id=1, tier=2):
    m = Meeting(owner_user_id=owner_user_id, circle_tier=tier, status="completed",
                consent_confirmed=True)
    db.add(m)
    await db.flush()
    return m


async def _add_fp(db, unit_vec, owner_user_id=1, tier=2, label="Speaker SEED"):
    fp = MeetingSpeakerFingerprint(
        owner_user_id=owner_user_id, label=label, circle_tier=tier, sample_count=1,
        centroid_b64=SpeakerService.embedding_to_base64(unit_vec),
    )
    db.add(fp)
    await db.flush()
    return fp


@pytest.fixture(autouse=True)
def _enable(monkeypatch):
    monkeypatch.setattr(settings, "meeting_fingerprints_enabled", True)
    monkeypatch.setattr(settings, "meeting_fingerprint_match_threshold", 0.60)
    monkeypatch.setattr(settings, "meeting_fingerprint_match_margin", 0.05)


async def _count_fps(db):
    from sqlalchemy import func, select
    return (await db.execute(select(func.count(MeetingSpeakerFingerprint.id)))).scalar()


class TestFingerprintMatching:
    async def test_disabled_returns_empty(self, db_session, monkeypatch):
        monkeypatch.setattr(settings, "meeting_fingerprints_enabled", False)
        m = await _meeting(db_session)
        resolved = await resolve_meeting_fingerprints(
            db_session, m, [_raw_seg("SPEAKER_00", _rand_unit(1))])
        assert resolved == {}
        assert await _count_fps(db_session) == 0

    async def test_mints_new_for_distinct_clusters(self, db_session):
        m = await _meeting(db_session)
        segs = [_raw_seg("SPEAKER_00", _rand_unit(1)), _raw_seg("SPEAKER_01", _rand_unit(2))]
        resolved = await resolve_meeting_fingerprints(db_session, m, segs)
        assert set(resolved) == {"SPEAKER_00", "SPEAKER_01"}
        assert resolved["SPEAKER_00"]["fingerprint_id"] != resolved["SPEAKER_01"]["fingerprint_id"]
        assert await _count_fps(db_session) == 2

    async def test_matches_same_person_across_meetings(self, db_session):
        person = _rand_unit(7)
        m1 = await _meeting(db_session)
        r1 = await resolve_meeting_fingerprints(db_session, m1, [_raw_seg("SPEAKER_00", person)])
        fp_id = r1["SPEAKER_00"]["fingerprint_id"]

        # A second meeting with (near-)identical audio → SAME fingerprint, folded.
        m2 = await _meeting(db_session)
        near = _unit(person + 0.01 * _rand_unit(99))  # cosine ~0.9999
        r2 = await resolve_meeting_fingerprints(db_session, m2, [_raw_seg("SPEAKER_00", near)])
        assert r2["SPEAKER_00"]["fingerprint_id"] == fp_id
        fp = await db_session.get(MeetingSpeakerFingerprint, fp_id)
        assert fp.sample_count == 2
        assert await _count_fps(db_session) == 1

    async def test_distinct_person_mints_new(self, db_session):
        m1 = await _meeting(db_session)
        await resolve_meeting_fingerprints(db_session, m1, [_raw_seg("SPEAKER_00", _rand_unit(3))])
        m2 = await _meeting(db_session)
        await resolve_meeting_fingerprints(db_session, m2, [_raw_seg("SPEAKER_00", _rand_unit(4))])
        assert await _count_fps(db_session) == 2  # orthogonal → two identities

    async def test_two_clusters_one_meeting_never_collapse(self, db_session):
        """Diarization already separated them → two clusters in ONE meeting are
        distinct people even if their centroids are similar."""
        base = _rand_unit(11)
        similar = _unit(base + 0.05 * _rand_unit(12))  # cosine well above threshold
        m = await _meeting(db_session)
        resolved = await resolve_meeting_fingerprints(
            db_session, m, [_raw_seg("SPEAKER_00", base), _raw_seg("SPEAKER_01", similar)])
        assert (resolved["SPEAKER_00"]["fingerprint_id"]
                != resolved["SPEAKER_01"]["fingerprint_id"])
        assert await _count_fps(db_session) == 2

    async def test_owner_isolation(self, db_session):
        person = _rand_unit(21)
        await _add_fp(db_session, person, owner_user_id=1, label="Speaker OWNER1")
        m = await _meeting(db_session, owner_user_id=2)  # different owner, same audio
        resolved = await resolve_meeting_fingerprints(db_session, m, [_raw_seg("SPEAKER_00", person)])
        # owner 2 must NOT reuse owner 1's fingerprint → a new one for owner 2.
        assert resolved["SPEAKER_00"]["fingerprint_label"] != "Speaker OWNER1"
        assert await _count_fps(db_session) == 2

    async def test_tier_isolation(self, db_session):
        person = _rand_unit(31)
        await _add_fp(db_session, person, owner_user_id=1, tier=1, label="Speaker TIER1")
        m = await _meeting(db_session, owner_user_id=1, tier=2)  # same owner, different tier
        resolved = await resolve_meeting_fingerprints(db_session, m, [_raw_seg("SPEAKER_00", person)])
        assert resolved["SPEAKER_00"]["fingerprint_label"] != "Speaker TIER1"
        assert await _count_fps(db_session) == 2

    async def test_ambiguous_match_prefers_split(self, db_session):
        """A cluster near-equidistant to two fingerprints (best - second < margin)
        is ambiguous → mint a new one rather than risk a false merge."""
        a = _rand_unit(41)
        b = _unit(a + 0.25 * _rand_unit(42))  # a,b similar (cos high) but two rows
        await _add_fp(db_session, a, label="Speaker A")
        await _add_fp(db_session, b, label="Speaker B")
        query = _unit(a + b)  # symmetric → cos(query,a) == cos(query,b), diff 0 < margin
        m = await _meeting(db_session)
        resolved = await resolve_meeting_fingerprints(db_session, m, [_raw_seg("SPEAKER_00", query)])
        assert resolved["SPEAKER_00"]["fingerprint_label"] not in {"Speaker A", "Speaker B"}
        assert await _count_fps(db_session) == 3

    async def test_annotate_segments_rides_identity(self):
        segs = [{"speaker": "Sprecher 1", "speaker_key": "SPEAKER_00", "text": "x"},
                {"speaker": "Sprecher 2", "speaker_key": "SPEAKER_01", "text": "y"}]
        annotate_segments(segs, {"SPEAKER_00": {"fingerprint_id": 5, "fingerprint_label": "Speaker AB"}})
        assert segs[0]["fingerprint_id"] == 5 and segs[0]["fingerprint_label"] == "Speaker AB"
        assert "fingerprint_id" not in segs[1]  # no resolution for this cluster
