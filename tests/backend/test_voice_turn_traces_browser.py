"""Voice turn traces (#1613), the BROWSER path.

A browser voice turn's chat message carries a content-free ``voice_turn``
object. With ``VOICE_TURN_TRACES_ENABLED`` the chat handler schedules ONE
``source="browser"`` row from it: the reason clamped, the numbers bounded, the
per-page-load turn key as ``device_key``, ``room`` NULL, no user. A gap since
the previous answer of at most ``RESUME_WINDOW_SECONDS`` marks the previous
row of the same turn key as resumed. Real Postgres for the rows."""
from __future__ import annotations

import asyncio
import math
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from sqlalchemy import select
from starlette.testclient import TestClient

import api.websocket.chat_handler as ch
import services.voice_turn_trace_service as vts
from models.database import VoiceTurnTrace
from services.input_guard import InjectionResult
from utils.config import settings

KEY = "0b7c6c1e-4a51-4d0e-9a55-3f1f1c2a9d10"


@pytest.fixture
def traces_on(monkeypatch):
    monkeypatch.setattr(settings, "voice_turn_traces_enabled", True)


def _vt(**overrides) -> dict:
    raw = {"end_reason": "silence", "audio_duration_s": 3.2, "gap_since_answer_s": None, "turn_key": KEY}
    raw.update(overrides)
    return raw


async def _rows(db) -> list[VoiceTurnTrace]:
    db.expire_all()
    return list((await db.execute(
        select(VoiceTurnTrace).order_by(VoiceTurnTrace.created_at, VoiceTurnTrace.id)
    )).scalars().all())


# ------------------------------------------------------------ the sanitizer
class TestBrowserTurnFields:
    def test_a_well_formed_object(self):
        assert vts.browser_turn_fields(_vt(gap_since_answer_s=1.5)) == {
            "source": "browser", "device_key": KEY, "room": None, "reason": "silence",
            "audio_seconds": 3.2, "resume_gap_s": 1.5,
        }

    @pytest.mark.parametrize("reason", [
        "server_vad", "max_utterance", "client_flush", "silence", "button", "canceled", "unknown",
    ])
    def test_the_browser_reasons_are_kept(self, reason):
        assert vts.browser_turn_fields(_vt(end_reason=reason))["reason"] == reason

    @pytest.mark.parametrize("reason", ["because", "", 7, None, ["silence"], "x" * 500])
    def test_any_other_reason_is_other(self, reason):
        assert vts.browser_turn_fields(_vt(end_reason=reason))["reason"] == "other"

    @pytest.mark.parametrize(("value", "expected"), [
        (0, 0.0), (3600, 3600.0), (12.5, 12.5),
        (-0.1, None), (3600.1, None), (math.nan, None), (math.inf, None),
        (True, None), ("3", None), (None, None), ([1], None), (10**400, None),
    ])
    def test_the_numbers_are_bounded(self, value, expected):
        fields = vts.browser_turn_fields(_vt(audio_duration_s=value, gap_since_answer_s=value))
        assert fields["audio_seconds"] == expected
        assert fields["resume_gap_s"] == expected

    @pytest.mark.parametrize("key", ["", "short", "has space in it", "a" * 65, "Robert'); DROP", 123, None])
    def test_a_turn_key_that_is_not_a_short_random_id_is_dropped(self, key):
        assert vts.browser_turn_fields(_vt(turn_key=key))["device_key"] is None

    def test_extra_fields_are_ignored_never_identity(self):
        fields = vts.browser_turn_fields(_vt(user_id=7, room="Küche", text="hallo", device_key="evil"))
        assert fields["room"] is None and fields["device_key"] == KEY
        assert "user_id" not in fields and "text" not in fields

    @pytest.mark.parametrize("raw", ["silence", 3, ["x"], True, None])
    def test_not_an_object_is_no_trace(self, raw):
        assert vts.browser_turn_fields(raw) is None


class TestScheduleBrowser:
    def test_flag_off_does_not_even_look_at_the_metadata(self, monkeypatch):
        class _Untouchable(dict):
            def get(self, *_a, **_k):
                raise AssertionError("read with the flag off")

        monkeypatch.setattr(settings, "voice_turn_traces_enabled", False)
        assert vts.schedule_browser_voice_turn(_Untouchable()) is None

    def test_garbage_is_no_task(self, traces_on):
        assert vts.schedule_browser_voice_turn("garbage") is None

    def test_an_unexpected_error_is_swallowed(self, traces_on, monkeypatch):
        def _boom(_raw):
            raise RuntimeError("x")

        monkeypatch.setattr(vts, "browser_turn_fields", _boom)
        assert vts.schedule_browser_voice_turn(_vt()) is None


# ------------------------------------------------------------- the rows
@pytest.mark.database
class TestBrowserRows:
    async def test_flag_on_writes_one_browser_row_with_clamped_values(
        self, db_session, own_session_to_test_db, traces_on,
    ):
        task = vts.schedule_browser_voice_turn(_vt(end_reason="bogus", audio_duration_s=9999))
        assert task is not None
        await task
        (row,) = await _rows(db_session)
        assert (row.source, row.device_key, row.room, row.reason) == ("browser", KEY, None, "other")
        assert row.audio_seconds is None
        assert row.vad_speech_seen is None
        assert row.resumed is False

    async def test_flag_off_writes_nothing(self, db_session, own_session_to_test_db, monkeypatch):
        monkeypatch.setattr(settings, "voice_turn_traces_enabled", False)
        assert vts.schedule_browser_voice_turn(_vt()) is None
        assert await _rows(db_session) == []


@pytest.mark.database
class TestBrowserResume:
    async def _two(self, *, gap, second_key=KEY):
        await vts.schedule_browser_voice_turn(_vt())
        await asyncio.sleep(0.01)  # the second recording ends later
        await vts.schedule_browser_voice_turn(_vt(gap_since_answer_s=gap, turn_key=second_key))

    @pytest.mark.parametrize(("gap", "resumed"), [
        (0, True),       # a barge-in while the answer still played
        (2.0, True),
        (10.0, True),    # the window's edge
        (10.5, False),   # negative control: just outside
        (300, False),    # negative control: a later question
        (None, False),   # no previous answer measured
    ])
    async def test_the_client_gap_decides(self, db_session, own_session_to_test_db, traces_on, gap, resumed):
        await self._two(gap=gap)
        first, second = await _rows(db_session)
        assert first.resumed is resumed
        assert second.resumed is False, "the newest turn is never resumed yet"

    async def test_another_page_does_not_resume(self, db_session, own_session_to_test_db, traces_on):
        await self._two(gap=1.0, second_key="ffffffff-1111-2222-3333-444444444444")
        assert [r.resumed for r in await _rows(db_session)] == [False, False]

    async def test_a_dropped_turn_key_resumes_nothing(self, db_session, own_session_to_test_db, traces_on):
        await self._two(gap=1.0, second_key="bad key")
        assert [r.resumed for r in await _rows(db_session)] == [False, False]

    async def test_a_satellite_turn_is_never_a_browser_resume(
        self, db_session, own_session_to_test_db, traces_on,
    ):
        """Same device_key string, another source."""
        await vts.record_voice_turn(source="satellite", device_key=KEY, room="Küche", reason="silence")
        await asyncio.sleep(0.01)
        await vts.schedule_browser_voice_turn(_vt(gap_since_answer_s=1.0))
        assert [r.resumed for r in await _rows(db_session)] == [False, False]

    async def test_stats_give_a_rate_per_source(self, db_session, own_session_to_test_db, traces_on):
        await self._two(gap=1.0)
        stats = await vts.turn_stats(db_session, days=14)
        assert stats["by_source_reason"] == {"browser": {"silence": 2}}
        assert stats["resumed_rate_by_source"] == {"browser": 0.5}


# --------------------------------------------------------- the chat handler
@pytest.fixture
def chat(monkeypatch):
    """The real chat WS route; an injection block answers each turn without an
    LLM, AFTER the trace is scheduled."""
    async def _fake_auth(_ws, _token=None):
        return {"authenticated": True, "user_id": 42, "auth_method": "jwt"}

    class _AllowAll:
        def check(self, _ip, **_kwargs):
            return True, ""

    calls: list[object] = []
    real = ch.schedule_browser_voice_turn

    def _spy(raw):
        calls.append(raw)
        return real(raw)

    scheduled: list[dict] = []
    monkeypatch.setattr(vts, "schedule_voice_turn", lambda **f: scheduled.append(f))
    monkeypatch.setattr(ch, "schedule_browser_voice_turn", _spy)
    monkeypatch.setattr(ch, "authenticate_websocket", _fake_auth)
    monkeypatch.setattr(ch, "get_rate_limiter", lambda: _AllowAll())
    monkeypatch.setattr(
        ch, "detect_injection",
        lambda _text: InjectionResult(score=0.99, blocked=True, matched_patterns=["test"]),
    )
    app = FastAPI()
    app.state.ollama = SimpleNamespace(default_lang="de")
    app.include_router(ch.router)
    return TestClient(app), calls, scheduled


def _turn(ws, **extra) -> None:
    ws.send_json({"type": "text", "content": "Mach das Licht an", **extra})
    assert ws.receive_json()["type"] == "stream"
    assert ws.receive_json() == {"type": "done"}


class TestChatHandler:
    def test_a_voice_turn_schedules_one_browser_trace_without_a_user(self, chat, traces_on):
        client, _calls, scheduled = chat
        with client.websocket_connect("/ws") as ws:
            _turn(ws, voice_turn=_vt())
        assert len(scheduled) == 1
        assert scheduled[0]["source"] == "browser"
        assert "user_id" not in scheduled[0]
        assert scheduled[0]["room"] is None

    def test_a_typed_message_schedules_nothing(self, chat, traces_on):
        client, calls, scheduled = chat
        with client.websocket_connect("/ws") as ws:
            _turn(ws)
        assert calls == [] and scheduled == []

    @pytest.mark.parametrize("garbage", ["silence", 42, ["x"], {"end_reason": {"nested": 1}}])
    def test_garbage_metadata_never_breaks_the_turn(self, chat, traces_on, garbage):
        client, _calls, scheduled = chat
        with client.websocket_connect("/ws") as ws:
            _turn(ws, voice_turn=garbage)
            _turn(ws)  # the socket survived
        assert all(f["reason"] == "other" for f in scheduled)

    def test_flag_off_schedules_nothing(self, chat, monkeypatch):
        client, _calls, scheduled = chat
        monkeypatch.setattr(settings, "voice_turn_traces_enabled", False)
        with client.websocket_connect("/ws") as ws:
            _turn(ws, voice_turn=_vt())
        assert scheduled == []
