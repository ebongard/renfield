"""Backend unit tests for the kiosk push hub (Phase 1a).

Covers:
  * ``extract_subsystems_used`` — mcp-vs-internal mapping, the internal
    allowlist, dedup, order-preservation, the cap, and the empty case.
  * ``broadcast_kiosk_event`` — fire-and-forget: an empty registry is a no-op,
    a broken socket is pruned (not raised), a good socket receives the event.
  * ``build_kiosk_snapshot`` — the snapshot dict shape / content-free contract
    with all sources stubbed out (no DB / no app state required).

Run on the .159 build box (CI is non-functional): see
``memory/reference_test_runner_159.md``.
"""
from __future__ import annotations

import asyncio
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

import api.websocket.kiosk_handler as kiosk
from api.websocket.kiosk_data import (
    INTERNAL_SUBSYSTEM_LABELS,
    _MAX_SUBSYSTEMS_PER_TURN,
    broadcast_turn_activity,
    extract_subsystems_used,
)
from api.websocket.kiosk_handler import broadcast_kiosk_event, build_kiosk_snapshot


class _FakeWS:
    def __init__(self):
        self.sent: list[dict] = []

    async def send_json(self, msg):
        self.sent.append(msg)


async def _drain_fanout():
    """broadcast_kiosk_event enqueues; a single consumer drains the queue. Wait
    for every queued event to be fully processed before asserting."""
    if kiosk._event_queue is not None:
        await kiosk._event_queue.join()


@pytest.fixture(autouse=True)
def _clear_clients():
    # Reset the lazily-created queue+consumer so each test's event loop gets a
    # fresh pipeline (an asyncio.Queue/Task is bound to the loop it was made in).
    kiosk._kiosk_clients.clear()
    kiosk._event_queue = None
    kiosk._consumer_task = None
    kiosk._consumer_loop = None
    kiosk._active_chat_turns = 0
    yield
    if kiosk._consumer_task is not None:
        kiosk._consumer_task.cancel()
    kiosk._kiosk_clients.clear()
    kiosk._event_queue = None
    kiosk._consumer_task = None
    kiosk._consumer_loop = None


# --------------------------------------------------------------------------
# extract_subsystems_used
# --------------------------------------------------------------------------


@pytest.mark.backend
@pytest.mark.unit
def test_extract_mcp_tool_maps_to_server():
    assert extract_subsystems_used(
        [("mcp.homeassistant.turn_on", {})]
    ) == ["homeassistant"]


@pytest.mark.backend
@pytest.mark.unit
def test_extract_internal_tool_uses_allowlist():
    assert extract_subsystems_used([("internal.knowledge_search", {})]) == ["knowledge"]
    assert extract_subsystems_used([("internal.list_my_memories", {})]) == ["knowledge"]
    assert extract_subsystems_used([("internal.device_controls", {})]) == ["homeassistant"]
    assert extract_subsystems_used([("internal.announce_in_room", {})]) == ["homeassistant"]
    assert extract_subsystems_used([("internal.presence_history", {})]) == ["presence"]
    assert extract_subsystems_used([("internal.play_radio", {})]) == ["media"]
    assert extract_subsystems_used([("internal.weather_widget", {})]) == ["weather"]


@pytest.mark.backend
@pytest.mark.unit
def test_extract_unknown_internal_tool_skipped():
    assert extract_subsystems_used([("internal.something_new", {})]) == []
    # Pure Gen-UI formatting tools touch no subsystem → no pulse.
    assert extract_subsystems_used([("internal.render_table", {})]) == []
    assert extract_subsystems_used([("internal.render_list", {})]) == []


# Real MCP servers that `internal.*` tools bridge to — they already render as
# tool-ring nodes, so they need NO synthetic frontend node. Every OTHER mapped
# value is internal-only and MUST have a matching frontend synthetic node.
_REAL_MCP_SERVER_TARGETS = {"homeassistant", "weather"}


def _frontend_synthetic_node_ids() -> set[str] | None:
    """Extract the `id`s from the frontend INTERNAL_SUBSYSTEM_NODES so the
    coupling guard ENFORCES the real cross-file invariant (not two same-file
    literals). Returns None when the frontend tree isn't present (isolated
    backend test runs on .159 only rsync `src/backend` + `tests`) → the test
    skips there rather than falsely failing."""
    fe = (
        Path(__file__).resolve().parents[2]
        / "src/frontend/src/components/kiosk/useKioskModel.ts"
    )
    if not fe.exists():
        return None
    block = re.search(
        r"INTERNAL_SUBSYSTEM_NODES\b.*?=\s*\[(.*?)\];", fe.read_text(encoding="utf-8"), re.S
    )
    assert block, "INTERNAL_SUBSYSTEM_NODES not found in useKioskModel.ts"
    return set(re.findall(r"id:\s*'([^']+)'", block.group(1)))


@pytest.mark.backend
@pytest.mark.unit
def test_internal_only_subsystem_ids_match_frontend_synthetic_nodes():
    """Coupling guard: every subsystem id an internal tool can pulse is EITHER a
    real MCP server OR one of the internal-only ids the kiosk renders a synthetic
    node for. The frontend set is READ FROM THE ACTUAL SOURCE (useKioskModel.ts),
    so adding an internal-only mapping without a matching frontend node — the
    drift that would silently light nothing on the wall — fails this test."""
    frontend = _frontend_synthetic_node_ids()
    if frontend is None:
        pytest.skip("frontend tree not present (isolated backend run)")
    values = set(INTERNAL_SUBSYSTEM_LABELS.values())
    internal_only = values - _REAL_MCP_SERVER_TARGETS
    assert internal_only == frontend, (
        f"backend internal-only subsystem ids {internal_only} != frontend synthetic "
        f"nodes {frontend} — sync components/kiosk/useKioskModel.ts INTERNAL_SUBSYSTEM_NODES"
    )
    # Belt and braces: every mapped value resolves to a rendered node.
    assert values <= _REAL_MCP_SERVER_TARGETS | frontend


@pytest.mark.backend
@pytest.mark.unit
def test_extract_dedup_preserves_first_seen_order():
    results = [
        ("mcp.homeassistant.turn_on", {}),
        ("internal.device_action", {}),  # also -> homeassistant (dupe)
        ("mcp.weather.get_weather", {}),
        ("internal.knowledge_search", {}),  # -> knowledge
    ]
    assert extract_subsystems_used(results) == [
        "homeassistant",
        "weather",
        "knowledge",
    ]


@pytest.mark.backend
@pytest.mark.unit
def test_extract_caps_at_five_distinct_subsystems():
    results = [(f"mcp.server{i}.tool", {}) for i in range(8)]
    out = extract_subsystems_used(results)
    assert len(out) == _MAX_SUBSYSTEMS_PER_TURN == 5
    assert out == ["server0", "server1", "server2", "server3", "server4"]


@pytest.mark.backend
@pytest.mark.unit
def test_extract_empty_and_malformed_are_safe():
    assert extract_subsystems_used([]) == []
    # Malformed / non-tool entries are ignored, not raised.
    assert extract_subsystems_used(
        [(), ("", {}), ("plain_intent", {}), ("mcp.", {}), ("mcp.only", {})]
    ) == []


@pytest.mark.backend
@pytest.mark.unit
def test_allowlist_values_are_known_subsystem_ids():
    # Guard: every allowlisted internal tool maps to a non-empty id.
    assert all(v for v in INTERNAL_SUBSYSTEM_LABELS.values())


# --------------------------------------------------------------------------
# broadcast_kiosk_event
# --------------------------------------------------------------------------


@pytest.mark.backend
@pytest.mark.asyncio
async def test_broadcast_empty_registry_is_noop():
    # No clients registered — must return without error.
    await broadcast_kiosk_event({"type": "satellite_state"})
    assert kiosk._kiosk_clients == set()


@pytest.mark.backend
@pytest.mark.asyncio
async def test_broadcast_delivers_to_connected_clients():
    a, b = _FakeWS(), _FakeWS()
    kiosk._kiosk_clients.update({a, b})
    event = {"type": "turn_activity", "role": "smart_home", "subsystems": ["homeassistant"]}
    await broadcast_kiosk_event(event)
    await _drain_fanout()
    assert a.sent == [event]
    assert b.sent == [event]


@pytest.mark.backend
@pytest.mark.asyncio
async def test_note_chat_turn_active_edges_and_floor():
    """The core-activity counter broadcasts a chat_activity delta ONLY on the
    0↔1 edge (concurrent turns don't spam), and never goes negative."""
    kiosk._active_chat_turns = 0
    client = _FakeWS()
    kiosk._kiosk_clients.add(client)

    await kiosk.note_chat_turn_active(True)   # 0 -> 1 : edge, broadcast active
    await kiosk.note_chat_turn_active(True)   # 1 -> 2 : no edge, silent
    await _drain_fanout()
    assert kiosk._active_chat_turns == 2
    assert [e["active"] for e in client.sent] == [True]  # only the 0->1 edge

    await kiosk.note_chat_turn_active(False)  # 2 -> 1 : no edge, silent
    await kiosk.note_chat_turn_active(False)  # 1 -> 0 : edge, broadcast inactive
    await _drain_fanout()
    assert kiosk._active_chat_turns == 0
    assert [e["active"] for e in client.sent] == [True, False]

    # floor at 0 — a stray decrement (double-clear) can't go negative
    await kiosk.note_chat_turn_active(False)
    await _drain_fanout()
    assert kiosk._active_chat_turns == 0
    assert len(client.sent) == 2  # no extra broadcast
    kiosk._active_chat_turns = 0


@pytest.mark.backend
@pytest.mark.asyncio
async def test_broadcast_turn_activity_pushes_pulse_and_noops_when_empty():
    client = _FakeWS()
    kiosk._kiosk_clients.add(client)

    # nothing to show (no role AND no subsystems) → no push
    await broadcast_turn_activity(None, [], None)
    await _drain_fanout()
    assert client.sent == []

    # a voice turn that ran a tool → one content-free turn_activity pulse
    await broadcast_turn_activity(None, ["homeassistant"], True)
    await _drain_fanout()
    assert len(client.sent) == 1
    evt = client.sent[0]
    assert evt["type"] == "turn_activity"
    assert evt["subsystems"] == ["homeassistant"]
    assert evt["ok"] is True
    assert "at" in evt  # timestamp stamped
    # role-only turn (no tool) still pulses the role ring
    await broadcast_turn_activity("smart_home", [], None)
    await _drain_fanout()
    assert client.sent[-1]["role"] == "smart_home"


@pytest.mark.backend
@pytest.mark.asyncio
async def test_broadcast_prunes_broken_socket_not_raises():
    class _BrokenWS(_FakeWS):
        async def send_json(self, msg):
            raise RuntimeError("closed")

    good, bad = _FakeWS(), _BrokenWS()
    kiosk._kiosk_clients.update({good, bad})
    await broadcast_kiosk_event({"type": "satellite_state"})
    await _drain_fanout()
    assert bad not in kiosk._kiosk_clients  # pruned
    assert good in kiosk._kiosk_clients
    assert len(good.sent) == 1  # good socket still delivered


@pytest.mark.backend
@pytest.mark.asyncio
async def test_broadcast_prunes_stalled_socket(monkeypatch):
    """A socket whose send_json HANGS (backpressure — never raises) is pruned via
    the wait_for timeout, and a healthy peer is still delivered. This is the core
    of the non-blocking fix; without the timeout the consumer would hang here."""
    monkeypatch.setattr(kiosk, "_SEND_TIMEOUT_SECONDS", 0.05)

    class _StallWS(_FakeWS):
        async def send_json(self, msg):
            await asyncio.sleep(10)  # never completes within the timeout

    good, stalled = _FakeWS(), _StallWS()
    kiosk._kiosk_clients.update({good, stalled})
    await broadcast_kiosk_event({"type": "satellite_state"})
    await _drain_fanout()
    assert stalled not in kiosk._kiosk_clients  # pruned on timeout
    assert good in kiosk._kiosk_clients
    assert len(good.sent) == 1


@pytest.mark.backend
@pytest.mark.asyncio
async def test_hydrate_before_register(monkeypatch):
    """The socket must receive its snapshot BEFORE joining _kiosk_clients — else
    the consumer could send a delta on the same socket concurrently with the
    snapshot send, or the client would apply a delta before hydrating."""
    from unittest.mock import AsyncMock

    from fastapi import WebSocketDisconnect

    monkeypatch.setattr(
        kiosk, "authenticate_websocket",
        AsyncMock(return_value={"authenticated": True, "auth_skipped": True}),
    )
    monkeypatch.setattr(
        kiosk, "build_kiosk_snapshot", AsyncMock(return_value={"type": "snapshot"})
    )

    class _RecordingWS:
        def __init__(self):
            self.app = _FakeApp()
            self.sent: list[dict] = []
            self.registered_at_send: bool | None = None

        async def accept(self):
            pass

        async def send_json(self, msg):
            # Capture whether this socket was broadcast-eligible when hydrated.
            self.registered_at_send = self in kiosk._kiosk_clients
            self.sent.append(msg)

        async def receive_text(self):
            raise WebSocketDisconnect()

        async def close(self, **kwargs):
            pass

    ws = _RecordingWS()
    await kiosk.kiosk_live(ws, token=None)

    assert ws.sent == [{"type": "snapshot"}]
    assert ws.registered_at_send is False  # snapshot sent while UNREGISTERED
    assert ws not in kiosk._kiosk_clients  # disconnect cleaned up


# --------------------------------------------------------------------------
# build_kiosk_snapshot (shape + content-free)
# --------------------------------------------------------------------------


class _FakeState:
    def __init__(self):
        self.mcp_manager = None
        self.agent_router = None


class _FakeApp:
    def __init__(self):
        self.state = _FakeState()


@pytest.mark.backend
@pytest.mark.asyncio
async def test_snapshot_shape_with_all_sources_down(monkeypatch):
    """Every source failing must still yield a fully-shaped, content-free
    snapshot (a fresh tab always hydrates)."""
    # Neutralize the DB-backed sections so no database is required.
    async def _boom(*a, **k):
        raise RuntimeError("no db in unit test")

    # Force the tool-health / activity / peers / weather / now-playing sources
    # to their degraded empty branches by making their imports/queries fail.
    monkeypatch.setattr(kiosk, "AsyncSessionLocal", _boom, raising=True)
    # Peers moved to the shared kiosk_data.compute_peer_status (its own session,
    # not kiosk.AsyncSessionLocal), so neutralize it explicitly for the no-db path.
    import api.websocket.kiosk_data as kiosk_data
    monkeypatch.setattr(kiosk_data, "compute_peer_status", _boom, raising=True)

    snap = await build_kiosk_snapshot(_FakeApp())

    assert snap["type"] == "snapshot"
    assert isinstance(snap["at"], str)
    # All expected top-level keys present with safe empty defaults.
    for key in (
        "satellites",
        "presence",
        "mcp",
        "tool_health",
        "roles",
        "activity",
        "peers",
        "weather",
        "now_playing",
    ):
        assert key in snap
    assert snap["presence"] == {"rooms": [], "people_present": 0, "occupied_rooms": 0}
    assert snap["tool_health"] == []
    assert snap["activity"] == []
    assert snap["peers"] == []
    assert snap["weather"] is None
    assert snap["now_playing"] == []


@pytest.mark.backend
@pytest.mark.asyncio
async def test_snapshot_roles_content_free(monkeypatch):
    """Roles ride straight off the router; the shape is names + reach lists
    only (no message content)."""
    async def _boom(*a, **k):
        raise RuntimeError("no db")

    monkeypatch.setattr(kiosk, "AsyncSessionLocal", _boom, raising=True)

    class _Role:
        name = "smart_home"
        description = {"de": "Haussteuerung", "en": "Smart home"}
        mcp_servers = ["homeassistant"]
        internal_tools = ["internal.device_action"]
        has_agent_loop = True

    class _Router:
        roles = {"smart_home": _Role()}

    app = _FakeApp()
    app.state.agent_router = _Router()

    snap = await build_kiosk_snapshot(app)
    assert snap["roles"] == [
        {
            "name": "smart_home",
            "description": {"de": "Haussteuerung", "en": "Smart home"},
            "mcp_servers": ["homeassistant"],
            "internal_tools": ["internal.device_action"],
            "has_agent_loop": True,
        }
    ]


# --------------------------------------------------------------------------
# build_kiosk_snapshot — tool-health staleness / min-sample guards
# --------------------------------------------------------------------------
#
# The tool-outcome counters are cumulative over ALL time (no window/decay), so
# without these guards a handful of days-old failures pins a kiosk node red
# forever even after the server recovered (the xidra search+simba regression).
# The snapshot must only emit a functional-health verdict for tools that were
# exercised RECENTLY and have enough samples; everything else is omitted so the
# node falls back to its (green) connectivity/get_status health.


class _Stat:
    """Minimal stand-in for a ToolOutcomeStat row."""

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

    async def execute(self, *a, **k):  # every other DB-backed section degrades
        raise RuntimeError("no db in unit test")


def _patch_stats(monkeypatch, stats):
    """Route build_kiosk_snapshot's tool-health query to `stats`, and make all
    OTHER db-backed sections degrade to their empty branches (no real DB)."""
    from services.tool_outcome_service import ToolOutcomeService

    monkeypatch.setattr(kiosk, "AsyncSessionLocal", lambda: _NoopSession(), raising=True)
    monkeypatch.setattr(
        ToolOutcomeService, "list_stats", AsyncMock(return_value=stats), raising=True
    )


@pytest.mark.backend
@pytest.mark.asyncio
async def test_tool_health_excludes_stale_and_thin(monkeypatch):
    """A recent, well-sampled failing tool is reported degraded; a STALE failing
    tool and a THIN (too few samples) failing tool are omitted entirely."""
    now = datetime.now(UTC).replace(tzinfo=None)
    recent = now - timedelta(hours=1)
    stale = now - timedelta(hours=kiosk._TOOL_HEALTH_RECENT_HOURS + 5)
    stats = [
        # recent + enough samples + low rate → reported, degraded
        _Stat("mcp.a.tool", success=1, failure=5, last_used_at=recent),
        # stale (old) + low rate → omitted (no current signal)
        _Stat("mcp.b.tool", success=0, failure=5, last_used_at=stale),
        # recent but too few samples → omitted (noise)
        _Stat("mcp.c.tool", success=0, failure=2, last_used_at=recent),
    ]
    _patch_stats(monkeypatch, stats)

    snap = await build_kiosk_snapshot(_FakeApp())
    th = {t["tool_name"]: t for t in snap["tool_health"]}

    assert set(th) == {"mcp.a.tool"}
    assert th["mcp.a.tool"]["degraded"] is True
    assert th["mcp.a.tool"]["total"] == 6


@pytest.mark.backend
@pytest.mark.asyncio
async def test_tool_health_recent_healthy_reported_not_degraded(monkeypatch):
    """A recent, well-sampled tool with a good success rate is reported and NOT
    degraded (the guards don't hide healthy live signal)."""
    recent = datetime.now(UTC).replace(tzinfo=None) - timedelta(minutes=30)
    _patch_stats(monkeypatch, [_Stat("mcp.a.tool", success=9, failure=1, last_used_at=recent)])

    snap = await build_kiosk_snapshot(_FakeApp())
    th = {t["tool_name"]: t for t in snap["tool_health"]}

    assert th["mcp.a.tool"]["degraded"] is False
    assert th["mcp.a.tool"]["success_rate"] == 0.9


@pytest.mark.backend
@pytest.mark.asyncio
async def test_tool_health_all_stale_yields_empty(monkeypatch):
    """When every failing tool is stale (the xidra search+simba case), the
    payload is empty so the nodes fall back to connectivity health (green)."""
    stale = datetime.now(UTC).replace(tzinfo=None) - timedelta(days=3)
    stats = [
        _Stat("mcp.search.web_search", success=1, failure=5, last_used_at=stale),
        _Stat("mcp.simba.upload_documents", success=0, failure=5, last_used_at=stale),
        _Stat("mcp.simba.check_connection", success=0, failure=3, last_used_at=stale),
    ]
    _patch_stats(monkeypatch, stats)

    snap = await build_kiosk_snapshot(_FakeApp())
    assert snap["tool_health"] == []
