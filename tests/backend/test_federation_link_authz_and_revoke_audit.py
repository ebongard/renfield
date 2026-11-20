"""Who may create or remove a person link, and what a revocation leaves behind.

Two follow-ups from the review of #1548 ("every account can see the instance's
active pairings"), established at the code on 2026-11-14:

1. **Linking.** The self-service handshake was owner-bound all along (the
   offer route and `_peer_by_pubkey` both filter on `circle_owner_id`), but
   nothing proved it against two real accounts. The path that DID let an
   account which does not own a pairing create a link was the admin assertion,
   `POST /api/federation/user-links`: it took the linked account from the BODY.
   An `admin` holder could map a `querier_ref` to any local account on any
   active pairing, lending that account's full own reach to the far side
   without its consent -- and could read every other account's ref from the
   list. A person link also outlived the revocation of its pairing and came
   back to life when the pairing was revived.

2. **Audit.** Revoking a pairing or deleting a link as an `admin` left a log
   line and nothing durable.

Real Postgres with real, distinct accounts throughout: every assertion here is
about ownership BETWEEN accounts, and the foreign keys have to hold for it to
mean anything.
"""
from __future__ import annotations

import json
import secrets
import time
from datetime import UTC, datetime, timedelta

import pytest
from cryptography.hazmat.primitives.asymmetric import ed25519
from httpx import AsyncClient
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError

from models.database import FederationQueryLog, FederationUserLink, PeerUser, Role, User
from services.auth_service import get_current_user, get_user_or_default
from services.federation_audit import (
    BASIS_ADMIN,
    BASIS_OWNER,
    BASIS_SELF,
    LINK_REVOKED,
    PAIRING_REVIVED,
    PAIRING_REVOKED,
    REVOCATION_DIRECTION,
    prune_old_audit_rows,
)
from services.federation_identity import (
    FederationIdentity,
    canonical_bytes,
    get_federation_identity,
    init_federation_identity,
    reset_federation_identity_for_tests,
)
from services.federation_identity_link import resolve_linked_user
from utils.config import settings

pytestmark = [pytest.mark.backend, pytest.mark.database]

PEERS = "/api/federation/peers"
USER_LINKS = "/api/federation/user-links"
LINKS = "/api/federation/links"
AUDIT = "/api/federation/audit"

#: The far side's name for itself. It must never reach a revocation record.
FAR_NAME = "Far Instance Display Name"


async def _user(db, name: str, permissions: list[str], **kw) -> User:
    role = Role(name=f"role-{name}", permissions=permissions, is_system=False)
    db.add(role)
    await db.flush()
    user = User(username=name, password_hash="x", role_id=role.id, **kw)
    db.add(user)
    await db.commit()
    await db.refresh(user, attribute_names=["role"])
    return user


@pytest.fixture
def far_side() -> FederationIdentity:
    """The other instance, built alongside the singleton."""
    return FederationIdentity(ed25519.Ed25519PrivateKey.generate())


@pytest.fixture
def this_instance(tmp_path):
    reset_federation_identity_for_tests()
    init_federation_identity(tmp_path / "instance_key")
    yield get_federation_identity()
    reset_federation_identity_for_tests()


@pytest.fixture
async def fresh_pending_store():
    from services.federation_pending_store import reset_store_for_tests
    reset_store_for_tests()
    yield
    reset_store_for_tests()


@pytest.fixture
async def world(db_session, far_side):
    """owner: owns the one active pairing. member: a plain account that owns
    nothing. admin: holds ``admin`` and owns nothing either."""
    owner = await _user(db_session, "link-owner", ["chat.own"])
    member = await _user(db_session, "link-member", ["chat.own"])
    admin = await _user(db_session, "link-admin", ["admin"])
    assert len({owner.id, member.id, admin.id}) == 3

    pairing = PeerUser(
        circle_owner_id=owner.id, remote_pubkey=far_side.public_key_hex(),
        remote_display_name=FAR_NAME, remote_user_id=555,
        granted_tier=2, transport_config={},
        paired_at=datetime.now(UTC).replace(tzinfo=None) - timedelta(days=3),
    )
    db_session.add(pairing)
    await db_session.commit()
    return {
        "owner": owner, "member": member, "admin": admin, "pairing": pairing,
        "pairing_id": pairing.id,
    }


async def _reload(db, user: User) -> User:
    """Make an account usable again after a rollback expired it."""
    await db.refresh(user)
    await db.refresh(user, attribute_names=["role"])
    return user


@pytest.fixture
def as_user(app_with_test_db, monkeypatch):
    """Sign in as `user` with auth ON: both dependencies resolve to the same
    account, the way a real session does."""
    monkeypatch.setattr(settings, "auth_enabled", True)

    def _as(user: User) -> None:
        app_with_test_db.dependency_overrides[get_user_or_default] = lambda: user
        app_with_test_db.dependency_overrides[get_current_user] = lambda: user
    return _as


@pytest.fixture
def visible_to_all(monkeypatch):
    monkeypatch.setattr(settings, "federation_peers_visible_to_all", True)


@pytest.fixture
def purged(monkeypatch):
    """Records which pubkeys had their in-flight requests purged."""
    import services.federation_query_responder as responder
    seen: list[str] = []

    async def _purge(pubkey: str) -> int:
        seen.append(pubkey)
        return 0

    monkeypatch.setattr(responder, "purge_requests_for_pubkey", _purge)
    return seen


async def _link(db, pairing: PeerUser, user: User | None, ref: str | None = None) -> FederationUserLink:
    link = FederationUserLink(
        peer_id=pairing.id, querier_ref=ref or secrets.token_hex(32),
        local_user_id=user.id if user is not None else None,
    )
    db.add(link)
    await db.commit()
    return link


async def _links(db, pairing_id: int) -> list[FederationUserLink]:
    """By ID: a refused request rolls the shared session back, which expires
    every loaded object -- the pairing included."""
    return list((await db.execute(
        select(FederationUserLink)
        .where(FederationUserLink.peer_id == pairing_id)
        .order_by(FederationUserLink.id)
        .execution_options(populate_existing=True)
    )).scalars().all())


async def _revocations(db) -> list[FederationQueryLog]:
    return list((await db.execute(
        select(FederationQueryLog)
        .where(FederationQueryLog.direction == REVOCATION_DIRECTION)
        .order_by(FederationQueryLog.id)
    )).scalars().all())


def _signed_offer(far_side: FederationIdentity, this_instance: FederationIdentity) -> dict:
    """A genuine offer from the far side, naming this instance."""
    now = int(time.time())
    unsigned = {
        "version": 1,
        "initiator_pubkey": far_side.public_key_hex(),
        "responder_pubkey": this_instance.public_key_hex(),
        "stated_by": 4711,
        "querier_ref": secrets.token_hex(32),
        "nonce": secrets.token_hex(16),
        "issued_at": now,
        "expires_at": now + 600,
    }
    return {**unsigned, "signature": far_side.sign(canonical_bytes(unsigned)).hex()}


# ---------------------------------------------------------------------------
# 1a. The self-service handshake is bound to the pairing's owner
# ---------------------------------------------------------------------------


class TestHandshakeIsOwnerBound:
    async def test_an_account_that_does_not_own_the_pairing_gets_no_offer(
        self, async_client: AsyncClient, world, as_user, this_instance,
    ):
        as_user(world["member"])
        resp = await async_client.post(f"{LINKS}/offer", json={"peer_id": world["pairing"].id})
        assert resp.status_code == 404

        as_user(world["owner"])
        resp = await async_client.post(f"{LINKS}/offer", json={"peer_id": world["pairing"].id})
        assert resp.status_code == 200
        assert resp.json()["stated_by"] == world["owner"].id

    @pytest.mark.parametrize("who", ["member", "admin"])
    async def test_a_genuine_offer_cannot_be_accepted_by_anyone_but_the_owner(
        self, async_client: AsyncClient, db_session, world, as_user,
        this_instance, far_side, fresh_pending_store, who,
    ):
        """Not even an `admin` holder: accepting writes a link for the
        signed-in account on a pairing somebody else set up."""
        as_user(world[who])
        resp = await async_client.post(
            f"{LINKS}/accept", json={"offer": _signed_offer(far_side, this_instance)},
        )
        # The uniform refusal -- until 2026-11-14 this left as a 500, because
        # the route read `current_user.id` after the rollback.
        assert resp.status_code == 400
        assert resp.json() == {"detail": "Link handshake failed"}
        assert await _links(db_session, world["pairing_id"]) == []

    async def test_the_owner_accepts_and_is_the_one_linked(
        self, async_client: AsyncClient, db_session, world, as_user,
        this_instance, far_side, fresh_pending_store,
    ):
        as_user(world["owner"])
        offer = _signed_offer(far_side, this_instance)
        resp = await async_client.post(f"{LINKS}/accept", json={"offer": offer})
        assert resp.status_code == 200, resp.text
        (link,) = await _links(db_session, world["pairing_id"])
        assert link.local_user_id == world["owner"].id
        assert link.querier_ref == offer["querier_ref"]

    @pytest.mark.parametrize("who", ["member", "admin"])
    async def test_the_owners_offer_cannot_be_completed_by_another_account(
        self, async_client: AsyncClient, db_session, world, as_user,
        this_instance, far_side, fresh_pending_store, who,
    ):
        """Step 3 with a genuine confirmation from the far side: refused for
        everyone but the account that stated the offer -- as the uniform 400,
        not as a server error -- and completed for that account."""
        as_user(world["owner"])
        offer = (await async_client.post(
            f"{LINKS}/offer", json={"peer_id": world["pairing"].id},
        )).json()
        unsigned = {
            "version": 1,
            "nonce": offer["nonce"],
            "querier_ref": offer["querier_ref"],
            "responder_pubkey": far_side.public_key_hex(),
            "responder_user_id": 4711,
            "accepted_at": int(time.time()),
        }
        body = {"offer": offer, "confirmation": {
            **unsigned, "signature": far_side.sign(canonical_bytes(unsigned)).hex(),
        }}

        as_user(world[who])
        resp = await async_client.post(f"{LINKS}/complete", json=body)
        assert resp.status_code == 400
        assert resp.json() == {"detail": "Link handshake failed"}
        assert await _links(db_session, world["pairing_id"]) == []

        as_user(await _reload(db_session, world["owner"]))
        resp = await async_client.post(f"{LINKS}/complete", json=body)
        assert resp.status_code == 200, resp.text
        (link,) = await _links(db_session, world["pairing_id"])
        assert link.local_user_id == world["owner"].id


# ---------------------------------------------------------------------------
# 1b. The admin assertion links the SIGNED-IN account, nobody else
# ---------------------------------------------------------------------------


class TestAdminAssertionLinksOnlyTheSessionAccount:
    @pytest.mark.parametrize("victim", ["owner", "member"])
    async def test_an_admin_cannot_link_another_account(
        self, async_client: AsyncClient, db_session, world, as_user, victim,
    ):
        """The escalation: the far side, presenting the minted ref, would be
        served with the victim's full own reach."""
        as_user(world["admin"])
        resp = await async_client.post(USER_LINKS, json={
            "peer_id": world["pairing"].id, "local_user_id": world[victim].id,
        })
        assert resp.status_code == 403
        assert await _links(db_session, world["pairing_id"]) == []

    async def test_the_linked_account_comes_from_the_session(
        self, async_client: AsyncClient, db_session, world, as_user,
    ):
        as_user(world["admin"])
        resp = await async_client.post(USER_LINKS, json={"peer_id": world["pairing"].id})
        assert resp.status_code == 200, resp.text
        (link,) = await _links(db_session, world["pairing_id"])
        assert link.local_user_id == world["admin"].id
        assert link.created_by == world["admin"].id
        assert resp.json()["local_user_id"] == world["admin"].id
        # The caller's own ref is returned: it has to be mirrored on the far side.
        assert resp.json()["querier_ref"] == link.querier_ref

    async def test_naming_oneself_in_the_body_is_accepted(
        self, async_client: AsyncClient, db_session, world, as_user,
    ):
        as_user(world["admin"])
        resp = await async_client.post(USER_LINKS, json={
            "peer_id": world["pairing"].id, "local_user_id": world["admin"].id,
        })
        assert resp.status_code == 200
        (link,) = await _links(db_session, world["pairing_id"])
        assert link.local_user_id == world["admin"].id

    async def test_a_plain_account_is_refused_while_auth_is_on(
        self, async_client: AsyncClient, db_session, world, as_user,
    ):
        as_user(world["member"])
        assert (await async_client.post(
            USER_LINKS, json={"peer_id": world["pairing"].id},
        )).status_code == 403
        assert (await async_client.get(USER_LINKS)).status_code == 403
        link = await _link(db_session, world["pairing"], world["owner"])
        assert (await async_client.delete(f"{USER_LINKS}/{link.id}")).status_code == 403
        assert len(await _links(db_session, world["pairing_id"])) == 1

    async def test_a_revoked_pairing_cannot_be_linked(
        self, async_client: AsyncClient, db_session, world, as_user,
    ):
        world["pairing"].revoked_at = datetime.now(UTC).replace(tzinfo=None)
        await db_session.commit()
        as_user(world["admin"])
        resp = await async_client.post(USER_LINKS, json={"peer_id": world["pairing"].id})
        assert resp.status_code == 404

    async def test_a_ref_already_used_on_the_pairing_is_a_conflict(
        self, async_client: AsyncClient, db_session, world, as_user,
    ):
        taken = await _link(db_session, world["pairing"], world["owner"])
        as_user(world["admin"])
        resp = await async_client.post(USER_LINKS, json={
            "peer_id": world["pairing"].id, "querier_ref": taken.querier_ref,
        })
        assert resp.status_code == 409

    async def test_the_list_shows_a_ref_only_to_the_account_it_belongs_to(
        self, async_client: AsyncClient, db_session, world, as_user,
    ):
        """An admin who could read the owner's ref could delete that link,
        re-create it for itself and ask the far side as the owner."""
        theirs = await _link(db_session, world["pairing"], world["owner"])
        mine = await _link(db_session, world["pairing"], world["admin"])
        as_user(world["admin"])
        resp = await async_client.get(USER_LINKS, params={"peer_id": world["pairing"].id})
        assert resp.status_code == 200
        by_id = {row["id"]: row for row in resp.json()}
        assert by_id[theirs.id]["querier_ref"] is None
        assert by_id[theirs.id]["local_user_id"] == world["owner"].id
        assert by_id[mine.id]["querier_ref"] == mine.querier_ref
        assert theirs.querier_ref not in resp.text


# ---------------------------------------------------------------------------
# 1c. A link does not outlive its pairing, nor its account
# ---------------------------------------------------------------------------


class TestALinkEndsWithItsPairingAndItsAccount:
    async def test_revoking_the_pairing_removes_its_links(
        self, async_client: AsyncClient, db_session, world, as_user, purged,
    ):
        """The pairing row is kept and pairing again revives it under the same
        id. A link left behind would map again with it."""
        pairing = world["pairing"]
        link = await _link(db_session, pairing, world["owner"])
        ref, pairing_id = link.querier_ref, pairing.id
        await _link(db_session, pairing, world["admin"])

        as_user(world["owner"])
        assert (await async_client.delete(f"{PEERS}/{pairing_id}")).status_code == 204
        assert await _links(db_session, pairing_id) == []

        # Pairing again: `pairing_service` clears `revoked_at` on the same row.
        await db_session.refresh(pairing)
        pairing.revoked_at = None
        await db_session.commit()
        assert await resolve_linked_user(
            db_session, peer_id=pairing_id, querier_ref=ref,
        ) is None

    async def test_a_deactivated_account_is_not_served_through_its_link(
        self, db_session, world,
    ):
        link = await _link(db_session, world["pairing"], world["member"])
        resolved = await resolve_linked_user(
            db_session, peer_id=world["pairing"].id, querier_ref=link.querier_ref,
        )
        assert resolved == world["member"].id

        world["member"].is_active = False
        await db_session.commit()
        assert await resolve_linked_user(
            db_session, peer_id=world["pairing"].id, querier_ref=link.querier_ref,
        ) is None

    async def test_no_ref_and_an_unknown_ref_resolve_to_nobody(self, db_session, world):
        assert await resolve_linked_user(
            db_session, peer_id=world["pairing"].id, querier_ref=None,
        ) is None
        assert await resolve_linked_user(
            db_session, peer_id=world["pairing"].id, querier_ref="f" * 64,
        ) is None


# ---------------------------------------------------------------------------
# 2. A revocation leaves a durable record
# ---------------------------------------------------------------------------


def _assert_ids_only(row: FederationQueryLog) -> dict:
    """The record carries ids, a key and fixed words -- nothing the far side
    chose. Returns the decoded detail. (The instance-name snapshot is the
    VERIFIED name since #1558; the `world` pairing has none, so it is None
    here -- `TestTheRevocationView` covers a pairing that has one.)"""
    assert row.peer_display_name_snapshot == ""
    assert row.peer_instance_name_snapshot is None
    assert row.answer_excerpt is None
    assert row.error_message is None
    assert FAR_NAME not in row.query_text
    # Never filed under an account: the foreign key has no delete rule and the
    # row is kept for two years, so it would pin that account that long.
    assert row.user_id is None
    detail = json.loads(row.query_text)
    for key, value in detail.items():
        if key in ("action", "cause"):
            assert value in (PAIRING_REVOKED, LINK_REVOKED, PAIRING_REVIVED), (key, value)
        else:
            assert isinstance(value, (int, bool)), (key, value)
    return detail


class TestPairingRevocationIsRecorded:
    async def test_an_admin_revoking_someone_elses_pairing_is_on_record(
        self, async_client: AsyncClient, db_session, world, as_user,
        visible_to_all, purged,
    ):
        pairing, owner, admin = world["pairing"], world["owner"], world["admin"]
        pairing_id, pubkey = pairing.id, pairing.remote_pubkey
        await _link(db_session, pairing, owner)
        before = datetime.now(UTC).replace(tzinfo=None)

        as_user(admin)
        assert (await async_client.delete(f"{PEERS}/{pairing_id}")).status_code == 204

        link_row, row = await _revocations(db_session)
        assert link_row.final_status == LINK_REVOKED
        assert row.final_status == PAIRING_REVOKED
        assert row.identity_basis == BASIS_ADMIN
        assert row.peer_user_id == pairing_id
        assert row.peer_pubkey_snapshot == pubkey
        assert before <= row.initiated_at <= datetime.now(UTC).replace(tzinfo=None)
        assert _assert_ids_only(row) == {
            "action": PAIRING_REVOKED,
            "actor_user_id": admin.id,
            "as_admin": True,
            "owner_user_id": owner.id,
            "peer_id": pairing_id,
            "subject_user_id": owner.id,
            "links_removed": 1,
        }

    async def test_the_owner_revoking_is_recorded_as_the_owner(
        self, async_client: AsyncClient, db_session, world, as_user, purged,
    ):
        as_user(world["owner"])
        assert (await async_client.delete(f"{PEERS}/{world['pairing'].id}")).status_code == 204
        (row,) = await _revocations(db_session)
        assert row.identity_basis == BASIS_OWNER
        detail = _assert_ids_only(row)
        assert detail["actor_user_id"] == world["owner"].id
        assert detail["as_admin"] is False
        assert detail["links_removed"] == 0

    async def test_a_refused_revocation_leaves_no_record(
        self, async_client: AsyncClient, db_session, world, as_user,
        visible_to_all, purged,
    ):
        as_user(world["member"])
        assert (await async_client.delete(f"{PEERS}/{world['pairing'].id}")).status_code == 404
        assert await _revocations(db_session) == []
        await db_session.refresh(world["pairing"])
        assert world["pairing"].revoked_at is None

    async def test_revoking_twice_is_recorded_once(
        self, async_client: AsyncClient, db_session, world, as_user, purged,
    ):
        as_user(world["owner"])
        url = f"{PEERS}/{world['pairing'].id}"
        assert (await async_client.delete(url)).status_code == 204
        assert (await async_client.delete(url)).status_code == 204
        assert len(await _revocations(db_session)) == 1


class TestLinkRevocationIsRecorded:
    async def test_an_admin_deleting_someone_elses_link_is_on_record(
        self, async_client: AsyncClient, db_session, world, as_user, purged,
    ):
        pairing, owner, admin = world["pairing"], world["owner"], world["admin"]
        link = await _link(db_session, pairing, owner)
        link_id = link.id

        as_user(admin)
        resp = await async_client.delete(f"{USER_LINKS}/{link_id}")
        assert resp.status_code == 200
        assert resp.json() == {"deleted": link_id}
        assert await _links(db_session, world["pairing_id"]) == []

        (row,) = await _revocations(db_session)
        assert row.final_status == LINK_REVOKED
        assert row.identity_basis == BASIS_ADMIN
        assert row.peer_user_id == pairing.id
        assert _assert_ids_only(row) == {
            "action": LINK_REVOKED,
            "actor_user_id": admin.id,
            "as_admin": True,
            "owner_user_id": owner.id,
            "peer_id": pairing.id,
            "subject_user_id": owner.id,
            "link_id": link_id,
        }
        # Requests in flight are discarded, as on the self-service route.
        assert purged == [pairing.remote_pubkey]

    async def test_an_admin_deleting_its_own_link_did_not_act_as_admin(
        self, async_client: AsyncClient, db_session, world, as_user, purged,
    ):
        link = await _link(db_session, world["pairing"], world["admin"])
        as_user(world["admin"])
        assert (await async_client.delete(f"{USER_LINKS}/{link.id}")).status_code == 200
        (row,) = await _revocations(db_session)
        assert row.identity_basis == BASIS_SELF
        detail = _assert_ids_only(row)
        assert detail["subject_user_id"] == world["admin"].id
        assert detail["as_admin"] is False

    async def test_a_link_without_an_account_is_filed_under_the_pairing_owner(
        self, async_client: AsyncClient, db_session, world, as_user, purged,
    ):
        """`local_user_id` is NULL once the linked account was deleted."""
        link = await _link(db_session, world["pairing"], None)
        as_user(world["admin"])
        assert (await async_client.delete(f"{USER_LINKS}/{link.id}")).status_code == 200
        (row,) = await _revocations(db_session)
        assert _assert_ids_only(row)["subject_user_id"] == world["owner"].id
        assert row.identity_basis == BASIS_ADMIN

    async def test_an_unknown_link_is_a_404_and_no_record(
        self, async_client: AsyncClient, db_session, world, as_user, purged,
    ):
        as_user(world["admin"])
        assert (await async_client.delete(f"{USER_LINKS}/999999")).status_code == 404
        assert await _revocations(db_session) == []
        assert purged == []

    async def test_revoking_ones_own_link_is_recorded_as_self(
        self, async_client: AsyncClient, db_session, world, as_user, purged,
    ):
        link = await _link(db_session, world["pairing"], world["member"])
        link_id = link.id
        as_user(world["member"])
        assert (await async_client.delete(f"{LINKS}/{link_id}")).status_code == 204
        (row,) = await _revocations(db_session)
        assert row.final_status == LINK_REVOKED
        assert row.identity_basis == BASIS_SELF
        detail = _assert_ids_only(row)
        assert detail["subject_user_id"] == world["member"].id
        assert detail["actor_user_id"] == world["member"].id
        assert detail["link_id"] == link_id
        assert detail["as_admin"] is False

    async def test_someone_elses_link_cannot_be_revoked_on_the_self_service_route(
        self, async_client: AsyncClient, db_session, world, as_user, purged,
    ):
        """Not by a plain account and not by an `admin` holder either: this
        route knows only "mine". The admin's way is `/user-links`, on record."""
        link = await _link(db_session, world["pairing"], world["owner"])
        for who in ("member", "admin"):
            as_user(world[who])
            assert (await async_client.delete(f"{LINKS}/{link.id}")).status_code == 404
        assert len(await _links(db_session, world["pairing_id"])) == 1
        assert await _revocations(db_session) == []


class TestARevocationIsNotAQuery:
    async def _old_rows(self, db, world) -> None:
        old = datetime.now(UTC).replace(tzinfo=None) - timedelta(days=400)
        common = {
            "user_id": world["owner"].id, "peer_user_id": world["pairing"].id,
            "peer_pubkey_snapshot": world["pairing"].remote_pubkey,
            "initiated_at": old, "finalized_at": old, "verified_signature": False,
        }
        db.add_all([
            FederationQueryLog(
                direction="asked", final_status="success", query_text="a question",
                peer_display_name_snapshot="x", **common,
            ),
            FederationQueryLog(
                direction=REVOCATION_DIRECTION, final_status=PAIRING_REVOKED,
                identity_basis=BASIS_ADMIN, query_text="{}",
                peer_display_name_snapshot="", **{**common, "user_id": None},
            ),
        ])
        await db.commit()

    async def test_the_query_view_does_not_show_it_as_a_question(
        self, async_client: AsyncClient, db_session, world, as_user,
    ):
        await self._old_rows(db_session, world)
        as_user(world["owner"])
        resp = await async_client.get(AUDIT)
        assert resp.status_code == 200
        assert [e["direction"] for e in resp.json()["entries"]] == ["asked"]

    async def test_retention_prunes_queries_and_keeps_the_revocation(
        self, db_session, world, own_session_to_test_db,
    ):
        await self._old_rows(db_session, world)
        assert await prune_old_audit_rows() == 1
        remaining = (await db_session.execute(
            select(FederationQueryLog.direction, func.count())
            .group_by(FederationQueryLog.direction)
        )).all()
        assert remaining == [(REVOCATION_DIRECTION, 1)]


# ---------------------------------------------------------------------------
# 3. Review follow-ups (2026-11-14)
# ---------------------------------------------------------------------------


class TestARevocationDoesNotPinAnAccount:
    async def test_subject_and_actor_can_be_deleted_afterwards(
        self, async_client: AsyncClient, db_session, world, as_user, purged,
    ):
        """`federation_query_log.user_id` references `users.id` without a
        delete rule, and revocation rows are never pruned. Filed under the
        subject (or the actor), one revocation made that account undeletable
        for good."""
        member_id, admin_id = world["member"].id, world["admin"].id
        link = await _link(db_session, world["pairing"], world["member"])
        as_user(world["admin"])
        assert (await async_client.delete(f"{USER_LINKS}/{link.id}")).status_code == 200
        (row,) = await _revocations(db_session)
        detail = _assert_ids_only(row)
        assert (detail["subject_user_id"], detail["actor_user_id"]) == (member_id, admin_id)

        for user_id in (member_id, admin_id):       # the subject, then the actor
            await db_session.execute(delete(User).where(User.id == user_id))
            await db_session.commit()
        assert (await db_session.execute(
            select(func.count()).select_from(User).where(User.id.in_([member_id, admin_id]))
        )).scalar() == 0
        # The record outlives both, ids intact.
        (row,) = await _revocations(db_session)
        assert json.loads(row.query_text)["subject_user_id"] == member_id

    async def test_the_real_route_deletes_subject_and_actor_afterwards(
        self, async_client: AsyncClient, db_session, world, as_user, purged,
    ):
        """Through `DELETE /api/users/{id}`, which lets the database decide
        and answers 409 when a row still references the account.

        Nothing here looks at the record before the deletes: with the row
        filed under an account again, the DELETE itself has to be what fails.
        """
        manager = await _user(db_session, "link-manager", ["admin", "users.manage"])
        member_id, admin_id = world["member"].id, world["admin"].id
        link = await _link(db_session, world["pairing"], world["member"])
        as_user(world["admin"])
        assert (await async_client.delete(f"{USER_LINKS}/{link.id}")).status_code == 200

        as_user(manager)
        resp = await async_client.delete(f"/api/users/{member_id}")      # the subject
        assert resp.status_code == 200, resp.text
        resp = await async_client.delete(f"/api/users/{admin_id}")       # the actor
        assert resp.status_code == 200, resp.text
        assert (await db_session.execute(
            select(func.count()).select_from(User).where(User.id.in_([member_id, admin_id]))
        )).scalar() == 0
        (row,) = await _revocations(db_session)
        detail = json.loads(row.query_text)
        assert (detail["subject_user_id"], detail["actor_user_id"]) == (member_id, admin_id)

    async def test_a_query_row_without_an_account_is_refused_by_the_database(
        self, db_session, world,
    ):
        """Only a revocation record may go without `user_id`."""
        common = {
            "peer_user_id": world["pairing_id"],
            "peer_pubkey_snapshot": world["pairing"].remote_pubkey,
            "peer_display_name_snapshot": "", "query_text": "{}",
            "final_status": "success",
        }
        for direction in ("asked", "answered"):
            db_session.add(FederationQueryLog(direction=direction, user_id=None, **common))
            with pytest.raises(IntegrityError, match="ck_fed_query_log_user_or_revoke"):
                await db_session.commit()
            await db_session.rollback()
        db_session.add(FederationQueryLog(direction=REVOCATION_DIRECTION, user_id=None, **common))
        await db_session.commit()

    async def test_the_query_view_tolerates_rows_without_an_account(
        self, async_client: AsyncClient, db_session, world, as_user, purged,
    ):
        link = await _link(db_session, world["pairing"], world["owner"])
        as_user(world["owner"])
        assert (await async_client.delete(f"{LINKS}/{link.id}")).status_code == 204
        resp = await async_client.get(AUDIT)
        assert resp.status_code == 200
        assert resp.json()["entries"] == []


class TestEveryLinkRemovedWithAPairingIsOnRecord:
    async def test_one_record_per_removed_link_naming_whose_it_was(
        self, async_client: AsyncClient, db_session, world, as_user,
        visible_to_all, purged,
    ):
        """A count says nothing about WHOSE links went."""
        pairing, owner, member, admin = (
            world["pairing"], world["owner"], world["member"], world["admin"])
        mine = await _link(db_session, pairing, admin)
        theirs = await _link(db_session, pairing, member)
        owners = await _link(db_session, pairing, owner)
        expected = {
            mine.id: (admin.id, BASIS_SELF),
            theirs.id: (member.id, BASIS_ADMIN),
            owners.id: (owner.id, BASIS_ADMIN),
        }
        pairing_id, owner_id, admin_id = pairing.id, owner.id, admin.id

        as_user(admin)
        assert (await async_client.delete(f"{PEERS}/{pairing_id}")).status_code == 204

        rows = await _revocations(db_session)
        link_rows = [r for r in rows if r.final_status == LINK_REVOKED]
        (pairing_row,) = [r for r in rows if r.final_status == PAIRING_REVOKED]
        assert json.loads(pairing_row.query_text)["links_removed"] == 3
        seen = {}
        for row in link_rows:
            detail = _assert_ids_only(row)
            assert detail["cause"] == PAIRING_REVOKED
            assert detail["actor_user_id"] == admin_id
            assert detail["owner_user_id"] == owner_id
            assert detail["peer_id"] == pairing_id == row.peer_user_id
            assert detail["as_admin"] is (row.identity_basis == BASIS_ADMIN)
            seen[detail["link_id"]] = (detail["subject_user_id"], row.identity_basis)
        assert seen == expected

    async def test_the_owner_removing_anothers_link_is_recorded_as_owner(
        self, async_client: AsyncClient, db_session, world, as_user, purged,
    ):
        await _link(db_session, world["pairing"], world["member"])
        member_id = world["member"].id
        as_user(world["owner"])
        assert (await async_client.delete(f"{PEERS}/{world['pairing_id']}")).status_code == 204
        link_row, _pairing_row = await _revocations(db_session)
        assert link_row.identity_basis == BASIS_OWNER
        assert _assert_ids_only(link_row)["subject_user_id"] == member_id


class TestLinksOfAPairingRevokedEarlier:
    """A pairing revoked BEFORE the cleanup existed still carries its links."""

    async def _revoked_with_a_leftover_link(self, db, world) -> tuple[int, str]:
        pairing = world["pairing"]
        link = await _link(db, pairing, world["member"])
        pairing.revoked_at = datetime.now(UTC).replace(tzinfo=None) - timedelta(days=5)
        pairing.granted_tier = 4
        await db.commit()
        return link.id, link.querier_ref

    async def test_pairing_again_does_not_bring_the_old_link_back(
        self, db_session, world, this_instance,
    ):
        """`pairing_service` revives the row in place, same id. The far side
        still holds the old `querier_ref`."""
        from services.pairing_service import PairingService

        link_id, ref = await self._revoked_with_a_leftover_link(db_session, world)
        pairing_id, owner_id, member_id = (
            world["pairing_id"], world["owner"].id, world["member"].id)

        revived = await PairingService(db_session)._upsert_peer_user(
            owner_user_id=owner_id,
            remote_pubkey=world["pairing"].remote_pubkey,
            remote_display_name=FAR_NAME,
            remote_user_id=555,
            granted_tier=2,
            transport_config={},
        )
        assert revived.id == pairing_id
        assert revived.revoked_at is None

        assert await _links(db_session, pairing_id) == []
        assert await resolve_linked_user(
            db_session, peer_id=pairing_id, querier_ref=ref,
        ) is None
        (row,) = await _revocations(db_session)
        assert row.final_status == LINK_REVOKED
        assert row.identity_basis == BASIS_OWNER
        assert _assert_ids_only(row) == {
            "action": LINK_REVOKED,
            "cause": PAIRING_REVIVED,
            "actor_user_id": owner_id,
            "as_admin": False,
            "owner_user_id": owner_id,
            "peer_id": pairing_id,
            "subject_user_id": member_id,
            "link_id": link_id,
        }

    async def test_pairing_again_over_an_ACTIVE_pairing_keeps_its_links(
        self, db_session, world, this_instance,
    ):
        """Re-pairing a live pairing (a renewed certificate, a new endpoint)
        ends nobody's consent: the links were live a moment ago."""
        from services.pairing_service import PairingService

        await _link(db_session, world["pairing"], world["member"])
        await PairingService(db_session)._upsert_peer_user(
            owner_user_id=world["owner"].id,
            remote_pubkey=world["pairing"].remote_pubkey,
            remote_display_name=FAR_NAME,
            remote_user_id=555,
            granted_tier=3,
            transport_config={},
        )
        assert len(await _links(db_session, world["pairing_id"])) == 1
        assert await _revocations(db_session) == []

    async def test_revoking_it_again_clears_and_records_the_leftover(
        self, async_client: AsyncClient, db_session, world, as_user, purged,
    ):
        link_id, _ref = await self._revoked_with_a_leftover_link(db_session, world)
        as_user(world["owner"])
        assert (await async_client.delete(f"{PEERS}/{world['pairing_id']}")).status_code == 204

        assert await _links(db_session, world["pairing_id"]) == []
        # The link is on record; the pairing itself was not revoked a second time.
        (row,) = await _revocations(db_session)
        assert row.final_status == LINK_REVOKED
        detail = _assert_ids_only(row)
        assert (detail["link_id"], detail["cause"]) == (link_id, PAIRING_REVOKED)


# ---------------------------------------------------------------------------
# 4. The revocation view and its retention (#1558)
# ---------------------------------------------------------------------------

REVOCATIONS = "/api/federation/audit/revocations"


class TestTheRevocationView:
    """Who sees which record, through the real routes that write them."""

    async def _admin_removes_owners_link(self, async_client, db_session, world, as_user):
        link = await _link(db_session, world["pairing"], world["owner"])
        as_user(world["admin"])
        assert (await async_client.delete(f"{USER_LINKS}/{link.id}")).status_code == 200

    async def test_the_subject_and_the_actor_see_it_with_names(
        self, async_client: AsyncClient, db_session, world, as_user, purged,
    ):
        await _named(db_session, world)
        await self._admin_removes_owners_link(async_client, db_session, world, as_user)

        as_user(world["owner"])  # the subject
        (entry,) = (await async_client.get(REVOCATIONS)).json()["entries"]
        assert entry["action"] == LINK_REVOKED and entry["basis"] == BASIS_ADMIN
        assert entry["subject_is_you"] is True and entry["actor_is_you"] is False
        assert entry["subject_name"] == "Owner Person"
        assert entry["actor_name"] == "Admin Person"
        assert entry["owner_name"] == "Owner Person"

        as_user(world["admin"])  # the actor
        (entry,) = (await async_client.get(REVOCATIONS)).json()["entries"]
        assert entry["actor_is_you"] is True and entry["subject_is_you"] is False

    async def test_an_uninvolved_account_sees_none(
        self, async_client: AsyncClient, db_session, world, as_user, purged,
    ):
        await self._admin_removes_owners_link(async_client, db_session, world, as_user)
        as_user(world["member"])
        assert (await async_client.get(REVOCATIONS)).json()["entries"] == []

    async def test_an_admin_sees_records_it_had_no_part_in(
        self, async_client: AsyncClient, db_session, world, as_user, purged,
    ):
        link = await _link(db_session, world["pairing"], world["member"])
        as_user(world["member"])
        assert (await async_client.delete(f"{LINKS}/{link.id}")).status_code in (200, 204)

        as_user(world["owner"])  # neither subject nor actor
        assert (await async_client.get(REVOCATIONS)).json()["entries"] == []
        as_user(world["admin"])
        (entry,) = (await async_client.get(REVOCATIONS)).json()["entries"]
        assert entry["basis"] == BASIS_SELF
        assert entry["subject_is_you"] is False and entry["actor_is_you"] is False

    async def test_no_id_of_another_account_and_nothing_the_far_side_chose(
        self, async_client: AsyncClient, db_session, world, as_user, purged,
    ):
        world["pairing"].remote_instance_name = "verified-instance"
        await db_session.commit()
        await self._admin_removes_owners_link(async_client, db_session, world, as_user)

        as_user(world["owner"])
        resp = await async_client.get(REVOCATIONS)
        (entry,) = resp.json()["entries"]
        assert entry["peer_instance_name"] == "verified-instance"
        assert FAR_NAME not in resp.text
        assert world["pairing"].remote_pubkey not in resp.text
        assert not {k for k in entry if k.endswith("_id")}

    async def test_a_deleted_account_has_no_name(
        self, async_client: AsyncClient, db_session, world, as_user, purged,
    ):
        await self._admin_removes_owners_link(async_client, db_session, world, as_user)
        (row,) = await _revocations(db_session)
        detail = json.loads(row.query_text)
        detail["actor_user_id"] = 987654  # an account that no longer exists
        row.query_text = json.dumps(detail)
        await db_session.commit()

        as_user(world["owner"])
        (entry,) = (await async_client.get(REVOCATIONS)).json()["entries"]
        assert entry["actor_name"] is None

    async def test_query_rows_and_revocation_records_do_not_mix(
        self, async_client: AsyncClient, db_session, world, as_user, purged,
    ):
        now = datetime.now(UTC).replace(tzinfo=None)
        db_session.add(FederationQueryLog(
            direction="asked", final_status="success", query_text="not json at all",
            user_id=world["owner"].id, peer_user_id=world["pairing"].id,
            peer_pubkey_snapshot=world["pairing"].remote_pubkey,
            peer_display_name_snapshot="x", initiated_at=now, finalized_at=now,
            verified_signature=False,
        ))
        await db_session.commit()
        await self._admin_removes_owners_link(async_client, db_session, world, as_user)

        for who in ("owner", "admin"):
            as_user(world[who])
            resp = await async_client.get(REVOCATIONS)
            assert resp.status_code == 200, resp.text
            assert [e["action"] for e in resp.json()["entries"]] == [LINK_REVOKED]
        as_user(world["owner"])
        assert [e["direction"] for e in (await async_client.get(AUDIT)).json()["entries"]] == ["asked"]

    async def test_without_a_session_it_is_refused(
        self, async_client: AsyncClient, app_with_test_db, monkeypatch,
    ):
        from fastapi import HTTPException

        monkeypatch.setattr(settings, "auth_enabled", True)

        def _no_session():
            raise HTTPException(status_code=401, detail="Not authenticated")

        app_with_test_db.dependency_overrides[get_current_user] = _no_session
        app_with_test_db.dependency_overrides.pop(get_user_or_default, None)
        assert (await async_client.get(REVOCATIONS)).status_code == 401


async def _named(db, world) -> None:
    world["owner"].first_name, world["owner"].last_name = "Owner", "Person"
    world["admin"].first_name, world["admin"].last_name = "Admin", "Person"
    await db.commit()


class TestRevocationRetention:
    async def _rows(self, db, world, *, age_days: int) -> None:
        at = datetime.now(UTC).replace(tzinfo=None) - timedelta(days=age_days)
        db.add(FederationQueryLog(
            direction=REVOCATION_DIRECTION, final_status=PAIRING_REVOKED,
            identity_basis=BASIS_OWNER, query_text="{}", user_id=None,
            peer_user_id=world["pairing"].id,
            peer_pubkey_snapshot=world["pairing"].remote_pubkey,
            peer_display_name_snapshot="", initiated_at=at, finalized_at=at,
            verified_signature=False,
        ))
        await db.commit()

    async def test_a_record_older_than_two_years_goes_a_younger_one_stays(
        self, db_session, world, own_session_to_test_db,
    ):
        assert settings.federation_revocation_retention_days == 730
        await self._rows(db_session, world, age_days=731)
        await self._rows(db_session, world, age_days=729)
        assert await prune_old_audit_rows() == 1
        ages = (await db_session.execute(
            select(FederationQueryLog.initiated_at)
            .where(FederationQueryLog.direction == REVOCATION_DIRECTION)
        )).scalars().all()
        assert len(ages) == 1
        assert datetime.now(UTC).replace(tzinfo=None) - ages[0] < timedelta(days=730)

    async def test_the_period_is_a_setting(
        self, db_session, world, own_session_to_test_db, monkeypatch,
    ):
        monkeypatch.setattr(settings, "federation_revocation_retention_days", 30)
        await self._rows(db_session, world, age_days=31)
        assert await prune_old_audit_rows() == 1
