"""Generic output-provider registry for room media/control routing.

Design: docs/design/output-providers.md. A *provider* is a source of
controllable room-output targets (a TV, a renderer, a speaker). Two kinds:

  - **MCP-declared** — any MCP server carrying an ``output_provider:`` stanza in
    ``mcp_servers.yaml`` (samsung, sonos, …). ``McpOutputProvider`` adapts the
    four normalized contract methods onto that server's REAL tools.
  - **built-in** — the Renfield device registry + Home Assistant media_player
    domain (added in the aggregation phase). dlna keeps its proven legacy
    gapless-queue dispatch — it is not driven through this generic path.

**The adaptation lives entirely in renfield (config-driven), never in the MCP
servers.** We cannot require third-party servers (sonos, LG, …) to expose
wrapper tools, so the stanza declares, per contract method, the server's real
tool name plus an arg-template that maps the normalized inputs onto that tool's
native parameters. ``McpOutputProvider`` renders those templates and normalizes
the response. A new brand is a stanza, not backend code.

Stanza shape (samsung example)::

    output_provider:
      capabilities: [video, audio, power, transport]
      boot_timeout: 25
      discover: { tool: tv_discover, list_at: tvs }
      play:     { tool: tv_media, args: { action: play_url, url: "{url}", title: "{title}" } }
      status:   { tool: tv_media, args: { action: status }, state_at: state }
      control:
        on:     { tool: tv_power,  args: { action: "on" } }
        off:    { tool: tv_power,  args: { action: "off" } }
        volume: { tool: tv_volume, args: { level: "{value}" } }
        mute:   { tool: tv_volume, args: { action: mute } }

A method may also be a bare tool name (``discover: tv_discover``) when the tool
needs no arg mapping. ``control`` maps per-action (on/off/volume/mute/key/…)
because a brand's control surface is usually split across several tools.

Arg-template rendering:
  - ``"{key}"`` (whole value is one placeholder) substitutes the RAW context
    value (an int stays an int). If that key is absent/None, the arg is DROPPED.
  - ``"pre {key} post"`` (placeholder embedded in text) string-interpolates.
  - any non-string value is passed through literally (e.g. ``action: play_url``).

Contexts: play → {url, item_ref, title, target_id, mode}; control →
{target_id, action, value}; status/discover → {target_id}.

The module is inert unless ``OUTPUT_PROVIDERS_ENABLED`` is on — callers gate on
the flag before building the registry.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from loguru import logger

# --- Capability vocabulary -------------------------------------------------

CAP_AUDIO = "audio"
CAP_VIDEO = "video"
CAP_POWER = "power"
CAP_TRANSPORT = "transport"
CAP_QUEUE = "queue"
KNOWN_CAPABILITIES = frozenset(
    {CAP_AUDIO, CAP_VIDEO, CAP_POWER, CAP_TRANSPORT, CAP_QUEUE}
)

_PLACEHOLDER_RE = re.compile(r"\{(\w+)\}")
_WHOLE_PLACEHOLDER_RE = re.compile(r"^\{(\w+)\}$")


class OutputProviderError(RuntimeError):
    """A provider contract call failed (tool error, unreachable, bad shape).

    The aggregator catches this to render a provider as degraded/unreachable
    rather than silently dropping its targets (control-surface fail-visible).
    """


# --- Normalized contract shapes -------------------------------------------


@dataclass(frozen=True)
class OutputTarget:
    """One controllable output discovered from a provider."""

    provider: str            # == target_type: "samsung" | "dlna" | "renfield" | ...
    target_id: str           # provider-scoped id (TV host / renderer name / entity / device id)
    name: str
    capabilities: frozenset[str] = frozenset()
    room_hint: str | None = None   # advisory only (e.g. a renderer named "Wohnzimmer")
    reachable: bool = True         # False => degraded entry (provider couldn't be probed)

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "target_id": self.target_id,
            "name": self.name,
            "capabilities": sorted(self.capabilities),
            "room_hint": self.room_hint,
            "reachable": self.reachable,
        }


@dataclass(frozen=True)
class MediaRef:
    """A single playable item. The contract always carries a list of these
    (len 1 for a single track); gapless is a provider-optional `queue` cap."""

    url: str | None = None
    item_ref: str | None = None   # provider-native id (e.g. a Jellyfin/library object id)
    title: str | None = None


@dataclass(frozen=True)
class PlayResult:
    ok: bool
    state: str | None = None
    message: str = ""


@dataclass(frozen=True)
class ControlResult:
    ok: bool
    message: str = ""


@dataclass(frozen=True)
class TargetStatus:
    state: str               # "playing" | "paused" | "stopped" | "off" | "unknown"
    position: str | None = None
    reachable: bool = True

    @property
    def is_off(self) -> bool:
        return self.state == "off"


@runtime_checkable
class OutputProvider(Protocol):
    """The uniformity seam: built-in and MCP providers look identical here."""

    key: str                 # == target_type value space
    capabilities: frozenset[str]
    boot_timeout: float      # power-on poll cap (seconds); 0 => no power capability

    def has_capability(self, cap: str) -> bool: ...

    async def discover(self, room_id: int | None = None) -> list[OutputTarget]: ...

    async def play(
        self, target_id: str, items: list[MediaRef], mode: str = "now"
    ) -> PlayResult: ...

    async def control(
        self, target_id: str, action: str, value: Any | None = None
    ) -> ControlResult: ...

    async def status(self, target_id: str) -> TargetStatus: ...


# --- arg-template rendering ------------------------------------------------


def _render_value(template: Any, ctx: dict[str, Any]) -> tuple[Any, bool]:
    """Render one arg-template value. Returns (value, include).

    A whole-string placeholder substitutes the raw ctx value (type-preserving);
    a missing/None value means the arg is dropped (include=False). Embedded
    placeholders string-interpolate. Non-strings pass through.
    """
    if not isinstance(template, str):
        return template, True
    whole = _WHOLE_PLACEHOLDER_RE.match(template)
    if whole:
        key = whole.group(1)
        val = ctx.get(key)
        return (None, False) if val is None else (val, True)
    # embedded interpolation; drop only if it references a single missing key
    return _PLACEHOLDER_RE.sub(lambda m: str(ctx.get(m.group(1), "")), template), True


def _render_args(arg_template: dict[str, Any] | None, ctx: dict[str, Any]) -> dict[str, Any]:
    if not arg_template:
        return {}
    out: dict[str, Any] = {}
    for k, v in arg_template.items():
        rendered, include = _render_value(v, ctx)
        if include:
            out[k] = rendered
    return out


# --- MCP envelope helpers --------------------------------------------------


def _extract_payload(result: dict[str, Any]) -> Any:
    """Pull the inner tool JSON out of an MCPManager.execute_tool result.

    execute_tool returns {"success", "message", "data"} where the real tool
    output is a JSON string in data[].text or in message.
    """
    raw_text = ""
    data = result.get("data", [])
    if isinstance(data, list):
        for item in data:
            if isinstance(item, dict) and item.get("type") == "text":
                raw_text = item.get("text", "")
                break
    if not raw_text:
        raw_text = result.get("message", "") or ""
    if not raw_text:
        return None
    try:
        return json.loads(raw_text)
    except (ValueError, TypeError):
        return None


def _normalize_targets(payload: Any, provider_key: str, list_at: str | None = None) -> list[OutputTarget]:
    """Map a discover() payload into OutputTargets, tolerant of common shapes."""
    items: list[Any]
    if isinstance(payload, list):
        items = payload
    elif isinstance(payload, dict):
        if list_at and isinstance(payload.get(list_at), list):
            items = payload[list_at]
        else:
            items = (
                payload.get("targets")
                or payload.get("tvs")
                or payload.get("renderers")
                or payload.get("devices")
                or []
            )
    else:
        items = []

    targets: list[OutputTarget] = []
    for it in items:
        if not isinstance(it, dict):
            continue
        # Explicit id + display name, OR legacy {name, friendly_name} (name is id).
        explicit_id = it.get("id") or it.get("target_id") or it.get("host")
        if explicit_id:
            tid = explicit_id
            name = it.get("name") or it.get("friendly_name") or str(tid)
        else:
            tid = it.get("name")
            name = it.get("friendly_name") or it.get("name") or str(tid)
        if not tid:
            continue
        caps = it.get("capabilities")
        cap_set = (
            frozenset(c for c in caps if isinstance(c, str))
            if isinstance(caps, list)
            else frozenset()
        )
        targets.append(
            OutputTarget(
                provider=provider_key,
                target_id=str(tid),
                name=str(name),
                capabilities=cap_set,
                room_hint=it.get("room_hint"),
            )
        )
    return targets


# --- method-mapping records ------------------------------------------------


@dataclass(frozen=True)
class _MethodMap:
    """One contract method bound to a real tool + arg-template + response hints."""

    tool: str
    args: dict[str, Any] = field(default_factory=dict)
    list_at: str | None = None    # discover: key holding the targets list
    state_at: str | None = None   # status: key holding the state string


def _coerce_method(spec: Any) -> _MethodMap | None:
    """Accept ``"tool_name"`` or ``{tool, args, list_at, state_at}``."""
    if isinstance(spec, str) and spec:
        return _MethodMap(tool=spec)
    if isinstance(spec, dict) and isinstance(spec.get("tool"), str):
        return _MethodMap(
            tool=spec["tool"],
            args=spec.get("args") if isinstance(spec.get("args"), dict) else {},
            list_at=spec.get("list_at"),
            state_at=spec.get("state_at"),
        )
    return None


# --- MCP-declared provider -------------------------------------------------


@dataclass
class McpOutputProvider:
    """An MCP server carrying an ``output_provider:`` stanza. Adapts the four
    normalized contract methods onto the server's real tools (config-driven)."""

    key: str                              # the MCP server name (== target_type)
    capabilities: frozenset[str]
    discover_map: _MethodMap
    play_map: _MethodMap | None = None
    status_map: _MethodMap | None = None
    control_maps: dict[str, _MethodMap] = field(default_factory=dict)  # action -> map
    boot_timeout: float = 0.0
    mcp_manager: Any = None

    def has_capability(self, cap: str) -> bool:
        return cap in self.capabilities

    async def _call(self, tool: str, arguments: dict[str, Any]) -> Any:
        if self.mcp_manager is None:
            raise OutputProviderError(f"provider '{self.key}' has no mcp_manager")
        namespaced = f"mcp.{self.key}.{tool}"
        try:
            result = await self.mcp_manager.execute_tool(namespaced, arguments)
        except Exception as e:  # transport / session error
            raise OutputProviderError(f"{namespaced} raised: {e}") from e
        if not isinstance(result, dict) or not result.get("success"):
            msg = (result or {}).get("message", "tool failed") if isinstance(result, dict) else "tool failed"
            raise OutputProviderError(f"{namespaced} failed: {msg}")
        return _extract_payload(result)

    async def discover(self, room_id: int | None = None) -> list[OutputTarget]:
        args = _render_args(self.discover_map.args, {"target_id": None})
        payload = await self._call(self.discover_map.tool, args)
        return _normalize_targets(payload, self.key, self.discover_map.list_at)

    async def play(
        self, target_id: str, items: list[MediaRef], mode: str = "now"
    ) -> PlayResult:
        if self.play_map is None:
            raise OutputProviderError(f"provider '{self.key}' has no 'play' mapping")
        first = items[0] if items else MediaRef()
        ctx = {
            "target_id": target_id,
            "mode": mode,
            "url": first.url,
            "item_ref": first.item_ref,
            "title": first.title,
        }
        payload = await self._call(self.play_map.tool, _render_args(self.play_map.args, ctx))
        if isinstance(payload, dict):
            return PlayResult(
                ok=bool(payload.get("ok", True)),
                state=payload.get("state"),
                message=payload.get("message", ""),
            )
        return PlayResult(ok=True)

    async def control(
        self, target_id: str, action: str, value: Any | None = None
    ) -> ControlResult:
        m = self.control_maps.get(action)
        if m is None:
            raise OutputProviderError(
                f"provider '{self.key}' has no control mapping for action '{action}'"
            )
        ctx = {"target_id": target_id, "action": action, "value": value}
        payload = await self._call(m.tool, _render_args(m.args, ctx))
        if isinstance(payload, dict):
            return ControlResult(
                ok=bool(payload.get("ok", True)), message=payload.get("message", "")
            )
        return ControlResult(ok=True)

    async def status(self, target_id: str) -> TargetStatus:
        if self.status_map is None:
            raise OutputProviderError(f"provider '{self.key}' has no 'status' mapping")
        ctx = {"target_id": target_id}
        payload = await self._call(self.status_map.tool, _render_args(self.status_map.args, ctx))
        if isinstance(payload, dict):
            state_key = self.status_map.state_at or "state"
            return TargetStatus(
                state=str(payload.get(state_key, payload.get("state", "unknown"))),
                position=payload.get("position"),
                reachable=bool(payload.get("reachable", True)),
            )
        return TargetStatus(state="unknown")


# --- Registry assembly -----------------------------------------------------


def _parse_stanza(name: str, stanza: dict[str, Any], mcp_manager: Any) -> McpOutputProvider | None:
    """Validate one ``output_provider:`` stanza into a provider, or None if
    malformed (logged + skipped — never raises into the registry build)."""
    caps_raw = stanza.get("capabilities")
    if not isinstance(caps_raw, list) or not caps_raw:
        logger.warning(f"output_provider '{name}': missing/empty capabilities — skipping")
        return None
    caps = set()
    for c in caps_raw:
        if not isinstance(c, str):
            continue
        if c not in KNOWN_CAPABILITIES:
            logger.warning(f"output_provider '{name}': unknown capability '{c}' (kept)")
        caps.add(c)

    discover_map = _coerce_method(stanza.get("discover"))
    if discover_map is None:
        logger.warning(f"output_provider '{name}': missing/invalid 'discover' — skipping")
        return None

    play_map = _coerce_method(stanza.get("play"))
    status_map = _coerce_method(stanza.get("status"))

    control_maps: dict[str, _MethodMap] = {}
    control_raw = stanza.get("control")
    if isinstance(control_raw, dict):
        for action, spec in control_raw.items():
            # YAML 1.1 footgun: bare on/off keys parse as booleans. Coerce back so
            # an unquoted third-party stanza still maps to the "on"/"off" actions.
            if action is True:
                action = "on"
            elif action is False:
                action = "off"
            m = _coerce_method(spec)
            if m is not None:
                control_maps[str(action)] = m
            else:
                logger.warning(f"output_provider '{name}': bad control mapping for '{action}' (skipped)")

    boot_timeout = 0.0
    if CAP_POWER in caps:
        # power needs an 'on' control mapping to be actionable; warn if absent.
        if "on" not in control_maps:
            logger.warning(f"output_provider '{name}': power capability but no control.on mapping")
        try:
            boot_timeout = float(stanza.get("boot_timeout", 20.0))
        except (TypeError, ValueError):
            boot_timeout = 20.0

    return McpOutputProvider(
        key=name,
        capabilities=frozenset(caps),
        discover_map=discover_map,
        play_map=play_map,
        status_map=status_map,
        control_maps=control_maps,
        boot_timeout=boot_timeout,
        mcp_manager=mcp_manager,
    )


def build_mcp_output_providers(mcp_manager: Any) -> dict[str, McpOutputProvider]:
    """Build {server_name: McpOutputProvider} from every MCP server config that
    carries an ``output_provider:`` stanza. Malformed stanzas are skipped."""
    providers: dict[str, McpOutputProvider] = {}
    servers = getattr(mcp_manager, "_servers", None)
    if not servers:
        return providers
    for name, state in servers.items():
        config = getattr(state, "config", None)
        stanza = getattr(config, "output_provider", None) if config else None
        if not isinstance(stanza, dict):
            continue
        provider = _parse_stanza(name, stanza, mcp_manager)
        if provider is not None:
            providers[name] = provider
            logger.info(
                f"output provider '{name}' registered "
                f"(caps={sorted(provider.capabilities)}, boot_timeout={provider.boot_timeout}, "
                f"controls={sorted(provider.control_maps)})"
            )
    return providers
