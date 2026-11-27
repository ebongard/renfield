"""Voicemail transcript (#1669): the text of a message in its notification.

No voiceprint (the reply must confirm ``embedded: false``); only the original
recipients' live rows change and become confidential; a replay is one job; a
failure or timeout leaves the notification as it was; nothing of the content in
``source_data``, a suppression embedding or the logs. Fictitious data only --
this repository is public.
"""
from __future__ import annotations

import logging
import struct
from contextlib import asynccontextmanager
from datetime import timedelta

import httpx
import pytest
from sqlalchemy import func, select, update

import services.telephony_client as tc
import services.voicemail_transcript as vt
from models.database import (
    NOTIFICATION_DELIVERED,
    NOTIFICATION_DISMISSED,
    Notification,
    TelephonyVoicemail,
    _utcnow,
)
from services.voice_server_client import VoiceServerError
from tests.backend.test_kg_contact_points import _make_user

REC = "20261127-101500-1795700000.42"
WAV = b"RIFF" + struct.pack("<I", 36) + b"WAVEfmt " + b"\x00" * 28
SPOKEN = "Hallo, hier ist die Beispiel Praxis, bitte rufen Sie zurück."


class TestNames:
    @pytest.mark.parametrize("name", [REC, "20261127-101500-1.1"])
    def test_a_gateway_name_is_valid(self, name):
        assert vt.valid_recording(name)

    @pytest.mark.parametrize("name", [
        "../haushalt/" + REC, REC + ".wav16", REC + "\n", "2026112-101500-1.1",
        "20261127-101500-١٢٣.4",  # Arabic-Indic digits: \d would take them
        None, 42, "",
    ])
    def test_anything_else_is_not(self, name):
        assert not vt.valid_recording(name)

    def test_the_url_follows_the_event_socket(self, monkeypatch):
        monkeypatch.setattr(vt.settings, "telephony_gateway_url", "wss://gw.example:8090/v1/events")
        assert vt.recording_url(REC) == f"https://gw.example:8090/v1/recordings/{REC}"
        monkeypatch.setattr(vt.settings, "telephony_gateway_url", "ws://127.0.0.1:8090/v1/events")
        assert vt.recording_url(REC) == f"http://127.0.0.1:8090/v1/recordings/{REC}"


@pytest.fixture
def gateway(monkeypatch):
    """httpx against a fake gateway; returns the list of requests it saw."""
    monkeypatch.setattr(vt.settings, "telephony_gateway_url", "ws://127.0.0.1:8090/v1/events")
    seen: list[httpx.Request] = []
    state = {"handler": lambda req: httpx.Response(200, content=WAV)}
    real = httpx.AsyncClient

    def factory(**kw):
        def handle(req):
            seen.append(req)
            return state["handler"](req)

        kw.pop("verify", None)
        return real(transport=httpx.MockTransport(handle), **kw)

    monkeypatch.setattr(httpx, "AsyncClient", factory)
    return seen, state


class TestFetch:
    async def test_the_wav_with_the_instance_token(self, gateway, monkeypatch):
        from pydantic import SecretStr

        monkeypatch.setattr(vt.settings, "telephony_gateway_token", SecretStr("tok-example"))
        seen, _ = gateway
        assert await vt.fetch_recording(REC) == WAV
        assert seen[0].url.path == f"/v1/recordings/{REC}"
        assert seen[0].headers["authorization"] == "Bearer tok-example"

    @pytest.mark.parametrize("code", [425, 429, 500, 503])
    async def test_not_ready_or_busy_retries(self, gateway, code):
        gateway[1]["handler"] = lambda req: httpx.Response(code)
        with pytest.raises(vt._Retry):
            await vt.fetch_recording(REC)

    @pytest.mark.parametrize("code", [401, 403, 404, 413])
    async def test_gone_or_refused_fails(self, gateway, code):
        gateway[1]["handler"] = lambda req: httpx.Response(code)
        with pytest.raises(vt._Fail):
            await vt.fetch_recording(REC)

    async def test_the_size_cap_holds_while_streaming(self, gateway, monkeypatch):
        monkeypatch.setattr(vt, "MAX_RECORDING_BYTES", 16)
        with pytest.raises(vt._Fail):
            await vt.fetch_recording(REC)

    async def test_a_non_wav_body_fails(self, gateway):
        gateway[1]["handler"] = lambda req: httpx.Response(200, content=b"<html>nope</html>")
        with pytest.raises(vt._Fail):
            await vt.fetch_recording(REC)

    async def test_unreachable_retries(self, gateway):
        def boom(req):
            raise httpx.ConnectError("refused")

        gateway[1]["handler"] = boom
        with pytest.raises(vt._Retry):
            await vt.fetch_recording(REC)

    async def test_plain_http_only_to_localhost(self, gateway, monkeypatch):
        monkeypatch.setattr(vt.settings, "telephony_gateway_url", "ws://gw.example:8090/v1/events")
        with pytest.raises(vt._Fail):
            await vt.fetch_recording(REC)
        assert gateway[0] == []


@pytest.fixture
def stt(monkeypatch):
    calls: list[dict] = []
    state = {"reply": {"text": SPOKEN, "embedded": False, "speaker_embedding": None}}

    async def fake(audio, **kw):
        calls.append(kw)
        if isinstance(state["reply"], Exception):
            raise state["reply"]
        return state["reply"]

    import services.voice_server_client as vsc

    monkeypatch.setattr(vsc, "stt", fake)
    return calls, state


class TestTranscribe:
    async def test_asks_for_no_voiceprint(self, stt):
        assert await vt.transcribe(WAV) == SPOKEN
        assert stt[0][0]["embed"] is False and stt[0][0]["language"] == "de"

    @pytest.mark.parametrize("reply", [
        {"text": SPOKEN},                                                   # older server: field unknown
        {"text": SPOKEN, "embedded": True, "speaker_embedding": [0.1]},
        {"text": SPOKEN, "embedded": False, "speaker_embedding": [0.1]},
        {"text": SPOKEN, "embedded": "false"},
        "not a dict",
    ])
    async def test_no_confirmation_no_text(self, stt, reply):
        stt[1]["reply"] = reply
        with pytest.raises(vt._Fail):
            await vt.transcribe(WAV)

    @pytest.mark.parametrize("code,kind", [(500, vt._Retry), (None, vt._Retry), (429, vt._Retry),
                                           (422, vt._Fail), (401, vt._Fail)])
    async def test_server_errors(self, stt, code, kind):
        stt[1]["reply"] = VoiceServerError("x", status_code=code)
        with pytest.raises(kind):
            await vt.transcribe(WAV)

    async def test_untrusted_text_is_cleaned_and_capped(self, stt):
        stt[1]["reply"] = {"text": "Bitte‮ zurück​\nrufen " + "x" * 2000, "embedded": False}
        text = await vt.transcribe(WAV)
        assert text.startswith("Bitte zurück rufen ") and len(text) == vt.MAX_TRANSCRIPT_CHARS
        assert "‮" not in text and "\n" not in text


class TestEnqueueGuards:
    async def test_flag_off_touches_nothing(self, monkeypatch):
        import services.database as dbmod

        monkeypatch.setattr(vt.settings, "voicemail_transcript_enabled", False)
        monkeypatch.setattr(dbmod, "AsyncSessionLocal", lambda: pytest.fail("no DB with the flag off"))
        await vt.enqueue({"type": "voicemail", "call_id": "c1", "recording": REC})

    async def test_no_or_forged_recording_queues_nothing(self, monkeypatch):
        import services.database as dbmod

        monkeypatch.setattr(vt.settings, "voicemail_transcript_enabled", True)
        monkeypatch.setattr(dbmod, "AsyncSessionLocal", lambda: pytest.fail("nothing to queue"))
        await vt.enqueue({"type": "voicemail", "call_id": "c1"})
        await vt.enqueue({"type": "voicemail", "call_id": "c1", "recording": "../x"})

    async def test_a_database_error_never_raises(self, monkeypatch):
        import services.database as dbmod

        def broken():
            raise RuntimeError("db down")

        monkeypatch.setattr(vt.settings, "voicemail_transcript_enabled", True)
        monkeypatch.setattr(dbmod, "AsyncSessionLocal", broken)
        await vt.enqueue({"type": "voicemail", "call_id": "c1", "recording": REC})


# --- real Postgres --------------------------------------------------------------------------------

@pytest.fixture
def enabled(monkeypatch, pg_db_session):
    monkeypatch.setattr(vt.settings, "voicemail_transcript_enabled", True)
    monkeypatch.setattr(vt.settings, "telephony_enabled", True)
    monkeypatch.setattr(vt.settings, "voice_server_url", "http://voice.example")
    monkeypatch.setattr(vt.settings, "voice_server_auth_enabled", False)

    @asynccontextmanager
    async def _session():
        yield pg_db_session

    import services.database as dbmod

    monkeypatch.setattr(dbmod, "AsyncSessionLocal", _session)

    emitted: list[int] = []

    async def emit(user_id, reason):
        emitted.append(user_id)

    from services.notification_service import NotificationService

    monkeypatch.setattr(NotificationService, "_emit_changed", staticmethod(emit))

    async def fetch(name):
        assert name == REC
        return WAV

    monkeypatch.setattr(vt, "fetch_recording", fetch)
    return emitted


EVENT = {"type": "voicemail", "call_id": "c1", "caller": "+49301234567", "did": "+4921611234567",
         "duration_s": 23, "recording": REC}


async def _row(db, user, call_id="c1", **kw):
    base = tc.build_notification(EVENT)
    n = Notification(
        event_type=base["event_type"], title=base["title"], message=base["message"], urgency="info",
        source="telephony", source_data={**base["data"], "call_id": call_id}, privacy="personal",
        status=kw.pop("status", NOTIFICATION_DELIVERED), target_user_id=user.id,
        expires_at=kw.pop("expires_at", _utcnow() + timedelta(hours=24)),
    )
    db.add(n)
    await db.flush()
    return n


async def _jobs(db):
    return (await db.execute(select(TelephonyVoicemail))).scalars().all()


async def _make_due(db):
    await db.execute(update(TelephonyVoicemail).values(next_attempt_at=_utcnow() - timedelta(seconds=1)))
    await db.commit()


@pytest.mark.database
class TestJob:
    async def test_a_replay_is_one_job(self, pg_db_session, enabled):
        await vt.enqueue(EVENT)
        await vt.enqueue(EVENT)  # gateway restart: the same call again
        assert len(await _jobs(pg_db_session)) == 1

    async def test_only_the_recipients_live_rows_get_the_text(self, pg_db_session, enabled, stt, caplog):
        db = pg_db_session
        a, b, c, d = [await _make_user(db, n) for n in ("a", "b", "c", "d")]
        ra, rb = await _row(db, a), await _row(db, b)
        dismissed = await _row(db, c, status=NOTIFICATION_DISMISSED)
        expired = await _row(db, d, expires_at=_utcnow() - timedelta(minutes=1))
        other_call = await _row(db, a, call_id="c2")
        await db.commit()
        await vt.enqueue(EVENT)

        with caplog.at_level(logging.DEBUG):
            assert await vt.process_one() is True
        for n in (ra, rb, dismissed, expired, other_call):
            await db.refresh(n)
        for n in (ra, rb):
            assert n.message.endswith(f": „{SPOKEN}“ {vt.NOTE}")
            assert n.privacy == "confidential"
            assert n.source_data["transcript_status"] == "done"
            assert SPOKEN not in str(n.source_data)
        for n in (dismissed, expired, other_call):
            assert SPOKEN not in n.message and n.privacy == "personal"
            assert "transcript_status" not in n.source_data
        assert sorted(enabled) == sorted([a.id, b.id])
        assert (await _jobs(db))[0].status == "done"
        assert SPOKEN not in caplog.text and REC not in caplog.text and "0301234567" not in caplog.text
        assert await vt.process_one() is False  # nothing due any more

    async def test_a_repeated_job_does_not_append_twice(self, pg_db_session, enabled, stt):
        db = pg_db_session
        n = await _row(db, await _make_user(db, "a"))
        await db.commit()
        await vt.enqueue(EVENT)
        await vt.process_one()
        await db.execute(update(TelephonyVoicemail).values(status="pending"))
        await _make_due(db)
        await vt.process_one()
        await db.refresh(n)
        assert n.message.count(SPOKEN) == 1

    async def test_a_silent_message_changes_no_text(self, pg_db_session, enabled, stt):
        db = pg_db_session
        n = await _row(db, await _make_user(db, "a"))
        before = n.message
        await db.commit()
        stt[1]["reply"] = {"text": "  ", "embedded": False}
        await vt.enqueue(EVENT)
        await vt.process_one()
        await db.refresh(n)
        assert (n.message, n.privacy, n.source_data["transcript_status"]) == (before, "personal", "empty")
        assert enabled == []

    async def test_transient_failures_retry_then_give_up(self, pg_db_session, enabled, stt):
        db = pg_db_session
        n = await _row(db, await _make_user(db, "a"))
        before = n.message
        await db.commit()
        stt[1]["reply"] = VoiceServerError("down", status_code=503)
        await vt.enqueue(EVENT)
        for attempt in range(1, vt.MAX_ATTEMPTS + 1):
            assert await vt.process_one() is True
            job = (await _jobs(db))[0]
            await db.refresh(job)
            assert job.attempts == attempt
            if attempt < vt.MAX_ATTEMPTS:
                assert job.status == "pending"
                assert job.next_attempt_at > _utcnow() + timedelta(seconds=60)
                assert await vt.process_one() is False  # not due yet
                await _make_due(db)
        assert job.status == "failed"
        await db.refresh(n)
        assert n.message == before and "transcript_status" not in n.source_data

    async def test_no_embedded_false_fails_at_once(self, pg_db_session, enabled, stt):
        db = pg_db_session
        n = await _row(db, await _make_user(db, "a"))
        before = n.message
        await db.commit()
        stt[1]["reply"] = {"text": SPOKEN, "embedded": True, "speaker_embedding": [0.1]}
        await vt.enqueue(EVENT)
        await vt.process_one()
        job = (await _jobs(db))[0]
        await db.refresh(job)
        await db.refresh(n)
        assert (job.status, job.attempts) == ("failed", 1)
        assert n.message == before

    async def test_a_claimed_job_waits_for_its_lease(self, pg_db_session, enabled, stt):
        db = pg_db_session
        await vt.enqueue(EVENT)
        assert await vt._claim() is not None  # a pod claims it, then dies
        assert await vt.process_one() is False
        await _make_due(db)  # the lease ran out
        assert await vt.process_one() is True


@pytest.mark.database
class TestDelivery:
    @pytest.fixture
    def delivered(self, monkeypatch, pg_db_session, enabled):
        async def notify(event, user_ids, process, resolve=None):
            return len(user_ids)

        monkeypatch.setattr(tc, "notify_users", notify)

    async def test_flag_off_no_job(self, pg_db_session, delivered, monkeypatch):
        await _make_user(pg_db_session, "a")
        await pg_db_session.commit()
        monkeypatch.setattr(vt.settings, "voicemail_transcript_enabled", False)
        await tc.deliver_via_notification_service(EVENT)
        assert await _jobs(pg_db_session) == []

    async def test_flag_on_one_job_per_voicemail(self, pg_db_session, delivered):
        await _make_user(pg_db_session, "a")
        await pg_db_session.commit()
        await tc.deliver_via_notification_service(EVENT)
        await tc.deliver_via_notification_service({**EVENT, "type": "incoming_call"})
        assert [j.call_id for j in await _jobs(pg_db_session)] == ["c1"]

    async def test_a_queue_failure_never_fails_the_delivery(self, pg_db_session, delivered, monkeypatch):
        await _make_user(pg_db_session, "a")
        await pg_db_session.commit()
        import sqlalchemy.dialects.postgresql as pg

        def broken(*a, **kw):
            raise RuntimeError("db down")

        monkeypatch.setattr(pg, "insert", broken)
        await tc.deliver_via_notification_service(EVENT)  # does not raise -> event marked done
        assert (await pg_db_session.execute(select(func.count()).select_from(TelephonyVoicemail))).scalar() == 0


@pytest.mark.database
async def test_a_suppression_rule_never_embeds_the_spoken_words(pg_db_session, enabled, stt, monkeypatch):
    from services.notification_service import NotificationService

    db = pg_db_session
    user = await _make_user(db, "a")
    n = await _row(db, user)
    await db.commit()
    await vt.enqueue(EVENT)
    await vt.process_one()
    embedded: list[str] = []

    async def fake_embedding(self, text):
        embedded.append(text)
        return None

    monkeypatch.setattr(NotificationService, "_get_embedding", fake_embedding)
    await NotificationService(db).suppress_similar(n.id, user_id=user.id, restrict_to_viewer=True)
    assert embedded and SPOKEN not in embedded[0]
