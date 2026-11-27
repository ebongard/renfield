"""Fax reception (#1677, Telefonie Phase 3).

What must hold:
- flag off: ``fax_received`` is ignored exactly as before (not announced, no job);
- a received fax becomes ONE job (a replay is a no-op), a failed one is announced
  to the owner at once; only the configured owner is ever notified;
- the job files the PDF through ``folder_ingest.ingest_document`` with the owner,
  tier, the owner's fax KB, Paperless stamping and the ``telephony_fax`` source;
- transient trouble retries, a terminal failure (or used-up attempts) tells the
  owner that filing failed -- a fax is never lost silently;
- no number in a log line or in the document's file name.
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from datetime import timedelta

import httpx
import pytest
from sqlalchemy import select, update

import services.telephony_client as tc
import services.telephony_fax as tf
import services.voicemail_transcript as vt
from models.database import KnowledgeBase, TelephonyFax, _utcnow
from services.folder_ingest import IngestResult, IngestStatus
from tests.backend.test_kg_contact_points import _make_user

FAX = "20261127-191500-1795800000.42"
PDF = b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n"
CALLER = "+492161123456"
EVENT = {"type": "fax_received", "call_id": "f1", "caller": CALLER, "did": "+4921615745956",
         "status": "ok", "pages": 3, "fax": FAX}


# --- texts and routing (no database) ---------------------------------------------------------------

class TestText:
    def test_filed(self):
        n = tc.build_notification({**EVENT, "filed": True, "kb": "Fax", "document_id": 7})
        assert n["event_type"] == "telephony.fax_received"
        assert n["title"].startswith("Fax von ") and n["title"].endswith("(3 Seiten)")
        assert "abgelegt in „Fax“" in n["message"]
        assert n["data"]["pages"] == 3 and n["data"]["fax_status"] == "filed" and n["data"]["document_id"] == 7
        assert n["privacy"] == "personal" and n["tts"] is False

    def test_one_page_and_not_filed(self):
        n = tc.build_notification({**EVENT, "pages": 1, "filed": False})
        assert "(1 Seite)" in n["title"]
        assert "Ablage ist fehlgeschlagen" in n["message"] and n["data"]["fax_status"] == "not_filed"

    def test_failed(self):
        n = tc.build_notification({**EVENT, "status": "failed", "pages": 0})
        assert n["title"].endswith("fehlgeschlagen") and "nicht empfangen" in n["message"]
        assert n["data"]["fax_status"] == "failed"

    def test_other_events_are_unchanged(self):
        n = tc.build_notification({"type": "incoming_call", "call_id": "c", "caller": CALLER, "did": EVENT["did"]})
        assert n["title"].startswith("Anruf von ") and "pages" not in n["data"]


class TestFlag:
    def test_off_fax_is_not_announced(self, monkeypatch):
        monkeypatch.setattr(tc.settings, "telephony_fax_enabled", False)
        assert tc.FAX_TYPE not in tc.announced_types()
        assert tc.announced_types() == tc.ANNOUNCED_TYPES

    def test_on_fax_is_announced(self, monkeypatch):
        monkeypatch.setattr(tc.settings, "telephony_fax_enabled", True)
        assert tc.FAX_TYPE in tc.announced_types()

    async def test_off_the_client_skips_it_without_delivering(self, monkeypatch):
        monkeypatch.setattr(tc.settings, "telephony_fax_enabled", False)
        delivered = []

        class Store:
            async def is_done(self, key):
                return False

            async def mark_done(self, key):
                pass

        client = tc.TelephonyClient.__new__(tc.TelephonyClient)
        client._store, client._attempts = Store(), {}

        async def deliver(event):
            delivered.append(event)

        client._deliver = deliver
        assert await client._handle(EVENT) is True
        assert delivered == []

    def test_needs_an_owner(self, monkeypatch):
        monkeypatch.setattr(tf.settings, "telephony_fax_enabled", True)
        monkeypatch.setattr(tf.settings, "telephony_enabled", True)
        monkeypatch.setattr(tf.settings, "telephony_fax_owner", " ")
        assert tf.enabled() is False


@pytest.fixture
def gateway(monkeypatch):
    monkeypatch.setattr(vt.settings, "telephony_gateway_url", "ws://127.0.0.1:8090/v1/events")
    seen: list[httpx.Request] = []
    state = {"handler": lambda req: httpx.Response(200, content=PDF)}
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
    async def test_the_pdf_from_the_fax_path(self, gateway):
        seen, _ = gateway
        assert await tf.fetch_fax(FAX) == PDF
        assert seen[0].url.path == f"/v1/faxes/{FAX}"

    async def test_not_a_pdf_fails(self, gateway):
        gateway[1]["handler"] = lambda req: httpx.Response(200, content=b"RIFF....WAVE")
        with pytest.raises(vt._Fail):
            await tf.fetch_fax(FAX)

    async def test_not_ready_retries(self, gateway):
        gateway[1]["handler"] = lambda req: httpx.Response(425)
        with pytest.raises(vt._Retry):
            await tf.fetch_fax(FAX)

    async def test_the_recording_path_still_works(self, gateway):
        # the shared fetch (fetch_gateway_file) must not change the voicemail path
        wav = b"RIFF" + b"\x24\x00\x00\x00" + b"WAVEfmt " + b"\x00" * 28
        gateway[1]["handler"] = lambda req: httpx.Response(200, content=wav)
        assert await vt.fetch_recording(FAX) == wav
        assert gateway[0][0].url.path == f"/v1/recordings/{FAX}"


# --- real Postgres --------------------------------------------------------------------------------

@pytest.fixture
async def env(monkeypatch, pg_db_session):
    """Flag on, owner = a fresh user; notifications and the ingest are recorded, not run."""
    db = pg_db_session
    owner = await _make_user(db, "faxowner")
    other = await _make_user(db, "other")
    await db.commit()
    monkeypatch.setattr(tf.settings, "telephony_fax_enabled", True)
    monkeypatch.setattr(tf.settings, "telephony_enabled", True)
    monkeypatch.setattr(tf.settings, "telephony_fax_owner", owner.username)
    monkeypatch.setattr(tf.settings, "telephony_fax_tier", 1)
    monkeypatch.setattr(tf.settings, "telephony_fax_kb", "Fax")
    monkeypatch.setattr(tf.settings, "telephony_fax_to_paperless", True)
    monkeypatch.setattr(tf.settings, "caller_lookup_enabled", False)

    @asynccontextmanager
    async def _session():
        yield db

    import services.database as dbmod

    monkeypatch.setattr(dbmod, "AsyncSessionLocal", _session)

    notified: list[tuple[dict, list[int]]] = []

    async def notify(event, user_ids, process, resolve=None):
        notified.append((event, list(user_ids)))
        return len(user_ids)

    monkeypatch.setattr(tc, "notify_users", notify)

    async def fetch(name):
        assert name == FAX
        return PDF

    monkeypatch.setattr(tf, "fetch_fax", fetch)

    import api.routes.knowledge as kn

    state = {"worker": True, "result": IngestResult(IngestStatus.INGESTED, document_id=42, detail="enqueued")}

    async def alive():
        return state["worker"]

    monkeypatch.setattr(kn, "_worker_is_alive", alive)
    ingested: list[dict] = []

    async def ingest(file_bytes, meta, **kw):
        ingested.append({"bytes": file_bytes, "meta": meta, **kw})
        return state["result"]

    import services.folder_ingest as fi

    monkeypatch.setattr(fi, "ingest_document", ingest)
    return {"owner": owner, "other": other, "notified": notified, "ingested": ingested, "state": state}


async def _jobs(db):
    return (await db.execute(select(TelephonyFax).execution_options(populate_existing=True))).scalars().all()


async def _make_due(db):
    await db.execute(update(TelephonyFax).values(next_attempt_at=_utcnow() - timedelta(seconds=1)))
    await db.commit()


@pytest.mark.database
class TestEvent:
    async def test_a_replay_is_one_job_and_nobody_is_told_yet(self, pg_db_session, env):
        await tf.handle_event(EVENT)
        await tf.handle_event(EVENT)  # gateway restart: the same fax again
        jobs = await _jobs(pg_db_session)
        assert len(jobs) == 1 and jobs[0].pages == 3 and jobs[0].caller == CALLER
        assert env["notified"] == []

    async def test_a_failed_fax_is_told_to_the_owner_only(self, pg_db_session, env):
        await tf.handle_event({**EVENT, "status": "failed", "fax": None})
        assert await _jobs(pg_db_session) == []
        (event, users), = env["notified"]
        assert users == [env["owner"].id] and event["status"] == "failed"

    async def test_ok_without_a_valid_file_is_a_failed_fax(self, pg_db_session, env):
        await tf.handle_event({**EVENT, "fax": "../haushalt/x"})
        assert await _jobs(pg_db_session) == []
        assert env["notified"][0][0]["status"] == "failed"

    async def test_the_client_delivers_to_the_fax_path_not_to_everyone(self, pg_db_session, env):
        await tc.deliver_via_notification_service(EVENT)
        assert len(await _jobs(pg_db_session)) == 1
        assert env["notified"] == []  # not the every-user notice of a call


@pytest.mark.database
class TestJob:
    async def test_filed_for_the_owner_then_the_owner_is_told(self, pg_db_session, env, caplog):
        db = pg_db_session
        await tf.handle_event(EVENT)
        with caplog.at_level(logging.DEBUG):
            assert await tf.process_one() is True
        (call,) = env["ingested"]
        kb = (await db.execute(select(KnowledgeBase).where(KnowledgeBase.id == call["kb_id"]))).scalar_one()
        assert kb.name == "Fax" and kb.owner_id == env["owner"].id
        assert call["owner_user_id"] == env["owner"].id and call["default_tier"] == 1
        assert call["file_to_paperless"] is True and call["source"] == "telephony_fax"
        assert call["bytes"] == PDF and call["meta"].mime == "application/pdf"
        assert call["meta"].filename.endswith(".pdf") and "2161" not in call["meta"].filename
        (event, users), = env["notified"]
        assert users == [env["owner"].id]
        assert event["filed"] is True and event["kb"] == "Fax" and event["document_id"] == 42
        job = (await _jobs(db))[0]
        assert job.status == "done" and job.document_id == 42
        assert "2161" not in caplog.text and FAX not in caplog.text
        assert await tf.process_one() is False

    async def test_a_second_fax_reuses_the_owners_kb(self, pg_db_session, env):
        await tf.handle_event(EVENT)
        await tf.handle_event({**EVENT, "call_id": "f2"})
        await tf.process_one()
        await tf.process_one()
        a, b = env["ingested"]
        assert a["kb_id"] == b["kb_id"]

    @pytest.mark.parametrize("trouble", ["worker", "ingest_retry"])
    async def test_transient_trouble_retries_without_telling(self, pg_db_session, env, trouble):
        if trouble == "worker":
            env["state"]["worker"] = False
        else:
            env["state"]["result"] = IngestResult(IngestStatus.RETRY, detail="in_progress")
        await tf.handle_event(EVENT)
        await tf.process_one()
        job = (await _jobs(pg_db_session))[0]
        assert job.status == "pending" and job.attempts == 1 and job.next_attempt_at > _utcnow()
        assert env["notified"] == []

    async def test_used_up_attempts_tell_the_owner_filing_failed(self, pg_db_session, env):
        env["state"]["worker"] = False
        await tf.handle_event(EVENT)
        for _ in range(tf.MAX_ATTEMPTS):
            await _make_due(pg_db_session)
            await tf.process_one()
        assert (await _jobs(pg_db_session))[0].status == "failed"
        (event, users), = env["notified"]
        assert users == [env["owner"].id] and event["filed"] is False and event["status"] == "ok"

    async def test_a_terminal_ingest_failure_tells_the_owner_at_once(self, pg_db_session, env):
        env["state"]["result"] = IngestResult(IngestStatus.FAILED, detail="extension_not_allowed")
        await tf.handle_event(EVENT)
        await tf.process_one()
        assert (await _jobs(pg_db_session))[0].status == "failed"
        assert env["notified"][0][0]["filed"] is False

    async def test_a_job_whose_pod_died_every_time_is_given_up(self, pg_db_session, env):
        await tf.handle_event(EVENT)
        await pg_db_session.execute(update(TelephonyFax).values(attempts=tf.MAX_ATTEMPTS))
        await _make_due(pg_db_session)
        assert await tf.process_one() is True
        assert (await _jobs(pg_db_session))[0].status == "failed"
        assert env["notified"][0][0]["filed"] is False
        assert env["ingested"] == []
