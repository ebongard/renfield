"""Voice turn traces (#1613): a content-free row per voice turn end, the resume
proxy, the aggregates, the cleanup, the admin route and the satellite write site.

Mirrors ``test_routing_traces.py``. Real Postgres (``percentile_cont``)."""
from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select

from models.database import VoiceTurnTrace
from utils.config import settings


@pytest.fixture
def traces_on(monkeypatch):
    monkeypatch.setattr(settings, "voice_turn_traces_enabled", True)


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _epoch(naive_utc: datetime) -> float:
    return naive_utc.replace(tzinfo=UTC).timestamp()


async def _count(db) -> int:
    return (await db.execute(select(func.count(VoiceTurnTrace.id)))).scalar()


async def _rows(db) -> list[VoiceTurnTrace]:
    db.expire_all()
    return list((await db.execute(
        select(VoiceTurnTrace).order_by(VoiceTurnTrace.created_at, VoiceTurnTrace.id)
    )).scalars().all())


async def _add(db, *, source="satellite", reason="silence", room="Küche", device_key="sat-k",
               audio_seconds=2.0, resumed=False, age_days=0.0):
    row = VoiceTurnTrace(
        source=source, reason=reason, room=room, device_key=device_key,
        audio_seconds=audio_seconds, resumed=resumed,
        created_at=_now() - timedelta(days=age_days),
    )
    db.add(row)
    await db.commit()
    return row


def _turn(**overrides) -> dict:
    fields = dict(
        source="satellite", device_key="sat-k", room="Küche", reason="silence",
        audio_seconds=3.5, vad_speech_seen=True, started_at=None, ended_at=None,
    )
    fields.update(overrides)
    return fields


# --------------------------------------------------------------- the schema
def test_the_row_carries_no_person_no_text_no_audio():
    """The content-free contract IS the column list: a user id, a transcript or
    an audio column added later must fail here first."""
    assert set(VoiceTurnTrace.__table__.c.keys()) == {
        "id", "created_at", "source", "device_key", "room", "reason",
        "audio_seconds", "vad_speech_seen", "resumed", "turn_score",
    }
    assert not VoiceTurnTrace.__table__.foreign_keys


def test_the_indexes_are_declared_in_the_orm():
    """A fresh install builds them from create_all, not from the migration."""
    indexes = {i.name: [c.name for c in i.columns] for i in VoiceTurnTrace.__table__.indexes}
    assert indexes == {
        "idx_voice_turn_traces_created": ["created_at"],
        "idx_voice_turn_traces_source_created": ["source", "created_at"],
    }


# ------------------------------------------------------------------ record
@pytest.mark.database
class TestRecord:
    async def test_flag_off_writes_nothing_and_schedules_nothing(
        self, db_session, own_session_to_test_db, monkeypatch,
    ):
        from services.voice_turn_trace_service import record_voice_turn, schedule_voice_turn

        monkeypatch.setattr(settings, "voice_turn_traces_enabled", False)
        await record_voice_turn(**_turn())
        assert schedule_voice_turn(**_turn()) is None
        assert await _count(db_session) == 0

    async def test_flag_on_writes_one_row_with_the_fields(
        self, db_session, own_session_to_test_db, traces_on,
    ):
        from services.voice_turn_trace_service import record_voice_turn

        end = _now()
        await record_voice_turn(**_turn(ended_at=end, started_at=_epoch(end) - 4))
        (row,) = await _rows(db_session)
        assert (row.source, row.device_key, row.room, row.reason) == (
            "satellite", "sat-k", "Küche", "silence",
        )
        assert row.audio_seconds == pytest.approx(3.5)
        assert row.vad_speech_seen is True
        assert row.resumed is False
        assert row.turn_score is None
        assert row.created_at == end, "created_at is the turn's END"

    @pytest.mark.parametrize("reported", ["mumble", "", None, 42, "SILENCE"])
    async def test_an_unknown_reason_is_stored_as_other(
        self, db_session, own_session_to_test_db, traces_on, reported,
    ):
        from services.voice_turn_trace_service import record_voice_turn

        await record_voice_turn(**_turn(reason=reported))
        (row,) = await _rows(db_session)
        assert row.reason == "other"

    @pytest.mark.parametrize("reported", ["silence", "timeout", "button", "canceled", "unknown"])
    async def test_a_known_reason_is_kept(self, db_session, own_session_to_test_db, traces_on, reported):
        from services.voice_turn_trace_service import record_voice_turn

        await record_voice_turn(**_turn(reason=reported))
        (row,) = await _rows(db_session)
        assert row.reason == reported

    async def test_a_non_boolean_vad_verdict_is_not_stored(
        self, db_session, own_session_to_test_db, traces_on,
    ):
        from services.voice_turn_trace_service import record_voice_turn

        await record_voice_turn(**_turn(vad_speech_seen="false"))
        (row,) = await _rows(db_session)
        assert row.vad_speech_seen is None

    async def test_a_failing_write_never_raises(self, traces_on, monkeypatch):
        import services.database as _db

        def _broken():
            raise RuntimeError("db down")

        monkeypatch.setattr(_db, "AsyncSessionLocal", _broken)
        from services.voice_turn_trace_service import record_voice_turn

        await record_voice_turn(**_turn())  # must not raise

    async def test_schedule_keeps_a_reference_and_writes(
        self, db_session, own_session_to_test_db, traces_on,
    ):
        from services import voice_turn_trace_service as svc

        task = svc.schedule_voice_turn(**{k: v for k, v in _turn().items() if k != "ended_at"})
        assert task is not None
        assert task in svc._background_tasks, "an unreferenced task can be collected mid-flight"
        await task
        assert task not in svc._background_tasks
        assert await _count(db_session) == 1


# ------------------------------------------------------------------ resume
@pytest.mark.database
class TestResume:
    async def _two_turns(self, *, gap_seconds, first=None, second=None):
        """A first turn ending at T, a second starting ``gap_seconds`` after T."""
        from services.voice_turn_trace_service import record_voice_turn

        first_end = _now() - timedelta(seconds=60)
        await record_voice_turn(**_turn(
            ended_at=first_end, started_at=_epoch(first_end) - 5, **(first or {}),
        ))
        second_start = first_end + timedelta(seconds=gap_seconds)
        await record_voice_turn(**_turn(
            ended_at=second_start + timedelta(seconds=4), started_at=_epoch(second_start),
            **(second or {}),
        ))

    @pytest.mark.parametrize(("gap", "resumed"), [(0.5, True), (9.5, True), (10.5, False), (30, False)])
    async def test_same_device_within_the_window(
        self, db_session, own_session_to_test_db, traces_on, gap, resumed,
    ):
        """The window runs from the previous turn's END to the new turn's START.
        10.5 s and 30 s are the negative control on the same harness."""
        await self._two_turns(gap_seconds=gap)
        first, second = await _rows(db_session)
        assert first.resumed is resumed
        assert second.resumed is False, "the newest turn is never resumed yet"

    async def test_another_device_does_not_resume(self, db_session, own_session_to_test_db, traces_on):
        await self._two_turns(gap_seconds=2, second={"device_key": "sat-w", "room": "Wohnzimmer"})
        assert [r.resumed for r in await _rows(db_session)] == [False, False]

    async def test_another_source_does_not_resume(self, db_session, own_session_to_test_db, traces_on):
        await self._two_turns(gap_seconds=2, second={"source": "browser"})
        assert [r.resumed for r in await _rows(db_session)] == [False, False]

    async def test_without_a_start_time_nothing_is_resumed(
        self, db_session, own_session_to_test_db, traces_on,
    ):
        from services.voice_turn_trace_service import record_voice_turn

        await self._two_turns(gap_seconds=30)
        await record_voice_turn(**_turn(started_at=None))
        assert [r.resumed for r in await _rows(db_session)] == [False, False, False]

    async def test_only_the_latest_previous_turn_is_marked(
        self, db_session, own_session_to_test_db, traces_on,
    ):
        """Two earlier turns inside the window: only the one just before counts."""
        from services.voice_turn_trace_service import record_voice_turn

        base = _now() - timedelta(seconds=60)
        await record_voice_turn(**_turn(ended_at=base))
        await record_voice_turn(**_turn(ended_at=base + timedelta(seconds=3)))
        start = base + timedelta(seconds=5)
        await record_voice_turn(**_turn(ended_at=start + timedelta(seconds=2), started_at=_epoch(start)))
        assert [r.resumed for r in await _rows(db_session)] == [False, True, False]


# ------------------------------------------------------------------- stats
@pytest.mark.database
class TestStats:
    async def test_aggregates_and_percentiles(self, db_session):
        from services.voice_turn_trace_service import turn_stats

        for i in range(1, 11):  # audio_seconds 1..10
            await _add(
                db_session, audio_seconds=float(i),
                reason="timeout" if i == 10 else "silence",
                room="Küche" if i <= 6 else "Bad",
                resumed=i in (2, 3),
            )
        await _add(db_session, source="browser", reason="other", room=None, audio_seconds=None)
        # Outside the window: counted nowhere.
        await _add(db_session, audio_seconds=500.0, resumed=True, age_days=20)

        stats = await turn_stats(db_session, days=14)
        assert stats == {
            "days": 14,
            "total": 11,
            "by_source_reason": {
                "satellite": {"silence": 9, "timeout": 1},
                "browser": {"other": 1},
            },
            "resumed": 2,
            "resumed_rate": round(2 / 11, 4),
            "audio_seconds_p50": pytest.approx(5.5),
            "audio_seconds_p90": pytest.approx(9.1),
            "by_room": {"Küche": 6, "Bad": 4},
        }

    async def test_empty(self, db_session):
        from services.voice_turn_trace_service import turn_stats

        stats = await turn_stats(db_session, days=14)
        assert stats["total"] == 0
        assert stats["resumed_rate"] is None
        assert stats["audio_seconds_p50"] is None
        assert stats["by_source_reason"] == {} and stats["by_room"] == {}

    async def test_purge_keeps_the_window(self, db_session, monkeypatch):
        from services.voice_turn_trace_service import purge_expired

        monkeypatch.setattr(settings, "voice_turn_traces_retention_days", 30)
        await _add(db_session, age_days=31)
        await _add(db_session, age_days=29)
        assert await purge_expired(db_session) == 1
        assert await _count(db_session) == 1


@pytest.mark.database
class TestCleanupTask:
    async def test_purges_with_the_flag_off(self, db_session, own_session_to_test_db, monkeypatch):
        """Ungated on purpose, like the routing traces."""
        from services.scheduled_tasks import builtins

        monkeypatch.setattr(settings, "voice_turn_traces_enabled", False)
        await _add(db_session, age_days=settings.voice_turn_traces_retention_days + 1)
        out = await builtins._voice_turn_trace_cleanup_handler(SimpleNamespace(), {})
        assert out == "deleted=1"
        assert await _count(db_session) == 0

    def test_seeded_daily(self):
        from services.scheduled_tasks import builtins

        (seed,) = [s for s in builtins.builtin_task_seeds() if s.handler_key == "voice_turn_trace_cleanup"]
        assert seed.interval_seconds == 86400
        assert seed.enabled is True


def test_retention_is_clamped():
    from pydantic import ValidationError

    from utils.config import Settings

    for bad in (0, 366):
        with pytest.raises(ValidationError):
            Settings(voice_turn_traces_retention_days=bad)
    assert Settings.model_fields["voice_turn_traces_enabled"].default is False
    assert Settings.model_fields["voice_turn_traces_retention_days"].default == 30


# ------------------------------------------------------------------- route
def _as(app, *, admin: bool | None):
    from services.auth_service import get_current_user

    if admin is None:
        app.dependency_overrides[get_current_user] = lambda: None
        return
    user = SimpleNamespace(id=7, username="u", has_permission=lambda p: admin and p == "admin")
    app.dependency_overrides[get_current_user] = lambda: user


@pytest.mark.database
class TestRoute:
    PATH = "/api/admin/voice-turns/stats"

    async def test_admin_gets_the_stats(
        self, async_client: AsyncClient, app_with_test_db, db_session, monkeypatch,
    ):
        monkeypatch.setattr(settings, "auth_enabled", True)
        monkeypatch.setattr(settings, "voice_turn_traces_enabled", True)
        _as(app_with_test_db, admin=True)
        await _add(db_session, reason="timeout", audio_seconds=60.0, resumed=True)
        await _add(db_session, reason="silence", audio_seconds=2.0, age_days=3)

        resp = await async_client.get(self.PATH, params={"days": 1})
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body == {
            "days": 1,
            "total": 1,
            "by_source_reason": {"satellite": {"timeout": 1}},
            "resumed": 1,
            "resumed_rate": 1.0,
            "audio_seconds_p50": 60.0,
            "audio_seconds_p90": 60.0,
            "by_room": {"Küche": 1},
            "enabled": True,
            "retention_days": settings.voice_turn_traces_retention_days,
        }
        assert (await async_client.get(self.PATH)).json()["total"] == 2  # default 14 days

    @pytest.mark.parametrize(("admin", "status"), [(False, 403), (None, 401)])
    async def test_non_admin_is_refused(
        self, async_client: AsyncClient, app_with_test_db, monkeypatch, admin, status,
    ):
        monkeypatch.setattr(settings, "auth_enabled", True)
        _as(app_with_test_db, admin=admin)
        resp = await async_client.get(self.PATH)
        assert resp.status_code == status, resp.text

    async def test_flag_off_answers_with_enabled_false(
        self, async_client: AsyncClient, app_with_test_db, monkeypatch,
    ):
        monkeypatch.setattr(settings, "voice_turn_traces_enabled", False)
        _as(app_with_test_db, admin=True)
        resp = await async_client.get(self.PATH)
        assert resp.status_code == 200, resp.text
        assert resp.json()["enabled"] is False
        assert resp.json()["total"] == 0

    @pytest.mark.parametrize("days", [0, 366, "x"])
    async def test_days_is_bounded(self, async_client: AsyncClient, app_with_test_db, days):
        _as(app_with_test_db, admin=True)
        resp = await async_client.get(self.PATH, params={"days": days})
        assert resp.status_code == 422


# -------------------------------------------------- the satellite write site
class _FakeSatelliteSocket:
    """Plays text frames into ``satellite_websocket``, then disconnects."""

    def __init__(self, frames: list[dict]):
        self._frames = list(frames)
        self.sent: list[dict] = []
        self.client = MagicMock(host="10.0.0.5")
        self.app = MagicMock()

    async def accept(self):
        return None

    async def receive(self):
        if self._frames:
            return {"type": "websocket.receive", "text": json.dumps(self._frames.pop(0))}
        return {"type": "websocket.disconnect", "code": 1000}

    async def send_json(self, data):
        self.sent.append(data)

    async def close(self, **_kw):
        return None


class _NoDb:
    async def __aenter__(self):
        return object()

    async def __aexit__(self, *_a):
        return False


@pytest.fixture
def satellite_env(monkeypatch, traces_on):
    """The real handler and SatelliteManager, as in ``test_voice_empty_turn.py``;
    the trace write itself is replaced by a recorder."""
    from ha_glue.api.websocket import satellite_handler as handler
    from ha_glue.services import satellite_manager as manager_mod
    from services import voice_turn_trace_service as svc

    mgr = manager_mod.SatelliteManager()
    monkeypatch.setattr(manager_mod, "get_satellite_manager", lambda: mgr)
    limiter = MagicMock()
    limiter.can_connect.return_value = (True, "")
    monkeypatch.setattr(handler, "get_connection_limiter", lambda: limiter)
    rate = MagicMock()
    rate.check.return_value = (True, "")
    monkeypatch.setattr(handler, "get_rate_limiter", lambda: rate)
    monkeypatch.setattr(handler, "authenticate_websocket", AsyncMock(return_value={"sub": "t"}))
    monkeypatch.setattr(handler, "AsyncSessionLocal", lambda: _NoDb())
    builder = MagicMock()
    builder.build = AsyncMock(return_value="")
    monkeypatch.setattr("services.whisper_prompt_builder.get_whisper_prompt_builder", lambda: builder)
    monkeypatch.setattr(
        "services.whisper_prompt_builder.resolve_first_speaker_from_room", AsyncMock(return_value=None),
    )
    monkeypatch.setattr(settings, "speaker_recognition_enabled", False)
    whisper = MagicMock()
    whisper.transcribe_bytes = AsyncMock(return_value="")
    monkeypatch.setattr(handler, "get_whisper_service", lambda: whisper)
    monkeypatch.setattr("utils.metrics.record_voice_empty_turn", lambda s, k: None)

    recorded: list[dict] = []

    async def _record(**fields):
        recorded.append(fields)

    monkeypatch.setattr(svc, "record_voice_turn", _record)
    return handler, mgr, whisper, svc, recorded


async def _run_turn(handler, mgr, *, frame_extra: dict | None = None):
    frame = {"type": "audio_end", "session_id": "s1", "reason": "timeout", **(frame_extra or {})}
    ws = _FakeSatelliteSocket([frame])
    await mgr.register(satellite_id="sat-bad", room="Badezimmer", websocket=ws, capabilities={})
    await mgr.start_session("sat-bad", "hey_renfield", 0.9, session_id="s1")
    started_at = mgr.sessions["s1"].started_at
    mgr.buffer_audio_bytes("s1", b"\0" * 99840, 1)  # 3.12 s of PCM
    await handler.satellite_websocket(ws, token=None)
    ends = [e["details"] for e in mgr._satellite_history["sat-bad"] if e["type"] == "session_end"]
    return started_at, ends, ws


def _last_state(ws) -> dict:
    """The satellite's last frame, ignoring a trailing feedback cue."""
    return [m for m in ws.sent if m.get("type") != "feedback_cue"][-1]


@pytest.mark.unit
class TestSatelliteWriteSite:
    async def test_audio_end_schedules_exactly_one_trace(self, satellite_env):
        handler, mgr, whisper, svc, recorded = satellite_env
        started_at, ends, _ws = await _run_turn(handler, mgr, frame_extra={"vad_speech_seen": True})
        await asyncio.gather(*list(svc._background_tasks))

        assert len(recorded) == 1
        fields = recorded[0]
        assert fields.pop("ended_at") is not None
        assert fields == {
            "source": "satellite",
            "device_key": "sat-bad",
            "room": "Badezimmer",
            "reason": "timeout",
            "audio_seconds": pytest.approx(3.12),
            "vad_speech_seen": True,
            "started_at": started_at,
        }
        assert len(ends) == 1, "the turn still ran to its end"
        whisper.transcribe_bytes.assert_awaited()

    async def test_flag_off_schedules_nothing(self, satellite_env, monkeypatch):
        handler, mgr, whisper, svc, recorded = satellite_env
        monkeypatch.setattr(settings, "voice_turn_traces_enabled", False)
        _started, ends, _ws = await _run_turn(handler, mgr)
        assert recorded == [] and not svc._background_tasks
        assert len(ends) == 1
        whisper.transcribe_bytes.assert_awaited()

    async def test_a_raising_record_does_not_stop_the_turn(self, satellite_env, monkeypatch):
        handler, mgr, whisper, svc, _recorded = satellite_env

        async def _boom(**_fields):
            raise RuntimeError("trace store down")

        monkeypatch.setattr(svc, "record_voice_turn", _boom)
        _started, ends, ws = await _run_turn(handler, mgr)
        await asyncio.gather(*list(svc._background_tasks), return_exceptions=True)
        assert len(ends) == 1
        whisper.transcribe_bytes.assert_awaited()
        assert _last_state(ws) == {"type": "state", "state": "idle"}

    async def test_a_raising_scheduler_does_not_stop_the_turn(self, satellite_env, monkeypatch):
        handler, mgr, whisper, _svc, _recorded = satellite_env

        def _boom(**_fields):
            raise RuntimeError("no loop")

        monkeypatch.setattr(handler, "schedule_voice_turn", _boom)
        _started, ends, ws = await _run_turn(handler, mgr)
        assert len(ends) == 1
        whisper.transcribe_bytes.assert_awaited()
        assert _last_state(ws) == {"type": "state", "state": "idle"}
