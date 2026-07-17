"""Tests for the §2 meeting-transcription feature.

This module starts with the Meeting model + defaults (task #6); route/worker
tests are added alongside their code in later tasks.
"""
from datetime import date, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.database import Meeting
from utils.config import settings


@pytest.mark.database
class TestMeetingModel:
    async def test_meeting_defaults(self, db_session: AsyncSession):
        """A fresh meeting gets the lean §2 defaults: pending, tier 2, no consent."""
        meeting = Meeting(title="Standup")
        db_session.add(meeting)
        await db_session.commit()
        await db_session.refresh(meeting)

        assert meeting.id is not None
        assert meeting.status == "pending"
        assert meeting.circle_tier == 2
        assert meeting.consent_confirmed is False
        assert meeting.segments is None
        assert meeting.transcript_document_id is None
        assert meeting.heartbeat_at is None
        assert meeting.created_at is not None

    async def test_meeting_segments_jsonb_roundtrip(self, db_session: AsyncSession):
        """segments stores/returns a list of turn dicts unchanged (JSONB)."""
        segments = [
            {"speaker": "Sprecher 1", "start_s": 0.0, "end_s": 3.2, "text": "Hallo."},
            {"speaker": "Sprecher 2", "start_s": 3.2, "end_s": 6.0, "text": "Guten Tag."},
        ]
        meeting = Meeting(
            title="Kickoff",
            date=date(2026, 7, 14),
            status="completed",
            segments=segments,
            consent_confirmed=True,
            consent_note="Alle Teilnehmer informiert",
            retention_until=datetime.utcnow() + timedelta(days=30),
        )
        db_session.add(meeting)
        await db_session.commit()
        await db_session.refresh(meeting)

        reloaded = (
            await db_session.execute(select(Meeting).where(Meeting.id == meeting.id))
        ).scalar_one()
        assert reloaded.segments == segments
        assert reloaded.segments[1]["speaker"] == "Sprecher 2"
        assert reloaded.consent_confirmed is True
        assert reloaded.date == date(2026, 7, 14)

    async def test_meeting_heartbeat_and_status_transitions(self, db_session: AsyncSession):
        """status + heartbeat_at are the in-flight guard the worker maintains."""
        meeting = Meeting(status="pending")
        db_session.add(meeting)
        await db_session.commit()

        # worker claims it
        meeting.status = "processing"
        meeting.heartbeat_at = datetime.utcnow()
        await db_session.commit()
        await db_session.refresh(meeting)
        assert meeting.status == "processing"
        assert meeting.heartbeat_at is not None

        # worker finishes
        meeting.status = "completed"
        await db_session.commit()
        await db_session.refresh(meeting)
        assert meeting.status == "completed"


# ---------------------------------------------------------------------------
# Route tests — /api/meetings (SQLite harness, worker + queue mocked)
# ---------------------------------------------------------------------------

class _FakeQueue:
    """Records enqueued payloads instead of touching Redis."""

    enqueued: list[dict] = []

    def __init__(self, *args, **kwargs):
        pass

    async def enqueue(self, params: dict) -> str:
        _FakeQueue.enqueued.append(params)
        return "1-0"


def _override_user(user) -> None:
    from main import app
    from services.auth_service import get_optional_user

    app.dependency_overrides[get_optional_user] = lambda: user


def _enable(monkeypatch, tmp_path, *, auth: bool, worker_alive: bool = True) -> None:
    from api.routes import meetings

    monkeypatch.setattr(settings, "meeting_transcription_enabled", True)
    monkeypatch.setattr(settings, "auth_enabled", auth)
    monkeypatch.setattr(settings, "upload_dir", str(tmp_path))

    async def _alive() -> bool:
        return worker_alive

    monkeypatch.setattr(meetings, "_meeting_worker_is_alive", _alive)
    monkeypatch.setattr(meetings, "get_redis", lambda: None)
    monkeypatch.setattr(meetings, "MeetingTaskQueue", _FakeQueue)
    _FakeQueue.enqueued = []


def _wav() -> tuple:
    # minimal non-empty payload; the route never decodes it (the worker does).
    return ("meeting.wav", b"RIFF\x00\x00\x00\x00WAVEdata some audio bytes", "audio/wav")


@pytest.mark.asyncio
class TestMeetingRoutes:
    async def test_routes_404_when_flag_off(self, async_client, monkeypatch):
        monkeypatch.setattr(settings, "meeting_transcription_enabled", False)
        r = await async_client.post(
            "/api/meetings/transcribe",
            files={"audio": _wav()}, data={"consent_confirmed": "true"},
        )
        assert r.status_code == 404
        assert (await async_client.get("/api/meetings/1")).status_code == 404

    async def test_consent_required_422(self, async_client, monkeypatch, tmp_path):
        _enable(monkeypatch, tmp_path, auth=False)
        r = await async_client.post(
            "/api/meetings/transcribe",
            files={"audio": _wav()}, data={"consent_confirmed": "false"},
        )
        assert r.status_code == 422
        # missing entirely -> FastAPI validation 422
        r2 = await async_client.post("/api/meetings/transcribe", files={"audio": _wav()})
        assert r2.status_code == 422
        assert _FakeQueue.enqueued == []

    async def test_unsupported_format_422(self, async_client, monkeypatch, tmp_path):
        _enable(monkeypatch, tmp_path, auth=False)
        r = await async_client.post(
            "/api/meetings/transcribe",
            files={"audio": ("notes.txt", b"hello", "text/plain")},
            data={"consent_confirmed": "true"},
        )
        assert r.status_code == 422
        assert _FakeQueue.enqueued == []

    async def test_worker_down_503(self, async_client, monkeypatch, tmp_path):
        _enable(monkeypatch, tmp_path, auth=False, worker_alive=False)
        r = await async_client.post(
            "/api/meetings/transcribe",
            files={"audio": _wav()}, data={"consent_confirmed": "true"},
        )
        assert r.status_code == 503
        assert _FakeQueue.enqueued == []

    async def test_happy_path_202_creates_row_and_enqueues(
        self, async_client, db_session, monkeypatch, tmp_path
    ):
        _enable(monkeypatch, tmp_path, auth=False)
        r = await async_client.post(
            "/api/meetings/transcribe",
            files={"audio": _wav()},
            data={"consent_confirmed": "true", "title": "Standup", "date": "2026-07-14"},
        )
        assert r.status_code == 202, r.text
        body = r.json()
        assert body["status"] == "pending"
        assert body["title"] == "Standup"
        assert body["date"] == "2026-07-14"
        mid = body["id"]

        # row persisted with consent, audio streamed to disk, path enqueued.
        meeting = await db_session.get(Meeting, mid)
        assert meeting is not None
        assert meeting.consent_confirmed is True
        assert len(_FakeQueue.enqueued) == 1
        payload = _FakeQueue.enqueued[0]
        assert payload["meeting_id"] == mid
        assert payload["audio_path"].endswith(f"meeting-{mid}.wav")
        assert (tmp_path / "meetings" / f"meeting-{mid}.wav").exists()

    async def test_bad_date_422(self, async_client, monkeypatch, tmp_path):
        _enable(monkeypatch, tmp_path, auth=False)
        r = await async_client.post(
            "/api/meetings/transcribe",
            files={"audio": _wav()},
            data={"consent_confirmed": "true", "date": "14.07.2026"},
        )
        assert r.status_code == 422

    async def test_status_poll_and_owner_gating(self, async_client, monkeypatch, tmp_path):
        from models.database import User

        _enable(monkeypatch, tmp_path, auth=True)
        user_a = User(id=1, username="a", password_hash="x", is_active=True, role_id=1)
        user_b = User(id=2, username="b", password_hash="x", is_active=True, role_id=1)

        _override_user(user_a)
        created = await async_client.post(
            "/api/meetings/transcribe",
            files={"audio": _wav()}, data={"consent_confirmed": "true"},
        )
        assert created.status_code == 202
        mid = created.json()["id"]

        # owner A can poll
        _override_user(user_a)
        assert (await async_client.get(f"/api/meetings/{mid}")).status_code == 200
        # user B is owner-gated 404 (never leak existence)
        _override_user(user_b)
        assert (await async_client.get(f"/api/meetings/{mid}")).status_code == 404
        # missing -> 404
        _override_user(user_a)
        assert (await async_client.get("/api/meetings/999999")).status_code == 404

    async def test_list_404_when_flag_off(self, async_client, monkeypatch):
        monkeypatch.setattr(settings, "meeting_transcription_enabled", False)
        assert (await async_client.get("/api/meetings")).status_code == 404

    async def test_list_owner_scoped_newest_first(
        self, async_client, monkeypatch, tmp_path
    ):
        from models.database import User

        _enable(monkeypatch, tmp_path, auth=True)
        user_a = User(id=1, username="a", password_hash="x", is_active=True, role_id=1)
        user_b = User(id=2, username="b", password_hash="x", is_active=True, role_id=1)

        # A uploads two, B uploads one
        _override_user(user_a)
        first = (await async_client.post(
            "/api/meetings/transcribe",
            files={"audio": _wav()}, data={"consent_confirmed": "true", "title": "First"},
        )).json()["id"]
        second = (await async_client.post(
            "/api/meetings/transcribe",
            files={"audio": _wav()}, data={"consent_confirmed": "true", "title": "Second"},
        )).json()["id"]
        _override_user(user_b)
        await async_client.post(
            "/api/meetings/transcribe",
            files={"audio": _wav()}, data={"consent_confirmed": "true", "title": "B-only"},
        )

        # A sees only A's meetings, newest first
        _override_user(user_a)
        rows = (await async_client.get("/api/meetings")).json()
        assert [r["id"] for r in rows] == [second, first]
        assert {r["title"] for r in rows} == {"First", "Second"}

    async def test_list_auth_off_sees_all(self, async_client, monkeypatch, tmp_path):
        _enable(monkeypatch, tmp_path, auth=False)
        _override_user(None)
        await async_client.post(
            "/api/meetings/transcribe",
            files={"audio": _wav()}, data={"consent_confirmed": "true"},
        )
        rows = (await async_client.get("/api/meetings")).json()
        assert len(rows) >= 1

    async def test_delete_owner_gated_removes_row_and_audio(
        self, async_client, monkeypatch, tmp_path
    ):
        import os
        from models.database import User

        _enable(monkeypatch, tmp_path, auth=True)
        user_a = User(id=1, username="a", password_hash="x", is_active=True, role_id=1)
        user_b = User(id=2, username="b", password_hash="x", is_active=True, role_id=1)

        _override_user(user_a)
        created = await async_client.post(
            "/api/meetings/transcribe",
            files={"audio": _wav()}, data={"consent_confirmed": "true"},
        )
        mid = created.json()["id"]
        audio = os.path.join(str(tmp_path), "meetings", f"meeting-{mid}.wav")
        assert os.path.exists(audio)

        # user B cannot delete A's meeting (owner-gated 404), and A's audio survives
        _override_user(user_b)
        assert (await async_client.delete(f"/api/meetings/{mid}")).status_code == 404
        assert os.path.exists(audio)

        # owner A deletes it -> row + audio gone
        _override_user(user_a)
        r = await async_client.delete(f"/api/meetings/{mid}")
        assert r.status_code == 200 and r.json()["status"] == "deleted"
        assert (await async_client.get(f"/api/meetings/{mid}")).status_code == 404
        assert not os.path.exists(audio)

    async def test_delete_404_when_flag_off(self, async_client, monkeypatch):
        monkeypatch.setattr(settings, "meeting_transcription_enabled", False)
        assert (await async_client.delete("/api/meetings/1")).status_code == 404

    async def test_list_limit_caps_rows_and_rejects_out_of_range(
        self, async_client, monkeypatch, tmp_path
    ):
        _enable(monkeypatch, tmp_path, auth=False)
        _override_user(None)
        for _ in range(3):
            await async_client.post(
                "/api/meetings/transcribe",
                files={"audio": _wav()}, data={"consent_confirmed": "true"},
            )
        # limit actually caps the returned rows
        assert len((await async_client.get("/api/meetings?limit=2")).json()) == 2
        # out-of-range limits are 422 (ge=1, le=200)
        assert (await async_client.get("/api/meetings?limit=0")).status_code == 422
        assert (await async_client.get("/api/meetings?limit=201")).status_code == 422


# ---------------------------------------------------------------------------
# Pipeline pure functions — pseudonyms + render (no GPU, no DB)
# ---------------------------------------------------------------------------

class TestPseudonyms:
    def test_stable_first_appearance_mapping(self):
        from services.meeting_pipeline import apply_pseudonyms

        raw = [
            {"speaker": "SPEAKER_01", "text": "a"},
            {"speaker": "SPEAKER_00", "text": "b"},
            {"speaker": "SPEAKER_01", "text": "c"},
        ]
        out = apply_pseudonyms(raw)
        # first-appearance order: SPEAKER_01 -> Sprecher 1, SPEAKER_00 -> Sprecher 2
        assert [s["speaker"] for s in out] == ["Sprecher 1", "Sprecher 2", "Sprecher 1"]

    def test_preserves_fields_and_retains_cluster_key(self):
        from services.meeting_pipeline import apply_pseudonyms

        out = apply_pseudonyms([{"speaker": "SPEAKER_00", "text": "hi", "start_s": 1.0, "embedding": [0.1]}])
        assert out[0]["speaker"] == "Sprecher 1"
        assert out[0]["speaker_key"] == "SPEAKER_00"  # original cluster id retained
        assert out[0]["start_s"] == 1.0
        assert out[0]["embedding"] == [0.1]

    def test_render_markdown_skips_empty_turns(self, ):
        from datetime import date as _d

        from services.meeting_pipeline import render_transcript_markdown

        meeting = Meeting(id=7, title="Standup", date=_d(2026, 7, 14))
        md = render_transcript_markdown(meeting, [
            {"speaker": "Sprecher 1", "text": "Hallo"},
            {"speaker": "Sprecher 2", "text": "   "},   # blank -> skipped
            {"speaker": "Sprecher 2", "text": "Tag"},
        ])
        assert "# Standup" in md
        assert "2026-07-14" in md
        assert "**Sprecher 1:** Hallo" in md
        assert "**Sprecher 2:** Tag" in md
        assert md.count("**Sprecher 2:**") == 1  # the blank turn dropped


# ---------------------------------------------------------------------------
# Worker in-flight guard (row status + heartbeat_at) — real DB
# ---------------------------------------------------------------------------

class _SessionCtx:
    """Yields the shared test session without closing it (worker code does
    `async with AsyncSessionLocal() as db`)."""

    def __init__(self, sess):
        self.sess = sess

    async def __aenter__(self):
        return self.sess

    async def __aexit__(self, *a):
        return False


@pytest.mark.database
@pytest.mark.asyncio
class TestInFlightGuard:
    async def test_claim_states(self, db_session, monkeypatch):
        from datetime import datetime, timedelta

        import workers.meeting_worker as mw

        monkeypatch.setattr(mw, "AsyncSessionLocal", lambda: _SessionCtx(db_session))

        # gone
        assert await mw._claim_meeting_row(999999) == "gone"

        # completed -> skip
        m_done = Meeting(status="completed")
        db_session.add(m_done)
        await db_session.commit()
        assert await mw._claim_meeting_row(m_done.id) == "skip"

        # processing + fresh heartbeat -> wait
        m_live = Meeting(status="processing", heartbeat_at=datetime.utcnow())
        db_session.add(m_live)
        await db_session.commit()
        assert await mw._claim_meeting_row(m_live.id) == "wait"

        # pending -> proceed (claims it: processing + heartbeat set)
        m_new = Meeting(status="pending")
        db_session.add(m_new)
        await db_session.commit()
        assert await mw._claim_meeting_row(m_new.id) == "proceed"
        await db_session.refresh(m_new)
        assert m_new.status == "processing"
        assert m_new.heartbeat_at is not None

        # processing + STALE heartbeat -> proceed (dead attempt, retried)
        m_dead = Meeting(
            status="processing",
            heartbeat_at=datetime.utcnow() - timedelta(seconds=mw.ROW_HEARTBEAT_STALE_S + 60),
        )
        db_session.add(m_dead)
        await db_session.commit()
        assert await mw._claim_meeting_row(m_dead.id) == "proceed"


# ---------------------------------------------------------------------------
# Worker _process_entry — poison / skip / wait / proceed / transient / terminal
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestMeetingProcessEntry:
    def _fakes(self):
        from unittest.mock import AsyncMock, MagicMock

        redis = MagicMock()
        redis.get = AsyncMock(return_value=None)
        redis.incr = AsyncMock()
        redis.expire = AsyncMock()
        redis.delete = AsyncMock()
        q = MagicMock()
        q.ack = AsyncMock()
        return redis, q

    async def test_missing_ids_acked(self):
        import workers.meeting_worker as mw
        from services.task_queue import StreamEntry

        redis, q = self._fakes()
        await mw._process_entry(redis, q, StreamEntry(entry_id="1-0", params={}))
        q.ack.assert_awaited_once_with("1-0")

    async def test_poison_quarantine_over_cap(self, monkeypatch):
        from unittest.mock import MagicMock

        import workers.meeting_worker as mw
        from services.task_queue import StreamEntry

        monkeypatch.setattr(mw.settings, "worker_max_deliveries", 3)
        marked = []

        async def _fake_mark(mid, err):
            marked.append((mid, str(err)))
            return True

        monkeypatch.setattr(mw, "_mark_meeting_failed", _fake_mark)
        # claim must NOT be reached for a quarantined entry
        monkeypatch.setattr(
            mw, "_claim_meeting_row",
            MagicMock(side_effect=AssertionError("must not claim a quarantined entry")),
        )
        redis, q = self._fakes()
        entry = StreamEntry(
            entry_id="2-0",
            params={"meeting_id": 5, "audio_path": "/x/meeting-5.wav"},
            delivery_count=5,
        )
        await mw._process_entry(redis, q, entry)
        assert marked and marked[0][0] == 5
        q.ack.assert_awaited_once_with("2-0")

    async def test_completed_skip_acks_without_processing(self, monkeypatch):
        import workers.meeting_worker as mw
        from services.task_queue import StreamEntry

        async def _claim(mid):
            return "skip"

        called = []

        async def _proc(mid, path):
            called.append(mid)

        monkeypatch.setattr(mw, "_claim_meeting_row", _claim)
        monkeypatch.setattr(mw, "process_meeting", _proc)
        redis, q = self._fakes()
        await mw._process_entry(
            redis, q, StreamEntry(entry_id="3-0", params={"meeting_id": 1, "audio_path": "/x/a.wav"})
        )
        assert called == []  # process_meeting NOT called
        q.ack.assert_awaited_once_with("3-0")

    async def test_wait_leaves_entry_unacked(self, monkeypatch):
        import workers.meeting_worker as mw
        from services.task_queue import StreamEntry

        async def _claim(mid):
            return "wait"

        monkeypatch.setattr(mw, "_claim_meeting_row", _claim)
        redis, q = self._fakes()
        await mw._process_entry(
            redis, q, StreamEntry(entry_id="4-0", params={"meeting_id": 1, "audio_path": "/x/a.wav"})
        )
        q.ack.assert_not_awaited()  # live job — retried later via reclaim

    async def test_proceed_runs_pipeline_and_acks(self, monkeypatch):
        import workers.meeting_worker as mw
        from services.task_queue import StreamEntry

        async def _claim(mid):
            return "proceed"

        called = []

        async def _proc(mid, path):
            called.append((mid, path))

        async def _noop_hb(mid, ev):
            return

        monkeypatch.setattr(mw, "_claim_meeting_row", _claim)
        monkeypatch.setattr(mw, "process_meeting", _proc)
        monkeypatch.setattr(mw, "_row_heartbeat_loop", _noop_hb)
        redis, q = self._fakes()
        await mw._process_entry(
            redis, q, StreamEntry(entry_id="5-0", params={"meeting_id": 8, "audio_path": "/x/meeting-8.wav"})
        )
        assert called == [(8, "/x/meeting-8.wav")]
        q.ack.assert_awaited_once_with("5-0")

    async def test_transient_error_left_in_pel(self, monkeypatch):
        import workers.meeting_worker as mw
        from services.task_queue import StreamEntry
        from services.voice_server_client import VoiceServerError

        async def _claim(mid):
            return "proceed"

        async def _proc(mid, path):
            raise VoiceServerError("voice-server down")

        async def _noop_hb(mid, ev):
            return

        monkeypatch.setattr(mw, "_claim_meeting_row", _claim)
        monkeypatch.setattr(mw, "process_meeting", _proc)
        monkeypatch.setattr(mw, "_row_heartbeat_loop", _noop_hb)
        redis, q = self._fakes()
        await mw._process_entry(
            redis, q, StreamEntry(entry_id="6-0", params={"meeting_id": 9, "audio_path": "/x/a.wav"})
        )
        q.ack.assert_not_awaited()          # transient -> left for reclaim
        redis.incr.assert_awaited()         # clean transient leave recorded

    async def test_terminal_error_marks_failed_and_acks(self, monkeypatch):
        import workers.meeting_worker as mw
        from services.task_queue import StreamEntry

        async def _claim(mid):
            return "proceed"

        async def _proc(mid, path):
            raise ValueError("corrupt segments")

        async def _noop_hb(mid, ev):
            return

        marked = []

        async def _fake_mark(mid, err):
            marked.append(mid)
            return True

        monkeypatch.setattr(mw, "_claim_meeting_row", _claim)
        monkeypatch.setattr(mw, "process_meeting", _proc)
        monkeypatch.setattr(mw, "_row_heartbeat_loop", _noop_hb)
        monkeypatch.setattr(mw, "_mark_meeting_failed", _fake_mark)
        redis, q = self._fakes()
        await mw._process_entry(
            redis, q, StreamEntry(entry_id="7-0", params={"meeting_id": 3, "audio_path": "/x/a.wav"})
        )
        assert marked == [3]
        q.ack.assert_awaited_once_with("7-0")


# ---------------------------------------------------------------------------
# #10: Schicht-A gate (D14), ingest wiring, re-attribution, retention
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_schicht_a_hook_skips_meeting_transcripts(monkeypatch):
    """The Schicht-A hook returns early for source=meeting_transcript even when
    extraction is enabled — no phantom obligations from meeting small talk."""
    import services.schicht_a_extractor as sa

    monkeypatch.setattr(sa.settings, "schicht_a_extraction_enabled", True)
    # If the guard fails and the hook proceeds, it opens a DB session (imported
    # from services.database inside the hook) — blow up there.
    monkeypatch.setattr(
        "services.database.AsyncSessionLocal",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not process a transcript")),
    )
    # Valid inputs (document_id + field_text) so ONLY the source guard can stop it.
    await sa.schicht_a_post_document_ingest_hook(
        chunks=["x"], document_id=1, field_text="lots of small talk", source="meeting_transcript",
    )  # returns cleanly


@pytest.mark.database
@pytest.mark.asyncio
class TestPipelineIngestAndReattribution:
    async def test_process_meeting_ingests_and_completes(self, db_session, monkeypatch):
        from services import meeting_pipeline as mp

        monkeypatch.setattr(mp, "AsyncSessionLocal", lambda: _SessionCtx(db_session))

        async def _fake_transcribe(path, **kw):
            return {"segments": [
                {"speaker": "SPEAKER_00", "text": "hi", "start_s": 0.0, "end_s": 1.0},
                {"speaker": "SPEAKER_01", "text": "hello", "start_s": 1.0, "end_s": 2.0},
            ]}

        async def _fake_ingest(db, meeting, markdown):
            return 4242  # pretend the transcript Document got this id

        monkeypatch.setattr(mp, "transcribe_meeting", _fake_transcribe)
        monkeypatch.setattr(mp, "_ingest_transcript", _fake_ingest)

        m = Meeting(status="processing", title="Sync")
        db_session.add(m)
        await db_session.commit()

        await mp.process_meeting(m.id, "/x/meeting.wav")
        await db_session.refresh(m)
        assert m.status == "completed"
        assert m.transcript_document_id == 4242
        assert [s["speaker"] for s in m.segments] == ["Sprecher 1", "Sprecher 2"]
        assert m.segments[0]["speaker_key"] == "SPEAKER_00"

    async def test_reattribute_rewrites_cluster_and_reindexes(self, db_session, monkeypatch, tmp_path):
        from models.database import Document
        from services import meeting_pipeline as mp

        # a transcript Document with a real file on disk to overwrite
        doc_file = tmp_path / "meeting-1.md"
        doc_file.write_text("old")
        doc = Document(filename="meeting-1.md", file_path=str(doc_file), status="completed")
        db_session.add(doc)
        await db_session.commit()

        m = Meeting(status="completed", title="T", transcript_document_id=doc.id, segments=[
            {"speaker": "Sprecher 1", "speaker_key": "SPEAKER_00", "text": "hi"},
            {"speaker": "Sprecher 2", "speaker_key": "SPEAKER_01", "text": "yo"},
        ])
        db_session.add(m)
        await db_session.commit()

        enq = []

        class _FakeQ:
            def __init__(self, *a, **k):
                pass

            async def enqueue(self, params):
                enq.append(params)
                return "1-0"

        monkeypatch.setattr(mp, "DocumentTaskQueue", _FakeQ, raising=False)
        # reattribute imports these inside the function; patch at their source.
        import services.task_queue as tq
        monkeypatch.setattr(tq, "DocumentTaskQueue", _FakeQ)
        monkeypatch.setattr("services.redis_client.get_redis", lambda: None)

        ok = await mp.reattribute(db_session, m, "SPEAKER_00", "Alice")
        assert ok is True
        await db_session.refresh(m)
        # only the SPEAKER_00 cluster was relabeled
        by_key = {s["speaker_key"]: s["speaker"] for s in m.segments}
        assert by_key["SPEAKER_00"] == "Alice"
        assert by_key["SPEAKER_01"] == "Sprecher 2"
        # transcript file overwritten + reindex enqueued (stable doc id)
        assert "Alice" in doc_file.read_text()
        assert enq and enq[0]["document_id"] == doc.id
        assert enq[0]["trigger"] == "user_reindex"
        await db_session.refresh(doc)
        assert doc.status == "pending"

    async def test_reattribute_unknown_cluster_returns_false(self, db_session):
        from services import meeting_pipeline as mp

        m = Meeting(status="completed", segments=[
            {"speaker": "Sprecher 1", "speaker_key": "SPEAKER_00", "text": "hi"},
        ])
        db_session.add(m)
        await db_session.commit()
        assert await mp.reattribute(db_session, m, "SPEAKER_99", "Bob") is False


@pytest.mark.database
@pytest.mark.asyncio
class TestMeetingRetention:
    async def test_audio_grace_cleanup_and_full_purge(self, db_session, monkeypatch, tmp_path):
        from datetime import datetime, timedelta

        from services import meeting_retention as mr

        monkeypatch.setattr(mr, "AsyncSessionLocal", lambda: _SessionCtx(db_session))
        monkeypatch.setattr(mr.settings, "upload_dir", str(tmp_path))
        monkeypatch.setattr(mr.settings, "meeting_keep_audio", False)
        monkeypatch.setattr(mr.settings, "meeting_audio_grace_days", 7)
        meetings_dir = tmp_path / "meetings"
        meetings_dir.mkdir()

        # 1. old completed meeting -> audio deleted (transcript kept)
        old = Meeting(status="completed", created_at=datetime.utcnow() - timedelta(days=30))
        db_session.add(old)
        await db_session.commit()
        old_audio = meetings_dir / f"meeting-{old.id}.wav"
        old_audio.write_bytes(b"aud")

        # 2. recent completed meeting -> audio kept
        recent = Meeting(status="completed", created_at=datetime.utcnow())
        db_session.add(recent)
        await db_session.commit()
        recent_audio = meetings_dir / f"meeting-{recent.id}.wav"
        recent_audio.write_bytes(b"aud")

        # 3. expired meeting -> fully purged (row gone), delete_document called
        purged = []

        async def _fake_delete(self, doc_id):
            purged.append(doc_id)
            return True

        monkeypatch.setattr("services.rag_service.RAGService.delete_document", _fake_delete)
        expired = Meeting(
            status="completed", transcript_document_id=77,
            retention_until=datetime.utcnow() - timedelta(days=1),
        )
        db_session.add(expired)
        await db_session.commit()
        expired_id = expired.id
        (meetings_dir / f"meeting-{expired_id}.wav").write_bytes(b"aud")

        audio_deleted, meetings_purged = await mr.cleanup_meetings()

        assert not old_audio.exists()        # old audio freed
        assert recent_audio.exists()         # recent audio kept
        assert audio_deleted >= 1
        assert meetings_purged == 1
        assert purged == [77]                # transcript doc deleted via RAGService
        assert await db_session.get(Meeting, expired_id) is None  # row purged

    async def test_failed_meeting_audio_is_freed(self, db_session, monkeypatch, tmp_path):
        """A worker-FAILED meeting's audio is freed by the grace sweep, not just
        completed ones (the upload route only unlinks on upload failure)."""
        from datetime import datetime, timedelta

        from services import meeting_retention as mr

        monkeypatch.setattr(mr, "AsyncSessionLocal", lambda: _SessionCtx(db_session))
        monkeypatch.setattr(mr.settings, "upload_dir", str(tmp_path))
        monkeypatch.setattr(mr.settings, "meeting_keep_audio", False)
        monkeypatch.setattr(mr.settings, "meeting_audio_grace_days", 7)
        (tmp_path / "meetings").mkdir()

        failed = Meeting(status="failed", created_at=datetime.utcnow() - timedelta(days=30))
        db_session.add(failed)
        await db_session.commit()
        audio = tmp_path / "meetings" / f"meeting-{failed.id}.wav"
        audio.write_bytes(b"aud")

        await mr.cleanup_meetings()
        assert not audio.exists()

    async def test_one_bad_purge_does_not_abort_sweep(self, monkeypatch, tmp_path):
        """A delete_document failure on one expired meeting rolls back + continues;
        a second expired meeting still gets purged. Uses an ISOLATED engine so
        the per-meeting commit/rollback are real (the shared session can't model
        a rollback without breaking the outer test transaction)."""
        from datetime import datetime, timedelta

        from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
        from sqlalchemy.pool import StaticPool

        from models.database import Base
        from services import meeting_retention as mr

        engine = create_async_engine(
            "sqlite+aiosqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        maker = async_sessionmaker(engine, expire_on_commit=False)

        monkeypatch.setattr(mr, "AsyncSessionLocal", maker)
        monkeypatch.setattr(mr.settings, "upload_dir", str(tmp_path))
        monkeypatch.setattr(mr.settings, "meeting_keep_audio", True)  # isolate mechanism 2
        (tmp_path / "meetings").mkdir()

        async def _delete(self, doc_id):
            if doc_id == 111:
                raise RuntimeError("boom")
            return True

        monkeypatch.setattr("services.rag_service.RAGService.delete_document", _delete)
        past = datetime.utcnow() - timedelta(days=1)
        async with maker() as s:
            bad = Meeting(status="completed", transcript_document_id=111, retention_until=past)
            good = Meeting(status="completed", transcript_document_id=222, retention_until=past)
            s.add_all([bad, good])
            await s.commit()
            bad_id, good_id = bad.id, good.id

        _audio, purged = await mr.cleanup_meetings()

        async with maker() as s:
            assert await s.get(Meeting, good_id) is None     # purged
            assert await s.get(Meeting, bad_id) is not None  # rolled back, retried next sweep
        assert purged == 1
        await engine.dispose()


# ---------------------------------------------------------------------------
# Review-fix regression tests
# ---------------------------------------------------------------------------

def test_voiceservererror_terminal_vs_transient():
    """4xx = terminal (bad audio fails fast), 5xx / unreachable = retryable."""
    import workers.meeting_worker as mw
    from services.voice_server_client import VoiceServerError

    assert mw._is_transient_error(VoiceServerError("bad", status_code=400)) is False
    assert mw._is_transient_error(VoiceServerError("unproc", status_code=422)) is False
    assert mw._is_transient_error(VoiceServerError("down", status_code=503)) is True
    assert mw._is_transient_error(VoiceServerError("unreachable")) is True  # status None


def test_render_marker_makes_identical_transcripts_hash_distinct():
    from services.meeting_pipeline import render_transcript_markdown

    m1 = Meeting(id=1, title="Weekly")
    m2 = Meeting(id=2, title="Weekly")
    segs = [{"speaker": "Sprecher 1", "text": "same words"}]
    md1 = render_transcript_markdown(m1, segs)
    md2 = render_transcript_markdown(m2, segs)
    assert "meeting-id: 1" in md1 and "meeting-id: 2" in md2
    assert md1 != md2  # identical title + content still yields distinct bytes


@pytest.mark.database
@pytest.mark.asyncio
class TestReviewFixes:
    async def test_reprocess_overwrites_not_reingest(self, db_session, monkeypatch):
        """A redelivery of a meeting that ALREADY has a transcript doc overwrites
        in place (no second ingest_document → no orphaned doc)."""
        from services import meeting_pipeline as mp

        monkeypatch.setattr(mp, "AsyncSessionLocal", lambda: _SessionCtx(db_session))

        async def _fake_transcribe(path, **kw):
            return {"segments": [{"speaker": "SPEAKER_00", "text": "hi"}]}

        overwrote, ingested = [], []

        async def _fake_overwrite(db, meeting, segments):
            overwrote.append(meeting.id)

        async def _fake_ingest(db, meeting, markdown):
            ingested.append(meeting.id)
            return 1

        monkeypatch.setattr(mp, "transcribe_meeting", _fake_transcribe)
        monkeypatch.setattr(mp, "_overwrite_transcript_and_reindex", _fake_overwrite)
        monkeypatch.setattr(mp, "_ingest_transcript", _fake_ingest)

        m = Meeting(status="processing", transcript_document_id=55)  # already ingested
        db_session.add(m)
        await db_session.commit()

        await mp.process_meeting(m.id, "/x/a.wav")
        assert overwrote == [m.id]   # overwrite-in-place path
        assert ingested == []        # NOT re-ingested
        await db_session.refresh(m)
        assert m.status == "completed"

    async def test_upload_stamps_retention_until(self, async_client, db_session, monkeypatch, tmp_path):
        from sqlalchemy import select as _sel

        _enable(monkeypatch, tmp_path, auth=False)
        monkeypatch.setattr(settings, "meeting_retention_days", 365)
        r = await async_client.post(
            "/api/meetings/transcribe",
            files={"audio": _wav()}, data={"consent_confirmed": "true"},
        )
        assert r.status_code == 202
        m = (await db_session.execute(_sel(Meeting).where(Meeting.id == r.json()["id"]))).scalar_one()
        assert m.retention_until is not None

    async def test_enqueue_failure_503_fails_row_and_unlinks_audio(
        self, async_client, db_session, monkeypatch, tmp_path
    ):
        import glob as _glob
        from sqlalchemy import select as _sel

        from api.routes import meetings as meetings_mod

        _enable(monkeypatch, tmp_path, auth=False)

        class _BoomQueue:
            def __init__(self, *a, **k):
                pass

            async def enqueue(self, params):
                raise RuntimeError("redis down")

        monkeypatch.setattr(meetings_mod, "MeetingTaskQueue", _BoomQueue)
        r = await async_client.post(
            "/api/meetings/transcribe",
            files={"audio": _wav()}, data={"consent_confirmed": "true"},
        )
        assert r.status_code == 503
        m = (await db_session.execute(_sel(Meeting))).scalars().first()
        assert m is not None and m.status == "failed"
        assert _glob.glob(str(tmp_path / "meetings" / f"meeting-{m.id}.*")) == []  # audio cleaned
