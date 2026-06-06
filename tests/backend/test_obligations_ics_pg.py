"""Obligations .ics export — circle-filtered iCalendar of dated obligations.

Real PG: the export reuses DocumentFactRetrieval.obligations() (circle filter +
horizon), so it must run against Postgres. Asserts a valid VCALENDAR with one
all-day VEVENT per visible obligation, and that a non-owner peer's private
obligation does not leak into another user's export.
"""
from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from models.database import Atom, Document, DocumentFact, Role, User
from utils.config import settings

pytestmark = [pytest.mark.postgres, pytest.mark.asyncio]

_seq = 0
TODAY = dt.date(2026, 6, 6)


async def _make_user(db: AsyncSession, name: str) -> User:
    role = Role(name=f"{name}_role")
    db.add(role)
    await db.flush()
    u = User(username=name, email=f"{name}@ex.test", password_hash="x",
             role_id=role.id, is_active=True)
    db.add(u)
    await db.flush()
    return u


async def _mk_obligation(db, owner, *, ob_date, kind="zahlung", legal=False, tier=0,
                         amount=None, currency=None) -> int:
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
    fact = DocumentFact(document_id=doc.id, category="obligation", kind=kind, value=f"{kind} fällig",
                        obligation_date=ob_date, legal_gate=legal, amount_value=amount,
                        amount_currency=currency, source="llm", atom_id=fact_atom, circle_tier=tier)
    db.add(fact)
    await db.flush()
    return fact.id


def _commit_as_flush(db, monkeypatch):
    monkeypatch.setattr(db, "commit", db.flush)


class TestObligationsIcs:
    async def test_exports_vevents_for_visible_obligations(self, pg_db_session, monkeypatch):
        monkeypatch.setattr(settings, "auth_enabled", True)
        _commit_as_flush(pg_db_session, monkeypatch)
        from api.routes.atoms import export_obligations_ics
        owner = await _make_user(pg_db_session, "ics_owner")
        await _mk_obligation(pg_db_session, owner, ob_date=dt.date(2026, 6, 15),
                             kind="miete", amount=890, currency="EUR")
        await _mk_obligation(pg_db_session, owner, ob_date=dt.date(2026, 6, 20),
                             kind="widerspruch", legal=True)
        resp = await export_obligations_ics(due_before=None, db=pg_db_session, current_user=owner)
        body = resp.body.decode("utf-8")
        assert resp.media_type.startswith("text/calendar")
        assert 'attachment; filename="fristen.ics"' in resp.headers["Content-Disposition"]
        assert body.startswith("BEGIN:VCALENDAR")
        assert body.strip().endswith("END:VCALENDAR")
        assert body.count("BEGIN:VEVENT") == 2
        assert "DTSTART;VALUE=DATE:20260615" in body
        assert "SUMMARY:Frist: miete (890.0 EUR)" in body
        # legal-gate flagged + all-day on the printed date
        assert "DTSTART;VALUE=DATE:20260620" in body
        assert "widerspruch" in body
        # each event carries a stable UID
        assert body.count("UID:obligation-") == 2

    async def test_peer_private_obligation_not_in_export(self, pg_db_session, monkeypatch):
        monkeypatch.setattr(settings, "auth_enabled", True)
        _commit_as_flush(pg_db_session, monkeypatch)
        from api.routes.atoms import export_obligations_ics
        owner = await _make_user(pg_db_session, "ics_a")
        peer = await _make_user(pg_db_session, "ics_b")
        await _mk_obligation(pg_db_session, owner, ob_date=dt.date(2026, 6, 15), kind="geheim", tier=0)
        resp = await export_obligations_ics(due_before=None, db=pg_db_session, current_user=peer)
        body = resp.body.decode("utf-8")
        assert "geheim" not in body
        assert body.count("BEGIN:VEVENT") == 0  # peer sees none of the owner's self-tier obligations

    async def test_crlf_in_value_cannot_inject_ics_lines(self, pg_db_session, monkeypatch):
        # A crafted obligation value with CRLF must NOT forge calendar lines.
        monkeypatch.setattr(settings, "auth_enabled", True)
        _commit_as_flush(pg_db_session, monkeypatch)
        from api.routes.atoms import export_obligations_ics
        owner = await _make_user(pg_db_session, "ics_inj")
        fid = await _mk_obligation(pg_db_session, owner, ob_date=dt.date(2026, 6, 15), kind="zahlung")
        # overwrite value with an injection payload
        await pg_db_session.execute(
            text("UPDATE document_facts SET value = :v WHERE id = :id"),
            {"v": "evil\r\nEND:VEVENT\r\nBEGIN:VEVENT\r\nSUMMARY:Injected", "id": fid})
        await pg_db_session.flush()
        resp = await export_obligations_ics(due_before=None, db=pg_db_session, current_user=owner)
        body = resp.body.decode("utf-8")
        # Assert on real content-lines (CRLF-split), not substrings — the payload
        # survives as escaped text INSIDE the DESCRIPTION value, which is the point.
        lines = body.split("\r\n")
        assert lines.count("BEGIN:VEVENT") == 1   # no forged event line
        assert lines.count("END:VEVENT") == 1
        assert "SUMMARY:Injected" not in lines     # not promoted to a real content-line
        desc = next(line for line in lines if line.startswith("DESCRIPTION:"))
        assert "\\nEND:VEVENT" in desc             # CRLF collapsed to the literal \n escape

    async def test_empty_export_is_valid_calendar(self, pg_db_session, monkeypatch):
        monkeypatch.setattr(settings, "auth_enabled", True)
        _commit_as_flush(pg_db_session, monkeypatch)
        from api.routes.atoms import export_obligations_ics
        owner = await _make_user(pg_db_session, "ics_empty")
        resp = await export_obligations_ics(due_before=None, db=pg_db_session, current_user=owner)
        body = resp.body.decode("utf-8")
        assert "BEGIN:VCALENDAR" in body and "END:VCALENDAR" in body
        assert body.count("BEGIN:VEVENT") == 0
