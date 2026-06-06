"""Weekly obligation digest — the safety floor under the per-milestone notifier.

Real PG (``pg_db_session``). Verifies the digest gathers OPEN obligations with NO
lower date bound (so a very-overdue, late-extracted deadline the notifier's grace
window missed still surfaces — the F3 floor), is owner-targeted, skips confirmed,
and dedups one send per ISO week (restart-safe via obligation_digest_log).
``process_webhook`` commits internally, so commit→flush is patched.
"""
from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from models.database import Atom, Document, DocumentFact, Role, User
from services.obligation_digest import ObligationDigest, period_key
from utils.config import settings

pytestmark = [pytest.mark.postgres, pytest.mark.asyncio]

_seq = 0
TODAY = dt.date(2026, 6, 6)


@pytest.mark.unit
def test_period_key_iso_week():
    assert period_key(dt.date(2026, 6, 6)) == "2026-W23"
    assert period_key(dt.date(2026, 1, 1)) == "2026-W01"


async def _make_user(db: AsyncSession, name: str) -> User:
    role = Role(name=f"{name}_role")
    db.add(role)
    await db.flush()
    u = User(username=name, email=f"{name}@ex.test", password_hash="x",
             role_id=role.id, is_active=True)
    db.add(u)
    await db.flush()
    return u


async def _mk_obligation(db, owner, *, ob_date, kind="zahlung", legal=False, tier=0) -> int:
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
                        source="llm", atom_id=fact_atom, circle_tier=tier)
    db.add(fact)
    await db.flush()
    return fact.id


def _commit_as_flush(db, monkeypatch):
    monkeypatch.setattr(db, "commit", db.flush)


async def _digest_notifs(db, user_id) -> list[str]:
    r = await db.execute(
        text("SELECT message FROM notifications WHERE target_user_id=:u AND source='obligation_digest'"),
        {"u": user_id},
    )
    return [row[0] for row in r.fetchall()]


async def _log_count(db, user_id) -> int:
    r = await db.execute(
        text("SELECT count(*) FROM obligation_digest_log WHERE user_id=:u"), {"u": user_id})
    return int(r.scalar())


class TestDigest:
    async def test_sends_summary_of_open_obligations(self, pg_db_session, monkeypatch):
        _commit_as_flush(pg_db_session, monkeypatch)
        owner = await _make_user(pg_db_session, "dg_send")
        await _mk_obligation(pg_db_session, owner, ob_date=TODAY + dt.timedelta(days=5))
        await _mk_obligation(pg_db_session, owner, ob_date=TODAY - dt.timedelta(days=2), kind="miete")
        rep = await ObligationDigest(pg_db_session).run_for_user(owner.id, today=TODAY)
        assert rep.sent == 1 and rep.obligations == 2
        msgs = await _digest_notifs(pg_db_session, owner.id)
        assert len(msgs) == 1
        assert "2 offene Fristen" in msgs[0]
        assert await _log_count(pg_db_session, owner.id) == 1

    async def test_includes_very_overdue_late_extracted(self, pg_db_session, monkeypatch):
        # The F3 floor: an obligation 60 days overdue (outside the notifier's
        # 30-day grace window) MUST still appear in the digest.
        _commit_as_flush(pg_db_session, monkeypatch)
        owner = await _make_user(pg_db_session, "dg_overdue")
        await _mk_obligation(pg_db_session, owner, ob_date=TODAY - dt.timedelta(days=60), kind="widerspruch", legal=True)
        rep = await ObligationDigest(pg_db_session).run_for_user(owner.id, today=TODAY)
        assert rep.sent == 1 and rep.obligations == 1
        msg = (await _digest_notifs(pg_db_session, owner.id))[0]
        assert "widerspruch" in msg and "rechtlich" in msg  # legal flagged in the digest

    async def test_skips_confirmed(self, pg_db_session, monkeypatch):
        _commit_as_flush(pg_db_session, monkeypatch)
        owner = await _make_user(pg_db_session, "dg_conf")
        fid = await _mk_obligation(pg_db_session, owner, ob_date=TODAY + dt.timedelta(days=3))
        await pg_db_session.execute(
            text("INSERT INTO obligation_acknowledgements (document_fact_id, user_id, milestone) "
                 "VALUES (:f, :u, 'confirmed')"), {"f": fid, "u": owner.id})
        await pg_db_session.flush()
        rep = await ObligationDigest(pg_db_session).run_for_user(owner.id, today=TODAY)
        assert rep.obligations == 0 and rep.sent == 0  # confirmed obligation excluded
        assert await _log_count(pg_db_session, owner.id) == 0  # nothing open → no log row

    async def test_weekly_dedup_no_resend(self, pg_db_session, monkeypatch):
        _commit_as_flush(pg_db_session, monkeypatch)
        owner = await _make_user(pg_db_session, "dg_dedup")
        await _mk_obligation(pg_db_session, owner, ob_date=TODAY + dt.timedelta(days=4))
        svc = ObligationDigest(pg_db_session)
        first = await svc.run_for_user(owner.id, today=TODAY)
        # same ISO week — TODAY is Sat 2026-06-06 (W23); +1 = Sun, still W23.
        second = await svc.run_for_user(owner.id, today=TODAY + dt.timedelta(days=1))
        assert first.sent == 1
        assert second.sent == 0 and second.skipped_already_sent is True
        assert len(await _digest_notifs(pg_db_session, owner.id)) == 1

    async def test_next_week_sends_again(self, pg_db_session, monkeypatch):
        _commit_as_flush(pg_db_session, monkeypatch)
        owner = await _make_user(pg_db_session, "dg_nextwk")
        await _mk_obligation(pg_db_session, owner, ob_date=TODAY + dt.timedelta(days=20))
        svc = ObligationDigest(pg_db_session)
        await svc.run_for_user(owner.id, today=TODAY)
        nxt = await svc.run_for_user(owner.id, today=TODAY + dt.timedelta(days=7))
        assert nxt.sent == 1  # new ISO week → fresh digest
        assert len(await _digest_notifs(pg_db_session, owner.id)) == 2

    async def test_no_open_obligations_no_send(self, pg_db_session, monkeypatch):
        _commit_as_flush(pg_db_session, monkeypatch)
        owner = await _make_user(pg_db_session, "dg_empty")
        rep = await ObligationDigest(pg_db_session).run_for_user(owner.id, today=TODAY)
        assert rep.sent == 0 and rep.obligations == 0
        assert await _log_count(pg_db_session, owner.id) == 0

    async def test_owner_targeted(self, pg_db_session, monkeypatch):
        _commit_as_flush(pg_db_session, monkeypatch)
        owner = await _make_user(pg_db_session, "dg_owner")
        peer = await _make_user(pg_db_session, "dg_peer")
        await _mk_obligation(pg_db_session, owner, ob_date=TODAY + dt.timedelta(days=3), tier=2)
        svc = ObligationDigest(pg_db_session)
        peer_rep = await svc.run_for_user(peer.id, today=TODAY)
        owner_rep = await svc.run_for_user(owner.id, today=TODAY)
        assert peer_rep.sent == 0 and peer_rep.obligations == 0
        assert owner_rep.sent == 1
        assert await _digest_notifs(pg_db_session, peer.id) == []

    async def test_two_users_identical_content_both_sent(self, pg_db_session, monkeypatch):
        # Regression: two household members with an identical digest (same kind +
        # date → identical title/message) within the suppression window must BOTH
        # receive it. The dedup key now includes target_user_id, so the second is
        # not suppressed as a content-hash "duplicate".
        _commit_as_flush(pg_db_session, monkeypatch)
        a = await _make_user(pg_db_session, "dg_ident_a")
        b = await _make_user(pg_db_session, "dg_ident_b")
        await _mk_obligation(pg_db_session, a, ob_date=TODAY + dt.timedelta(days=4), kind="miete")
        await _mk_obligation(pg_db_session, b, ob_date=TODAY + dt.timedelta(days=4), kind="miete")
        svc = ObligationDigest(pg_db_session)
        ra = await svc.run_for_user(a.id, today=TODAY)
        rb = await svc.run_for_user(b.id, today=TODAY)
        assert ra.sent == 1 and rb.sent == 1
        assert len(await _digest_notifs(pg_db_session, a.id)) == 1
        assert len(await _digest_notifs(pg_db_session, b.id)) == 1

    async def test_list_owner_user_ids(self, pg_db_session, monkeypatch):
        _commit_as_flush(pg_db_session, monkeypatch)
        a = await _make_user(pg_db_session, "dg_lst_a")
        b = await _make_user(pg_db_session, "dg_lst_b")
        await _mk_obligation(pg_db_session, a, ob_date=TODAY - dt.timedelta(days=100))  # very overdue still counts
        ids = await ObligationDigest(pg_db_session).list_owner_user_ids(today=TODAY)
        assert a.id in ids and b.id not in ids

    async def test_advisory_lock_overlap_is_noop(self, pg_db_session, monkeypatch):
        _commit_as_flush(pg_db_session, monkeypatch)
        owner = await _make_user(pg_db_session, "dg_lock")
        await _mk_obligation(pg_db_session, owner, ob_date=TODAY + dt.timedelta(days=3))
        from services.obligation_digest import _DIGEST_LOCK_NS
        engine = pg_db_session.bind.engine
        async with engine.connect() as held:
            assert (await held.execute(text("SELECT pg_try_advisory_lock(:ns, :uid)"),
                                       {"ns": _DIGEST_LOCK_NS, "uid": owner.id})).scalar()
            rep = await ObligationDigest(pg_db_session).run_for_user(owner.id, today=TODAY)
            await held.execute(text("SELECT pg_advisory_unlock(:ns, :uid)"),
                               {"ns": _DIGEST_LOCK_NS, "uid": owner.id})
        assert rep.sent == 0
        assert any("holds this user's lock" in n for n in rep.notes)
