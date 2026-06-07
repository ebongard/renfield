"""Generic output-provider registry for room media/control routing.

Design: docs/design/output-providers.md. A *provider* is a source of
controllable room-output targets (a TV, a renderer, a speaker). Two kinds:

  - **MCP-declared** — any MCP server carrying an ``output_provider:`` stanza in
    ``mcp_servers.yaml`` (dlna, samsung, sonos, …). ``McpOutputProvider`` maps the
    four contract methods onto ``mcp.<server>.<tool>`` calls. This is the payoff:
    a new brand is config + a small MCP contract, not backend code.
  - **built-in** — the Renfield device registry + Home Assistant media_player
    domain, wrapped as adapters over ``OutputRoutingService``. Added in the
    aggregation phase where their ``discover()`` is consumed (Phase 4); this
    module currently ships the MCP machinery only.

Every provider speaks the SAME normalized shapes so the aggregator/dispatcher
never special-cases a brand:

    discover()                         -> list[OutputTarget]
    play(target_id, items, mode)       -> PlayResult
    control(target_id, action, value)  -> ControlResult
    status(target_id)                  -> TargetStatus

Capabilities a target/provider may advertise: audio, video, power, transport,
queue (gapless). A provider only implements what it has; missing capability =
missing behaviour.

                build_mcp_output_providers(mcp_manager)
                                │
            ┌───────────────────┴───────────────────┐
   reads MCPServerConfig.output_provider stanzas   {key: McpOutputProvider}
   (capabilities + discover/play/control/status     keyed by server name,
    tool names + optional boot_timeout)             which == the target_type
                                                    value space.

The whole module is inert unless ``OUTPUT_PROVIDERS_ENABLED`` is on — callers gate
on the flag before building the registry. Flag off => legacy routing, untouched.
"""

from __future__ import annotations

import json
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

# The four contract methods a provider stanza must map to tool names. `status`
# may reuse another tool (e.g. samsung's tv_media doubles as play + status).
_CONTRACT_METHODS = ("discover", "play", "control", "status")


class OutputProviderError(RuntimeError):
    """A provider contract call failed (tool error, unreachable, bad shape).

    The aggregator catches this to render a provider as degraded/unreachable
    rather than silently dropping its targets (control-surface fail-visible).
    """


# --- Normalized contract shapes -------------------------------------------


@dataclass(frozen=True)
class OutputTarget:
    """One controllable output discovered from a provider."""

    provider: str            # == target_type: "dlna" | "samsung" | "renfield" | ...
    target_id: str           # provider-scoped id (renderer name / TV host / entity / device id)
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

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {}
        if self.url is not None:
            d["url"] = self.url
        if self.item_ref is not None:
            d["item_ref"] = self.item_ref
        if self.title is not None:
            d["title"] = self.title
        return d


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


# --- MCP envelope helpers --------------------------------------------------


def _extract_payload(result: dict[str, Any]) -> Any:
    """Pull the inner tool JSON out of an MCPManager.execute_tool result.

    execute_tool returns {"success", "message", "data"} where the real tool
    output is a JSON string sitting in data[].text or in message. Mirrors the
    parse in OutputRoutingService.get_available_dlna_renderers.
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


def _normalize_targets(payload: Any, provider_key: str) -> list[OutputTarget]:
    """Map a discover() payload into OutputTargets, tolerant of common shapes.

    Accepts {"targets": [...]}, a bare list, or the legacy per-brand keys
    ({"renderers": [...]}, {"tvs": [...]}). Each item: id from id|target_id|
    host|name, name from name|friendly_name|id.
    """
    items: list[Any]
    if isinstance(payload, list):
        items = payload
    elif isinstance(payload, dict):
        items = (
            payload.get("targets")
            or payload.get("renderers")
            or payload.get("tvs")
            or payload.get("devices")
            or []
        )
    else:
        items = []

    targets: list[OutputTarget] = []
    for it in items:
        if not isinstance(it, dict):
            continue
        # Two shapes: the normalized contract carries an explicit `id` + a `name`
        # display; the legacy per-brand shape ({name, friendly_name}, e.g. dlna
        # renderers) uses `name` AS the identifier with `friendly_name` as display.
        explicit_id = it.get("id") or it.get("target_id") or it.get("host")
        if explicit_id:
            tid = explicit_id
            name = it.get("name") or it.get("friendly_name") or str(tid)
        else:
            tid = it.get("name")  # legacy: name is the identifier
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


# --- MCP-declared provider -------------------------------------------------


@dataclass
class McpOutputProvider:
    """An MCP server carrying an ``output_provider:`` stanza, exposed as a
    provider. Maps the four contract methods onto ``mcp.<server>.<tool>``."""

    key: str                              # the MCP server name (== target_type)
    capabilities: frozenset[str]
    tools: dict[str, str]                 # {"discover": tool, "play": ..., "control": ..., "status": ...}
    boot_timeout: float = 0.0
    mcp_manager: Any = None               # MCPManager; injected at build time

    def has_capability(self, cap: str) -> bool:
        return cap in self.capabilities

    async def _call(self, method: str, arguments: dict[str, Any]) -> Any:
        tool = self.tools.get(method)
        if not tool:
            raise OutputProviderError(
                f"provider '{self.key}' has no '{method}' tool declared"
            )
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
        payload = await self._call("discover", {})
        return _normalize_targets(payload, self.key)

    async def play(
        self, target_id: str, items: list[MediaRef], mode: str = "now"
    ) -> PlayResult:
        payload = await self._call(
            "play",
            {
                "target_id": target_id,
                "items": [m.to_dict() for m in items],
                "mode": mode,
            },
        )
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
        args: dict[str, Any] = {"target_id": target_id, "action": action}
        if value is not None:
            args["value"] = value
        payload = await self._call("control", args)
        if isinstance(payload, dict):
            return ControlResult(
                ok=bool(payload.get("ok", True)), message=payload.get("message", "")
            )
        return ControlResult(ok=True)

    async def status(self, target_id: str) -> TargetStatus:
        payload = await self._call("status", {"target_id": target_id})
        if isinstance(payload, dict):
            return TargetStatus(
                state=str(payload.get("state", "unknown")),
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
        logger.warning(
            f"output_provider '{name}': missing/empty capabilities — skipping"
        )
        return None
    caps = set()
    for c in caps_raw:
        if not isinstance(c, str):
            continue
        if c not in KNOWN_CAPABILITIES:
            logger.warning(f"output_provider '{name}': unknown capability '{c}' (kept)")
        caps.add(c)

    tools: dict[str, str] = {}
    for method in _CONTRACT_METHODS:
        tool = stanza.get(method)
        if isinstance(tool, str) and tool:
            tools[method] = tool
    # discover is mandatory (a provider that can't enumerate targets is useless).
    if "discover" not in tools:
        logger.warning(
            f"output_provider '{name}': no 'discover' tool — skipping"
        )
        return None

    boot_timeout = 0.0
    if CAP_POWER in caps:
        try:
            boot_timeout = float(stanza.get("boot_timeout", 20.0))
        except (TypeError, ValueError):
            boot_timeout = 20.0

    return McpOutputProvider(
        key=name,
        capabilities=frozenset(caps),
        tools=tools,
        boot_timeout=boot_timeout,
        mcp_manager=mcp_manager,
    )


def build_mcp_output_providers(mcp_manager: Any) -> dict[str, McpOutputProvider]:
    """Build {server_name: McpOutputProvider} from every MCP server config that
    carries an ``output_provider:`` stanza. Malformed stanzas are skipped.

    Built-in providers (renfield, homeassistant) are assembled separately in the
    aggregation phase; this returns the MCP-declared providers only.
    """
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
                f"(caps={sorted(provider.capabilities)}, boot_timeout={provider.boot_timeout})"
            )
    return providers
