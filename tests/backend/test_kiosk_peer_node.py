"""A paired instance on the wall display: ONE node, on the peers arc.

Until 2026-11-13 the same peer showed twice -- as a `peer_<id>` diamond on the
tool ring (green for ever: the registry sets `connected=True` on an entry that
has no session behind it) and as a ring on the peers arc. This file pins:

  - the peer's MCP entry is in neither the snapshot's server list, nor its
    tool-health rows, nor the tool counts, and no `tool_health_changed` delta
    is sent for it -- snapshot and delta agree;
  - it is recognised by TRANSPORT, never by the `peer_` name prefix;
  - what the arc node's payload carries: unchanged with the reachability
    feature off, the documented extra keys with it on, the instance name only
    when the far side sent a VERIFIED one -- and never a key, an address, a
    host name or any content;
  - the periodic check is consumer-gated: it runs while a wall display is
    connected, and not otherwise.
"""
from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

import api.websocket.kiosk_data as kiosk_data
import api.websocket.kiosk_handler as kiosk
import services.federation_reachability as reach
from api.websocket.kiosk_data import (
    compute_peer_status,
    is_peer_tool,
    kiosk_mcp_status,
    probe_and_push_peer_status,
    schedule_peer_probe_on_connect,
)
from api.websocket.kiosk_handler import build_kiosk_snapshot
from models.database import FederationQueryLog, PeerUser, Role, User
from services.federation_query_asker import PING_PIN_MISMATCH, PING_REACHABLE, PING_UNSUPPORTED
from services.federation_reachability import MemoryReachabilityStore, set_store_for_tests
from services.mcp_client import (
    MCPManager,
    MCPServerConfig,
    MCPServerState,
    MCPToolInfo,
    MCPTransportType,
)
from utils.config import settings

#: The four keys a kiosk peer entry has always carried.
LEGACY_PEER_KEYS = {"id", "name", "last_seen_at", "reachable"}
PROBE_KEYS = {"reachability", "reachability_reason", "checked_at", "recently_active"}


@pytest.fixture(autouse=True)
def memory_store():
    store = MemoryReachabilityStore()
    set_store_for_tests(store)
    yield store
    set_store_for_tests(None)


@pytest.fixture
def feature_on(monkeypatch):
    monkeypatch.setattr(settings, "federation_reachability_enabled", True)


@pytest.fixture
def feature_off(monkeypatch):
    monkeypatch.setattr(settings, "federation_reachability_enabled", False)


# =============================================================================
# The tool ring: no peer on it
# =============================================================================


def _add_server(manager: MCPManager, name: str, transport: MCPTransportType, tools: list[str]):
    state = MCPServerState(config=MCPServerConfig(
        name=name, transport=transport, enabled=True,
        **({"streaming": True, "peer_user_id": 3} if transport == MCPTransportType.FEDERATION else {}),
    ))
    state.connected = True
    infos = [
        MCPToolInfo(
            server_name=name, original_name=tool, namespaced_name=f"mcp.{name}.{tool}",
            description="", input_schema={},
        )
        for tool in tools
    ]
    state.tools = infos
    state.all_discovered_tools = infos
    for info in infos:
        manager._tool_index[info.namespaced_name] = info
    manager._servers[name] = state


@pytest.fixture
def manager() -> MCPManager:
    """A real manager: two configured servers and one paired peer, built the
    way `peer_mcp_registry.sync_peers` builds it."""
    m = MCPManager()
    _add_server(m, "paperless", MCPTransportType.STDIO, ["search", "upload"])
    # 🛑 A CONFIGURED server whose name happens to start with `peer_`. It is a
    # tool server and has to stay on the ring: the filter keys on transport.
    _add_server(m, "peer_review", MCPTransportType.STDIO, ["list"])
    _add_server(m, "peer_3", MCPTransportType.FEDERATION, ["query_brain"])
    return m


class TestToolRing:
    @pytest.mark.unit
    def test_the_managers_own_status_still_lists_the_peer(self, manager):
        """`get_status()` is left alone -- the health monitor and the admin
        surfaces read it. (And the control for the tests below: the peer IS
        there to be filtered.)"""
        status = manager.get_status()
        assert {s["name"] for s in status["servers"]} == {"paperless", "peer_review", "peer_3"}
        assert status["total_tools"] == 4

    @pytest.mark.unit
    def test_the_kiosk_status_leaves_the_peer_out_and_does_not_count_it(self, manager):
        status = kiosk_mcp_status(manager)
        assert {s["name"] for s in status["servers"]} == {"paperless", "peer_review"}
        assert status["total_tools"] == 3
        assert status["enabled"] is True

    @pytest.mark.unit
    def test_without_a_peer_the_status_is_what_the_manager_says(self):
        m = MCPManager()
        _add_server(m, "paperless", MCPTransportType.STDIO, ["search"])
        assert kiosk_mcp_status(m) == m.get_status()

    @pytest.mark.unit
    @pytest.mark.parametrize(("tool", "expected"), [
        ("mcp.peer_3.query_brain", True),
        ("mcp.peer_review.list", False),     # the name prefix decides nothing
        ("mcp.paperless.search", False),
        ("mcp.unknown.tool", False),
        ("internal.remember", False),
        ("mcp.peer_3", False),
        ("", False),
        (None, False),
    ])
    def test_is_peer_tool(self, manager, tool, expected):
        assert is_peer_tool(manager, tool) is expected

    @pytest.mark.unit
    def test_no_manager_and_a_mock_manager_hide_nothing(self):
        """Many kiosk tests run on a MagicMock manager. Only the manager's
        explicit `True` may hide a tool's statistics."""
        assert is_peer_tool(None, "mcp.peer_3.query_brain") is False
        assert is_peer_tool(MagicMock(), "mcp.peer_3.query_brain") is False


class _Stat:
    def __init__(self, tool_name, success, failure, last_used_at):
        self.tool_name = tool_name
        self.success_count = success
        self.failure_count = failure
        self.last_used_at = last_used_at


class _NoopSession:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def execute(self, *a, **k):
        raise RuntimeError("no db in unit test")


class _App:
    def __init__(self, mcp_manager):
        self.state = MagicMock(spec=["mcp_manager", "agent_router"])
        self.state.mcp_manager = mcp_manager
        self.state.agent_router = None


class TestSnapshot:
    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_the_peer_is_on_neither_list_nor_counted(self, manager, monkeypatch, feature_off):
        from services.tool_outcome_service import ToolOutcomeService

        recent = datetime.now(UTC).replace(tzinfo=None) - timedelta(minutes=10)
        stats = [
            _Stat("mcp.paperless.search", success=9, failure=1, last_used_at=recent),
            # Well sampled, recent and failing: on the ring this would paint a
            # `peer_3` node red. It must not appear at all.
            _Stat("mcp.peer_3.query_brain", success=1, failure=9, last_used_at=recent),
            _Stat("mcp.peer_review.list", success=5, failure=0, last_used_at=recent),
        ]
        monkeypatch.setattr(kiosk, "AsyncSessionLocal", lambda: _NoopSession(), raising=True)
        monkeypatch.setattr(ToolOutcomeService, "list_stats", AsyncMock(return_value=stats))
        monkeypatch.setattr(kiosk_data, "compute_peer_status", AsyncMock(return_value=[]))

        snap = await build_kiosk_snapshot(_App(manager))

        assert {s["name"] for s in snap["mcp"]["servers"]} == {"paperless", "peer_review"}
        assert snap["mcp"]["total_tools"] == 3
        assert {t["tool_name"] for t in snap["tool_health"]} == {
            "mcp.paperless.search", "mcp.peer_review.list"}
        # Nothing anywhere in the tool part of the snapshot names the peer.
        assert "peer_3" not in repr(snap["mcp"]) + repr(snap["tool_health"])


class TestDelta:
    """The one delta that could put the peer back: the frontend ADDS a server
    it does not know when `tool_health_changed` names one."""

    @pytest.fixture
    def sent(self, monkeypatch):
        events: list[dict] = []

        async def _capture(event):
            events.append(event)

        monkeypatch.setattr(kiosk, "broadcast_kiosk_event", _capture)
        return events

    @pytest.mark.asyncio
    @pytest.mark.unit
    @pytest.mark.parametrize("connected", [True, False])
    async def test_no_tool_health_delta_for_a_peer(self, manager, sent, connected):
        await manager._broadcast_tool_health("peer_3", connected)
        assert sent == []

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_a_configured_server_still_gets_its_delta(self, manager, sent):
        """Negative control: the suppression is not a blanket one."""
        await manager._broadcast_tool_health("paperless", False)
        await manager._broadcast_tool_health("peer_review", True)
        assert [e["server"] for e in sent] == ["paperless", "peer_review"]
        assert all(e["type"] == "tool_health_changed" for e in sent)


# =============================================================================
# The arc node's payload, on real Postgres
# =============================================================================


@pytest.fixture
async def pairings(db_session, own_session_to_test_db):
    role = Role(name="role-kiosk-peer", permissions=["chat.own"], is_system=False)
    db_session.add(role)
    await db_session.flush()
    owner = User(username="kiosk-peer-owner", password_hash="x", role_id=role.id)
    db_session.add(owner)
    await db_session.flush()
    now = datetime.now(UTC).replace(tzinfo=None)

    def _peer(key: str, **kw) -> PeerUser:
        return PeerUser(
            circle_owner_id=owner.id, remote_pubkey=key * 32, remote_display_name="evdb",
            granted_tier=2, paired_at=now - timedelta(days=3), **kw,
        )

    plain = _peer("ab", transport_config={}, last_seen_at=now - timedelta(minutes=1))
    named = _peer(
        "bc", remote_instance_name="Instance B", last_seen_at=None,
        transport_config={
            "endpoints": ["https://far.example.org:8443"], "tls_fingerprint": "f0" * 32},
    )
    hosted = _peer(
        "cd", transport_config={"endpoints": ["https://other.example.org"]},
        last_seen_at=now - timedelta(days=2),
    )
    revoked = _peer("de", transport_config={}, revoked_at=now)
    db_session.add_all([plain, named, hosted, revoked])
    await db_session.commit()
    return {"owner": owner, "plain": plain, "named": named, "hosted": hosted, "revoked": revoked}


def _entry(peers: list[dict], peer: PeerUser) -> dict:
    return next(p for p in peers if p["id"] == peer.id)


@pytest.mark.database
class TestArcPayloadFeatureOff:
    async def test_a_peer_without_a_label_is_byte_identical(self, pairings, feature_off):
        plain = pairings["plain"]
        assert _entry(await compute_peer_status(), plain) == {
            "id": plain.id,
            "name": "evdb",
            "last_seen_at": plain.last_seen_at.isoformat(),
            "reachable": True,           # today's rule: it queried us a minute ago
        }

    async def test_todays_reachable_rule_is_untouched(self, pairings, feature_off, memory_store):
        """Even with a stored probe result lying around (the feature was on
        and got switched off): the legacy rule alone decides."""
        await memory_store.save(pairings["hosted"].id, PING_REACHABLE, time.time())
        peers = await compute_peer_status()
        assert _entry(peers, pairings["plain"])["reachable"] is True
        assert _entry(peers, pairings["hosted"])["reachable"] is False   # last seen 2 days ago
        assert _entry(peers, pairings["named"])["reachable"] is False    # never seen
        assert all(PROBE_KEYS.isdisjoint(p) for p in peers)

    async def test_a_verified_name_adds_exactly_one_key(self, pairings, feature_off):
        peers = await compute_peer_status()
        named = _entry(peers, pairings["named"])
        assert set(named) == LEGACY_PEER_KEYS | {"instance_name"}
        assert named["instance_name"] == "Instance B"
        assert named["name"] == "evdb", "the person stays in `name`"

    async def test_an_endpoint_host_is_no_label_on_the_wall(self, pairings, feature_off):
        """🛑 The peers page falls back to the host of the stored endpoint
        while no name is known -- behind a login. The wall display does not:
        its payload is content-free (`.claude/rules/kiosk.md`), and a host
        name is an address. Without a verified name the node shows the person
        alone, exactly as before this feature."""
        hosted = _entry(await compute_peer_status(), pairings["hosted"])
        assert set(hosted) == LEGACY_PEER_KEYS
        assert hosted == {
            "id": pairings["hosted"].id,
            "name": "evdb",
            "last_seen_at": pairings["hosted"].last_seen_at.isoformat(),
            "reachable": False,
        }

    async def test_a_revoked_pairing_is_not_on_the_wall(self, pairings, feature_off):
        ids = {p["id"] for p in await compute_peer_status()}
        assert pairings["revoked"].id not in ids
        assert len(ids) == 3


@pytest.mark.database
class TestArcPayloadFeatureOn:
    async def test_the_documented_keys_and_nothing_else(self, pairings, feature_on):
        peers = await compute_peer_status()
        assert set(_entry(peers, pairings["plain"])) == LEGACY_PEER_KEYS | PROBE_KEYS
        assert set(_entry(peers, pairings["named"])) == (
            LEGACY_PEER_KEYS | PROBE_KEYS | {"instance_name"})

    async def test_never_probed_is_unknown_not_unreachable(self, pairings, feature_on):
        entry = _entry(await compute_peer_status(), pairings["named"])
        assert entry["reachability"] == "unknown"
        assert entry["reachability_reason"] == "not_checked"
        assert entry["checked_at"] is None
        assert entry["reachable"] is False

    async def test_reachable_now_means_the_round_trip_not_the_last_query(
        self, pairings, feature_on, memory_store,
    ):
        """🛑 The point of the feature. `named` never queried us and is
        reachable; `plain` queried us a minute ago and its certificate pin is
        stale. The old rule had both the wrong way round."""
        await memory_store.save(pairings["named"].id, PING_REACHABLE, time.time())
        await memory_store.save(pairings["plain"].id, PING_PIN_MISMATCH, time.time())
        peers = await compute_peer_status()
        named, plain = _entry(peers, pairings["named"]), _entry(peers, pairings["plain"])

        assert (named["reachability"], named["reachable"]) == ("reachable", True)
        assert named["reachability_reason"] is None
        assert named["recently_active"] is False

        assert (plain["reachability"], plain["reachable"]) == ("unreachable", False)
        assert plain["reachability_reason"] == "pin_mismatch"
        assert plain["recently_active"] is True, "what the old rule actually measured"

    async def test_a_far_side_that_cannot_answer_pings_is_unknown(
        self, pairings, feature_on, memory_store,
    ):
        await memory_store.save(pairings["hosted"].id, PING_UNSUPPORTED, time.time())
        entry = _entry(await compute_peer_status(), pairings["hosted"])
        assert (entry["reachability"], entry["reachability_reason"]) == ("unknown", "unsupported")

    async def test_our_own_question_counts_as_activity(self, db_session, pairings, feature_on):
        named = pairings["named"]
        db_session.add(FederationQueryLog(
            user_id=pairings["owner"].id, peer_user_id=named.id,
            peer_pubkey_snapshot=named.remote_pubkey, peer_display_name_snapshot="evdb",
            query_text="frage", final_status="success", verified_signature=True,
            initiated_at=datetime.now(UTC).replace(tzinfo=None) - timedelta(minutes=1),
        ))
        await db_session.commit()
        assert _entry(await compute_peer_status(), named)["recently_active"] is True

    async def test_reading_never_sends_a_ping(self, pairings, feature_on, monkeypatch):
        """This runs inside the connect snapshot: a hanging far instance must
        not hold a wall display on its skeleton."""
        ping = AsyncMock(return_value=PING_REACHABLE)
        monkeypatch.setattr(reach, "ping_peer", ping)
        await compute_peer_status()
        ping.assert_not_awaited()

    @pytest.mark.parametrize("flag", [True, False])
    async def test_the_payload_is_content_free(
        self, pairings, monkeypatch, memory_store, flag,
    ):
        """Names, times and state codes. No key, no address, no host name, no
        fingerprint, no query text -- with the feature on or off."""
        monkeypatch.setattr(settings, "federation_reachability_enabled", flag)
        await memory_store.save(pairings["named"].id, PING_REACHABLE, time.time())
        rendered = repr(await compute_peer_status())
        for secret in (
            "bc" * 32, "ab" * 32, "f0" * 32, "https://", ":8443", "tls_fingerprint",
            "far.example.org", "other.example.org", "example.org",
        ):
            assert secret not in rendered, secret


# =============================================================================
# Two accounts paired with the SAME far instance: still one node
# =============================================================================


@pytest.fixture
async def twin(db_session, pairings):
    """Two accounts here are paired with ONE far instance: two rows on one
    remote key, each with its own id, person and reading.

    🛑 The row with the HIGHER id is written FIRST. A table this small is read
    front to back, so an unordered select returns the higher id first -- which
    is how the surviving row, and with it the node's identity, the person
    shown and the pulse id, used to depend on where a row happened to lie.
    """
    second_owner = User(
        username="kiosk-peer-second", password_hash="x", role_id=pairings["owner"].role_id)
    db_session.add(second_owner)
    await db_session.flush()
    now = datetime.now(UTC).replace(tzinfo=None)

    def _row(row_id: int, owner_id: int, **kw) -> PeerUser:
        return PeerUser(
            id=row_id, circle_owner_id=owner_id, remote_pubkey="ef" * 32,
            granted_tier=2, paired_at=now - timedelta(days=1),
            transport_config={"endpoints": ["https://twin.example.org"]}, **kw,
        )

    high = _row(
        9002, second_owner.id, remote_display_name="second person",
        remote_instance_name="Instance C", last_seen_at=now - timedelta(minutes=2),
    )
    db_session.add(high)
    await db_session.commit()
    low = _row(
        9001, pairings["owner"].id, remote_display_name="evdb",
        last_seen_at=now - timedelta(days=2),
    )
    db_session.add(low)
    await db_session.commit()
    return {"low": low, "high": high}


def _group_entry(peers: list[dict], *rows: PeerUser) -> dict:
    ids = {row.id for row in rows}
    found = [p for p in peers if p["id"] in ids]
    assert len(found) == 1, f"one node per remote identity, got {found}"
    return found[0]


@pytest.mark.database
class TestOneNodePerRemoteIdentity:
    async def test_the_node_is_the_lowest_row_and_names_every_pulse_id(
        self, pairings, twin, feature_off,
    ):
        low, high = twin["low"], twin["high"]
        peers = await compute_peer_status()
        assert len(peers) == 4
        entry = _group_entry(peers, low, high)
        assert entry["id"] == low.id, "the lowest row id, whatever order the heap returns"
        assert entry["name"] == "evdb", "the person of that same row"
        # A turn runs through the asking account's own row: both must light the node.
        assert entry["pulse_ids"] == [f"peer_{low.id}", f"peer_{high.id}"]

    async def test_the_same_node_on_every_push(self, pairings, twin, feature_off):
        first = await compute_peer_status()
        second = await compute_peer_status()
        assert first == second
        assert [p["id"] for p in first] == sorted(p["id"] for p in first)

    async def test_a_single_pairing_sends_no_pulse_ids(self, pairings, twin, feature_off):
        peers = await compute_peer_status()
        assert "pulse_ids" not in _entry(peers, pairings["plain"])
        assert "pulse_ids" not in _entry(peers, pairings["named"])

    async def test_the_verified_name_of_any_row_and_the_latest_request_count(
        self, pairings, twin, feature_off,
    ):
        entry = _group_entry(await compute_peer_status(), twin["low"], twin["high"])
        # The lower row was told no name; the second row was.
        assert entry["instance_name"] == "Instance C"
        # The lower row was last seen two days ago, the second two minutes ago.
        assert entry["last_seen_at"] == twin["high"].last_seen_at.isoformat()
        assert entry["reachable"] is True

    @pytest.mark.parametrize("newer", ["first", "second"])
    async def test_the_most_recent_check_is_the_one_shown(
        self, pairings, twin, feature_on, memory_store, newer,
    ):
        low, high, now = twin["low"], twin["high"], time.time()
        await memory_store.save(
            low.id, PING_PIN_MISMATCH, now - (0 if newer == "first" else 200))
        await memory_store.save(
            high.id, PING_REACHABLE, now - (0 if newer == "second" else 200))
        entry = _group_entry(await compute_peer_status(), low, high)
        if newer == "first":
            assert (entry["reachability"], entry["reachability_reason"]) == (
                "unreachable", "pin_mismatch")
        else:
            assert (entry["reachability"], entry["reachability_reason"]) == ("reachable", None)
        assert entry["recently_active"] is True, "the second row was seen two minutes ago"

    async def test_one_checked_row_is_enough(self, pairings, twin, feature_on, memory_store):
        await memory_store.save(twin["high"].id, PING_REACHABLE, time.time())
        entry = _group_entry(await compute_peer_status(), twin["low"], twin["high"])
        assert entry["reachability"] == "reachable"
        assert entry["checked_at"] is not None

    async def test_the_group_stays_content_free(self, pairings, twin, feature_on):
        rendered = repr(await compute_peer_status())
        assert "ef" * 32 not in rendered and "twin.example.org" not in rendered


# =============================================================================
# When a probe happens
# =============================================================================


class TestProbeTriggers:
    @pytest.fixture
    def order(self, monkeypatch):
        calls: list[str] = []

        async def _probe():
            calls.append("probe")

        async def _push():
            calls.append("push")

        monkeypatch.setattr(reach, "probe_due_peers", _probe)
        monkeypatch.setattr(kiosk_data, "refresh_and_push_peer_status", _push)
        return calls

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_the_tick_probes_first_then_pushes(self, order, feature_on):
        await probe_and_push_peer_status()
        assert order == ["probe", "push"]

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_feature_off_the_tick_is_the_old_refresh(self, order, feature_off):
        await probe_and_push_peer_status()
        assert order == ["push"]

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_a_connecting_display_probes_in_the_background(self, order, feature_on):
        """Scheduled, not awaited: the connect path returns at once."""
        schedule_peer_probe_on_connect()
        assert order == [], "nothing ran inline"
        await asyncio.gather(*list(kiosk_data._peer_probe_tasks))
        await asyncio.sleep(0)   # let the done-callback drop the reference
        assert order == ["probe", "push"]
        assert kiosk_data._peer_probe_tasks == set()

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_feature_off_a_connecting_display_schedules_nothing(self, order, feature_off):
        schedule_peer_probe_on_connect()
        await asyncio.sleep(0)
        assert order == []
        assert kiosk_data._peer_probe_tasks == set()

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_no_wall_display_no_probe(self, order, feature_on, monkeypatch):
        """🛑 Consumer-gated: the refresher tick does nothing while nobody is
        watching, so no ping leaves the instance."""
        import api.lifecycle as lifecycle

        ticks: list = []
        monkeypatch.setattr(
            lifecycle, "_spawn_periodic_task",
            lambda **kw: ticks.append(kw["work"]),
        )
        lifecycle._schedule_kiosk_peer_status_refresh(MagicMock())
        (tick,) = ticks

        monkeypatch.setattr(kiosk, "_kiosk_clients", set())
        await tick()
        assert order == []

        monkeypatch.setattr(kiosk, "_kiosk_clients", {object()})
        await tick()
        assert order == ["probe", "push"]
