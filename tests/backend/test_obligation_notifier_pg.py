"""Obligation-deadline notifier — notifier ledger + Bestätigt endpoints.

Real PG (``pg_db_session``): the notifier scans by ``atoms.owner_user_id``, the
endpoints circle-filter via ``document_facts_circles_filter``, and the ledger
uses a unique constraint + ``ANY(:ids)`` — all PG-shaped. The notifier and
``process_webhook`` commit internally, so under the rollback-isolated fixture we
patch ``commit -> flush`` (same trick as test_kg_reconciler_pg).
"""
from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from models.database import Atom, Document, DocumentFact, Notification, Role, User
from services.obligation_deadline_notifier import (
    ObligationDeadlineNotifier,
    current_milestone,
)
from utils.config import settings

pytestmark = [pytest.mark.postgres, pytest.mark.asyncio]

_seq = 0


# ---------------------------------------------------------------------------
# current_milestone — pure function, no DB (the no-storm guarantee)
# ---------------------------------------------------------------------------
@pytest.mark.unit
@pytest.mark.parametrize("days,expected", [
    (-5, "overdue"), (-1, "overdue"),
    (0, "due"),
    (1, "1d"),
    (2, "3d"), (3, "3d"),
    (4, "7d"), (7, "7d"),
    (8, "14d"), (14, "14d"),
    (15, None), (30, None),
])
def test_current_milestone_buckets(days, expected):
    assert current_milestone(days) == expected


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------
async def _make_user(db: AsyncSession, name: str) -> User:
    role = Role(name=f"{name}_role")
    db.add(role)
    await db.flush()
    u = User(username=name, email=f"{name}@ex.test", password_hash="x",
             role_id=role.id, is_active=True)
    db.add(u)
    await db.flush()
    return u


async def _mk_obligation(
    db: AsyncSession, owner: User, *, ob_date: dt.date, kind: str = "zahlung",
    legal: bool = False, tier: int = 0, amount=None, currency: str | None = None,
) -> int:
    """Create a dated obligation fact owned by ``owner`` (document atom + fact
    atom both owned by the user so the circle owner-branch resolves)."""
    global _seq
    _seq += 1
    doc_atom = f"00000000-0000-0000-0000-{_seq:012d}"
    db.add(Atom(atom_id=doc_atom, atom_type="kb_document", source_table="documents",
                source_id=f"doc-{_seq}", owner_user_id=owner.id, policy={"tier": tier}))
    await db.flush()
    doc = Document(filename="d.pdf", file_path="/x/d.pdf", status="completed",
                   circle_tier=tier, atom_id=doc_atom)
    db.add(doc)
    await db.flush()
    _seq += 1
    fact_atom = f"00000000-0000-0000-0000-{_seq:012d}"
    db.add(Atom(atom_id=fact_atom, atom_type="document_fact", source_table="document_facts",
                source_id=f"fact-{_seq}", owner_user_id=owner.id, policy={"tier": tier}))
    await db.flush()
    fact = DocumentFact(document_id=doc.id, category="obligation", kind=kind,
                        value=kind, obligation_date=ob_date, legal_gate=legal,
                        amount_value=amount, amount_currency=currency,
                        source="llm", atom_id=fact_atom, circle_tier=tier)
    db.add(fact)
    await db.flush()
    return fact.id


def _commit_as_flush(db, monkeypatch):
    monkeypatch.setattr(db, "commit", db.flush)


async def _ack_count(db, fact_id, user_id) -> int:
    r = await db.execute(
        text("SELECT count(*) FROM obligation_acknowledgements "
             "WHERE document_fact_id=:f AND user_id=:u"),
        {"f": fact_id, "u": user_id},
    )
    return int(r.scalar())


async def _notif_count(db, user_id) -> int:
    r = await db.execute(
        text("SELECT count(*) FROM notifications WHERE target_user_id=:u"),
        {"u": user_id},
    )
    return int(r.scalar())


TODAY = dt.date(2026, 6, 6)


# ---------------------------------------------------------------------------
# notifier scan (T1)
# ---------------------------------------------------------------------------
class TestNotifierScan:
    async def test_first_enable_fires_single_bucket_no_storm(self, pg_db_session, monkeypatch):
        # An obligation 2 days out must fire ONLY "3d", not 14d+7d+3d at once.
        _commit_as_flush(pg_db_session, monkeypatch)
        owner = await _make_user(pg_db_session, "ob_storm")
        fid = await _mk_obligation(pg_db_session, owner, ob_date=TODAY + dt.timedelta(days=2))
        rep = await ObligationDeadlineNotifier(pg_db_session).run_for_user(owner.id, today=TODAY)
        assert rep.notified == 1
        assert await _ack_count(pg_db_session, fid, owner.id) == 1
        row = (await pg_db_session.execute(
            text("SELECT milestone FROM obligation_acknowledgements WHERE document_fact_id=:f"),
            {"f": fid})).scalar()
        assert row == "3d"

    async def test_no_refire_same_day(self, pg_db_session, monkeypatch):
        _commit_as_flush(pg_db_session, monkeypatch)
        owner = await _make_user(pg_db_session, "ob_refire")
        await _mk_obligation(pg_db_session, owner, ob_date=TODAY + dt.timedelta(days=3))
        svc = ObligationDeadlineNotifier(pg_db_session)
        first = await svc.run_for_user(owner.id, today=TODAY)
        second = await svc.run_for_user(owner.id, today=TODAY)
        assert first.notified == 1
        assert second.notified == 0 and second.skipped_ledger == 1

    async def test_progression_fires_each_new_bucket_once(self, pg_db_session, monkeypatch):
        _commit_as_flush(pg_db_session, monkeypatch)
        owner = await _make_user(pg_db_session, "ob_prog")
        due = TODAY + dt.timedelta(days=10)
        await _mk_obligation(pg_db_session, owner, ob_date=due)
        svc = ObligationDeadlineNotifier(pg_db_session)
        # day 0: 10 days out → "14d"
        r1 = await svc.run_for_user(owner.id, today=TODAY)
        # +4 days: 6 out → "7d"; +7: 3 out → "3d"; +9: 1 out → "1d"; +10: due
        r2 = await svc.run_for_user(owner.id, today=TODAY + dt.timedelta(days=4))
        r3 = await svc.run_for_user(owner.id, today=due)  # 0 out → "due"
        assert (r1.notified, r2.notified, r3.notified) == (1, 1, 1)
        ms = {m for (m,) in (await pg_db_session.execute(
            text("SELECT milestone FROM obligation_acknowledgements"))).all()}
        assert ms == {"14d", "7d", "due"}

    async def test_owner_targeted_peer_not_notified(self, pg_db_session, monkeypatch):
        _commit_as_flush(pg_db_session, monkeypatch)
        owner = await _make_user(pg_db_session, "ob_owner")
        peer = await _make_user(pg_db_session, "ob_peer")
        await _mk_obligation(pg_db_session, owner, ob_date=TODAY + dt.timedelta(days=1), tier=2)
        svc = ObligationDeadlineNotifier(pg_db_session)
        peer_rep = await svc.run_for_user(peer.id, today=TODAY)
        owner_rep = await svc.run_for_user(owner.id, today=TODAY)
        assert peer_rep.scanned == 0 and peer_rep.notified == 0   # peer owns nothing
        assert owner_rep.notified == 1
        assert await _notif_count(pg_db_session, peer.id) == 0
        assert await _notif_count(pg_db_session, owner.id) == 1

    async def test_confirmed_suppresses(self, pg_db_session, monkeypatch):
        _commit_as_flush(pg_db_session, monkeypatch)
        owner = await _make_user(pg_db_session, "ob_conf")
        fid = await _mk_obligation(pg_db_session, owner, ob_date=TODAY + dt.timedelta(days=1))
        pg_db_session.add_all([])  # noop
        await pg_db_session.execute(
            text("INSERT INTO obligation_acknowledgements (document_fact_id, user_id, milestone) "
                 "VALUES (:f, :u, 'confirmed')"), {"f": fid, "u": owner.id})
        await pg_db_session.flush()
        rep = await ObligationDeadlineNotifier(pg_db_session).run_for_user(owner.id, today=TODAY)
        assert rep.skipped_confirmed == 1 and rep.notified == 0

    async def test_legal_gate_fires_and_is_flagged(self, pg_db_session, monkeypatch):
        _commit_as_flush(pg_db_session, monkeypatch)
        owner = await _make_user(pg_db_session, "ob_legal")
        await _mk_obligation(pg_db_session, owner, ob_date=TODAY + dt.timedelta(days=7),
                             kind="widerspruch", legal=True)
        rep = await ObligationDeadlineNotifier(pg_db_session).run_for_user(owner.id, today=TODAY)
        assert rep.notified == 1
        row = (await pg_db_session.execute(
            text("SELECT urgency, source_data, message FROM notifications WHERE target_user_id=:u"),
            {"u": owner.id})).first()
        urgency, data, message = row
        assert urgency == "critical"             # legal raises urgency
        assert data["legal_gate"] is True
        assert "/brain/review" in message        # human-gated pointer

    async def test_too_far_out_not_notified(self, pg_db_session, monkeypatch):
        _commit_as_flush(pg_db_session, monkeypatch)
        owner = await _make_user(pg_db_session, "ob_far")
        await _mk_obligation(pg_db_session, owner, ob_date=TODAY + dt.timedelta(days=40))
        rep = await ObligationDeadlineNotifier(pg_db_session).run_for_user(owner.id, today=TODAY)
        assert rep.scanned == 0 and rep.notified == 0  # outside the +14d window

    async def test_advisory_lock_overlap_is_noop(self, pg_db_session, monkeypatch):
        _commit_as_flush(pg_db_session, monkeypatch)
        owner = await _make_user(pg_db_session, "ob_lock")
        await _mk_obligation(pg_db_session, owner, ob_date=TODAY + dt.timedelta(days=1))
        # Hold the user's notifier advisory lock on a separate connection.
        from services.obligation_deadline_notifier import _NOTIFIER_LOCK_NS
        engine = pg_db_session.bind.engine
        async with engine.connect() as held:
            got = (await held.execute(text("SELECT pg_try_advisory_lock(:ns, :uid)"),
                                      {"ns": _NOTIFIER_LOCK_NS, "uid": owner.id})).scalar()
            assert got
            rep = await ObligationDeadlineNotifier(pg_db_session).run_for_user(owner.id, today=TODAY)
            await held.execute(text("SELECT pg_advisory_unlock(:ns, :uid)"),
                               {"ns": _NOTIFIER_LOCK_NS, "uid": owner.id})
        assert rep.notified == 0
        assert any("holds this user's lock" in n for n in rep.notes)

    async def test_list_owner_user_ids_only_in_window(self, pg_db_session, monkeypatch):
        _commit_as_flush(pg_db_session, monkeypatch)
        a = await _make_user(pg_db_session, "ob_lst_a")
        b = await _make_user(pg_db_session, "ob_lst_b")
        await _mk_obligation(pg_db_session, a, ob_date=TODAY + dt.timedelta(days=5))
        await _mk_obligation(pg_db_session, b, ob_date=TODAY + dt.timedelta(days=99))  # out of window
        ids = await ObligationDeadlineNotifier(pg_db_session).list_owner_user_ids(today=TODAY)
        assert a.id in ids and b.id not in ids


# ---------------------------------------------------------------------------
# Bestätigt endpoints (T2)
# ---------------------------------------------------------------------------
class TestConfirmEndpoints:
    async def test_confirm_then_reopen_roundtrip(self, pg_db_session, monkeypatch):
        monkeypatch.setattr(settings, "auth_enabled", True)
        _commit_as_flush(pg_db_session, monkeypatch)
        from api.routes.atoms import confirm_obligation, reopen_obligation, get_obligations
        owner = await _make_user(pg_db_session, "ep_owner")
        fid = await _mk_obligation(pg_db_session, owner, ob_date=TODAY + dt.timedelta(days=3))

        res = await confirm_obligation(fid, db=pg_db_session, current_user=owner)
        assert res.confirmed is True
        assert await _ack_count(pg_db_session, fid, owner.id) == 1
        # idempotent
        res2 = await confirm_obligation(fid, db=pg_db_session, current_user=owner)
        assert res2.confirmed is True
        assert await _ack_count(pg_db_session, fid, owner.id) == 1
        # obligations response carries confirmed=True
        facts = await get_obligations(due_before=None, limit=50, offset=0,
                                      db=pg_db_session, current_user=owner)
        assert [f.confirmed for f in facts if f.id == fid] == [True]
        # reopen
        res3 = await reopen_obligation(fid, db=pg_db_session, current_user=owner)
        assert res3.confirmed is False
        assert await _ack_count(pg_db_session, fid, owner.id) == 0

    async def test_confirm_not_visible_404(self, pg_db_session, monkeypatch):
        monkeypatch.setattr(settings, "auth_enabled", True)
        _commit_as_flush(pg_db_session, monkeypatch)
        from fastapi import HTTPException
        from api.routes.atoms import confirm_obligation
        owner = await _make_user(pg_db_session, "ep_own2")
        peer = await _make_user(pg_db_session, "ep_peer2")
        fid = await _mk_obligation(pg_db_session, owner, ob_date=TODAY + dt.timedelta(days=3), tier=0)
        with pytest.raises(HTTPException) as ei:
            await confirm_obligation(fid, db=pg_db_session, current_user=peer)
        assert ei.value.status_code == 404
        assert await _ack_count(pg_db_session, fid, peer.id) == 0

    async def test_confirm_is_per_user(self, pg_db_session, monkeypatch):
        # A household peer confirming their own view must not flip the owner's.
        monkeypatch.setattr(settings, "auth_enabled", True)
        _commit_as_flush(pg_db_session, monkeypatch)
        from api.routes.atoms import confirm_obligation, get_obligations
        owner = await _make_user(pg_db_session, "ep_hh_owner")
        peer = await _make_user(pg_db_session, "ep_hh_peer")
        # tier=4 (public) so the peer can see it without circle setup
        fid = await _mk_obligation(pg_db_session, owner, ob_date=TODAY + dt.timedelta(days=3), tier=4)
        await confirm_obligation(fid, db=pg_db_session, current_user=peer)
        owner_facts = await get_obligations(due_before=None, limit=50, offset=0,
                                            db=pg_db_session, current_user=owner)
        assert [f.confirmed for f in owner_facts if f.id == fid] == [False]  # owner's view untouched
