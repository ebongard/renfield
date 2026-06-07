"""Tests for the generic output-provider registry + config-driven MCP adapter.

The adapter lives entirely in renfield: each stanza maps the normalized contract
methods onto a server's REAL tools via an arg-template, and McpOutputProvider
renders those templates + normalizes responses. No MCP-server changes required.
Pure module (no heavy deps) — imports directly, no stubbing.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ha_glue.services.output_providers import (
    CAP_POWER,
    MediaRef,
    OutputProviderError,
    OutputTarget,
    TargetStatus,
    _extract_payload,
    _normalize_targets,
    _render_args,
    _render_value,
    build_mcp_output_providers,
)


# --- helpers ---------------------------------------------------------------


def _envelope(payload_json: str) -> dict:
    return {"success": True, "message": "", "data": [{"type": "text", "text": payload_json}]}


def _fake_manager(servers: dict) -> SimpleNamespace:
    states = {
        name: SimpleNamespace(config=SimpleNamespace(output_provider=stanza))
        for name, stanza in servers.items()
    }
    return SimpleNamespace(_servers=states, execute_tool=AsyncMock())


# A samsung-style stanza: real tools (tv_discover/tv_media/tv_power/tv_volume)
# with arg-templates that adapt the normalized contract onto them.
_SAMSUNG_STANZA = {
    "capabilities": ["video", "audio", "power", "transport"],
    "boot_timeout": 25,
    "discover": {"tool": "tv_discover", "list_at": "tvs"},
    "play": {"tool": "tv_media", "args": {"action": "play_url", "url": "{url}", "title": "{title}"}},
    "status": {"tool": "tv_media", "args": {"action": "status"}, "state_at": "state"},
    "control": {
        "on": {"tool": "tv_power", "args": {"action": "on"}},
        "off": {"tool": "tv_power", "args": {"action": "off"}},
        "volume": {"tool": "tv_volume", "args": {"level": "{value}"}},
        "mute": {"tool": "tv_volume", "args": {"action": "mute"}},
    },
}
# A minimal contract-native stanza (bare tool names, no arg mapping).
_BARE_STANZA = {"capabilities": ["audio"], "discover": "list_things"}


# --- arg-template rendering ------------------------------------------------


def test_render_value_whole_placeholder_preserves_type():
    assert _render_value("{value}", {"value": 30}) == (30, True)        # int stays int
    assert _render_value("{url}", {"url": "http://x"}) == ("http://x", True)


def test_render_value_missing_whole_placeholder_drops():
    assert _render_value("{title}", {"title": None}) == (None, False)
    assert _render_value("{title}", {}) == (None, False)


def test_render_value_embedded_interpolates():
    assert _render_value("KEY_{action}", {"action": "VOLUP"}) == ("KEY_VOLUP", True)


def test_render_value_literal_passthrough():
    assert _render_value("play_url", {}) == ("play_url", True)
    assert _render_value(5, {}) == (5, True)


def test_render_args_drops_missing_keeps_literals():
    tmpl = {"action": "play_url", "url": "{url}", "title": "{title}"}
    # title missing → dropped; action literal kept; url filled
    assert _render_args(tmpl, {"url": "http://a", "title": None}) == {
        "action": "play_url",
        "url": "http://a",
    }


# --- registry build --------------------------------------------------------


def test_build_registers_stanzas():
    mgr = _fake_manager({"samsung": _SAMSUNG_STANZA, "bare": _BARE_STANZA, "paperless": None})
    providers = build_mcp_output_providers(mgr)
    assert set(providers) == {"samsung", "bare"}
    s = providers["samsung"]
    assert s.capabilities == frozenset({"video", "audio", "power", "transport"})
    assert s.boot_timeout == 25.0
    assert set(s.control_maps) == {"on", "off", "volume", "mute"}
    assert s.play_map.tool == "tv_media"


def test_build_skips_malformed():
    mgr = _fake_manager(
        {
            "no_caps": {"discover": "x"},
            "empty_caps": {"capabilities": [], "discover": "x"},
            "no_discover": {"capabilities": ["audio"], "play": {"tool": "p"}},
            "bad_discover": {"capabilities": ["audio"], "discover": {"no_tool": 1}},
            "good": _BARE_STANZA,
        }
    )
    assert set(build_mcp_output_providers(mgr)) == {"good"}


def test_build_keeps_unknown_capability():
    mgr = _fake_manager({"weird": {"capabilities": ["audio", "hologram"], "discover": "d"}})
    assert "hologram" in build_mcp_output_providers(mgr)["weird"].capabilities


def test_build_empty_manager():
    assert build_mcp_output_providers(SimpleNamespace(_servers={})) == {}


# --- payload extraction + target normalization -----------------------------


def test_extract_payload_from_data_text():
    assert _extract_payload(_envelope('{"a": 1}')) == {"a": 1}


def test_extract_payload_from_message():
    assert _extract_payload({"success": True, "message": '{"b": 2}', "data": []}) == {"b": 2}


@pytest.mark.parametrize(
    "payload",
    [
        {"tvs": [{"id": "192.168.1.47", "name": "Living Room"}]},
        {"targets": [{"id": "192.168.1.47", "name": "Living Room"}]},
        [{"host": "192.168.1.47", "name": "Living Room"}],
    ],
)
def test_normalize_targets_shapes(payload):
    targets = _normalize_targets(payload, "samsung", list_at="tvs")
    assert targets == [OutputTarget(provider="samsung", target_id="192.168.1.47", name="Living Room")]


def test_normalize_legacy_name_is_id():
    targets = _normalize_targets({"renderers": [{"name": "rid", "friendly_name": "Wohnzimmer"}]}, "dlna")
    assert targets == [OutputTarget(provider="dlna", target_id="rid", name="Wohnzimmer")]


# --- contract calls adapt onto real tools ----------------------------------


@pytest.mark.asyncio
async def test_discover_uses_mapped_tool_and_list_at():
    mgr = _fake_manager({"samsung": _SAMSUNG_STANZA})
    p = build_mcp_output_providers(mgr)["samsung"]
    mgr.execute_tool.return_value = _envelope('{"tvs": [{"id": "tv1", "name": "TV"}]}')
    targets = await p.discover()
    mgr.execute_tool.assert_awaited_once_with("mcp.samsung.tv_discover", {})
    assert targets == [OutputTarget(provider="samsung", target_id="tv1", name="TV")]


@pytest.mark.asyncio
async def test_play_renders_native_args():
    mgr = _fake_manager({"samsung": _SAMSUNG_STANZA})
    p = build_mcp_output_providers(mgr)["samsung"]
    mgr.execute_tool.return_value = _envelope('{"ok": true, "state": "playing"}')
    res = await p.play("tv1", [MediaRef(url="http://x/a.mp4", title="A")])
    args = mgr.execute_tool.await_args.args
    assert args[0] == "mcp.samsung.tv_media"
    # normalized {target_id, items, mode} → native {action, url, title}; no target_id leaks
    assert args[1] == {"action": "play_url", "url": "http://x/a.mp4", "title": "A"}
    assert res.ok and res.state == "playing"


@pytest.mark.asyncio
async def test_play_drops_missing_title():
    mgr = _fake_manager({"samsung": _SAMSUNG_STANZA})
    p = build_mcp_output_providers(mgr)["samsung"]
    mgr.execute_tool.return_value = _envelope('{"ok": true}')
    await p.play("tv1", [MediaRef(url="http://x/a.mp4")])  # no title
    assert mgr.execute_tool.await_args.args[1] == {"action": "play_url", "url": "http://x/a.mp4"}


@pytest.mark.asyncio
async def test_control_per_action_routing_and_raw_value():
    mgr = _fake_manager({"samsung": _SAMSUNG_STANZA})
    p = build_mcp_output_providers(mgr)["samsung"]
    mgr.execute_tool.return_value = _envelope('{"ok": true}')
    await p.control("tv1", "on")
    assert mgr.execute_tool.await_args.args == ("mcp.samsung.tv_power", {"action": "on"})
    await p.control("tv1", "volume", value=30)
    # whole-placeholder {value} preserves the int
    assert mgr.execute_tool.await_args.args == ("mcp.samsung.tv_volume", {"level": 30})


@pytest.mark.asyncio
async def test_control_unknown_action_raises():
    mgr = _fake_manager({"samsung": _SAMSUNG_STANZA})
    p = build_mcp_output_providers(mgr)["samsung"]
    with pytest.raises(OutputProviderError, match="no control mapping for action 'seek'"):
        await p.control("tv1", "seek")


@pytest.mark.asyncio
async def test_status_reads_state_at():
    mgr = _fake_manager({"samsung": _SAMSUNG_STANZA})
    p = build_mcp_output_providers(mgr)["samsung"]
    mgr.execute_tool.return_value = _envelope('{"state": "off"}')
    st = await p.status("tv1")
    assert st == TargetStatus(state="off") and st.is_off


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
async def test_play_without_mapping_raises():
    mgr = _fake_manager({"bare": _BARE_STANZA})  # discover only
    p = build_mcp_output_providers(mgr)["bare"]
    with pytest.raises(OutputProviderError, match="no 'play' mapping"):
        await p.play("t1", [MediaRef(url="u")])
