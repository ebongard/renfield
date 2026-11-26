"""Caller lookup (#1240 follow-up): the call notification names the caller per recipient.

Text first (a name is a hint beside the raw number, marked unchecked; flag off =
byte-identical), then the per-recipient fan-out, then real Postgres: what one
recipient may see, the other may not; fax never answers; manual beats document;
two entities read "A oder B"; failures and timeouts fall back to the number.
Fictitious numbers only -- this repository is public.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

import pytest

import services.telephony_client as tc
from services.caller_lookup import CallerHint, offline_place, resolve_caller
from services.contact_points import ContactPointService
from tests.backend.test_kg_contact_points import _entity, _make_user

CALL = {"type": "incoming_call", "call_id": "c1", "caller": "+49301234567", "did": "+4921611234567"}
VOICEMAIL = {**CALL, "type": "voicemail", "duration_s": 42}


class TestText:
    def test_no_hint_is_byte_identical(self):
        assert tc.build_notification(CALL, None) == tc.build_notification(CALL)
        assert "caller_name" not in tc.build_notification(CALL)["data"]

    def test_name_in_title_number_and_caveat_in_message(self):
        kw = tc.build_notification(CALL, CallerHint(names=("Beispiel Praxis",), source="manual"))
        assert kw["title"] == "Anruf von Beispiel Praxis an 021611234567"
        assert kw["message"] == (
            "Eingehender Anruf von Beispiel Praxis (0301234567; Name laut Kontaktdaten, "
            "nicht geprüft) an 021611234567"
        )
        assert kw["tts"] is False
        assert kw["data"]["caller_name"] == "Beispiel Praxis"
        assert kw["data"]["caller"] == "+49301234567"

    def test_a_name_makes_the_notification_confidential(self):
        # The name passed THIS recipient's circle filter; a shared room display must
        # not show it to another household member ("personal" would allow that).
        assert tc.build_notification(CALL)["privacy"] == "personal"
        assert tc.build_notification(CALL, CallerHint(place="Berlin"))["privacy"] == "personal"
        named = tc.build_notification(CALL, CallerHint(names=("Beispiel Praxis",), source="manual"))
        assert named["privacy"] == "confidential"

    def test_untrusted_name_loses_bidi_and_control_characters(self):
        from services.caller_lookup import _clean

        assert _clean("Beispiel\u202e Praxis\u200b\x85\u2028GmbH\n") == "Beispiel Praxis GmbH"
        assert len(_clean("x" * 500)) == 80

    def test_document_source_is_named(self):
        kw = tc.build_notification(VOICEMAIL, CallerHint(names=("Beispiel Stadtwerke",), source="document"))
        assert "Name laut Dokument, nicht geprüft" in kw["message"]
        assert kw["message"].endswith("(0:42)")

    def test_two_names_and_more(self):
        hint = CallerHint(names=("A GmbH", "B AG"), more=True, source="manual")
        assert tc.build_notification(CALL, hint)["title"] == "Anruf von A GmbH oder B AG und weitere an 021611234567"

    def test_place_hint_on_a_miss(self):
        kw = tc.build_notification(CALL, CallerHint(place="Berlin"))
        assert kw["title"] == "Anruf von 0301234567 (Berlin) an 021611234567"
        assert "caller_name" not in kw["data"]

    def test_offline_place(self):
        assert offline_place("+49301234567") == "Berlin"
        assert offline_place("+491701234567") == "Mobilfunk"
        assert offline_place("anonymous") is None


class TestFanOut:
    async def test_each_recipient_gets_their_own_text(self):
        sent: dict[int, str] = {}

        async def process(**kw):
            sent[kw["target_user_id"]] = kw["title"]

        async def resolve(caller, user_id):
            assert caller == "+49301234567"
            return CallerHint(names=("Beispiel Praxis",), source="manual") if user_id == 1 else None

        assert await tc.notify_users(CALL, [1, 2], process, resolve) == 2
        assert sent[1].startswith("Anruf von Beispiel Praxis")
        assert sent[2] == tc.build_notification(CALL)["title"]

    async def test_withheld_number_is_never_looked_up(self):
        called = []

        async def resolve(caller, user_id):
            called.append(caller)

        async def process(**kw):
            pass

        await tc.notify_users({**CALL, "caller": "anonymous"}, [1], process, resolve)
        assert called == []


@pytest.fixture
def enabled(monkeypatch, pg_db_session):
    from utils.config import settings

    monkeypatch.setattr(settings, "contact_points_enabled", True)
    monkeypatch.setattr(settings, "caller_lookup_enabled", True)
    monkeypatch.setattr(settings, "contact_points_default_region", "DE")
    monkeypatch.setattr(settings, "auth_enabled", True)

    @asynccontextmanager
    async def _session():
        yield pg_db_session

    import services.database as dbmod

    monkeypatch.setattr(dbmod, "AsyncSessionLocal", _session)
    return settings


@pytest.mark.database
class TestResolve:
    async def test_the_owner_sees_the_name_a_stranger_only_the_place(self, pg_db_session, enabled):
        db = pg_db_session
        owner, stranger = await _make_user(db, "own"), await _make_user(db, "str")
        praxis = await _entity(db, owner, "Beispiel Praxis", tier=0, etype="organization")
        await db.commit()
        await ContactPointService(db).add(praxis, kind="phone", value="030 1234567")
        hit = await resolve_caller("+49301234567", owner.id)
        assert hit.label() == "Beispiel Praxis" and hit.source == "manual"
        miss = await resolve_caller("+49301234567", stranger.id)
        assert (miss.label(), miss.place) == (None, "Berlin")

    async def test_fax_never_answers_for_a_caller(self, pg_db_session, enabled):
        db = pg_db_session
        owner = await _make_user(db, "own")
        firm = await _entity(db, owner, "Beispiel Fax GmbH", tier=0, etype="organization")
        await db.commit()
        await ContactPointService(db).add(firm, kind="fax", value="030 1234568")
        assert (await resolve_caller("+49301234568", owner.id)).label() is None

    async def test_manual_beats_document_and_two_entities_read_a_or_b(self, pg_db_session, enabled):
        db = pg_db_session
        owner = await _make_user(db, "own")
        a = await _entity(db, owner, "A Person", tier=0)
        b = await _entity(db, owner, "B Person", tier=0)
        c = await _entity(db, owner, "C Firma", tier=0, etype="organization")
        await db.commit()
        svc = ContactPointService(db)
        await svc.add(a, kind="phone", value="030 7654321")
        await svc.add(b, kind="phone", value="030 7654321")
        await svc.add(c, kind="phone", value="030 7654321", source_type="document")
        hit = await resolve_caller("030 7654321", owner.id)
        assert set(hit.names) == {"A Person", "B Person"} and hit.label().count(" oder ") == 1

    async def test_flag_off_and_no_number(self, pg_db_session, enabled):
        assert await resolve_caller(None, 1) is None
        assert await resolve_caller("anonymous", 1) is None
        enabled.caller_lookup_enabled = False
        assert await resolve_caller("+49301234567", 1) is None

    async def test_timeout_and_failure_fall_back(self, pg_db_session, enabled, monkeypatch):
        import services.caller_lookup as cl

        async def slow(*a):
            await asyncio.sleep(1)

        monkeypatch.setattr(cl, "_lookup", slow)
        enabled.caller_lookup_timeout_s = 0.05
        assert await resolve_caller("+49301234567", 1) is None

        async def boom(*a):
            raise RuntimeError("db down")

        monkeypatch.setattr(cl, "_lookup", boom)
        assert await resolve_caller("+49301234567", 1) is None
