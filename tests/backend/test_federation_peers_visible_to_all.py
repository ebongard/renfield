"""``GET /api/federation/peers`` + ``DELETE /api/federation/peers/{id}`` behind
``federation_peers_visible_to_all``.

Any local account can already USE any active pairing (the asking side looks the
peer up by id, without an owner check), but the list showed a pairing only to
the account that owns it. With the flag on, every account sees the instance's
active pairings and an ``admin`` holder may revoke one it does not own.

Flag off must stay exactly what it was -- for the owner and for everyone else.

Real Postgres, through the HTTP layer: the flag-off guarantee is a property of
the SERIALISED response (``response_model_exclude_unset``), which a direct call
of the route function would not exercise.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import pytest
from httpx import AsyncClient

from api.routes.federation_pairing import _account_label, _holds_admin
from models.database import FederationUserLink, PeerUser, Role, User
from services.auth_service import get_user_or_default
from utils.config import settings

pytestmark = [pytest.mark.backend, pytest.mark.database]

PEERS = "/api/federation/peers"
LINKS_MINE = "/api/federation/links/mine"

#: The seven keys the list has always carried. Flag off: exactly these.
LEGACY_KEYS = {
    "id", "remote_pubkey", "remote_display_name", "remote_user_id",
    "paired_at", "last_seen_at", "circle_tier",
}
NEW_KEYS = {"owned_by_me", "paired_by", "linked", "can_revoke"}

OWNER_REF = "ref-of-the-owner-" + "0" * 16
MEMBER_REF = "ref-of-the-member-" + "1" * 16


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
async def world(db_session):
    """owner: two pairings (one active at tier 2, one revoked).
    member: plain account, owns nothing. admin: holds ``admin``, owns nothing.
    outsider: a second plain account, used for the links of SOMEONE ELSE."""
    owner = await _user(db_session, "fed-owner", ["chat.own"], first_name="Olga", last_name="Owner")
    member = await _user(db_session, "fed-member", ["chat.own"])
    admin = await _user(db_session, "fed-admin", ["admin"])
    outsider = await _user(db_session, "fed-outsider", ["chat.own"])

    now = datetime.now(UTC).replace(tzinfo=None)
    active = PeerUser(
        circle_owner_id=owner.id, remote_pubkey="ab" * 32,
        remote_display_name="Far Instance", remote_user_id=555,
        granted_tier=2, transport_config={}, paired_at=now - timedelta(days=3),
        last_seen_at=now - timedelta(days=1),
    )
    revoked = PeerUser(
        circle_owner_id=owner.id, remote_pubkey="cd" * 32,
        remote_display_name="Gone Instance", remote_user_id=556,
        granted_tier=4, transport_config={}, paired_at=now - timedelta(days=9),
        revoked_at=now - timedelta(days=2),
    )
    db_session.add_all([active, revoked])
    await db_session.commit()
    return {
        "owner": owner, "member": member, "admin": admin, "outsider": outsider,
        "active": active, "revoked": revoked,
    }


@pytest.fixture
def as_user(app_with_test_db):
    def _as(user: User) -> None:
        app_with_test_db.dependency_overrides[get_user_or_default] = lambda: user
    return _as


@pytest.fixture
def flag_on(monkeypatch):
    monkeypatch.setattr(settings, "federation_peers_visible_to_all", True)


@pytest.fixture
def flag_off(monkeypatch):
    monkeypatch.setattr(settings, "federation_peers_visible_to_all", False)


@pytest.fixture
def links_on(monkeypatch):
    monkeypatch.setattr(settings, "federation_identity_links_enabled", True)


async def _link(db, peer: PeerUser, user: User, ref: str) -> None:
    db.add(FederationUserLink(peer_id=peer.id, querier_ref=ref, local_user_id=user.id))
    await db.commit()


async def _is_revoked(db, peer: PeerUser) -> bool:
    await db.refresh(peer)
    return peer.revoked_at is not None


# ---------------------------------------------------------------------------
# Flag OFF -- exactly today's behaviour
# ---------------------------------------------------------------------------


class TestFlagOff:
    async def test_owner_gets_the_legacy_shape_and_nothing_else(
        self, async_client: AsyncClient, world, as_user, flag_off,
    ):
        as_user(world["owner"])
        resp = await async_client.get(PEERS)
        assert resp.status_code == 200
        active = world["active"]
        assert resp.json() == {"peers": [{
            "id": active.id,
            "remote_pubkey": "ab" * 32,
            "remote_display_name": "Far Instance",
            "remote_user_id": 555,
            "paired_at": active.paired_at.isoformat(),
            "last_seen_at": active.last_seen_at.isoformat(),
            "circle_tier": 2,
        }]}

    async def test_a_null_last_seen_is_still_emitted_as_null(
        self, async_client: AsyncClient, db_session, world, as_user, flag_off,
    ):
        """`exclude_unset` must drop the four NEW keys only -- a legacy key
        that happens to be null stays in the response."""
        world["active"].last_seen_at = None
        await db_session.commit()
        as_user(world["owner"])
        row = (await async_client.get(PEERS)).json()["peers"][0]
        assert set(row) == LEGACY_KEYS
        assert row["last_seen_at"] is None

    @pytest.mark.parametrize("who", ["member", "admin"])
    async def test_non_owner_sees_nothing(
        self, async_client: AsyncClient, world, as_user, flag_off, who,
    ):
        as_user(world[who])
        resp = await async_client.get(PEERS)
        assert resp.status_code == 200
        assert resp.json() == {"peers": []}

    @pytest.mark.parametrize("who", ["member", "admin"])
    async def test_non_owner_cannot_revoke_not_even_an_admin(
        self, async_client: AsyncClient, db_session, world, as_user, flag_off, who,
    ):
        as_user(world[who])
        resp = await async_client.delete(f"{PEERS}/{world['active'].id}")
        assert resp.status_code == 404
        assert resp.json() == {"detail": "Peer not found"}
        assert not await _is_revoked(db_session, world["active"])

    async def test_owner_revokes(
        self, async_client: AsyncClient, db_session, world, as_user, flag_off,
    ):
        as_user(world["owner"])
        resp = await async_client.delete(f"{PEERS}/{world['active'].id}")
        assert resp.status_code == 204
        assert await _is_revoked(db_session, world["active"])
        assert world["active"].granted_tier == 4


# ---------------------------------------------------------------------------
# Flag ON -- the list
# ---------------------------------------------------------------------------


class TestFlagOnList:
    async def test_non_owner_sees_the_pairing_as_someone_elses(
        self, async_client: AsyncClient, world, as_user, flag_on,
    ):
        as_user(world["member"])
        peers = (await async_client.get(PEERS)).json()["peers"]
        assert len(peers) == 1
        row = peers[0]
        assert set(row) == LEGACY_KEYS | NEW_KEYS
        assert row["id"] == world["active"].id
        assert row["remote_display_name"] == "Far Instance"
        assert row["circle_tier"] == 2
        assert row["owned_by_me"] is False
        assert row["paired_by"] == "Olga Owner"
        assert row["linked"] is False
        assert row["can_revoke"] is False

    async def test_non_owner_gets_the_public_key_but_not_the_remote_account_number(
        self, async_client: AsyncClient, world, as_user, flag_on,
    ):
        as_user(world["member"])
        row = (await async_client.get(PEERS)).json()["peers"][0]
        assert row["remote_pubkey"] == "ab" * 32
        assert row["remote_user_id"] is None

    async def test_owner_sees_it_as_their_own(
        self, async_client: AsyncClient, world, as_user, flag_on,
    ):
        as_user(world["owner"])
        row = (await async_client.get(PEERS)).json()["peers"][0]
        assert row["owned_by_me"] is True
        assert row["paired_by"] == "Olga Owner"
        assert row["can_revoke"] is True
        assert row["remote_user_id"] == 555

    @pytest.mark.parametrize("who", ["owner", "member", "admin"])
    async def test_revoked_pairings_are_never_listed(
        self, async_client: AsyncClient, world, as_user, flag_on, who,
    ):
        as_user(world[who])
        peers = (await async_client.get(PEERS)).json()["peers"]
        assert [p["id"] for p in peers] == [world["active"].id]

    async def test_admin_non_owner_may_revoke(
        self, async_client: AsyncClient, world, as_user, flag_on,
    ):
        as_user(world["admin"])
        row = (await async_client.get(PEERS)).json()["peers"][0]
        assert row["owned_by_me"] is False
        assert row["can_revoke"] is True

    async def test_own_pairings_come_first(
        self, async_client: AsyncClient, db_session, world, as_user, flag_on,
    ):
        """The member's own pairing is OLDER than the owner's, and still leads."""
        own = PeerUser(
            circle_owner_id=world["member"].id, remote_pubkey="ef" * 32,
            remote_display_name="Member's Peer", remote_user_id=7,
            granted_tier=3, transport_config={},
            paired_at=datetime.now(UTC).replace(tzinfo=None) - timedelta(days=30),
        )
        db_session.add(own)
        await db_session.commit()
        as_user(world["member"])
        peers = (await async_client.get(PEERS)).json()["peers"]
        assert [(p["id"], p["owned_by_me"]) for p in peers] == [
            (own.id, True), (world["active"].id, False),
        ]
        assert peers[0]["paired_by"] == "fed-member"   # no name set -> username


class TestFlagOnLinked:
    async def test_linked_is_true_for_the_callers_own_link(
        self, async_client: AsyncClient, db_session, world, as_user, flag_on, links_on,
    ):
        await _link(db_session, world["active"], world["member"], MEMBER_REF)
        as_user(world["member"])
        row = (await async_client.get(PEERS)).json()["peers"][0]
        assert row["linked"] is True

    async def test_someone_elses_link_does_not_make_me_linked(
        self, async_client: AsyncClient, db_session, world, as_user, flag_on, links_on,
    ):
        await _link(db_session, world["active"], world["owner"], OWNER_REF)
        await _link(db_session, world["active"], world["outsider"], MEMBER_REF)
        as_user(world["member"])
        row = (await async_client.get(PEERS)).json()["peers"][0]
        assert row["linked"] is False

    async def test_a_link_behind_the_dark_identity_flag_is_not_linked(
        self, async_client: AsyncClient, db_session, world, as_user, flag_on, monkeypatch,
    ):
        """The asking side sends no `querier_ref` while
        `federation_identity_links_enabled` is off -- so the row does not ask
        "as yourself", whatever the table says."""
        monkeypatch.setattr(settings, "federation_identity_links_enabled", False)
        await _link(db_session, world["active"], world["member"], MEMBER_REF)
        as_user(world["member"])
        row = (await async_client.get(PEERS)).json()["peers"][0]
        assert row["linked"] is False

    async def test_linked_is_decided_per_pairing(
        self, async_client: AsyncClient, db_session, world, as_user, flag_on, links_on,
    ):
        """Two active pairings, a link to exactly one of them: `linked` is
        true for that id and false for the other -- not "has any link"."""
        other = PeerUser(
            circle_owner_id=world["owner"].id, remote_pubkey="12" * 32,
            remote_display_name="Second Instance", remote_user_id=9,
            granted_tier=3, transport_config={},
        )
        db_session.add(other)
        await db_session.commit()
        await _link(db_session, world["active"], world["member"], MEMBER_REF)
        as_user(world["member"])
        peers = (await async_client.get(PEERS)).json()["peers"]
        assert {p["id"]: p["linked"] for p in peers} == {
            world["active"].id: True, other.id: False,
        }

    async def test_no_link_of_another_account_appears_anywhere(
        self, async_client: AsyncClient, db_session, world, as_user, flag_on, links_on,
    ):
        """Neither the list nor `/links/mine` gives the member anything about
        the owner's or the outsider's link -- no ref, no row, no count."""
        await _link(db_session, world["active"], world["owner"], OWNER_REF)
        await _link(db_session, world["active"], world["outsider"], MEMBER_REF)
        as_user(world["member"])

        peers_resp = await async_client.get(PEERS)
        assert OWNER_REF not in peers_resp.text
        assert MEMBER_REF not in peers_resp.text
        assert "querier_ref" not in peers_resp.text
        assert set(peers_resp.json()["peers"][0]) == LEGACY_KEYS | NEW_KEYS

        mine = await async_client.get(LINKS_MINE)
        assert mine.status_code == 200
        assert mine.json() == []

    async def test_links_mine_stays_per_account(
        self, async_client: AsyncClient, db_session, world, as_user, flag_on, links_on,
    ):
        await _link(db_session, world["active"], world["owner"], OWNER_REF)
        await _link(db_session, world["active"], world["member"], MEMBER_REF)
        as_user(world["member"])
        mine = (await async_client.get(LINKS_MINE)).json()
        assert len(mine) == 1
        assert mine[0]["peer_id"] == world["active"].id
        assert "querier_ref" not in mine[0]


# ---------------------------------------------------------------------------
# Flag ON -- revoke
# ---------------------------------------------------------------------------


class TestFlagOnRevoke:
    async def test_admin_non_owner_revokes(
        self, async_client: AsyncClient, db_session, world, as_user, flag_on,
    ):
        as_user(world["admin"])
        resp = await async_client.delete(f"{PEERS}/{world['active'].id}")
        assert resp.status_code == 204
        assert await _is_revoked(db_session, world["active"])
        assert world["active"].granted_tier == 4
        assert (await async_client.get(PEERS)).json() == {"peers": []}

    async def test_plain_non_owner_still_gets_the_same_404(
        self, async_client: AsyncClient, db_session, world, as_user, flag_on,
    ):
        as_user(world["member"])
        resp = await async_client.delete(f"{PEERS}/{world['active'].id}")
        assert resp.status_code == 404
        assert resp.json() == {"detail": "Peer not found"}
        assert not await _is_revoked(db_session, world["active"])

    async def test_owner_still_revokes(
        self, async_client: AsyncClient, db_session, world, as_user, flag_on,
    ):
        as_user(world["owner"])
        resp = await async_client.delete(f"{PEERS}/{world['active'].id}")
        assert resp.status_code == 204
        assert await _is_revoked(db_session, world["active"])

    async def test_admin_cannot_touch_a_pairing_the_list_does_not_show(
        self, async_client: AsyncClient, db_session, world, as_user, flag_on,
    ):
        """An already revoked pairing of someone else is not in the admin's
        list, so the admin gets the 404 -- and the audit timestamp stays."""
        before = world["revoked"].revoked_at
        as_user(world["admin"])
        resp = await async_client.delete(f"{PEERS}/{world['revoked'].id}")
        assert resp.status_code == 404
        await db_session.refresh(world["revoked"])
        assert world["revoked"].revoked_at == before

    async def test_unknown_id_is_the_same_404_for_an_admin(
        self, async_client: AsyncClient, world, as_user, flag_on,
    ):
        as_user(world["admin"])
        resp = await async_client.delete(f"{PEERS}/999999")
        assert resp.status_code == 404
        assert resp.json() == {"detail": "Peer not found"}


# ---------------------------------------------------------------------------
# Through the REAL `get_user_or_default` -- no override of it
# ---------------------------------------------------------------------------


class TestRealAuthDependency:
    """The tests above hand the route a ready user. These two let the real
    dependency resolve it, so the admin check runs on a user loaded the way
    production loads it (role eager-loaded, or not at all)."""

    async def test_auth_off_resolves_to_the_default_admin(
        self, async_client: AsyncClient, app_with_test_db, db_session, world, flag_on,
    ):
        """Auth off: `get_current_user` yields None and `get_user_or_default`
        falls back to the account named `admin`. It owns nothing, sees every
        pairing as revocable, and revokes a foreign one."""
        from services.auth_service import get_current_user

        await _user(db_session, "admin", ["admin"])
        app_with_test_db.dependency_overrides[get_current_user] = lambda: None

        peers = (await async_client.get(PEERS)).json()["peers"]
        assert [(p["id"], p["owned_by_me"], p["can_revoke"]) for p in peers] == [
            (world["active"].id, False, True),
        ]

        resp = await async_client.delete(f"{PEERS}/{world['active'].id}")
        assert resp.status_code == 204
        assert await _is_revoked(db_session, world["active"])

    async def test_a_real_bearer_token_of_an_admin_account(
        self, async_client: AsyncClient, db_session, world, flag_on, monkeypatch,
    ):
        """Auth on, nothing overridden: a real access token for the admin
        account goes through `get_current_user` -> `get_user_or_default`."""
        from unittest.mock import AsyncMock

        from services.auth_service import create_access_token

        monkeypatch.setattr(settings, "auth_enabled", True)
        # The revocation check would need a live Redis.
        monkeypatch.setattr(
            "services.token_blacklist.token_blacklist.is_blacklisted",
            AsyncMock(return_value=False),
        )

        def bearer(user: User) -> dict[str, str]:
            token = create_access_token(
                {"sub": str(user.id)}, token_epoch=int(user.token_epoch or 0),
            )
            return {"Authorization": f"Bearer {token}"}

        assert (await async_client.get(PEERS)).status_code == 401

        admin, member = bearer(world["admin"]), bearer(world["member"])
        row = (await async_client.get(PEERS, headers=admin)).json()["peers"][0]
        assert (row["owned_by_me"], row["can_revoke"]) == (False, True)
        row = (await async_client.get(PEERS, headers=member)).json()["peers"][0]
        assert (row["owned_by_me"], row["can_revoke"]) == (False, False)

        url = f"{PEERS}/{world['active'].id}"
        assert (await async_client.delete(url, headers=member)).status_code == 404
        assert not await _is_revoked(db_session, world["active"])
        assert (await async_client.delete(url, headers=admin)).status_code == 204
        assert await _is_revoked(db_session, world["active"])


# ---------------------------------------------------------------------------
# The two helpers
# ---------------------------------------------------------------------------


class TestHelpers:
    @pytest.mark.unit
    def test_admin_lookup_fails_closed(self):
        user = MagicMock(id=7)
        user.get_permissions.side_effect = RuntimeError("role not loaded")
        assert _holds_admin(user) is False

    @pytest.mark.unit
    @pytest.mark.parametrize("permissions,expected", [
        (["admin"], True),
        # `admin` is not implied by any other permission, however broad.
        (["users.manage", "roles.manage", "kb.all", "circles.manage"], False),
        ([], False),
    ])
    def test_admin_is_asked_for_by_name(self, permissions, expected):
        user = MagicMock(id=7)
        user.get_permissions.return_value = permissions
        assert _holds_admin(user) is expected

    @pytest.mark.unit
    @pytest.mark.parametrize("first,last,username,expected", [
        ("Olga", "Owner", "olga", "Olga Owner"),
        ("Olga", None, "olga", "Olga"),
        (None, None, "olga", "olga"),
        ("", "", "olga", "olga"),
    ])
    def test_account_label(self, first, last, username, expected):
        user = MagicMock(id=7, first_name=first, last_name=last, username=username)
        assert _account_label(user) == expected
