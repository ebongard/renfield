"""Per-fact tier override — the doc→facts cascade preserves explicit overrides.

Within one document a public issuer can coexist with private content. A fact's
tier is set independently (tier_overridden=True) and the parent-document tier
cascade (AtomService.update_tier) must NOT stomp it — sticky in BOTH directions.
reset_fact_tier clears the override back to the document tier. Real PG.
"""
from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from models.database import Atom, Document, DocumentFact, Role, User
from services.atom_service import AtomService
from utils.config import settings

pytestmark = [pytest.mark.postgres, pytest.mark.asyncio]

_seq = 0


async def _make_user(db: AsyncSession, name: str) -> User:
    role = Role(name=f"{name}_role")
    db.add(role)
    await db.flush()
    u = User(username=name, email=f"{name}@ex.test", password_hash="x",
             role_id=role.id, is_active=True)
    db.add(u)
    await db.flush()
    return u


async def _make_doc(db: AsyncSession, owner: User, *, tier: int) -> tuple[Document, str]:
    """Document + its kb_document atom (the re-tier handle)."""
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
    # finalize the doc atom source_id to the real id (the re-tier cascade keys on it)
    await db.execute(text("UPDATE atoms SET source_id = :sid WHERE atom_id = :aid"),
                     {"sid": str(doc.id), "aid": doc_atom})
    return doc, doc_atom


async def _make_fact(db, owner, doc, *, kind: str, tier: int) -> tuple[int, str]:
    global _seq
    _seq += 1
    fact_atom = f"00000000-0000-0000-0000-{_seq:012d}"
    db.add(Atom(atom_id=fact_atom, atom_type="document_fact", source_table="document_facts",
                source_id=f"fact-{_seq}", owner_user_id=owner.id, policy={"tier": tier}))
    await db.flush()
    fact = DocumentFact(document_id=doc.id, category="universal", kind=kind, value=kind,
                        source="llm", atom_id=fact_atom, circle_tier=tier)
    db.add(fact)
    await db.flush()
    await db.execute(text("UPDATE atoms SET source_id = :sid WHERE atom_id = :aid"),
                     {"sid": str(fact.id), "aid": fact_atom})
    return fact.id, fact_atom


async def _fact(db, fact_id) -> tuple[int, bool]:
    r = (await db.execute(
        text("SELECT circle_tier, tier_overridden FROM document_facts WHERE id = :id"),
        {"id": fact_id})).first()
    return int(r[0]), bool(r[1])


def _commit_as_flush(db, monkeypatch):
    monkeypatch.setattr(db, "commit", db.flush)


class TestPerFactTierOverride:
    async def test_patch_fact_sets_overridden(self, pg_db_session, monkeypatch):
        _commit_as_flush(pg_db_session, monkeypatch)
        owner = await _make_user(pg_db_session, "ov_patch")
        doc, _ = await _make_doc(pg_db_session, owner, tier=0)
        fid, fatom = await _make_fact(pg_db_session, owner, doc, kind="issuer", tier=0)
        await AtomService(pg_db_session).update_tier(fatom, {"tier": 4})
        tier, overridden = await _fact(pg_db_session, fid)
        assert tier == 4 and overridden is True

    async def test_doc_retier_preserves_override_both_directions(self, pg_db_session, monkeypatch):
        _commit_as_flush(pg_db_session, monkeypatch)
        owner = await _make_user(pg_db_session, "ov_sticky")
        doc, doc_atom = await _make_doc(pg_db_session, owner, tier=0)
        issuer_id, issuer_atom = await _make_fact(pg_db_session, owner, doc, kind="issuer", tier=0)
        content_id, _ = await _make_fact(pg_db_session, owner, doc, kind="amount", tier=0)
        svc = AtomService(pg_db_session)
        # issuer → public override
        await svc.update_tier(issuer_atom, {"tier": 4})
        # re-tier the DOCUMENT to household (2): content follows, issuer sticks public
        await svc.update_tier(doc_atom, {"tier": 2})
        assert await _fact(pg_db_session, content_id) == (2, False)
        assert await _fact(pg_db_session, issuer_id) == (4, True)
        # re-tier the DOCUMENT to self (0, more private): issuer STAYS public (sticky both ways)
        await svc.update_tier(doc_atom, {"tier": 0})
        assert await _fact(pg_db_session, content_id) == (0, False)
        assert await _fact(pg_db_session, issuer_id) == (4, True)

    async def test_override_atom_policy_synced_and_survives_cascade(self, pg_db_session, monkeypatch):
        _commit_as_flush(pg_db_session, monkeypatch)
        owner = await _make_user(pg_db_session, "ov_policy")
        doc, doc_atom = await _make_doc(pg_db_session, owner, tier=0)
        issuer_id, issuer_atom = await _make_fact(pg_db_session, owner, doc, kind="issuer", tier=0)
        svc = AtomService(pg_db_session)
        await svc.update_tier(issuer_atom, {"tier": 4})
        await svc.update_tier(doc_atom, {"tier": 1})
        # the overridden fact's atom policy must NOT have been stomped to the doc tier
        pol = (await pg_db_session.execute(
            text("SELECT policy FROM atoms WHERE atom_id = :a"), {"a": issuer_atom})).scalar()
        assert int(pol["tier"]) == 4

    async def test_reset_clears_override_to_doc_tier(self, pg_db_session, monkeypatch):
        _commit_as_flush(pg_db_session, monkeypatch)
        owner = await _make_user(pg_db_session, "ov_reset")
        doc, doc_atom = await _make_doc(pg_db_session, owner, tier=2)
        fid, fatom = await _make_fact(pg_db_session, owner, doc, kind="issuer", tier=2)
        svc = AtomService(pg_db_session)
        await svc.update_tier(fatom, {"tier": 4})
        assert await _fact(pg_db_session, fid) == (4, True)
        new_tier = await svc.reset_fact_tier(fid)
        assert new_tier == 2
        assert await _fact(pg_db_session, fid) == (2, False)
        pol = (await pg_db_session.execute(
            text("SELECT policy FROM atoms WHERE atom_id = :a"), {"a": fatom})).scalar()
        assert int(pol["tier"]) == 2

    async def test_non_overridden_fact_follows_doc(self, pg_db_session, monkeypatch):
        _commit_as_flush(pg_db_session, monkeypatch)
        owner = await _make_user(pg_db_session, "ov_follow")
        doc, doc_atom = await _make_doc(pg_db_session, owner, tier=0)
        fid, _ = await _make_fact(pg_db_session, owner, doc, kind="amount", tier=0)
        await AtomService(pg_db_session).update_tier(doc_atom, {"tier": 3})
        assert await _fact(pg_db_session, fid) == (3, False)

    async def test_reset_missing_fact_returns_none(self, pg_db_session, monkeypatch):
        _commit_as_flush(pg_db_session, monkeypatch)
        assert await AtomService(pg_db_session).reset_fact_tier(999999) is None


class TestResetRoute:
    async def test_reset_route_owner_and_404(self, pg_db_session, monkeypatch):
        monkeypatch.setattr(settings, "auth_enabled", True)
        _commit_as_flush(pg_db_session, monkeypatch)
        from fastapi import HTTPException
        from api.routes.atoms import reset_fact_tier as reset_route
        owner = await _make_user(pg_db_session, "rr_owner")
        peer = await _make_user(pg_db_session, "rr_peer")
        doc, _ = await _make_doc(pg_db_session, owner, tier=1)
        fid, fatom = await _make_fact(pg_db_session, owner, doc, kind="issuer", tier=1)
        await AtomService(pg_db_session).update_tier(fatom, {"tier": 4})
        # non-owner → 404
        with pytest.raises(HTTPException) as ei:
            await reset_route(fid, db=pg_db_session, current_user=peer)
        assert ei.value.status_code == 404
        assert (await _fact(pg_db_session, fid)) == (4, True)  # unchanged
        # owner → resets to doc tier
        res = await reset_route(fid, db=pg_db_session, current_user=owner)
        assert res.tier_overridden is False and res.circle_tier == 1
        assert await _fact(pg_db_session, fid) == (1, False)
