"""Tests for the generic output-provider registry (Phase 1 foundation).

Covers stanza parsing / registry build, capability handling, and the
McpOutputProvider contract-method normalization against a mocked MCPManager.
The module is pure (no heavy deps), so no import stubbing is needed.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ha_glue.services.output_providers import (
    CAP_POWER,
    McpOutputProvider,
    OutputProviderError,
    OutputTarget,
    MediaRef,
    TargetStatus,
    _extract_payload,
    _normalize_targets,
    build_mcp_output_providers,
)


# --- helpers ---------------------------------------------------------------


def _envelope(payload_json: str) -> dict:
    """Shape MCPManager.execute_tool returns: success + data[].text JSON."""
    return {"success": True, "message": "", "data": [{"type": "text", "text": payload_json}]}


def _fake_manager(servers: dict) -> SimpleNamespace:
    """servers: {name: output_provider_stanza_or_None}."""
    states = {
        name: SimpleNamespace(config=SimpleNamespace(output_provider=stanza))
        for name, stanza in servers.items()
    }
    return SimpleNamespace(_servers=states, execute_tool=AsyncMock())


_DLNA_STANZA = {
    "capabilities": ["audio", "video", "transport"],
    "discover": "list_renderers",
    "play": "play_tracks",
    "control": "media_control",
    "status": "get_status",
}
_SAMSUNG_STANZA = {
    "capabilities": ["video", "audio", "power", "transport"],
    "discover": "tv_discover",
    "play": "tv_media",
    "control": "tv_power",
    "status": "tv_media",
}


# --- registry build --------------------------------------------------------


def test_build_registers_mcp_stanzas():
    mgr = _fake_manager({"dlna": _DLNA_STANZA, "samsung": _SAMSUNG_STANZA, "paperless": None})
    providers = build_mcp_output_providers(mgr)
    assert set(providers) == {"dlna", "samsung"}  # paperless (no stanza) excluded
    assert providers["dlna"].capabilities == frozenset({"audio", "video", "transport"})
    assert providers["dlna"].boot_timeout == 0.0  # no power cap
    # samsung has power → default boot_timeout
    assert providers["samsung"].has_capability(CAP_POWER)
    assert providers["samsung"].boot_timeout == 20.0


def test_build_skips_malformed_stanzas():
    mgr = _fake_manager(
        {
            "no_caps": {"discover": "x"},                       # missing capabilities
            "empty_caps": {"capabilities": [], "discover": "x"},  # empty capabilities
            "no_discover": {"capabilities": ["audio"], "play": "p"},  # no discover tool
            "good": _DLNA_STANZA,
        }
    )
    providers = build_mcp_output_providers(mgr)
    assert set(providers) == {"good"}


def test_build_keeps_unknown_capability():
    mgr = _fake_manager({"weird": {"capabilities": ["audio", "hologram"], "discover": "d"}})
    providers = build_mcp_output_providers(mgr)
    assert "hologram" in providers["weird"].capabilities  # kept (warned, not dropped)


def test_build_custom_boot_timeout():
    stanza = dict(_SAMSUNG_STANZA, boot_timeout=35)
    mgr = _fake_manager({"samsung": stanza})
    assert build_mcp_output_providers(mgr)["samsung"].boot_timeout == 35.0


def test_build_empty_manager():
    assert build_mcp_output_providers(SimpleNamespace(_servers={})) == {}


# --- payload extraction ----------------------------------------------------


def test_extract_payload_from_data_text():
    assert _extract_payload(_envelope('{"a": 1}')) == {"a": 1}


def test_extract_payload_from_message():
    assert _extract_payload({"success": True, "message": '{"b": 2}', "data": []}) == {"b": 2}


def test_extract_payload_unparseable_returns_none():
    assert _extract_payload({"success": True, "message": "not json", "data": []}) is None


# --- target normalization --------------------------------------------------


@pytest.mark.parametrize(
    "payload",
    [
        {"targets": [{"id": "tv1", "name": "Living Room", "capabilities": ["video", "power"]}]},
        [{"id": "tv1", "name": "Living Room", "capabilities": ["video", "power"]}],
        {"tvs": [{"id": "tv1", "name": "Living Room", "capabilities": ["video", "power"]}]},
        {"renderers": [{"name": "tv1", "friendly_name": "Living Room", "capabilities": ["video", "power"]}]},
    ],
)
def test_normalize_targets_shapes(payload):
    targets = _normalize_targets(payload, "samsung")
    assert len(targets) == 1
    t = targets[0]
    assert t.provider == "samsung"
    assert t.target_id == "tv1"
    assert t.name == "Living Room"
    assert t.capabilities == frozenset({"video", "power"})


def test_normalize_targets_skips_idless_items():
    targets = _normalize_targets({"targets": [{"name": "no id here, but name is the id"}, {}]}, "dlna")
    # first item: name becomes the id; second: empty dict skipped
    assert [t.target_id for t in targets] == ["no id here, but name is the id"]


# --- McpOutputProvider contract calls --------------------------------------


@pytest.mark.asyncio
async def test_discover_calls_namespaced_tool_and_normalizes():
    mgr = _fake_manager({"samsung": _SAMSUNG_STANZA})
    p = build_mcp_output_providers(mgr)["samsung"]
    mgr.execute_tool.return_value = _envelope('{"targets": [{"id": "tv1", "name": "TV"}]}')
    targets = await p.discover()
    mgr.execute_tool.assert_awaited_once_with("mcp.samsung.tv_discover", {})
    assert targets == [OutputTarget(provider="samsung", target_id="tv1", name="TV")]


@pytest.mark.asyncio
async def test_discover_raises_on_tool_failure():
    mgr = _fake_manager({"samsung": _SAMSUNG_STANZA})
    p = build_mcp_output_providers(mgr)["samsung"]
    mgr.execute_tool.return_value = {"success": False, "message": "TV asleep", "data": []}
    with pytest.raises(OutputProviderError, match="TV asleep"):
        await p.discover()


@pytest.mark.asyncio
async def test_discover_raises_on_transport_exception():
    mgr = _fake_manager({"samsung": _SAMSUNG_STANZA})
    p = build_mcp_output_providers(mgr)["samsung"]
    mgr.execute_tool.side_effect = RuntimeError("session terminated")
    with pytest.raises(OutputProviderError, match="session terminated"):
        await p.discover()


@pytest.mark.asyncio
async def test_play_sends_normalized_items_and_parses_result():
    mgr = _fake_manager({"dlna": _DLNA_STANZA})
    p = build_mcp_output_providers(mgr)["dlna"]
    mgr.execute_tool.return_value = _envelope('{"ok": true, "state": "playing"}')
    res = await p.play("Wohnzimmer", [MediaRef(url="http://x/a.mp3", title="A")], mode="now")
    args = mgr.execute_tool.await_args.args
    assert args[0] == "mcp.dlna.play_tracks"
    assert args[1] == {
        "target_id": "Wohnzimmer",
        "items": [{"url": "http://x/a.mp3", "title": "A"}],
        "mode": "now",
    }
    assert res.ok and res.state == "playing"


@pytest.mark.asyncio
async def test_control_omits_value_when_none():
    mgr = _fake_manager({"samsung": _SAMSUNG_STANZA})
    p = build_mcp_output_providers(mgr)["samsung"]
    mgr.execute_tool.return_value = _envelope('{"ok": true}')
    await p.control("tv1", "on")
    assert mgr.execute_tool.await_args.args[1] == {"target_id": "tv1", "action": "on"}
    await p.control("tv1", "volume", value=30)
    assert mgr.execute_tool.await_args.args[1] == {"target_id": "tv1", "action": "volume", "value": 30}


@pytest.mark.asyncio
async def test_status_parses_off_state():
    mgr = _fake_manager({"samsung": _SAMSUNG_STANZA})
    p = build_mcp_output_providers(mgr)["samsung"]
    mgr.execute_tool.return_value = _envelope('{"state": "off"}')
    st = await p.status("tv1")
    assert st == TargetStatus(state="off")
    assert st.is_off


@pytest.mark.asyncio
async def test_missing_status_tool_raises():
    # A stanza with no status tool — control method missing from tools map.
    stanza = {"capabilities": ["audio"], "discover": "d"}
    mgr = _fake_manager({"x": stanza})
    p = build_mcp_output_providers(mgr)["x"]
    with pytest.raises(OutputProviderError, match="no 'status' tool"):
        await p.status("t1")
