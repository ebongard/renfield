"""Contact points on KG entities (#1240), stage 1.

Real Postgres. Only fictitious numbers (…1234567) -- this repository is public.

Covers: normalization; storing + live uniqueness; the two-filter read (a value is
never more visible than its entity OR its own source tier); the tier rule on every
entity tier change (with and without an atom), on a merge and on a document tier
change; the soft delete; the routes (flag gate, ownership-gated writes); the agent
tool; and a source guard that pairs every edge recompute with a contact-point one.
"""
from __future__ import annotations

import re
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from models.database import (
    Atom,
    Document,
    KGEntity,
    KGEntityContactPoint,
    KnowledgeBase,
    Role,
    User,
)
from services.atom_service import AtomService
from services.contact_point_tier import contact_point_tier
from services.contact_points import ContactPointError, ContactPointService, normalize_phone
from services.knowledge_graph_service import KnowledgeGraphService

MOBILE = "0170 1234567"
MOBILE_E164 = "+491701234567"
LANDLINE = "030 1234567"
LANDLINE_E164 = "+49301234567"

_seq = 0


def _next() -> int:
    global _seq
    _seq += 1
    return _seq


def _commit_as_flush(db, monkeypatch):
    monkeypatch.setattr(db, "commit", db.flush)


async def _no_embedding(_text):
    return None


async def _make_user(db: AsyncSession, name: str, permissions: list[str] | None = None) -> User:
    role = Role(name=f"{name}_role_{_next()}", permissions=permissions or ["kg.view", "kg.manage"])
    db.add(role)
    await db.flush()
    u = User(username=f"{name}{_next()}", email=f"{name}{_next()}@ex.test", password_hash="x",
             role_id=role.id, is_active=True)
    db.add(u)
    await db.flush()
    return u


async def _entity(db, owner, name, *, tier: int, with_atom: bool = False, etype="person") -> KGEntity:
    atom_id = None
    if with_atom:
        atom_id = f"00000000-0000-0000-1240-{_next():012d}"
        db.add(Atom(atom_id=atom_id, atom_type="kg_node", source_table="kg_entities",
                    source_id="0", owner_user_id=owner.id, policy={"tier": tier}))
        await db.flush()
    e = KGEntity(user_id=owner.id, name=name, entity_type=etype, circle_tier=tier, atom_id=atom_id)
    db.add(e)
    await db.flush()
    if with_atom:
        await db.execute(text("UPDATE atoms SET source_id = :s WHERE atom_id = :a"),
                         {"s": str(e.id), "a": atom_id})
    return e


async def _doc(db, owner, *, tier: int) -> Document:
    kb = KnowledgeBase(name=f"KB {_next()}", is_active=True, owner_id=owner.id)
    db.add(kb)
    await db.flush()
    atom_id = f"00000000-0000-0000-1241-{_next():012d}"
    db.add(Atom(atom_id=atom_id, atom_type="kb_document", source_table="documents",
                source_id="0", owner_user_id=owner.id, policy={"tier": tier}))
    await db.flush()
    doc = Document(filename="d.pdf", file_path="/x/d.pdf", status="completed",
                   chunk_count=0, knowledge_base_id=kb.id, circle_tier=tier, atom_id=atom_id)
    db.add(doc)
    await db.flush()
    await db.execute(text("UPDATE atoms SET source_id = :s WHERE atom_id = :a"),
                     {"s": str(doc.id), "a": atom_id})
    return doc


async def _tiers(db, point_id: int) -> tuple[int, int]:
    """(source_tier, circle_tier) of one contact point."""
    return tuple((await db.execute(text(
        "SELECT source_tier, circle_tier FROM kg_entity_contact_points WHERE id = :i"),
        {"i": point_id})).one())


@pytest.fixture
def enabled(monkeypatch):
    from utils.config import settings

    monkeypatch.setattr(settings, "contact_points_enabled", True)
    monkeypatch.setattr(settings, "contact_points_default_region", "DE")
    monkeypatch.setattr(settings, "auth_enabled", True)
    return settings


# --- pure ---------------------------------------------------------------------------------

class TestNormalize:
    pytestmark = [pytest.mark.unit]

    def test_national_and_international_notation_agree(self, enabled):
        assert normalize_phone(MOBILE) == (MOBILE_E164, "mobile")
        assert normalize_phone("+49 170 1234567") == (MOBILE_E164, "mobile")
        assert normalize_phone("0049 (170) 123-4567") == (MOBILE_E164, "mobile")
        assert normalize_phone(LANDLINE) == (LANDLINE_E164, None)

    def test_rejects_what_is_no_number(self, enabled):
        for bad in ("", "abc", "12", "0000", "x" * 80, "DE89370400440532013000"):
            assert normalize_phone(bad) is None

    def test_tier_rule(self):
        assert contact_point_tier(2, 4) == 2
        assert contact_point_tier(4, 0) == 0


# --- service ------------------------------------------------------------------------------

class TestStore:
    pytestmark = [pytest.mark.postgres, pytest.mark.asyncio]

    async def test_add_normalizes_and_detects_mobile(self, pg_db_session, monkeypatch, enabled):
        _commit_as_flush(pg_db_session, monkeypatch)
        owner = await _make_user(pg_db_session, "cp_add")
        hans = await _entity(pg_db_session, owner, "Hans Beispiel", tier=2)
        cp = await ContactPointService(pg_db_session).add(hans, kind="phone", value=MOBILE, actor_id=owner.id)
        assert (cp.kind, cp.normalized, cp.value, cp.source_type) == ("mobile", MOBILE_E164, MOBILE, "manual")
        assert (cp.user_id, cp.source_tier, cp.circle_tier) == (owner.id, 2, 2)

    async def test_own_lower_tier_is_kept_higher_is_capped(self, pg_db_session, monkeypatch, enabled):
        _commit_as_flush(pg_db_session, monkeypatch)
        owner = await _make_user(pg_db_session, "cp_tier")
        hans = await _entity(pg_db_session, owner, "Hans Tier", tier=2)
        svc = ContactPointService(pg_db_session)
        private = await svc.add(hans, kind="mobile", value=MOBILE, source_tier=0)
        public = await svc.add(hans, kind="phone", value=LANDLINE, source_tier=4)
        assert await _tiers(pg_db_session, private.id) == (0, 0)
        assert await _tiers(pg_db_session, public.id) == (4, 2)

    async def test_invalid_number_and_kind_refused(self, pg_db_session, monkeypatch, enabled):
        _commit_as_flush(pg_db_session, monkeypatch)
        owner = await _make_user(pg_db_session, "cp_bad")
        hans = await _entity(pg_db_session, owner, "Hans Bad", tier=2)
        svc = ContactPointService(pg_db_session)
        with pytest.raises(ContactPointError) as e:
            await svc.add(hans, kind="phone", value="Rechnung 4711")
        assert e.value.code == "invalid_number"
        with pytest.raises(ContactPointError) as e:
            await svc.add(hans, kind="pager", value=MOBILE)
        assert e.value.code == "invalid_kind"

    async def test_same_live_value_twice_is_a_duplicate(self, pg_db_session, enabled):
        owner = await _make_user(pg_db_session, "cp_dup")
        hans = await _entity(pg_db_session, owner, "Hans Dup", tier=2)
        await pg_db_session.commit()
        svc = ContactPointService(pg_db_session)
        await svc.add(hans, kind="mobile", value=MOBILE)
        with pytest.raises(ContactPointError) as e:
            await svc.add(hans, kind="mobile", value="+49 170 1234567")
        assert e.value.code == "duplicate"

    async def test_a_shared_number_on_two_entities_is_legitimate(self, pg_db_session, monkeypatch, enabled):
        """A switchboard / family landline: two entities, one number, NO merge (§7)."""
        _commit_as_flush(pg_db_session, monkeypatch)
        owner = await _make_user(pg_db_session, "cp_shared")
        a = await _entity(pg_db_session, owner, "Anna Shared", tier=2)
        b = await _entity(pg_db_session, owner, "Bernd Shared", tier=2)
        svc = ContactPointService(pg_db_session)
        await svc.add(a, kind="phone", value=LANDLINE)
        await svc.add(b, kind="phone", value=LANDLINE)
        hits = await svc.lookup_number(LANDLINE, owner.id)
        assert sorted(h.entity_name for h in hits) == ["Anna Shared", "Bernd Shared"]
        for e in (a, b):
            await pg_db_session.refresh(e)
            assert e.is_active and e.canonical_id is None


class TestVisibility:
    pytestmark = [pytest.mark.postgres, pytest.mark.asyncio]

    async def test_value_never_more_visible_than_its_source_or_entity(self, pg_db_session, monkeypatch, enabled):
        _commit_as_flush(pg_db_session, monkeypatch)
        owner = await _make_user(pg_db_session, "cp_vis_owner")
        stranger = await _make_user(pg_db_session, "cp_vis_stranger")
        firm = await _entity(pg_db_session, owner, "Praxis Beispiel", tier=4, etype="organization")
        private_person = await _entity(pg_db_session, owner, "Privat Person", tier=0)
        svc = ContactPointService(pg_db_session)
        public_line = await svc.add(firm, kind="phone", value=LANDLINE)
        await svc.add(firm, kind="mobile", value=MOBILE, source_tier=0)
        await svc.add(private_person, kind="mobile", value="0171 1234567", source_tier=4)

        # The stranger sees the public firm's public line -- not its private mobile,
        # and nothing of the private person even though that value claims "public".
        seen = await svc.list_for_entity(firm.id, stranger.id)
        assert [p.id for p in seen] == [public_line.id]
        assert await svc.list_for_entity(private_person.id, stranger.id) == []
        assert await svc.lookup_number("0171 1234567", stranger.id) == []
        assert await svc.find_for_name("praxis beispiel", stranger.id, kind="mobile") == []
        # The owner sees everything.
        assert len(await svc.list_for_entity(firm.id, owner.id)) == 2
        assert len(await svc.lookup_number("0171 1234567", owner.id)) == 1

    async def test_tombstone_and_ended_values_never_match(self, pg_db_session, monkeypatch, enabled):
        _commit_as_flush(pg_db_session, monkeypatch)
        owner = await _make_user(pg_db_session, "cp_tomb")
        a = await _entity(pg_db_session, owner, "Tomb A", tier=2)
        svc = ContactPointService(pg_db_session)
        cp = await svc.add(a, kind="mobile", value=MOBILE)
        await pg_db_session.execute(text(
            "UPDATE kg_entity_contact_points SET valid_to = now() WHERE id = :i"), {"i": cp.id})
        assert await svc.lookup_number(MOBILE, owner.id) == []

    async def test_alias_finds_the_entity(self, pg_db_session, monkeypatch, enabled):
        _commit_as_flush(pg_db_session, monkeypatch)
        owner = await _make_user(pg_db_session, "cp_alias")
        e = await _entity(pg_db_session, owner, "Johannes Beispiel", tier=2)
        e.surface_forms = ["Hannes"]
        await pg_db_session.flush()
        await ContactPointService(pg_db_session).add(e, kind="mobile", value=MOBILE)
        hits = await ContactPointService(pg_db_session).find_for_name("hannes", owner.id)
        assert [h.entity_name for h in hits] == ["Johannes Beispiel"]


class TestTierCascade:
    pytestmark = [pytest.mark.postgres, pytest.mark.asyncio]

    async def test_entity_tier_change_without_atom(self, pg_db_session, monkeypatch, enabled):
        _commit_as_flush(pg_db_session, monkeypatch)
        owner = await _make_user(pg_db_session, "cp_casc")
        e = await _entity(pg_db_session, owner, "Casc A", tier=2)
        cp = await ContactPointService(pg_db_session).add(e, kind="mobile", value=MOBILE)
        kg = KnowledgeGraphService(pg_db_session)
        await kg.update_entity_circle_tier(e.id, 0)
        assert await _tiers(pg_db_session, cp.id) == (2, 0)
        # Lifting the entity again restores the value's own reach, never beyond it.
        await kg.update_entity_circle_tier(e.id, 4)
        assert await _tiers(pg_db_session, cp.id) == (2, 2)

    async def test_entity_tier_change_through_the_atom(self, pg_db_session, monkeypatch, enabled):
        _commit_as_flush(pg_db_session, monkeypatch)
        owner = await _make_user(pg_db_session, "cp_casc_atom")
        e = await _entity(pg_db_session, owner, "Casc B", tier=2, with_atom=True)
        cp = await ContactPointService(pg_db_session).add(e, kind="mobile", value=MOBILE)
        await AtomService(pg_db_session).update_tier(e.atom_id, {"tier": 1})
        assert await _tiers(pg_db_session, cp.id) == (2, 1)

    async def test_document_tier_lowers_its_values_raising_does_not(self, pg_db_session, monkeypatch, enabled):
        _commit_as_flush(pg_db_session, monkeypatch)
        owner = await _make_user(pg_db_session, "cp_doc")
        doc = await _doc(pg_db_session, owner, tier=2)
        firm = await _entity(pg_db_session, owner, "Stadtwerke Beispiel", tier=4, etype="organization")
        cp = await ContactPointService(pg_db_session).add(firm, kind="phone", value=LANDLINE, source_tier=2)
        cp.document_id = doc.id
        cp.source_type = "document"
        await pg_db_session.flush()
        atoms = AtomService(pg_db_session)
        await atoms.update_tier(doc.atom_id, {"tier": 0})
        assert await _tiers(pg_db_session, cp.id) == (0, 0)
        await atoms.update_tier(doc.atom_id, {"tier": 4})
        assert await _tiers(pg_db_session, cp.id) == (0, 0)

    async def test_deleting_the_document_takes_its_values(self, pg_db_session, monkeypatch, enabled):
        _commit_as_flush(pg_db_session, monkeypatch)
        owner = await _make_user(pg_db_session, "cp_docdel")
        doc = await _doc(pg_db_session, owner, tier=2)
        firm = await _entity(pg_db_session, owner, "Firma Del", tier=2, etype="organization")
        cp = await ContactPointService(pg_db_session).add(firm, kind="phone", value=LANDLINE)
        cp.document_id = doc.id
        await pg_db_session.flush()
        await pg_db_session.execute(text("DELETE FROM documents WHERE id = :d"), {"d": doc.id})
        assert (await pg_db_session.execute(text(
            "SELECT count(*) FROM kg_entity_contact_points WHERE id = :i"), {"i": cp.id})).scalar() == 0


class TestMerge:
    pytestmark = [pytest.mark.postgres, pytest.mark.asyncio]

    async def test_merge_moves_values_and_folds_collisions_at_the_lower_tier(
        self, pg_db_session, monkeypatch, enabled,
    ):
        _commit_as_flush(pg_db_session, monkeypatch)
        owner = await _make_user(pg_db_session, "cp_merge")
        winner = await _entity(pg_db_session, owner, "Hans Merge", tier=2)
        loser = await _entity(pg_db_session, owner, "Hans Merge GmbH", tier=2)
        svc = ContactPointService(pg_db_session)
        w_mobile = await svc.add(winner, kind="mobile", value=MOBILE, source_tier=2)
        l_mobile = await svc.add(loser, kind="mobile", value=MOBILE, source_tier=0)
        l_land = await svc.add(loser, kind="phone", value=LANDLINE)
        kg = KnowledgeGraphService(pg_db_session)
        monkeypatch.setattr(kg, "_get_embedding", _no_embedding)
        assert await kg.merge_entities(loser_id=loser.id, winner_id=winner.id) is not None

        rows = {
            r.id: r for r in (await pg_db_session.execute(
                select(KGEntityContactPoint).execution_options(populate_existing=True)
                .where(KGEntityContactPoint.id.in_([w_mobile.id, l_mobile.id, l_land.id]))
            )).scalars()
        }
        assert all(r.entity_id == winner.id for r in rows.values())
        # The collision: one live row, at the LOWER source tier; the other ended.
        assert rows[w_mobile.id].valid_to is None
        assert (rows[w_mobile.id].source_tier, rows[w_mobile.id].circle_tier) == (0, 0)
        assert rows[l_mobile.id].valid_to is not None
        assert rows[l_mobile.id].superseded_by_id == w_mobile.id
        assert rows[l_land.id].valid_to is None
        assert len(await svc.lookup_number(MOBILE, owner.id)) == 1


class TestSoftDelete:
    pytestmark = [pytest.mark.postgres, pytest.mark.asyncio]

    async def test_soft_deleted_entity_loses_its_values(self, pg_db_session, monkeypatch, enabled):
        _commit_as_flush(pg_db_session, monkeypatch)
        owner = await _make_user(pg_db_session, "cp_softdel")
        e = await _entity(pg_db_session, owner, "Weg Person", tier=2)
        await ContactPointService(pg_db_session).add(e, kind="mobile", value=MOBILE)
        assert await KnowledgeGraphService(pg_db_session).delete_entity(e.id)
        assert (await pg_db_session.execute(text(
            "SELECT count(*) FROM kg_entity_contact_points WHERE entity_id = :e"), {"e": e.id})).scalar() == 0


# --- routes -------------------------------------------------------------------------------

@pytest.fixture
async def client_as(pg_db_session: AsyncSession, enabled):
    from slowapi.errors import RateLimitExceeded

    from api.routes import knowledge_graph as route
    from services.api_rate_limiter import limiter, rate_limit_exceeded_handler
    from services.auth_service import get_current_user
    from services.database import get_db

    @asynccontextmanager
    async def make(user: User | None):
        app = FastAPI()
        app.state.limiter = limiter
        app.add_exception_handler(RateLimitExceeded, rate_limit_exceeded_handler)
        app.include_router(route.router, prefix="/api/knowledge-graph")

        async def _db():
            yield pg_db_session

        loaded = None
        if user is not None:
            loaded = (await pg_db_session.execute(
                select(User).options(selectinload(User.role)).where(User.id == user.id))).scalar_one()
        app.dependency_overrides[get_db] = _db
        app.dependency_overrides[get_current_user] = lambda: loaded
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            yield c

    return make


class TestRoutes:
    pytestmark = [pytest.mark.postgres, pytest.mark.asyncio]

    async def test_owner_crud_and_lookup(self, pg_db_session, monkeypatch, client_as):
        _commit_as_flush(pg_db_session, monkeypatch)
        owner = await _make_user(pg_db_session, "cp_route")
        e = await _entity(pg_db_session, owner, "Route Person", tier=2)
        base = f"/api/knowledge-graph/entities/{e.id}/contact-points"
        async with client_as(owner) as c:
            created = await c.post(base, json={"kind": "phone", "value": MOBILE, "label": "private"})
            assert created.status_code == 201, created.text
            body = created.json()
            assert (body["kind"], body["normalized"], body["label"]) == ("mobile", MOBILE_E164, "private")
            dup = await c.post(base, json={"kind": "mobile", "value": MOBILE})
            assert (dup.status_code, dup.json()["detail"]) == (409, "duplicate")
            bad = await c.post(base, json={"kind": "phone", "value": "keine Nummer"})
            assert (bad.status_code, bad.json()["detail"]) == (422, "invalid_number")
            listed = await c.get(base)
            assert [p["id"] for p in listed.json()["contact_points"]] == [body["id"]]
            hit = await c.get("/api/knowledge-graph/contact-points/lookup", params={"number": "+491701234567"})
            assert [h["entity_name"] for h in hit.json()["hits"]] == ["Route Person"]
            patched = await c.patch(f"/api/knowledge-graph/contact-points/{body['id']}",
                                    json={"label": "work", "circle_tier": 0})
            assert (patched.json()["label"], patched.json()["circle_tier"]) == ("work", 0)
            gone = await c.delete(f"/api/knowledge-graph/contact-points/{body['id']}")
            assert gone.status_code == 200
            assert (await c.get(base)).json()["contact_points"] == []

    async def test_non_owner_cannot_write_or_see_private(self, pg_db_session, monkeypatch, client_as):
        _commit_as_flush(pg_db_session, monkeypatch)
        owner = await _make_user(pg_db_session, "cp_r_owner")
        other = await _make_user(pg_db_session, "cp_r_other")
        public = await _entity(pg_db_session, owner, "Oeffentliche Firma", tier=4, etype="organization")
        private = await _entity(pg_db_session, owner, "Private Person", tier=0)
        cp = await ContactPointService(pg_db_session).add(public, kind="phone", value=LANDLINE)
        async with client_as(other) as c:
            # Sees the public value, cannot change it or add one.
            assert len((await c.get(f"/api/knowledge-graph/entities/{public.id}/contact-points"))
                       .json()["contact_points"]) == 1
            assert (await c.post(f"/api/knowledge-graph/entities/{public.id}/contact-points",
                                 json={"kind": "mobile", "value": MOBILE})).status_code == 403
            assert (await c.delete(f"/api/knowledge-graph/contact-points/{cp.id}")).status_code == 403
            # The private entity does not exist for him.
            assert (await c.get(f"/api/knowledge-graph/entities/{private.id}/contact-points")).status_code == 404

    async def test_anonymous_refused(self, client_as):
        async with client_as(None) as c:
            assert (await c.get("/api/knowledge-graph/contact-points/lookup",
                                params={"number": MOBILE})).status_code in (401, 403)

    async def test_everything_404_when_off(self, pg_db_session, monkeypatch, client_as, enabled):
        _commit_as_flush(pg_db_session, monkeypatch)
        owner = await _make_user(pg_db_session, "cp_off")
        e = await _entity(pg_db_session, owner, "Off Person", tier=2)
        enabled.contact_points_enabled = False
        async with client_as(owner) as c:
            assert (await c.get(f"/api/knowledge-graph/entities/{e.id}/contact-points")).status_code == 404
            assert (await c.post(f"/api/knowledge-graph/entities/{e.id}/contact-points",
                                 json={"kind": "mobile", "value": MOBILE})).status_code == 404
            assert (await c.get("/api/knowledge-graph/contact-points/lookup",
                                params={"number": MOBILE})).status_code == 404


# --- agent tool ---------------------------------------------------------------------------

class TestTool:

    @pytest.mark.asyncio
    async def test_refuses_when_off(self, enabled):
        from services.contact_point_tool import find_contact

        enabled.contact_points_enabled = False
        out = await find_contact({"name": "Hans"}, user_id=1)
        assert out["data"]["enabled"] is False

    @pytest.mark.postgres
    @pytest.mark.asyncio
    async def test_finds_by_name_and_number_for_the_asker_only(self, pg_db_session, monkeypatch, enabled):
        import services.contact_point_tool as tool

        owner = await _make_user(pg_db_session, "cp_tool")
        stranger = await _make_user(pg_db_session, "cp_tool_x")
        e = await _entity(pg_db_session, owner, "Tool Person", tier=0)
        await pg_db_session.commit()
        await ContactPointService(pg_db_session).add(e, kind="mobile", value=MOBILE)

        @asynccontextmanager
        async def _session():
            yield pg_db_session

        monkeypatch.setattr(tool, "AsyncSessionLocal", _session)
        by_name = await tool.find_contact({"name": "tool person"}, user_id=owner.id)
        assert [c["normalized"] for c in by_name["data"]["contacts"]] == [MOBILE_E164]
        by_number = await tool.find_contact({"number": "+49 170 1234567"}, user_id=owner.id)
        assert [c["name"] for c in by_number["data"]["contacts"]] == ["Tool Person"]
        # A user id in the LLM's arguments changes nothing: only the injected one counts.
        hidden = await tool.find_contact({"name": "tool person", "user_id": owner.id}, user_id=stranger.id)
        assert hidden["data"]["contacts"] == []

    def test_tool_advertised_only_when_enabled(self, enabled):
        from services.agent_tools import AgentToolRegistry

        registry = AgentToolRegistry.__new__(AgentToolRegistry)
        registry._tools = {}
        registry._register_internal_tools()
        assert "internal.find_contact" in registry._tools
        enabled.contact_points_enabled = False
        registry._tools = {}
        registry._register_internal_tools()
        assert "internal.find_contact" not in registry._tools


# --- source guard -------------------------------------------------------------------------

@pytest.mark.unit
def test_every_edge_recompute_also_recomputes_contact_points():
    """An entity tier change that recomputes its edges must recompute its values too."""
    backend = Path(__file__).resolve().parents[2] / "src" / "backend"
    offenders = []
    for path in backend.rglob("*.py"):
        if path.name == "kg_edge_tier.py":
            continue
        source = path.read_text(encoding="utf-8")
        edge_calls = len(re.findall(r"await recompute_incident_edge_tiers\(", source))
        cp_calls = len(re.findall(r"await recompute_entity_contact_point_tiers\(", source))
        if edge_calls != cp_calls:
            offenders.append(f"{path.relative_to(backend)}: {edge_calls} edge vs {cp_calls} contact-point")
    assert offenders == []
