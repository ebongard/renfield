"""Postgres-only tests for the KG endpoint-name circle leak fix.

`KGRetrieval._resolve_entity_names` is the shared circle gate that both
`get_relevant_context` (agent LLM context) and `get_relevant_atoms` (the
/wissen drawer) route relation-endpoint name lookups through. Before the fix
the endpoint fetch was unfiltered, so a relation the asker may see could
disclose the NAME of an endpoint entity in a circle they cannot reach. The
guarantee under test: an inaccessible endpoint id is simply absent from the
returned map (callers default it to "?"), never named.

Real PG via pg_db_session — the circle filter diverges on sqlite (json::int
cast), so this must run against Postgres to be a true signal.
"""
from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from models.database import TIER_PUBLIC, KGEntity, Role, User
from services.kg_retrieval import KGRetrieval
from utils.config import settings

pytestmark = [pytest.mark.postgres, pytest.mark.asyncio]


async def _make_user(db: AsyncSession, name: str) -> User:
    role = Role(name=f"{name}_role")
    db.add(role)
    await db.flush()
    u = User(username=name, email=f"{name}@ex.test", password_hash="x", role_id=role.id, is_active=True)
    db.add(u)
    await db.flush()
    return u


async def _entity(db, owner, name, *, tier=0, etype="person") -> KGEntity:
    e = KGEntity(user_id=owner.id, name=name, entity_type=etype, circle_tier=tier, is_active=True)
    db.add(e)
    await db.flush()
    return e


class TestResolveEntityNames:
    async def test_owner_resolves_own_entity(self, pg_db_session, monkeypatch):
        monkeypatch.setattr(settings, "auth_enabled", True)
        owner = await _make_user(pg_db_session, "kgn_owner")
        e = await _entity(pg_db_session, owner, "Mine", tier=0)
        out = await KGRetrieval(pg_db_session)._resolve_entity_names([e.id], owner.id)
        assert out == {e.id: "Mine"}

    async def test_other_users_self_tier_entity_is_not_named(self, pg_db_session, monkeypatch):
        # CRITICAL: the leak. B's self-tier (tier 0) entity must NOT resolve for A.
        monkeypatch.setattr(settings, "auth_enabled", True)
        a_user = await _make_user(pg_db_session, "kgn_a")
        b_user = await _make_user(pg_db_session, "kgn_b")
        secret = await _entity(pg_db_session, b_user, "Secret", tier=0)
        out = await KGRetrieval(pg_db_session)._resolve_entity_names([secret.id], a_user.id)
        assert secret.id not in out  # → caller shows "?", name never disclosed

    async def test_other_users_public_entity_is_named(self, pg_db_session, monkeypatch):
        monkeypatch.setattr(settings, "auth_enabled", True)
        a_user = await _make_user(pg_db_session, "kgn_pa")
        b_user = await _make_user(pg_db_session, "kgn_pb")
        pub = await _entity(pg_db_session, b_user, "PublicOrg", tier=TIER_PUBLIC, etype="organization")
        out = await KGRetrieval(pg_db_session)._resolve_entity_names([pub.id], a_user.id)
        assert out == {pub.id: "PublicOrg"}

    async def test_mixed_set_only_accessible_ids_returned(self, pg_db_session, monkeypatch):
        monkeypatch.setattr(settings, "auth_enabled", True)
        a_user = await _make_user(pg_db_session, "kgn_ma")
        b_user = await _make_user(pg_db_session, "kgn_mb")
        mine = await _entity(pg_db_session, a_user, "Mine", tier=0)
        secret = await _entity(pg_db_session, b_user, "Secret", tier=0)
        out = await KGRetrieval(pg_db_session)._resolve_entity_names(
            [mine.id, secret.id], a_user.id
        )
        assert out == {mine.id: "Mine"}

    async def test_anonymous_asker_public_only(self, pg_db_session, monkeypatch):
        # asker_id=None (anonymous, auth on) → only public-tier names resolve.
        monkeypatch.setattr(settings, "auth_enabled", True)
        owner = await _make_user(pg_db_session, "kgn_anon")
        pub = await _entity(pg_db_session, owner, "PublicOrg", tier=TIER_PUBLIC, etype="organization")
        priv = await _entity(pg_db_session, owner, "PrivatePerson", tier=0)
        out = await KGRetrieval(pg_db_session)._resolve_entity_names([pub.id, priv.id], None)
        assert out == {pub.id: "PublicOrg"}

    async def test_auth_disabled_resolves_everything(self, pg_db_session, monkeypatch):
        # Single-user mode: no circle filter, all names resolve.
        monkeypatch.setattr(settings, "auth_enabled", False)
        owner = await _make_user(pg_db_session, "kgn_auth_off")
        other = await _make_user(pg_db_session, "kgn_auth_off2")
        mine = await _entity(pg_db_session, owner, "Mine", tier=0)
        theirs = await _entity(pg_db_session, other, "Theirs", tier=0)
        out = await KGRetrieval(pg_db_session)._resolve_entity_names(
            [mine.id, theirs.id], owner.id
        )
        assert out == {mine.id: "Mine", theirs.id: "Theirs"}

    async def test_empty_ids_returns_empty(self, pg_db_session, monkeypatch):
        monkeypatch.setattr(settings, "auth_enabled", True)
        owner = await _make_user(pg_db_session, "kgn_empty")
        out = await KGRetrieval(pg_db_session)._resolve_entity_names([], owner.id)
        assert out == {}

    async def test_inactive_entity_is_not_named(self, pg_db_session, monkeypatch):
        # is_active=false rows must not resolve (mirrors the retrieval SQL guard).
        monkeypatch.setattr(settings, "auth_enabled", True)
        owner = await _make_user(pg_db_session, "kgn_inact")
        dead = await _entity(pg_db_session, owner, "Tombstone", tier=0)
        dead.is_active = False
        await pg_db_session.flush()
        out = await KGRetrieval(pg_db_session)._resolve_entity_names([dead.id], owner.id)
        assert dead.id not in out
