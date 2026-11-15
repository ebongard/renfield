"""An endpoint host is an address, not a name.

Until a paired instance has sent its name in a verified attachment, the only
thing this side knows about it is the host of the endpoint stored at pairing
time. `peer_instance_label` falls back to that host -- for the peers page,
which needs something to tell two pairings apart.

What these tests pin:

- the peers page data says WHICH of the two it got (`instance_name_verified`
  on a pairing, `peer_instance_name_verified` on a person link), so the UI
  can keep a host out of a headline;
- everything that is shown elsewhere or STORED takes the verified name or
  nothing: the chat status frame and the audit snapshot of a query this
  instance asks. (The answering side is pinned in
  `test_federation_instance_name.py`, the kiosk in `test_kiosk_peer_node.py`,
  the tool description in `test_peer_tool_description.py`.)
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from httpx import AsyncClient

from api.routes.federation_links import _to_response
from models.database import FederationUserLink, PeerUser, Role, User
from services.auth_service import get_user_or_default

pytestmark = [pytest.mark.backend]

PEERS = "/api/federation/peers"
LINKS_MINE = "/api/federation/links/mine"
HOST = "peer.example"
ENDPOINTS = {"endpoints": [f"https://{HOST}:8443"]}


@pytest.fixture
async def world(db_session):
    """One account with three pairings: no label at all, a verified name, and
    an endpoint only -- and a person link on each."""
    role = Role(name="role-host-label", permissions=["chat.own"], is_system=False)
    db_session.add(role)
    await db_session.flush()
    owner = User(username="host-label-owner", password_hash="x", role_id=role.id)
    db_session.add(owner)
    await db_session.commit()
    await db_session.refresh(owner, attribute_names=["role"])

    now = datetime.now(UTC).replace(tzinfo=None)

    def _peer(key: str, **kw) -> PeerUser:
        return PeerUser(
            circle_owner_id=owner.id, remote_pubkey=key * 32,
            remote_display_name="alex", remote_user_id=555, granted_tier=2,
            paired_at=now - timedelta(days=3), **kw,
        )

    plain = _peer("ab", transport_config={})
    named = _peer("bc", transport_config=ENDPOINTS, remote_instance_name="Instance B")
    hosted = _peer("cd", transport_config=ENDPOINTS)
    db_session.add_all([plain, named, hosted])
    await db_session.commit()

    links = {}
    for label, peer in (("plain", plain), ("named", named), ("hosted", hosted)):
        link = FederationUserLink(
            peer_id=peer.id, querier_ref=f"ref-{label}-" + "0" * 24, local_user_id=owner.id,
        )
        db_session.add(link)
        links[label] = link
    await db_session.commit()
    return {"owner": owner, "plain": plain, "named": named, "hosted": hosted, "links": links}


@pytest.fixture
def as_owner(app_with_test_db, world):
    app_with_test_db.dependency_overrides[get_user_or_default] = lambda: world["owner"]


@pytest.mark.database
class TestPeersPageSaysWhatItGot:
    async def test_a_pairing_says_whether_its_label_is_a_verified_name(
        self, async_client: AsyncClient, world, as_owner,
    ):
        rows = {p["id"]: p for p in (await async_client.get(PEERS)).json()["peers"]}
        named, hosted, plain = (rows[world[k].id] for k in ("named", "hosted", "plain"))

        assert named["instance_name"] == "Instance B"
        assert named["instance_name_verified"] is True
        # The host is still sent -- and marked as what it is.
        assert hosted["instance_name"] == HOST
        assert hosted["instance_name_verified"] is False
        # No label, no statement about it: the row keeps its exact key set.
        assert "instance_name" not in plain
        assert "instance_name_verified" not in plain

    async def test_a_person_link_says_the_same_about_the_same_pairing(
        self, async_client: AsyncClient, world, as_owner,
    ):
        """The links section sits on the same page as the pairing rows: the
        two must never disagree about one pairing."""
        mine = {row["peer_id"]: row for row in (await async_client.get(LINKS_MINE)).json()}
        named, hosted, plain = (mine[world[k].id] for k in ("named", "hosted", "plain"))

        assert (named["peer_instance_name"], named["peer_instance_name_verified"]) == (
            "Instance B", True)
        assert (hosted["peer_instance_name"], hosted["peer_instance_name_verified"]) == (
            HOST, False)
        assert "peer_instance_name" not in plain
        assert "peer_instance_name_verified" not in plain

        peers = {p["id"]: p for p in (await async_client.get(PEERS)).json()["peers"]}
        for key in ("named", "hosted", "plain"):
            peer, link = peers[world[key].id], mine[world[key].id]
            assert link.get("peer_instance_name") == peer.get("instance_name")
            assert link.get("peer_instance_name_verified") == peer.get("instance_name_verified")

    async def test_the_answer_to_a_completed_link_says_it_too(self, db_session, world):
        """`POST /links/complete` answers through `_to_response`."""
        def dumped(label: str):
            return _to_response(db_session, world["links"][label])

        named = (await dumped("named")).model_dump(exclude_none=True)
        hosted = (await dumped("hosted")).model_dump(exclude_none=True)
        plain = (await dumped("plain")).model_dump(exclude_none=True)
        assert (named["peer_instance_name"], named["peer_instance_name_verified"]) == (
            "Instance B", True)
        assert (hosted["peer_instance_name"], hosted["peer_instance_name_verified"]) == (
            HOST, False)
        assert "peer_instance_name" not in plain
        assert "peer_instance_name_verified" not in plain


class TestAskingSideNeverShowsOrStoresAHost:
    """`MCPManager._execute_federation_streaming`: the chat status frame and
    the audit row of a query THIS instance asks."""

    async def _ask(self, tmp_path, monkeypatch, *, remote_instance_name):
        from services.federation_identity import (
            init_federation_identity,
            reset_federation_identity_for_tests,
        )
        from services.mcp_client import (
            MCPManager,
            MCPServerConfig,
            MCPServerState,
            MCPToolInfo,
            MCPTransportType,
        )
        from services.mcp_streaming import ProgressChunk

        reset_federation_identity_for_tests()
        init_federation_identity(tmp_path / "key")

        manager = MCPManager()
        config = MCPServerConfig(
            name="peer_42", transport=MCPTransportType.FEDERATION,
            streaming=True, peer_user_id=42,
        )
        state = MCPServerState(config=config)
        state.connected = True
        manager._servers["peer_42"] = state
        manager._tool_index["mcp.peer_42.query_brain"] = MCPToolInfo(
            server_name="peer_42", original_name="query_brain",
            namespaced_name="mcp.peer_42.query_brain", description="", input_schema={},
        )

        peer = SimpleNamespace(
            id=42, remote_pubkey="e" * 64, remote_display_name="alex", revoked_at=None,
            remote_instance_name=remote_instance_name, transport_config=ENDPOINTS,
        )
        session = AsyncMock()
        result = MagicMock()
        result.scalar_one_or_none = lambda: peer
        session.execute = AsyncMock(return_value=result)
        session.__aenter__ = AsyncMock(return_value=session)
        session.__aexit__ = AsyncMock(return_value=False)
        monkeypatch.setattr("services.database.AsyncSessionLocal", lambda: session)

        async def fake_query_peer(self, peer, text, user_id=None):
            yield ProgressChunk(label="retrieving", sequence=1)
            yield {"success": True, "message": "the answer", "data": None}

        monkeypatch.setattr(
            "services.federation_query_asker.FederationQueryAsker.query_peer", fake_query_peer,
        )
        audits: list[dict] = []

        async def audit_spy(**kwargs):
            audits.append(kwargs)

        monkeypatch.setattr("services.federation_audit.write_federation_audit", audit_spy)
        frames: list[dict] = []

        async def sink(frame: dict) -> None:
            frames.append(frame)

        try:
            final = await manager.execute_tool(
                "mcp.peer_42.query_brain", {"query": "what is new?"},
                user_id=99, progress_sink=sink,
            )
        finally:
            reset_federation_identity_for_tests()
        assert final["success"] is True
        assert len(audits) == 1 and len(frames) == 1
        return frames[0], audits[0]

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_an_unverified_pairing_has_no_instance_part(self, tmp_path, monkeypatch):
        frame, audit = await self._ask(tmp_path, monkeypatch, remote_instance_name=None)
        # The status line reads as it did before the instance name existed ...
        assert "peer_instance_name" not in frame
        assert frame["peer_display_name"] == "alex"
        # ... and the row that is kept for good stores NULL, not an address.
        assert audit["peer_instance_name_snapshot"] is None
        assert audit["peer_display_name_snapshot"] == "alex"
        assert HOST not in repr(frame) and HOST not in repr(audit)

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_a_verified_name_is_shown_and_stored(self, tmp_path, monkeypatch):
        frame, audit = await self._ask(tmp_path, monkeypatch, remote_instance_name="Instance B")
        assert frame["peer_instance_name"] == "Instance B"
        assert audit["peer_instance_name_snapshot"] == "Instance B"
