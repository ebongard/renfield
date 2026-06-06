"""Obligation → calendar reconciler + calendar-pref routes.

Real PG (ledger SQL + circle/atom joins + advisory lock). The Calendar MCP is
mocked (a FakeMcp recording execute_tool calls and returning the create/update/
delete shapes). Covers create/update/delete/orphan/no-pref/idempotent/failure and
the GET/PUT calendar-pref routes.
"""
from __future__ import annotations

import datetime as dt
import json
from types import SimpleNamespace

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from models.database import Atom, Document, DocumentFact, Role, User
from services.obligation_calendar_sync import ObligationCalendarSync
from utils.config import settings

pytestmark = [pytest.mark.postgres, pytest.mark.asyncio]

_seq = 0
TODAY = dt.date(2026, 6, 6)


class FakeMcp:
    """Records execute_tool calls; returns the calendar MCP response shapes."""
    def __init__(self, *, calendars=None, fail_create=False, delete_not_found=False):
        self.calls: list[tuple[str, dict]] = []
        self._calendars = calendars if calendars is not None else [{"name": "family", "label": "Familie"}]
        self._fail_create = fail_create
        self._delete_not_found = delete_not_found
        self._n = 0

    async def execute_tool(self, tool, args, user_permissions=None, user_id=None, **kw):
        self.calls.append((tool, args))
        if tool.endswith("list_calendars"):
            return {"success": True, "message": json.dumps({"calendars": self._calendars})}
        if tool.endswith("create_event"):
            if self._fail_create:
                return {"success": False, "message": "boom"}
            self._n += 1
            return {"success": True, "message": json.dumps(
                {"success": True, "event": {"id": f"ev-{self._n}", "start": args["start"]}})}
        if tool.endswith("update_event"):
            return {"success": True, "message": json.dumps(
                {"success": True, "event": {"id": args["event_id"]}})}
        if tool.endswith("delete_event"):
            if self._delete_not_found:
                return {"success": False, "message": json.dumps(
                    {"error": f"Event not found: {args.get('event_id')}"})}
            return {"success": True, "message": json.dumps(
                {"success": True, "message": "Event deleted"})}
        return {"success": False, "message": "unknown tool"}

    def calls_for(self, suffix: str) -> list[dict]:
        return [a for (tool, a) in self.calls if tool.endswith(suffix)]


async def _make_user(db: AsyncSession, name: str) -> User:
    role = Role(name=f"{name}_role")
    db.add(role)
    await db.flush()
    u = User(username=name, email=f"{name}@ex.test", password_hash="x",
             role_id=role.id, is_active=True)
    db.add(u)
    await db.flush()
    return u


async def _mk_obligation(db, owner, *, ob_date, kind="zahlung") -> int:
    global _seq
    _seq += 1
    doc_atom = f"00000000-0000-0000-0000-{_seq:012d}"
    db.add(Atom(atom_id=doc_atom, atom_type="kb_document", source_table="documents",
                source_id=f"doc-{_seq}", owner_user_id=owner.id, policy={"tier": 0}))
    await db.flush()
    doc = Document(filename="d.pdf", file_path="/x/d.pdf", status="completed",
                   circle_tier=0, atom_id=doc_atom)
    db.add(doc)
    await db.flush()
    _seq += 1
    fact_atom = f"00000000-0000-0000-0000-{_seq:012d}"
    db.add(Atom(atom_id=fact_atom, atom_type="document_fact", source_table="document_facts",
                source_id=f"fact-{_seq}", owner_user_id=owner.id, policy={"tier": 0}))
    await db.flush()
    fact = DocumentFact(document_id=doc.id, category="obligation", kind=kind, value=f"{kind} fällig",
                        obligation_date=ob_date, source="llm", atom_id=fact_atom, circle_tier=0)
    db.add(fact)
    await db.flush()
    return fact.id


async def _set_pref(db, user_id, calendar):
    await db.execute(
        text("INSERT INTO obligation_calendar_pref (user_id, calendar_name) VALUES (:u, :c)"),
        {"u": user_id, "c": calendar})
    await db.flush()


async def _ledger(db, user_id) -> list[tuple]:
    r = await db.execute(
        text("SELECT document_fact_id, event_id, synced_obligation_date FROM "
             "obligation_calendar_events WHERE user_id = :u ORDER BY id"),
        {"u": user_id})
    return r.fetchall()


def _commit_as_flush(db, monkeypatch):
    monkeypatch.setattr(db, "commit", db.flush)


class TestReconciler:
    async def test_creates_event_and_ledger(self, pg_db_session, monkeypatch):
        _commit_as_flush(pg_db_session, monkeypatch)
        owner = await _make_user(pg_db_session, "cal_create")
        await _set_pref(pg_db_session, owner.id, "family")
        await _mk_obligation(pg_db_session, owner, ob_date=TODAY + dt.timedelta(days=10))
        mcp = FakeMcp()
        rep = await ObligationCalendarSync(pg_db_session, mcp).run_for_user(owner.id, today=TODAY)
        assert rep.created == 1 and rep.errors == 0
        led = await _ledger(pg_db_session, owner.id)
        assert len(led) == 1 and led[0][1] == "ev-1"
        assert mcp.calls_for("create_event")[0]["calendar"] == "family"

    async def test_idempotent_no_duplicate(self, pg_db_session, monkeypatch):
        _commit_as_flush(pg_db_session, monkeypatch)
        owner = await _make_user(pg_db_session, "cal_idem")
        await _set_pref(pg_db_session, owner.id, "family")
        await _mk_obligation(pg_db_session, owner, ob_date=TODAY + dt.timedelta(days=10))
        svc = ObligationCalendarSync(pg_db_session, FakeMcp())
        r1 = await svc.run_for_user(owner.id, today=TODAY)
        svc2 = ObligationCalendarSync(pg_db_session, FakeMcp())
        r2 = await svc2.run_for_user(owner.id, today=TODAY)
        assert r1.created == 1 and r2.created == 0 and r2.updated == 0
        assert len(await _ledger(pg_db_session, owner.id)) == 1

    async def test_update_on_date_change(self, pg_db_session, monkeypatch):
        _commit_as_flush(pg_db_session, monkeypatch)
        owner = await _make_user(pg_db_session, "cal_upd")
        await _set_pref(pg_db_session, owner.id, "family")
        fid = await _mk_obligation(pg_db_session, owner, ob_date=TODAY + dt.timedelta(days=10))
        await ObligationCalendarSync(pg_db_session, FakeMcp()).run_for_user(owner.id, today=TODAY)
        # move the date
        await pg_db_session.execute(
            text("UPDATE document_facts SET obligation_date = :d WHERE id = :id"),
            {"d": TODAY + dt.timedelta(days=12), "id": fid})
        await pg_db_session.flush()
        mcp = FakeMcp()
        rep = await ObligationCalendarSync(pg_db_session, mcp).run_for_user(owner.id, today=TODAY)
        assert rep.updated == 1 and rep.created == 0
        assert len(mcp.calls_for("update_event")) == 1
        led = await _ledger(pg_db_session, owner.id)
        assert led[0][2] == TODAY + dt.timedelta(days=12)

    async def test_delete_on_confirm(self, pg_db_session, monkeypatch):
        _commit_as_flush(pg_db_session, monkeypatch)
        owner = await _make_user(pg_db_session, "cal_conf")
        await _set_pref(pg_db_session, owner.id, "family")
        fid = await _mk_obligation(pg_db_session, owner, ob_date=TODAY + dt.timedelta(days=10))
        await ObligationCalendarSync(pg_db_session, FakeMcp()).run_for_user(owner.id, today=TODAY)
        await pg_db_session.execute(
            text("INSERT INTO obligation_acknowledgements (document_fact_id, user_id, milestone) "
                 "VALUES (:f, :u, 'confirmed')"), {"f": fid, "u": owner.id})
        await pg_db_session.flush()
        mcp = FakeMcp()
        rep = await ObligationCalendarSync(pg_db_session, mcp).run_for_user(owner.id, today=TODAY)
        assert rep.deleted == 1
        assert len(mcp.calls_for("delete_event")) == 1
        assert await _ledger(pg_db_session, owner.id) == []

    async def test_orphan_fact_purged_deletes_event(self, pg_db_session, monkeypatch):
        _commit_as_flush(pg_db_session, monkeypatch)
        owner = await _make_user(pg_db_session, "cal_orphan")
        await _set_pref(pg_db_session, owner.id, "family")
        fid = await _mk_obligation(pg_db_session, owner, ob_date=TODAY + dt.timedelta(days=10))
        await ObligationCalendarSync(pg_db_session, FakeMcp()).run_for_user(owner.id, today=TODAY)
        # purge the fact → ledger.document_fact_id SET NULL (orphan)
        await pg_db_session.execute(text("DELETE FROM document_facts WHERE id = :id"), {"id": fid})
        await pg_db_session.flush()
        mcp = FakeMcp()
        rep = await ObligationCalendarSync(pg_db_session, mcp).run_for_user(owner.id, today=TODAY)
        assert rep.deleted == 1
        assert await _ledger(pg_db_session, owner.id) == []

    async def test_out_of_window_deletes(self, pg_db_session, monkeypatch):
        _commit_as_flush(pg_db_session, monkeypatch)
        owner = await _make_user(pg_db_session, "cal_window")
        await _set_pref(pg_db_session, owner.id, "family")
        fid = await _mk_obligation(pg_db_session, owner, ob_date=TODAY + dt.timedelta(days=10))
        await ObligationCalendarSync(pg_db_session, FakeMcp()).run_for_user(owner.id, today=TODAY)
        # move far beyond horizon (default 90)
        await pg_db_session.execute(
            text("UPDATE document_facts SET obligation_date = :d WHERE id = :id"),
            {"d": TODAY + dt.timedelta(days=400), "id": fid})
        await pg_db_session.flush()
        rep = await ObligationCalendarSync(pg_db_session, FakeMcp()).run_for_user(owner.id, today=TODAY)
        assert rep.deleted == 1 and await _ledger(pg_db_session, owner.id) == []

    async def test_no_pref_skips(self, pg_db_session, monkeypatch):
        _commit_as_flush(pg_db_session, monkeypatch)
        owner = await _make_user(pg_db_session, "cal_nopref")
        await _mk_obligation(pg_db_session, owner, ob_date=TODAY + dt.timedelta(days=10))
        mcp = FakeMcp()
        rep = await ObligationCalendarSync(pg_db_session, mcp).run_for_user(owner.id, today=TODAY)
        assert rep.skipped_no_pref is True and rep.created == 0
        assert mcp.calls == []

    async def test_create_failure_no_ledger_row_retries(self, pg_db_session, monkeypatch):
        _commit_as_flush(pg_db_session, monkeypatch)
        owner = await _make_user(pg_db_session, "cal_fail")
        await _set_pref(pg_db_session, owner.id, "family")
        await _mk_obligation(pg_db_session, owner, ob_date=TODAY + dt.timedelta(days=10))
        rep = await ObligationCalendarSync(pg_db_session, FakeMcp(fail_create=True)).run_for_user(owner.id, today=TODAY)
        assert rep.created == 0 and rep.errors == 1
        assert await _ledger(pg_db_session, owner.id) == []  # nothing claimed → retried next pass

    async def test_delete_not_found_treated_as_success(self, pg_db_session, monkeypatch):
        # F4: the user removed the event in their calendar → not-found on delete
        # must clear the ledger row, not loop forever as an error.
        _commit_as_flush(pg_db_session, monkeypatch)
        owner = await _make_user(pg_db_session, "cal_gone")
        await _set_pref(pg_db_session, owner.id, "family")
        fid = await _mk_obligation(pg_db_session, owner, ob_date=TODAY + dt.timedelta(days=10))
        await ObligationCalendarSync(pg_db_session, FakeMcp()).run_for_user(owner.id, today=TODAY)
        await pg_db_session.execute(
            text("INSERT INTO obligation_acknowledgements (document_fact_id, user_id, milestone) "
                 "VALUES (:f, :u, 'confirmed')"), {"f": fid, "u": owner.id})
        await pg_db_session.flush()
        rep = await ObligationCalendarSync(pg_db_session, FakeMcp(delete_not_found=True)).run_for_user(owner.id, today=TODAY)
        assert rep.deleted == 1 and rep.errors == 0
        assert await _ledger(pg_db_session, owner.id) == []

    async def test_op_cap_defers_remainder(self, pg_db_session, monkeypatch):
        # F7: a per-pass cap bounds the serial MCP fan-out.
        _commit_as_flush(pg_db_session, monkeypatch)
        monkeypatch.setattr(settings, "obligation_calendar_max_ops_per_run", 2)
        owner = await _make_user(pg_db_session, "cal_cap")
        await _set_pref(pg_db_session, owner.id, "family")
        for i in range(3):
            await _mk_obligation(pg_db_session, owner, ob_date=TODAY + dt.timedelta(days=5 + i))
        rep = await ObligationCalendarSync(pg_db_session, FakeMcp()).run_for_user(owner.id, today=TODAY)
        assert rep.created == 2
        assert any("op cap" in n for n in rep.notes)
        assert len(await _ledger(pg_db_session, owner.id)) == 2

    async def test_advisory_lock_overlap_is_noop(self, pg_db_session, monkeypatch):
        _commit_as_flush(pg_db_session, monkeypatch)
        owner = await _make_user(pg_db_session, "cal_lock")
        await _set_pref(pg_db_session, owner.id, "family")
        await _mk_obligation(pg_db_session, owner, ob_date=TODAY + dt.timedelta(days=10))
        from services.obligation_calendar_sync import _CAL_LOCK_NS
        engine = pg_db_session.bind.engine
        async with engine.connect() as held:
            assert (await held.execute(text("SELECT pg_try_advisory_lock(:ns, :uid)"),
                                       {"ns": _CAL_LOCK_NS, "uid": owner.id})).scalar()
            rep = await ObligationCalendarSync(pg_db_session, FakeMcp()).run_for_user(owner.id, today=TODAY)
            await held.execute(text("SELECT pg_advisory_unlock(:ns, :uid)"),
                               {"ns": _CAL_LOCK_NS, "uid": owner.id})
        assert rep.created == 0 and any("holds this user's lock" in n for n in rep.notes)


class TestCalendarPrefRoutes:
    def _req(self, mcp):
        return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(mcp_manager=mcp)))

    async def test_get_pref_lists_available(self, pg_db_session, monkeypatch):
        _commit_as_flush(pg_db_session, monkeypatch)
        from api.routes.atoms import get_calendar_pref
        owner = await _make_user(pg_db_session, "pref_get")
        res = await get_calendar_pref(request=self._req(FakeMcp()), db=pg_db_session, current_user=owner)
        assert res.calendar_name is None
        assert [c.name for c in res.available] == ["family"]

    async def test_put_pref_validates_and_persists(self, pg_db_session, monkeypatch):
        _commit_as_flush(pg_db_session, monkeypatch)
        from fastapi import HTTPException
        from api.routes.atoms import set_calendar_pref, get_calendar_pref, SetCalendarPrefRequest
        owner = await _make_user(pg_db_session, "pref_put")
        req = self._req(FakeMcp())
        # invalid calendar → 400
        with pytest.raises(HTTPException) as ei:
            await set_calendar_pref(SetCalendarPrefRequest(calendar_name="nope"), request=req,
                                    db=pg_db_session, current_user=owner)
        assert ei.value.status_code == 400
        # valid → persisted
        res = await set_calendar_pref(SetCalendarPrefRequest(calendar_name="family"), request=req,
                                      db=pg_db_session, current_user=owner)
        assert res.calendar_name == "family"
        got = await get_calendar_pref(request=req, db=pg_db_session, current_user=owner)
        assert got.calendar_name == "family"
        # clear (null) → removed
        cleared = await set_calendar_pref(SetCalendarPrefRequest(calendar_name=None), request=req,
                                          db=pg_db_session, current_user=owner)
        assert cleared.calendar_name is None
        assert (await get_calendar_pref(request=req, db=pg_db_session, current_user=owner)).calendar_name is None

    async def test_clear_pref_tears_down_events(self, pg_db_session, monkeypatch):
        # F2: turning sync off must remove the user's already-synced events
        # (not orphan them in the calendar forever).
        _commit_as_flush(pg_db_session, monkeypatch)
        from api.routes.atoms import set_calendar_pref, SetCalendarPrefRequest
        owner = await _make_user(pg_db_session, "cal_teardown")
        await _set_pref(pg_db_session, owner.id, "family")
        await _mk_obligation(pg_db_session, owner, ob_date=TODAY + dt.timedelta(days=10))
        mcp = FakeMcp()
        await ObligationCalendarSync(pg_db_session, mcp).run_for_user(owner.id, today=TODAY)
        assert len(await _ledger(pg_db_session, owner.id)) == 1
        # clear the pref via the route → teardown deletes the event + ledger row
        await set_calendar_pref(SetCalendarPrefRequest(calendar_name=None), request=self._req(mcp),
                                db=pg_db_session, current_user=owner)
        assert len(mcp.calls_for("delete_event")) == 1
        assert await _ledger(pg_db_session, owner.id) == []
        pref = (await pg_db_session.execute(
            text("SELECT 1 FROM obligation_calendar_pref WHERE user_id = :u"), {"u": owner.id})).first()
        assert pref is None
