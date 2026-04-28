"""
MCP Client — Connects to external MCP servers and exposes their tools.

Manages multiple MCP server connections with:
- YAML-based configuration with env-var substitution
- Eager connection at startup with background reconnect
- Exponential backoff for failed reconnection attempts
- Tool discovery and namespacing (mcp.<server>.<tool>)
- Tool execution with timeout handling
- Input validation against JSON schema
- Response truncation for large outputs
- Per-server rate limiting
"""

import asyncio
import json
import os
import random
import re
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AsyncExitStack, suppress
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

# Sink signature: chat_handler passes this down so federation ProgressChunks
# reach the user's WebSocket as they happen. `None` means nobody's listening
# (the default path — non-chat callers don't need progress relay).
ProgressSink = Callable[[dict], Awaitable[None]]

import anyio
import httpx
import yaml
from loguru import logger

from services.mcp_streaming import FinalResult, ProgressChunk
from utils.config import settings

# Optional jsonschema import (graceful degradation if not installed)
try:
    import jsonschema
    JSONSCHEMA_AVAILABLE = True
except ImportError:
    JSONSCHEMA_AVAILABLE = False
    logger.warning("jsonschema not installed — MCP input validation disabled")


# Suppress noisy JSONRPC parse errors from MCP stdio client.
# MCP servers (especially npm packages) often write non-JSONRPC content
# to stdout (telemetry banners, debug logs, tool schemas). The MCP SDK
# logs each line as ERROR with full traceback. Downgrade to DEBUG.
import logging as _logging


class _MCPStdioNoiseFilter(_logging.Filter):
    """Demote 'Failed to parse JSONRPC message' from ERROR to DEBUG."""

    def filter(self, record: _logging.LogRecord) -> bool:
        if "Failed to parse JSONRPC message" in record.getMessage():
            record.levelno = _logging.DEBUG
            record.levelname = "DEBUG"
        return True


_mcp_stdio_logger = _logging.getLogger("mcp.client.stdio")
_mcp_stdio_logger.addFilter(_MCPStdioNoiseFilter())


# === Constants ===
MAX_RESPONSE_SIZE = settings.mcp_max_response_size
DEFAULT_RATE_LIMIT_PER_MINUTE = 60  # Default rate limit per MCP server

# Exponential Backoff constants for reconnection
BACKOFF_INITIAL_DELAY = 1.0  # Initial delay in seconds
BACKOFF_MAX_DELAY = 300.0  # Maximum delay (5 minutes)
BACKOFF_MULTIPLIER = 2.0  # Exponential multiplier
BACKOFF_JITTER = 0.1  # Random jitter factor (10%)


class ExponentialBackoff:
    """
    Tracks exponential backoff state for reconnection attempts.

    Implements:
    - Exponential delay increase with configurable multiplier
    - Maximum delay cap
    - Random jitter to prevent thundering herd
    - Reset on successful connection
    """

    def __init__(
        self,
        initial_delay: float = BACKOFF_INITIAL_DELAY,
        max_delay: float = BACKOFF_MAX_DELAY,
        multiplier: float = BACKOFF_MULTIPLIER,
        jitter: float = BACKOFF_JITTER,
    ):
        self.initial_delay = initial_delay
        self.max_delay = max_delay
        self.multiplier = multiplier
        self.jitter = jitter

        self._attempt = 0
        self._next_retry_time: float = 0.0

    @property
    def attempt_count(self) -> int:
        """Number of failed attempts."""
        return self._attempt

    def record_failure(self) -> float:
        """
        Record a failed connection attempt.

        Returns:
            Delay in seconds before next retry.
        """
        self._attempt += 1

        # Calculate exponential delay
        delay = self.initial_delay * (self.multiplier ** (self._attempt - 1))
        delay = min(delay, self.max_delay)

        # Add random jitter
        jitter_range = delay * self.jitter
        delay += random.uniform(-jitter_range, jitter_range)
        delay = max(0.0, delay)

        self._next_retry_time = time.monotonic() + delay
        return delay

    def record_success(self) -> None:
        """Reset backoff state on successful connection."""
        self._attempt = 0
        self._next_retry_time = 0.0

    def should_retry(self) -> bool:
        """Check if enough time has passed for the next retry."""
        return time.monotonic() >= self._next_retry_time

    def time_until_retry(self) -> float:
        """Return seconds until next retry is allowed (0 if ready)."""
        remaining = self._next_retry_time - time.monotonic()
        return max(0.0, remaining)


# === Geocode HTTP client singleton ===
_geocode_client: Any = None


def _get_geocode_client() -> Any:
    global _geocode_client
    if _geocode_client is None:
        _geocode_client = httpx.AsyncClient(timeout=settings.geocode_http_timeout)
    return _geocode_client


async def close_geocode_client() -> None:
    """Close the geocode HTTP client singleton. Call on shutdown."""
    global _geocode_client
    if _geocode_client is not None:
        await _geocode_client.aclose()
        _geocode_client = None


class MCPValidationError(Exception):
    """Raised when MCP tool input validation fails."""
    pass


class MCPRateLimitError(Exception):
    """Raised when MCP rate limit is exceeded."""
    pass


class TokenBucketRateLimiter:
    """
    Simple token bucket rate limiter for MCP calls.

    Thread-safe via asyncio lock.
    """

    def __init__(self, rate_per_minute: int = DEFAULT_RATE_LIMIT_PER_MINUTE):
        self.rate = rate_per_minute
        self.tokens = float(rate_per_minute)
        self.max_tokens = float(rate_per_minute)
        self.last_update = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self) -> bool:
        """
        Try to acquire a token. Returns True if successful, False if rate limited.
        """
        async with self._lock:
            now = time.monotonic()
            elapsed = now - self.last_update
            self.last_update = now

            # Refill tokens based on elapsed time
            self.tokens = min(
                self.max_tokens,
                self.tokens + elapsed * (self.rate / 60.0)
            )

            if self.tokens >= 1.0:
                self.tokens -= 1.0
                return True
            return False

    def reset(self) -> None:
        """Reset the rate limiter to full capacity."""
        self.tokens = self.max_tokens
        self.last_update = time.monotonic()


def _coerce_arguments(arguments: dict, input_schema: dict) -> dict:
    """
    Coerce LLM-produced flat arguments to match nested JSON schemas.

    Handles two common mismatches:
    1. Flat string → nested object: LLM produces {"location": "Berlin"} but schema
       expects {"location": {"city": "Berlin"}}. Wraps using first string property.
    2. Location string → lat/lon: LLM produces {"location": "Berlin"} but schema
       expects {"latitude": number, "longitude": number}. Drops the location key
       (geocoding is handled async by _geocode_location_arguments).

    Returns:
        Coerced copy of arguments (original is not mutated).
    """
    if not input_schema:
        return arguments

    properties = input_schema.get("properties", {})
    if not properties:
        return arguments

    coerced = dict(arguments)

    # Unwrap LLM "request" wrapper: {"request": {...}} → {...}
    # Many LLMs wrap all parameters in a "request" key that doesn't exist in the schema.
    if (
        list(coerced.keys()) == ["request"]
        and isinstance(coerced["request"], dict)
        and "request" not in properties
    ):
        logger.info(f"🔄 Unwrapping 'request' wrapper: {list(coerced['request'].keys())}")
        coerced = coerced["request"]

    # Strip invalid values: null for non-nullable fields, wrong types
    required = set(input_schema.get("required", []))
    _type_map = {"string": str, "integer": (int,), "number": (int, float), "boolean": (bool,),
                 "object": (dict,), "array": (list,)}
    for key, value in list(coerced.items()):
        prop_schema = properties.get(key, {})
        prop_type = prop_schema.get("type", "")

        if value is None and key not in required:
            # Strip null for optional non-nullable fields
            if prop_type and prop_type != "null" and not (
                isinstance(prop_type, list) and "null" in prop_type
            ):
                logger.info(f"🔄 Stripping null value for optional field '{key}'")
                del coerced[key]
        elif value is not None and prop_type in _type_map:
            # Strip values with wrong type (e.g. {} for a string field)
            # Skip "object" types here — Phase 2 below handles string→object coercion
            expected = _type_map[prop_type]
            if not isinstance(value, expected) and prop_type != "object":
                if key not in required:
                    logger.info(f"🔄 Stripping '{key}': expected {prop_type}, got {type(value).__name__}")
                    del coerced[key]
                elif "default" in prop_schema:
                    logger.info(f"🔄 Replacing '{key}' (wrong type {type(value).__name__}) with default: {prop_schema['default']}")
                    coerced[key] = prop_schema["default"]

    for key, value in list(coerced.items()):
        if not isinstance(value, str):
            continue
        prop_schema = properties.get(key, {})
        if prop_schema.get("type") == "object":
            # Value is a string but schema expects an object — wrap it
            nested_props = prop_schema.get("properties", {})
            target_field = None
            for nested_key, nested_schema in nested_props.items():
                if nested_schema.get("type") == "string":
                    target_field = nested_key
                    break
            if target_field:
                logger.info(
                    f"🔄 Coercing '{key}': \"{value}\" → {{\"{target_field}\": \"{value}\"}}"
                )
                coerced[key] = {target_field: value}
        elif "enum" in prop_schema:
            # Value doesn't match enum exactly — try case-insensitive match
            enum_values = prop_schema["enum"]
            if value not in enum_values:
                lower_map = {str(v).lower(): v for v in enum_values}
                matched = lower_map.get(value.lower())
                if not matched:
                    # Try prefix match: "movie" → "Movies"
                    for ev in enum_values:
                        if str(ev).lower().startswith(value.lower()) or value.lower().startswith(str(ev).lower()):
                            matched = ev
                            break
                if not matched:
                    # Fall back to schema default if available
                    default = prop_schema.get("default")
                    if default is not None:
                        matched = default
                        logger.info(
                            f"🔄 Enum '{key}': \"{value}\" not in {enum_values}, using default \"{default}\""
                        )
                if matched:
                    logger.info(
                        f"🔄 Coercing enum '{key}': \"{value}\" → \"{matched}\""
                    )
                    coerced[key] = matched
        elif key == "location" and key not in properties:
            # LLM produced a "location" key but schema has no such property.
            # This is kept for _geocode_location_arguments to handle.
            pass

    # Fill missing required fields from schema defaults or constraints
    for key in required:
        if key in coerced:
            continue
        prop_schema = properties.get(key, {})
        default = prop_schema.get("default")
        if default is not None:
            logger.info(f"🔄 Filling missing required field '{key}' with schema default: {default}")
            coerced[key] = default
        elif prop_schema.get("type") == "integer":
            # Infer from constraints: prefer minimum, else 25 as sensible page size
            minimum = prop_schema.get("minimum")
            if minimum is not None:
                inferred = max(minimum, 25) if prop_schema.get("maximum", 0) >= 25 else minimum
                logger.info(f"🔄 Filling missing required field '{key}' with inferred default: {inferred}")
                coerced[key] = inferred
        elif prop_schema.get("type") == "string" and "enum" in prop_schema:
            first_enum = prop_schema["enum"][0]
            logger.info(f"🔄 Filling missing required field '{key}' with first enum value: \"{first_enum}\"")
            coerced[key] = first_enum

    return coerced


async def _geocode_location_arguments(arguments: dict, input_schema: dict) -> dict:
    """
    Auto-geocode when LLM provides a location name but tool needs lat/lon.

    The LLM often extracts {"location": "Berlin"} for weather tools, but tools
    like Open-Meteo require {"latitude": 52.52, "longitude": 13.405}.
    This function detects the mismatch and resolves it via the Open-Meteo
    geocoding API (free, no key required).

    Returns:
        Arguments with location resolved to latitude/longitude if applicable.
    """
    if not input_schema:
        return arguments

    properties = input_schema.get("properties", {})

    # Check: does schema require lat/lon but LLM provided a location string?
    has_lat = "latitude" in properties
    has_lon = "longitude" in properties
    location_value = arguments.get("location")

    if not (has_lat and has_lon and isinstance(location_value, str)):
        return arguments

    # Geocode using Open-Meteo API (free, no key) with retry
    client = _get_geocode_client()

    for attempt in range(2):
        try:
            resp = await client.get(
                "https://geocoding-api.open-meteo.com/v1/search",
                params={"name": location_value, "count": 5, "language": "de"},
            )
            resp.raise_for_status()
            data = resp.json()
            results = data.get("results", [])
            if results:
                # Pick the result with the highest population to avoid
                # matching small towns (e.g. York, NE instead of New York City)
                geo = max(results, key=lambda r: r.get("population", 0))

                # Sanity check: if the best result's name doesn't match the
                # query well (e.g. "York" for "New York"), retry with " City".
                # A partial substring match ("york" in "new york") is not
                # sufficient — the result name should START with or EQUAL
                # the query, or vice versa.
                query_lower = location_value.lower().strip()
                geo_name_lower = geo.get("name", "").lower()
                is_good_match = (
                    query_lower == geo_name_lower
                    or geo_name_lower.startswith(query_lower)
                    or query_lower.startswith(geo_name_lower + " ")  # e.g. "new york" starts with "new york"
                )
                if not is_good_match:
                    retry_resp = await client.get(
                        "https://geocoding-api.open-meteo.com/v1/search",
                        params={"name": f"{location_value} City", "count": 3, "language": "de"},
                    )
                    retry_resp.raise_for_status()
                    retry_results = retry_resp.json().get("results", [])
                    if retry_results:
                        retry_best = max(retry_results, key=lambda r: r.get("population", 0))
                        if retry_best.get("population", 0) > geo.get("population", 0):
                            geo = retry_best
                    coerced = {k: v for k, v in arguments.items() if k != "location"}
                    coerced["latitude"] = geo["latitude"]
                    coerced["longitude"] = geo["longitude"]

                    # Default: include current weather + basic daily forecast if nothing specified
                    if "current_weather" in properties and "current_weather" not in coerced:
                        coerced["current_weather"] = True
                    if "daily" in properties and "daily" not in coerced:
                        coerced["daily"] = [
                            "temperature_2m_max", "temperature_2m_min",
                            "precipitation_sum", "weather_code",
                        ]
                    if "timezone" in properties and "timezone" not in coerced:
                        coerced["timezone"] = geo.get("timezone", "auto")
                    if "forecast_days" in properties and "forecast_days" not in coerced:
                        coerced["forecast_days"] = 3

                    logger.info(
                        f"🌍 Geocoded '{location_value}' → "
                        f"lat={geo['latitude']}, lon={geo['longitude']} "
                        f"({geo.get('name', '')}, {geo.get('country', '')})"
                    )
                    return coerced
                else:
                    logger.warning(f"🌍 Geocoding failed: no results for '{location_value}'")
                    break  # No point retrying if API returned empty results
        except Exception as e:
            logger.warning(
                f"🌍 Geocoding error for '{location_value}' "
                f"(attempt {attempt + 1}/2): {type(e).__name__}: {e}"
            )
            if attempt == 0:
                await asyncio.sleep(0.5)  # Brief pause before retry

    return arguments


def _validate_tool_input(arguments: dict, input_schema: dict) -> None:
    """
    Validate tool arguments against JSON schema.

    Args:
        arguments: The arguments to validate
        input_schema: JSON schema from tool definition

    Raises:
        MCPValidationError: If validation fails
    """
    if not JSONSCHEMA_AVAILABLE:
        return  # Skip validation if jsonschema not installed

    if not input_schema:
        return  # No schema defined, skip validation

    try:
        jsonschema.validate(instance=arguments, schema=input_schema)
    except jsonschema.ValidationError as e:
        raise MCPValidationError(f"Input validation failed: {e.message}")
    except jsonschema.SchemaError as e:
        logger.warning(f"Invalid MCP tool schema: {e.message}")
        # Don't fail on schema errors — the MCP server may handle it


def _slim_array_items(items: list) -> list:
    """
    Strip large text fields from array items to fit more results.
    Keeps titles, dates, IDs — removes full-text content.
    """
    # Fields that are typically large and redundant for summaries
    large_fields = {"content", "body", "text", "description", "full_text", "raw_text"}
    slimmed = []
    for item in items:
        if isinstance(item, dict):
            slim = {}
            for k, v in item.items():
                if k.lower() in large_fields and isinstance(v, str) and len(v) > 200:
                    slim[k] = v[:200] + "..."
                else:
                    slim[k] = v
            slimmed.append(slim)
        else:
            slimmed.append(item)
    return slimmed


def _truncate_response(text: str, max_size: int = MAX_RESPONSE_SIZE) -> str:
    """
    Truncate response text to max_size bytes.
    For JSON with arrays: slims large text fields, then keeps complete items.
    """
    text_bytes = text.encode('utf-8')
    if len(text_bytes) <= max_size:
        return text

    # Try smart JSON truncation: keep complete items in arrays
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            # Find the largest array field (e.g. "results", "documents", etc.)
            array_key = None
            array_val = None
            for k, v in data.items():
                if isinstance(v, list) and len(v) > 0 and (array_val is None or len(v) > len(array_val)):
                    array_key = k
                    array_val = v

            if array_key and array_val:
                total = len(array_val)
                # Step 1: Slim large text fields (e.g. OCR content)
                slimmed = _slim_array_items(array_val)

                # Step 2: Check if slimmed version fits entirely
                data[array_key] = slimmed
                full_str = json.dumps(data, ensure_ascii=False)
                if len(full_str.encode('utf-8')) <= max_size:
                    return full_str

                # Step 3: Binary search for max items that fit
                lo, hi = 1, len(slimmed)
                best = 1
                while lo <= hi:
                    mid = (lo + hi) // 2
                    trial = dict(data)
                    trial[array_key] = slimmed[:mid]
                    trial_str = json.dumps(trial, ensure_ascii=False)
                    if len(trial_str.encode('utf-8')) <= max_size - 100:
                        best = mid
                        lo = mid + 1
                    else:
                        hi = mid - 1
                data[array_key] = slimmed[:best]
                # Embed the truncation note INSIDE the JSON envelope so
                # downstream callers that do `json.loads(message)` (e.g.
                # paperless_metadata_extractor._list_via_mcp) still
                # succeed. A human-readable suffix appended after the
                # closing brace was producing
                # `MCP tool ... returned non-JSON message` warnings and
                # a silent fall-through to "empty taxonomy" extraction
                # — which masked the size issue for months.
                if best < total:
                    data["_truncation"] = {
                        "showing": best,
                        "total": total,
                        "note": f"Showing {best} of {total} results",
                    }
                return json.dumps(data, ensure_ascii=False)
    except (json.JSONDecodeError, TypeError, KeyError):
        pass

    # Fallback: byte-level truncation. For non-JSON MCP responses (rare),
    # the human-readable suffix is fine — there's nothing structured to
    # parse. The size in the message is computed live so it stays
    # accurate when MAX_RESPONSE_SIZE changes.
    truncated = text_bytes[:max_size - 50].decode('utf-8', errors='ignore')
    return truncated + (
        f"\n\n[... Response truncated (exceeded {max_size // 1024}KB limit)]"
    )


# Regex pattern to detect and redact credentials in MCP responses.
# Matches common query-string patterns like api_key=..., token=..., apikey=..., etc.
_CREDENTIAL_PATTERN = re.compile(
    r'([?&](?:api[_-]?key|token|secret|password|auth|access[_-]?token|bearer)'
    r'=)([^&"\s\]},]+)',
    re.IGNORECASE,
)


def _sanitize_credentials(text: str) -> str:
    """
    Redact credential values from MCP tool response text.

    Replaces values in URL query parameters like api_key=XXXX with api_key=***REDACTED***.
    This prevents API keys from leaking to the frontend/LLM.
    """
    return _CREDENTIAL_PATTERN.sub(r'\1***REDACTED***', text)


def _detect_inner_error(message: str) -> bool:
    """
    Detect application-level errors inside MCP response text.

    Some MCP servers (e.g. n8n-mcp) wrap all responses in a JSON envelope
    like ``{"success": false, "error": "..."}`` while the MCP protocol-level
    ``isError`` flag stays False.  This function parses the message to detect
    such inner failures.

    Also detects ``{"error": "..."}`` without a ``success`` field — a common
    pattern in simple MCP servers (e.g. DLNA).

    Returns True if the inner response indicates an error, False otherwise.
    """
    try:
        data = json.loads(message)
        if isinstance(data, dict):
            if "success" in data:
                return data["success"] is False
            if "error" in data and isinstance(data["error"], str):
                return True
    except (json.JSONDecodeError, TypeError, ValueError):
        pass
    return False


# Exceptions that indicate the MCP transport itself is broken (session
# died, stream closed, server bounced). These — and only these — trigger
# a single auto-reconnect-and-retry inside execute_tool(). MCP application
# errors (mcp.shared.exceptions.McpError, validation, schema mismatch)
# must NOT be in this list: reconnecting wouldn't help and would tear
# down a perfectly healthy session over a malformed argument.
_SESSION_DEAD_EXCEPTIONS: tuple[type[BaseException], ...] = (
    anyio.ClosedResourceError,
    anyio.BrokenResourceError,
    anyio.EndOfStream,
    httpx.RemoteProtocolError,
    httpx.ReadError,
    httpx.WriteError,
    httpx.ConnectError,
    ConnectionError,
)


class MCPTransportType(str, Enum):
    STREAMABLE_HTTP = "streamable_http"
    SSE = "sse"
    STDIO = "stdio"

    # Federation peers (F3c) — not a real MCP transport; a virtual one.
    # State rows with this transport are looked up at request time to find
    # the underlying PeerUser row, and execute_tool_streaming routes them
    # through FederationQueryAsker instead of session.call_tool. The
    # only tool such servers expose is `query_brain`. Registry lives in
    # services/peer_mcp_registry.py; it syncs peers into _servers at
    # startup (and on pair/unpair events).
    FEDERATION = "federation"


class MCPPermissionError(Exception):
    """Raised when user lacks permission for an MCP tool."""
    pass


@dataclass
class MCPServerConfig:
    """Configuration for a single MCP server."""
    name: str
    url: str | None = None
    transport: MCPTransportType = MCPTransportType.STREAMABLE_HTTP
    auth_token_env: str | None = None
    headers: dict[str, str] = field(default_factory=dict)
    command: str | None = None
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)  # Extra env vars for stdio subprocess
    enabled: bool = True
    refresh_interval: int = 300
    examples: dict[str, list[str]] = field(default_factory=dict)  # {"de": [...], "en": [...]}
    example_intent: str | None = None  # Override intent name used in prompt examples
    prompt_tools: list[str] | None = None  # Tool names to register from server (None = all)
    tool_hints: dict[str, str] = field(default_factory=dict)  # {tool_name: "hint to append to description"}
    permissions: list[str] = field(default_factory=list)  # e.g. ["mcp.calendar.read", "mcp.calendar.manage"]
    tool_permissions: dict[str, str] = field(default_factory=dict)  # e.g. {"list_events": "mcp.calendar.read"}
    notifications: dict | None = None  # {"enabled": true, "poll_interval": 900, "tool": "get_pending_notifications"}
    streaming: bool = False  # Opt-in: server emits progress notifications via MCP progress_callback.
                              # When true, execute_tool_streaming wires an asyncio.Queue to capture
                              # notifications and yield ProgressChunks. First consumer: federation
                              # query_brain (F3). Non-streaming servers ignore this flag — the
                              # progress queue stays empty and only the final result is yielded.

    # Federation-transport only (F3c): the local PeerUser.id this virtual
    # server represents. execute_tool_streaming looks up the peer row at
    # request time (so revocation is picked up without needing a registry
    # refresh). Unset for non-federation servers.
    peer_user_id: int | None = None


@dataclass
class MCPToolInfo:
    """Metadata for a single tool discovered from an MCP server."""
    server_name: str
    original_name: str
    namespaced_name: str  # "mcp.<server>.<tool>"
    description: str
    input_schema: dict = field(default_factory=dict)


@dataclass
class MCPServerState:
    """Runtime state for a connected MCP server."""
    config: MCPServerConfig
    connected: bool = False
    tools: list[MCPToolInfo] = field(default_factory=list)
    all_discovered_tools: list[MCPToolInfo] = field(default_factory=list)  # Unfiltered full list
    last_error: str | None = None
    session: Any = None  # mcp.ClientSession
    exit_stack: AsyncExitStack | None = None
    rate_limiter: TokenBucketRateLimiter | None = None
    backoff: ExponentialBackoff | None = None  # Reconnection backoff tracker
    # Serializes concurrent reconnect attempts per server. Initialized
    # synchronously via default_factory so concurrent first-callers can't
    # each construct their own Lock and end up reconnecting in parallel
    # (which would race exit_stack teardown against re-entry).
    reconnect_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    last_successful_call: float = 0.0  # monotonic timestamp; 0 = never


def _substitute_env_vars(value: str) -> str:
    """
    Replace ${VAR} and ${VAR:-default} patterns with environment variable values.

    Raises ValueError if a required variable (no default) is not set.
    """
    def _replace(match):
        var_name = match.group(1)
        default = match.group(3)  # None if no default specified
        env_value = os.environ.get(var_name)
        if env_value is not None:
            return env_value
        if default is not None:
            return default
        # Required var not set — return empty string (will be logged)
        return ""

    return re.sub(r"\$\{(\w+)(:-(.*?))?\}", _replace, value)


def _parse_notifications(raw: dict | None) -> dict | None:
    """Parse and validate the notifications section from YAML config."""
    if not raw or not isinstance(raw, dict):
        return None
    enabled = _resolve_value(raw.get("enabled", False))
    if not enabled:
        return None
    return {
        "enabled": True,
        "poll_interval": int(raw.get("poll_interval", 900)),
        "tool": raw.get("tool", "get_pending_notifications"),
        "lookahead_minutes": int(raw.get("lookahead_minutes", 45)),
    }


def _resolve_value(value: Any) -> Any:
    """Resolve env vars in a value. Handles strings and booleans."""
    if isinstance(value, str):
        resolved = _substitute_env_vars(value)
        # Handle boolean-like strings
        if resolved.lower() in ("true", "1", "yes"):
            return True
        if resolved.lower() in ("false", "0", "no"):
            return False
        return resolved
    return value


class MCPManager:
    """
    Manages connections to multiple MCP servers.

    Lifecycle:
    1. load_config() — Parse YAML, resolve env vars
    2. connect_all() — Connect to all enabled servers in parallel
    3. start_refresh_loop() — Background health check + tool refresh
    4. execute_tool() / get_all_tools() — Runtime usage
    5. shutdown() — Close all sessions
    """

    def __init__(self):
        self._servers: dict[str, MCPServerState] = {}
        self._tool_index: dict[str, MCPToolInfo] = {}  # namespaced_name -> MCPToolInfo
        self._tool_overrides: dict[str, list[str] | None] = {}  # DB overrides per server
        self._refresh_task: asyncio.Task | None = None

    def load_config(self, path: str) -> None:
        """Load MCP server configuration from YAML file."""
        # Inject Docker secrets into os.environ so ${VAR} substitution
        # in YAML config can resolve API keys stored in /run/secrets/.
        # Only sets vars that are not already present in the environment.
        secrets_dir = Path("/run/secrets")
        if secrets_dir.is_dir():
            for secret_file in secrets_dir.iterdir():
                if secret_file.is_file() and not secret_file.name.startswith("."):
                    env_name = secret_file.name.upper()
                    if env_name not in os.environ:
                        try:
                            os.environ[env_name] = secret_file.read_text().strip()
                        except Exception:
                            pass

        config_path = Path(path)
        if not config_path.exists():
            logger.warning(f"MCP config file not found: {path}")
            return

        try:
            with open(config_path) as f:
                raw = yaml.safe_load(f)
        except Exception as e:
            logger.error(f"Failed to parse MCP config: {e}")
            return

        if not raw or not raw.get("servers"):
            logger.info("MCP config loaded but no servers defined")
            return

        for entry in raw["servers"]:
            try:
                transport_str = _resolve_value(entry.get("transport", "streamable_http"))
                transport = MCPTransportType(transport_str)
                # FEDERATION is registry-managed (paired peers) — refuse
                # YAML definitions so an admin can't accidentally register
                # a federation entry without going through the pairing
                # handshake, which would have no PeerUser row and crash
                # at request time.
                if transport == MCPTransportType.FEDERATION:
                    raise ValueError(
                        f"MCP server '{entry['name']}': transport='federation' is "
                        f"registry-managed (paired peers only), not YAML-configured. "
                        f"Remove this server from mcp_servers.yaml."
                    )
                config = MCPServerConfig(
                    name=entry["name"],
                    url=_resolve_value(entry.get("url")),
                    transport=transport,
                    auth_token_env=entry.get("auth_token_env"),
                    headers={
                        k: _resolve_value(v)
                        for k, v in entry.get("headers", {}).items()
                    },
                    command=_resolve_value(entry.get("command")),
                    args=[_resolve_value(a) for a in entry.get("args", [])],
                    env={
                        k: str(_resolve_value(v))
                        for k, v in entry.get("env", {}).items()
                    },
                    enabled=_resolve_value(entry.get("enabled", True)),
                    refresh_interval=int(
                        _resolve_value(entry.get("refresh_interval", 300))
                    ),
                    examples={
                        lang: exs
                        for lang, exs in entry.get("examples", {}).items()
                        if isinstance(exs, list)
                    },
                    example_intent=entry.get("example_intent"),
                    prompt_tools=entry.get("prompt_tools"),
                    tool_hints=entry.get("tool_hints", {}),
                    permissions=entry.get("permissions", []),
                    tool_permissions=entry.get("tool_permissions", {}),
                    notifications=_parse_notifications(entry.get("notifications")),
                    streaming=bool(_resolve_value(entry.get("streaming", False))),
                )

                if not config.enabled:
                    logger.info(f"MCP server '{config.name}' is disabled, skipping")
                    continue

                # Initialize server state with rate limiter and backoff tracker
                rate_limiter = TokenBucketRateLimiter(
                    rate_per_minute=DEFAULT_RATE_LIMIT_PER_MINUTE
                )
                backoff = ExponentialBackoff()
                self._servers[config.name] = MCPServerState(
                    config=config,
                    rate_limiter=rate_limiter,
                    backoff=backoff,
                )
                logger.info(f"MCP server configured: {config.name} ({config.transport.value})")

            except Exception as e:
                logger.error(f"Failed to parse MCP server config entry: {e}")

        logger.info(f"MCP config loaded: {len(self._servers)} server(s) enabled")

    async def connect_all(self) -> None:
        """Connect to all configured servers in parallel."""
        if not self._servers:
            return

        tasks = [
            self._connect_server(state)
            for state in self._servers.values()
        ]
        await asyncio.gather(*tasks, return_exceptions=True)

        connected = sum(1 for s in self._servers.values() if s.connected)
        total_tools = len(self._tool_index)
        logger.info(f"MCP connected: {connected}/{len(self._servers)} servers, {total_tools} tools discovered")

    async def _connect_server(self, state: MCPServerState) -> None:
        """Connect to a single MCP server and discover its tools."""
        config = state.config
        try:
            from mcp import ClientSession
            from mcp.client.sse import sse_client
            from mcp.client.stdio import StdioServerParameters, stdio_client
            from mcp.client.streamable_http import streamablehttp_client

            exit_stack = AsyncExitStack()
            await exit_stack.__aenter__()

            # Build headers (including auth)
            headers = dict(config.headers)
            if config.auth_token_env:
                token = os.environ.get(config.auth_token_env, "")
                if token:
                    headers["Authorization"] = f"Bearer {token}"

            # Connect based on transport type
            if config.transport == MCPTransportType.STREAMABLE_HTTP:
                if not config.url:
                    raise ValueError("URL required for streamable_http transport")
                transport = await exit_stack.enter_async_context(
                    streamablehttp_client(url=config.url, headers=headers)
                )
            elif config.transport == MCPTransportType.SSE:
                if not config.url:
                    raise ValueError("URL required for SSE transport")
                transport = await exit_stack.enter_async_context(
                    sse_client(url=config.url, headers=headers)
                )
            elif config.transport == MCPTransportType.STDIO:
                if not config.command:
                    raise ValueError("Command required for stdio transport")
                # Pass current environment to subprocess so MCP servers
                # can access API keys and configuration.
                # Also inject Docker secrets (/run/secrets/) as env vars
                # (uppercase filename → value) so stdio MCP servers can
                # read API keys without exposing them in .env.
                _MCP_ENV_WHITELIST = {
                    "PATH", "HOME", "USER", "LANG", "LC_ALL", "LC_CTYPE",
                    "NODE_PATH", "NODE_ENV", "NPM_CONFIG_PREFIX",
                    "TERM", "SHELL", "TMPDIR", "TMP", "TEMP",
                }
                subprocess_env = {k: v for k, v in os.environ.items() if k in _MCP_ENV_WHITELIST}
                secrets_dir = Path("/run/secrets")
                if secrets_dir.is_dir():
                    for secret_file in secrets_dir.iterdir():
                        if secret_file.is_file() and not secret_file.name.startswith("."):
                            env_name = secret_file.name.upper()
                            if env_name not in subprocess_env:
                                try:
                                    subprocess_env[env_name] = secret_file.read_text().strip()
                                except Exception:
                                    pass
                # Merge per-server env vars from mcp_servers.yaml
                if config.env:
                    subprocess_env.update(config.env)
                params = StdioServerParameters(
                    command=config.command,
                    args=config.args,
                    env=subprocess_env,
                )
                transport = await exit_stack.enter_async_context(
                    stdio_client(server=params)
                )
            else:
                raise ValueError(f"Unknown transport: {config.transport}")

            # transport is a tuple of (read_stream, write_stream) or
            # (read_stream, write_stream, get_session_id) for streamable_http
            if len(transport) == 3:
                read_stream, write_stream, _ = transport
            else:
                read_stream, write_stream = transport
            session = await exit_stack.enter_async_context(
                ClientSession(read_stream, write_stream)
            )

            # Initialize session
            await asyncio.wait_for(
                session.initialize(),
                timeout=settings.mcp_connect_timeout,
            )

            # Discover tools
            tools_result = await asyncio.wait_for(
                session.list_tools(),
                timeout=settings.mcp_connect_timeout,
            )

            # Build full list of all discovered tools (for admin UI)
            all_tools = []
            for tool in tools_result.tools:
                namespaced = f"mcp.{config.name}.{tool.name}"
                # Apply tool hints from config (append to description)
                description = tool.description or ""
                if config.tool_hints and tool.name in config.tool_hints:
                    hint = config.tool_hints[tool.name]
                    description = f"{description} {hint}".strip()
                info = MCPToolInfo(
                    server_name=config.name,
                    original_name=tool.name,
                    namespaced_name=namespaced,
                    description=description,
                    input_schema=tool.inputSchema if hasattr(tool, "inputSchema") else {},
                )
                all_tools.append(info)

            state.session = session
            state.exit_stack = exit_stack
            state.connected = True
            state.all_discovered_tools = all_tools
            state.last_error = None

            # Filter to active tools only (DB override > YAML prompt_tools > all)
            active_tools_list = self._get_active_tools(config)
            allowed = set(active_tools_list) if active_tools_list else None
            state.tools = []
            for tool_info in all_tools:
                if allowed and tool_info.original_name not in allowed:
                    continue
                state.tools.append(tool_info)
                self._tool_index[tool_info.namespaced_name] = tool_info

            # Reset backoff on successful connection
            if state.backoff:
                state.backoff.record_success()

            if allowed:
                logger.info(f"MCP server '{config.name}' connected: {len(state.tools)}/{len(all_tools)} tools (filtered)")
            else:
                logger.info(f"MCP server '{config.name}' connected: {len(state.tools)} tools")

        except Exception as e:
            state.connected = False
            state.last_error = str(e)

            # Record failure for exponential backoff
            if state.backoff:
                next_delay = state.backoff.record_failure()
                logger.warning(
                    f"MCP server '{config.name}' connection failed: {e} "
                    f"(attempt {state.backoff.attempt_count}, next retry in {next_delay:.1f}s)"
                )
            else:
                logger.warning(f"MCP server '{config.name}' connection failed: {e}")

            # Clean up exit stack on failure
            if state.exit_stack:
                try:
                    await state.exit_stack.__aexit__(None, None, None)
                except Exception:
                    pass
                state.exit_stack = None

    async def _reconnect_server(self, state: MCPServerState) -> bool:
        """Tear down a stale session and re-establish.

        Single concurrent reconnect per server: callers contend on
        ``state.reconnect_lock`` (initialised synchronously on the dataclass).
        Whoever wins the lock first does the actual work; subsequent
        callers see ``state.connected == True`` and short-circuit. Caller-
        side: invoke when a tool call raises a session-shape error, then
        retry the operation once.
        """
        async with state.reconnect_lock:
            # Another caller may have already restored the session.
            if state.connected and state.session is not None:
                return True
            # Tear down old session/streams. Failures here are expected
            # (the resource is half-broken — that's why we're here).
            if state.exit_stack is not None:
                try:
                    await state.exit_stack.__aexit__(None, None, None)
                except Exception:
                    pass
                state.exit_stack = None
                state.session = None
            logger.info(f"MCP reconnecting to '{state.config.name}'...")
            await self._connect_server(state)
            return state.connected

    async def probe_server(self, server_name: str) -> dict:
        """Active probe for an MCP server's session via the universal
        ``tools/list`` method.

        Vendor-agnostic: every conformant MCP server supports
        ``tools/list``, so this works against servers we don't control.

        Auto-reconnects on probe failure (single-shot) so a /api/health
        call from Reva not only reports the live state but also drives
        recovery without waiting for the next ``refresh_tools`` tick.

        Returns ``{"ok": bool, "latency_ms": float | None, "detail": str | None}``.
        """
        state = self._servers.get(server_name)
        if state is None:
            return {"ok": False, "latency_ms": None, "detail": "unknown server"}

        async def _probe_once() -> tuple[bool, float | None, str | None]:
            if state.session is None:
                return False, None, "no session"
            t0 = time.monotonic()
            try:
                await asyncio.wait_for(state.session.list_tools(), timeout=2.0)
            except asyncio.TimeoutError:
                return False, None, "timeout >2s"
            except Exception as exc:  # noqa: BLE001 - surface the type
                return False, None, f"{type(exc).__name__}: {exc}"[:200]
            latency_ms = (time.monotonic() - t0) * 1000
            return True, round(latency_ms, 1), None

        ok, latency, detail = await _probe_once()
        if ok:
            state.last_successful_call = time.monotonic()
            return {"ok": True, "latency_ms": latency, "detail": None}

        # Probe failed → mark stale and try one reconnect, then re-probe.
        state.connected = False
        state.last_error = detail
        reconnected = await self._reconnect_server(state)
        if not reconnected:
            return {"ok": False, "latency_ms": None, "detail": f"reconnect failed: {state.last_error}"}
        ok, latency, detail = await _probe_once()
        if ok:
            state.last_successful_call = time.monotonic()
        else:
            # Reconnect succeeded but the fresh session still can't list_tools.
            # Don't leave state.connected=True after we've seen evidence of
            # breakage — next caller would try to use a known-bad session.
            state.connected = False
            state.last_error = detail
        return {"ok": ok, "latency_ms": latency, "detail": detail}

    def _check_tool_permission(
        self,
        tool_info: MCPToolInfo,
        user_permissions: list[str] | None,
    ) -> str | None:
        """
        Check if user has permission to call this MCP tool.

        Returns None if allowed, or an error message string if denied.

        Permission resolution order:
        1. user_permissions is None → allow (AUTH_ENABLED=false, backwards-compatible)
        2. "mcp.*" in user_permissions → allow (admin wildcard)
        3. tool_permissions has mapping for this tool → check specific permission
        4. permissions defined (server-level) → check if user has at least one
        5. Nothing defined → convention: check "mcp.<server_name>" in user_permissions
        6. No match → denied
        """
        if user_permissions is None:
            return None

        server_name = tool_info.server_name
        tool_name = tool_info.original_name

        # Import here to avoid circular dependency
        from models.permissions import has_mcp_permission

        # Admin wildcard
        if has_mcp_permission(user_permissions, "mcp.*"):
            return None

        state = self._servers.get(server_name)
        config = state.config if state else None

        # Tool-level permission mapping
        if config and config.tool_permissions and tool_name in config.tool_permissions:
            required = config.tool_permissions[tool_name]
            if has_mcp_permission(user_permissions, required):
                return None
            return f"Permission denied: {required} required for {tool_info.namespaced_name}"

        # Server-level permissions
        if config and config.permissions:
            for perm in config.permissions:
                if has_mcp_permission(user_permissions, perm):
                    return None
            return f"Permission denied: one of {config.permissions} required for {tool_info.namespaced_name}"

        # Convention: mcp.<server_name>
        convention_perm = f"mcp.{server_name}"
        if has_mcp_permission(user_permissions, convention_perm):
            return None

        return f"Permission denied: mcp.{server_name} required for {tool_info.namespaced_name}"

    def has_server(self, server_name: str) -> bool:
        """Check if a server is configured and connected."""
        state = self._servers.get(server_name)
        return state is not None and state.connected

    async def execute_tool(
        self,
        namespaced_name: str,
        arguments: dict,
        user_permissions: list[str] | None = None,
        user_id: int | None = None,
        progress_sink: ProgressSink | None = None,
    ) -> dict:
        """
        Execute an MCP tool by its namespaced name.

        Includes:
        - Permission checking (if user_permissions provided)
        - Input validation against JSON schema
        - Rate limiting per server
        - Response truncation for large outputs

        Args:
            namespaced_name: Tool name in "mcp.<server>.<tool>" format
            arguments: Tool arguments
            user_permissions: User's permission strings (None = no auth / allow all)
            user_id: Authenticated user ID for audit logging
            progress_sink: Optional async callback that receives one dict per
                federation ProgressChunk (enriched with peer identity). F4c
                uses this to relay "asking Mom's brain…" status to the chat
                WebSocket. Non-federation tools ignore the sink.

        Returns:
            {"success": bool, "message": str, "data": Any}
        """
        tool_info = self._tool_index.get(namespaced_name)
        if not tool_info:
            # Exact match in all_discovered_tools (not just prompt-filtered ones).
            # Internal tools (e.g. play_radio) may call MCP tools that are discovered
            # but filtered out of the LLM prompt by prompt_tools config.
            parts = namespaced_name.split(".")
            if len(parts) >= 3 and parts[0] == "mcp":
                server_name = parts[1]
                tool_base = ".".join(parts[2:])
                server_state = self._servers.get(server_name)
                if server_state:
                    for discovered in server_state.all_discovered_tools:
                        if discovered.original_name == tool_base:
                            tool_info = discovered
                            break

            # Fuzzy fallback: LLM may hallucinate tool names (e.g. "get_current_weather"
            # when the actual tool is "weather_forecast"). Try matching by server prefix.
            if not tool_info and len(parts) >= 3 and parts[0] == "mcp":
                server_name = parts[1]
                # Find the first prompt_tools entry for this server, or any tool
                fallback = None
                for _name, info in self._tool_index.items():
                    if info.server_name == server_name:
                        if fallback is None:
                            fallback = info
                        # Prefer tools listed in prompt_tools config
                        server_state = self._servers.get(server_name)
                        if server_state and server_state.config.prompt_tools:
                            if info.original_name in server_state.config.prompt_tools:
                                fallback = info
                                break
                if fallback:
                    logger.info(
                        f"🔄 Tool '{namespaced_name}' not found, falling back to "
                        f"'{fallback.namespaced_name}' (same server: {server_name})"
                    )
                    tool_info = fallback
                    namespaced_name = fallback.namespaced_name

        if not tool_info:
            return {
                "success": False,
                "message": f"Unknown MCP tool: {namespaced_name}",
                "data": None,
            }

        # === Permission Check ===
        perm_error = self._check_tool_permission(tool_info, user_permissions)
        if perm_error:
            logger.warning(f"🔒 MCP permission denied: {namespaced_name} — {perm_error}")
            return {
                "success": False,
                "message": perm_error,
                "data": None,
            }

        state = self._servers.get(tool_info.server_name)

        # Federation-transport branch (F3c): virtual servers have no MCP
        # session at all — they route through HTTP federation. The agent
        # loop dispatches through execute_tool (non-streaming), so we
        # need the federation bridge here too. Collect the final
        # FinalResult from _execute_federation_streaming; ProgressChunks
        # go to progress_sink if one was threaded in by the chat handler
        # (F4c) — otherwise they're discarded (non-chat callers don't
        # care about live progress).
        if state is not None and state.config.transport == MCPTransportType.FEDERATION:
            final_result: dict | None = None
            async for item in self._execute_federation_streaming(
                state=state,
                namespaced_name=namespaced_name,
                arguments=arguments,
                user_permissions=user_permissions,
                user_id=user_id,
                progress_sink=progress_sink,
            ):
                if not isinstance(item, ProgressChunk):
                    final_result = item
            if final_result is None:
                return {
                    "success": False,
                    "message": "Federation tool yielded no final result",
                    "data": None,
                }
            return final_result

        if not state or not state.connected or not state.session:
            return {
                "success": False,
                "message": f"MCP Server '{tool_info.server_name}' nicht verbunden",
                "data": None,
            }

        # === Rate Limiting ===
        if state.rate_limiter:
            if not await state.rate_limiter.acquire():
                logger.warning(f"MCP rate limit exceeded for server '{tool_info.server_name}'")
                return {
                    "success": False,
                    "message": f"Rate limit exceeded for MCP server '{tool_info.server_name}'",
                    "data": None,
                }

        # === Argument Coercion (LLM flat → schema nested) ===
        arguments = _coerce_arguments(arguments, tool_info.input_schema)

        # === Geocode location names to lat/lon if needed ===
        arguments = await _geocode_location_arguments(arguments, tool_info.input_schema)

        # === Input Validation ===
        try:
            _validate_tool_input(arguments, tool_info.input_schema)
        except MCPValidationError as e:
            logger.warning(f"MCP input validation failed for {namespaced_name}: {e}")
            return {
                "success": False,
                "message": str(e),
                "data": None,
            }

        user_info = f" (user_id={user_id})" if user_id is not None else ""
        logger.debug(f"MCP call: {namespaced_name}{user_info}")

        async def _do_call() -> Any:
            return await asyncio.wait_for(
                state.session.call_tool(tool_info.original_name, arguments),
                timeout=settings.mcp_call_timeout,
            )

        # Try once; on a session-shape exception (transport-layer death —
        # see _SESSION_DEAD_EXCEPTIONS) reconnect and retry once. Application
        # errors (McpError, validation) and timeouts fall through immediately:
        # reconnecting wouldn't help and would tear down a healthy session.
        result = None
        last_exc: BaseException | None = None
        for attempt in range(2):
            try:
                result = await _do_call()
                last_exc = None
                break
            except TimeoutError:
                logger.error(f"MCP tool call timeout: {namespaced_name}")
                return {
                    "success": False,
                    "message": f"Tool-Aufruf Timeout: {namespaced_name}",
                    "data": None,
                }
            except _SESSION_DEAD_EXCEPTIONS as e:
                last_exc = e
                if attempt == 0:
                    logger.warning(
                        f"MCP session died on {namespaced_name}: {type(e).__name__}: {e}; "
                        f"reconnecting and retrying once"
                    )
                    state.connected = False
                    state.last_error = str(e)
                    reconnected = await self._reconnect_server(state)
                    if not reconnected:
                        break
                    continue
                logger.error(
                    f"MCP tool call failed after reconnect: {namespaced_name}: {e}"
                )
                state.connected = False
                state.last_error = str(e)
            except Exception as e:  # noqa: BLE001 - bubble in last_exc
                # Application-level error (McpError, schema, etc.). The
                # session is fine; just surface the failure.
                last_exc = e
                logger.error(f"MCP tool call failed: {namespaced_name}: {e}")
                state.last_error = str(e)
                break

        if result is None:
            return {
                "success": False,
                "message": f"Tool-Aufruf fehlgeschlagen: {last_exc}",
                "data": None,
            }

        state.last_successful_call = time.monotonic()

        # Convert CallToolResult to our format
        is_error = getattr(result, "isError", False)
        content_parts = []
        raw_data = []

        for item in result.content:
            text = getattr(item, "text", None)
            if text:
                # === Response Truncation ===
                truncated_text = _truncate_response(text)
                content_parts.append(truncated_text)
            raw_data.append(
                {"type": getattr(item, "type", "unknown"), "text": text}
            )

        message = "\n".join(content_parts) if content_parts else "Tool executed"

        # Truncate final message if still too large
        message = _truncate_response(message)

        # NOTE: Credential sanitization is NOT done here — the agent loop
        # needs real API keys in tool results (e.g. Jellyfin stream URLs
        # passed to play_in_room). Sanitization happens in
        # step_to_ws_message() before sending to the frontend.

        # Some MCP servers (e.g. n8n-mcp) wrap responses in their own
        # JSON envelope: {"success": false, "error": "..."}. The MCP-level
        # isError flag stays False even on application errors, so we check
        # the inner JSON to detect real failures.
        if not is_error:
            is_error = _detect_inner_error(message)

        return {
            "success": not is_error,
            "message": message,
            "data": raw_data if raw_data else None,
        }

    async def execute_tool_streaming(
        self,
        namespaced_name: str,
        arguments: dict,
        user_permissions: list[str] | None = None,
        user_id: int | None = None,
        progress_sink: ProgressSink | None = None,
    ) -> AsyncIterator[ProgressChunk | FinalResult]:
        """
        Like `execute_tool` but yields an AsyncIterator of progress + result.

        For tools with no native streaming support (the current default for
        every MCP server in the fleet) this yields exactly one item — the
        same `FinalResult` dict that `execute_tool` returns. Consumers can
        treat the iterator as "fire-and-forget" for those.

        For streaming-capable tools (Lane F1.3 — federation `query_brain`
        being the first), intermediate `ProgressChunk` items appear before
        the final dict. The chunk vocabulary is locked in
        `services/mcp_streaming.PROGRESS_LABELS` so chunk consumers can
        switch on `label` without parsing free-form strings.

        Consumer contract (locked):
            The iterator yields zero or more `ProgressChunk` followed by
            exactly one `FinalResult` (dict). Discriminate via
            `isinstance(chunk, ProgressChunk)` — `FinalResult` is a plain
            `dict` alias and does not support isinstance checks.

        Yields:
            ProgressChunk*, FinalResult — exactly one FinalResult is the
            final yield for every successful call. Errors also surface as
            a FinalResult (`success=False`) so consumers don't need a
            separate exception path.

        Cancellation:
            If the consumer aborts the iterator (`break`, `aclose()`,
            GC) AFTER the first `__anext__()` but before the final yield,
            the underlying tool call is cancelled. If `aclose()` is
            called BEFORE the first `__anext__()`, the underlying tool
            is never invoked — creating the generator does not start
            work, the first `__anext__()` does.
        """
        # Resolve the server first so we can branch on the streaming flag.
        # Tool lookup happens inside execute_tool anyway, so for the
        # non-streaming path we just wrap that single call. For the
        # streaming path we need to duplicate the lookup because we want
        # the state object to check `config.streaming` on.
        tool_info = self._tool_index.get(namespaced_name)
        state = None
        if tool_info is not None:
            state = self._servers.get(tool_info.server_name)

        # Federation-transport branch (F3c) — peers aren't real MCP
        # servers; we route them through FederationQueryAsker which
        # drives the initiate/retrieve HTTP protocol against the
        # remote Renfield and yields ProgressChunks as progress labels
        # transition. State machinery (rate limiter, validation, etc.)
        # still applies via the helper.
        if state is not None and state.config.transport == MCPTransportType.FEDERATION:
            async for item in self._execute_federation_streaming(
                state=state,
                namespaced_name=namespaced_name,
                arguments=arguments,
                user_permissions=user_permissions,
                user_id=user_id,
                progress_sink=progress_sink,
            ):
                yield item
            return

        # Non-streaming path: yield once, same shape as execute_tool.
        # Sink is forwarded defensively so any future streaming tool that
        # is registered but drops back through this branch (e.g., server
        # lost streaming mid-session) still has the sink available. Today
        # only the FEDERATION branch above actually invokes the sink.
        if state is None or not state.config.streaming:
            result = await self.execute_tool(
                namespaced_name=namespaced_name,
                arguments=arguments,
                user_permissions=user_permissions,
                user_id=user_id,
                progress_sink=progress_sink,
            )
            yield result
            return

        # Streaming path — progress_callback → asyncio.Queue → consumer.
        # Shares the prep machinery (perm/rate/validation/timeout/format)
        # with execute_tool via the code below; kept inline rather than in
        # a helper because the streaming concurrency shape is distinct
        # enough that extracting it obscures more than it saves.
        async for item in self._execute_tool_streaming_impl(
            tool_info=tool_info,
            state=state,
            namespaced_name=namespaced_name,
            arguments=arguments,
            user_permissions=user_permissions,
            user_id=user_id,
        ):
            yield item

    async def _execute_federation_streaming(
        self,
        state: "MCPServerState",
        namespaced_name: str,
        arguments: dict,
        user_permissions: list[str] | None,
        user_id: int | None,
        progress_sink: ProgressSink | None = None,
    ) -> AsyncIterator[ProgressChunk | FinalResult]:
        """
        Route a federation-transport tool call to the remote Renfield peer.

        Looks up the PeerUser row each call (not once-at-registration)
        so revocation takes effect immediately. Opens its own AsyncSession
        because the request-scoped one was closed by FastAPI before this
        (agent-loop) call path — same pattern as the responder's bg task.

        Permission enforcement (review BLOCKING #2): reads the tool from
        _tool_index and calls _check_tool_permission just like the
        non-federation paths. The agent loop picking the tool is not a
        permission boundary — it's a tool-selection heuristic.

        Schema note: federation tools bypass _coerce_arguments /
        _validate_tool_input because the schema is intentionally
        documentation-only (single `query: str` param). If query_brain
        grows fields in F3d/F5, wire validation here.
        """
        from services.database import AsyncSessionLocal
        from services.federation_query_asker import FederationQueryAsker
        from models.database import PeerUser
        from sqlalchemy import select

        peer_user_id = state.config.peer_user_id
        if peer_user_id is None:
            yield {
                "success": False,
                "message": f"Federation server {state.config.name} has no peer_user_id",
                "data": None,
            }
            return

        # Permission check — same semantics as execute_tool's gate.
        # Enforces whatever `permissions` the registry attached to the
        # federation config (default: empty = no permission string
        # required, i.e. any authenticated user can query any peer).
        # F5 may tighten this to per-peer permission strings.
        tool_info = self._tool_index.get(namespaced_name)
        if tool_info is not None:
            perm_error = self._check_tool_permission(tool_info, user_permissions)
            if perm_error:
                logger.warning(
                    f"🔒 Federation permission denied: {namespaced_name} — {perm_error}"
                )
                yield {
                    "success": False,
                    "message": perm_error,
                    "data": None,
                }
                return

        query_text = arguments.get("query") or arguments.get("text") or ""
        if not query_text:
            yield {
                "success": False,
                "message": "query_brain requires a 'query' argument",
                "data": None,
            }
            return

        async with AsyncSessionLocal() as session:
            peer = (await session.execute(
                select(PeerUser).where(
                    PeerUser.id == peer_user_id,
                    PeerUser.revoked_at.is_(None),
                )
            )).scalar_one_or_none()

        if peer is None:
            logger.warning(
                f"Federation tool call: peer {peer_user_id} unknown or revoked "
                f"(namespaced={namespaced_name}, user_id={user_id})"
            )
            yield {
                "success": False,
                "message": "Federation peer is unknown or has been revoked",
                "data": None,
            }
            return

        # F5b — outbound rate limit keyed by peer.remote_pubkey. Before
        # we spend any time on the asker, check that we haven't already
        # fired too many queries at this peer in the last minute. Hit
        # surfaces as a FinalResult failure (no retry) — the agent loop
        # will see it as a normal tool error and move on.
        from services.federation_rate_limits import acquire_asker_token
        if not await acquire_asker_token(peer.remote_pubkey):
            logger.warning(
                f"Federation asker rate limit hit for peer "
                f"{peer.remote_display_name} ({peer.remote_pubkey[:12]}…)"
            )
            yield {
                "success": False,
                "message": (
                    f"Rate limit reached for peer "
                    f"{peer.remote_display_name}. Try again in a moment."
                ),
                "data": None,
            }
            return

        # F4d — snapshot peer identity at query time so later display-name
        # changes or peer deletion don't rewrite history.
        from datetime import UTC, datetime as _dt
        initiated_at = _dt.now(UTC).replace(tzinfo=None)
        peer_pubkey_snapshot = peer.remote_pubkey
        peer_display_snapshot = peer.remote_display_name
        peer_id_snapshot = peer.id

        asker = FederationQueryAsker()
        final_item: dict | None = None
        try:
            async for item in asker.query_peer(peer, query_text):
                # F4c — relay ProgressChunks to the chat WS sink if one was
                # threaded in. Enrich with stable peer identity (remote_pubkey)
                # so the frontend can key status lines per-peer even when the
                # display name changes or collides. Sink failures must not
                # abort the tool call — log and continue.
                if progress_sink is not None and isinstance(item, ProgressChunk):
                    try:
                        await progress_sink({
                            "peer_pubkey": peer.remote_pubkey,
                            "peer_display_name": peer.remote_display_name,
                            "label": item.label,
                            "detail": item.detail,
                            "sequence": item.sequence,
                        })
                    except Exception as sink_err:  # pragma: no cover — sink is best-effort
                        logger.warning(
                            f"Federation progress_sink raised (continuing): {sink_err}"
                        )
                if not isinstance(item, ProgressChunk):
                    final_item = item
                yield item
        finally:
            # F4d — audit write in `finally` so cancellation, caller-side
            # `aclose()`, or a consumer raising mid-iteration still produces
            # one audit row per federated query. `final_item` stays None if
            # we never reached a terminal yield → _classify_final maps that
            # to `final_status="unknown"` with an explanatory error_message,
            # which is the honest record of "I asked but we didn't finish".
            # Write failures are swallowed in write_federation_audit.
            from services.federation_audit import write_federation_audit
            await write_federation_audit(
                user_id=user_id,
                peer_user_id=peer_id_snapshot,
                peer_pubkey_snapshot=peer_pubkey_snapshot,
                peer_display_name_snapshot=peer_display_snapshot,
                query_text=query_text,
                initiated_at=initiated_at,
                final_item=final_item,
            )

    async def _execute_tool_streaming_impl(
        self,
        tool_info: "MCPToolInfo",
        state: "MCPServerState",
        namespaced_name: str,
        arguments: dict,
        user_permissions: list[str] | None,
        user_id: int | None,
    ) -> AsyncIterator[ProgressChunk | FinalResult]:
        """Streaming-path implementation for F1.3.

        Runs the tool call in a background task with an asyncio.Queue-backed
        progress_callback. Yields ProgressChunks as they arrive, then the
        final FinalResult dict (same shape execute_tool returns).
        """
        from services.mcp_streaming import (
            PROGRESS_LABEL_TOOL_RUNNING,
            PROGRESS_LABELS,
        )

        # === Permission check ===
        perm_error = self._check_tool_permission(tool_info, user_permissions)
        if perm_error:
            logger.warning(f"🔒 MCP permission denied: {namespaced_name} — {perm_error}")
            yield {"success": False, "message": perm_error, "data": None}
            return

        if not state.connected or not state.session:
            yield {
                "success": False,
                "message": f"MCP Server '{tool_info.server_name}' nicht verbunden",
                "data": None,
            }
            return

        # === Rate limiting ===
        if state.rate_limiter and not await state.rate_limiter.acquire():
            logger.warning(f"MCP rate limit exceeded for server '{tool_info.server_name}'")
            yield {
                "success": False,
                "message": f"Rate limit exceeded for MCP server '{tool_info.server_name}'",
                "data": None,
            }
            return

        # === Argument coercion + validation ===
        arguments = _coerce_arguments(arguments, tool_info.input_schema)
        arguments = await _geocode_location_arguments(arguments, tool_info.input_schema)
        try:
            _validate_tool_input(arguments, tool_info.input_schema)
        except MCPValidationError as e:
            logger.warning(f"MCP input validation failed for {namespaced_name}: {e}")
            yield {"success": False, "message": str(e), "data": None}
            return

        # === Set up progress queue + callback ===
        progress_queue: asyncio.Queue = asyncio.Queue()
        sequence_counter = 0

        async def progress_cb(progress: float, total: float | None, message: str | None) -> None:
            """MCP SDK passes (progress, total, message). `message` carries the
            progress label; we validate it against PROGRESS_LABELS and fall
            back to TOOL_RUNNING for unknown labels (defence against a
            misbehaving or malicious responder emitting arbitrary strings)."""
            nonlocal sequence_counter
            sequence_counter += 1
            label = message if message in PROGRESS_LABELS else PROGRESS_LABEL_TOOL_RUNNING
            detail: dict[str, Any] = {"progress": float(progress)}
            if total is not None:
                detail["total"] = float(total)
            await progress_queue.put(ProgressChunk(
                label=label, detail=detail, sequence=sequence_counter,
            ))

        # === Run the tool call as a background task ===
        try:
            call_coro = state.session.call_tool(
                tool_info.original_name, arguments, progress_callback=progress_cb,
            )
        except TypeError as e:
            # Narrow catch: only swallow the "unexpected keyword argument
            # 'progress_callback'" case (pre-ProgressFnT MCP SDK). Any other
            # TypeError (bad arguments type, missing positional, ...) must
            # bubble up as a FinalResult error so the consumer sees a real
            # diagnostic instead of a silent retry that re-raises.
            if "progress_callback" not in str(e):
                raise
            logger.debug(
                f"MCP SDK call_tool has no progress_callback kwarg; "
                f"{namespaced_name} runs without streaming."
            )
            call_coro = state.session.call_tool(tool_info.original_name, arguments)

        user_info = f" (user_id={user_id})" if user_id is not None else ""
        logger.debug(f"MCP streaming call: {namespaced_name}{user_info}")

        call_task = asyncio.create_task(
            asyncio.wait_for(call_coro, timeout=settings.mcp_call_timeout)
        )

        # === Drain progress chunks while task runs ===
        try:
            while not call_task.done():
                try:
                    chunk = await asyncio.wait_for(progress_queue.get(), timeout=0.05)
                    yield chunk
                except TimeoutError:
                    continue
            # Flush anything that arrived after the last poll.
            while not progress_queue.empty():
                yield progress_queue.get_nowait()
        except (asyncio.CancelledError, GeneratorExit):
            # Consumer closed the generator — cancel the tool call AND await
            # it (via suppress) so the task-destroyed-but-pending warning
            # doesn't fire and any transport-level cleanup runs before we
            # re-raise.
            call_task.cancel()
            with suppress(BaseException):
                await call_task
            raise

        # === Yield the final result (same format as execute_tool) ===
        try:
            result = await call_task
        except TimeoutError:
            logger.error(f"MCP tool call timeout: {namespaced_name}")
            yield {
                "success": False,
                "message": f"Tool-Aufruf Timeout: {namespaced_name}",
                "data": None,
            }
            return
        except Exception as e:
            logger.error(f"MCP tool call failed: {namespaced_name}: {e}")
            state.connected = False
            state.last_error = str(e)
            yield {
                "success": False,
                "message": f"Tool-Aufruf fehlgeschlagen: {e}",
                "data": None,
            }
            return

        # Convert CallToolResult → FinalResult dict (same logic as execute_tool).
        is_error = getattr(result, "isError", False)
        content_parts = []
        raw_data = []
        for item in result.content:
            text = getattr(item, "text", None)
            if text:
                content_parts.append(_truncate_response(text))
            raw_data.append({"type": getattr(item, "type", "unknown"), "text": text})

        message_text = "\n".join(content_parts) if content_parts else "Tool executed"
        message_text = _truncate_response(message_text)

        if not is_error:
            is_error = _detect_inner_error(message_text)

        yield {
            "success": not is_error,
            "message": message_text,
            "data": raw_data if raw_data else None,
        }

    def get_all_tools(self) -> list[MCPToolInfo]:
        """Return all discovered MCP tools."""
        return list(self._tool_index.values())

    def get_connected_server_names(self) -> list[str]:
        """Return names of all currently connected MCP servers."""
        return [name for name, state in self._servers.items() if state.connected]

    def get_server_examples(self) -> dict[str, dict]:
        """Return configured examples for all servers.

        Returns:
            Dict mapping server name to {"de": [...], "en": [...], "example_intent": "mcp.server.tool"}
        """
        result = {}
        for name, state in self._servers.items():
            if state.config.examples:
                data = dict(state.config.examples)  # copy lang -> examples
                if state.config.example_intent:
                    data["_example_intent"] = state.config.example_intent
                result[name] = data
        return result

    def get_prompt_tools_config(self) -> dict[str, list[str]]:
        """Return per-server prompt_tools filter from YAML config.

        Returns:
            Dict mapping server name to list of tool base names.
            Only servers with prompt_tools configured are included.
        """
        result = {}
        for name, state in self._servers.items():
            if state.config.prompt_tools is not None:
                result[name] = state.config.prompt_tools
        return result

    def _get_active_tools(self, config: MCPServerConfig) -> list[str] | None:
        """Get active tools list: DB override > YAML prompt_tools > None (all)."""
        override = self._tool_overrides.get(config.name)
        if override is not None:
            return override
        return config.prompt_tools

    def is_mcp_tool(self, name: str) -> bool:
        """Check if a name is a known MCP tool."""
        return name in self._tool_index

    def get_status(self) -> dict:
        """Return status information for all servers."""
        servers = []
        for name, state in self._servers.items():
            server_info = {
                "name": name,
                "transport": state.config.transport.value,
                "connected": state.connected,
                "tool_count": len(state.tools),
                "total_tool_count": len(state.all_discovered_tools),
                "last_error": state.last_error,
            }
            # Include backoff info for disconnected servers
            if not state.connected and state.backoff and state.backoff.attempt_count > 0:
                server_info["reconnect_attempts"] = state.backoff.attempt_count
                server_info["next_retry_in"] = round(state.backoff.time_until_retry(), 1)
            servers.append(server_info)
        return {
            "enabled": True,
            "total_tools": len(self._tool_index),
            "servers": servers,
        }

    async def refresh_tools(self) -> None:
        """Refresh tool lists from all connected servers and reconnect failed ones."""
        for state in self._servers.values():
            # Federation-transport servers have no MCP session; their
            # single `query_brain` tool is managed by PeerMCPRegistry,
            # not discovered via list_tools. Skip explicitly so future
            # refactors don't accidentally include them.
            if state.config.transport == MCPTransportType.FEDERATION:
                continue
            if state.connected and state.session:
                try:
                    tools_result = await asyncio.wait_for(
                        state.session.list_tools(),
                        timeout=settings.mcp_connect_timeout,
                    )
                    # Remove old tools from index
                    for old_name in [t.namespaced_name for t in state.tools]:
                        self._tool_index.pop(old_name, None)

                    # Store all discovered tools (unfiltered)
                    state.all_discovered_tools = []
                    for tool in tools_result.tools:
                        namespaced = f"mcp.{state.config.name}.{tool.name}"
                        info = MCPToolInfo(
                            server_name=state.config.name,
                            original_name=tool.name,
                            namespaced_name=namespaced,
                            description=tool.description or "",
                            input_schema=tool.inputSchema if hasattr(tool, "inputSchema") else {},
                        )
                        state.all_discovered_tools.append(info)

                    # Re-register with active filter applied
                    active = self._get_active_tools(state.config)
                    allowed = set(active) if active else None
                    state.tools = []
                    for tool_info in state.all_discovered_tools:
                        if allowed and tool_info.original_name not in allowed:
                            continue
                        state.tools.append(tool_info)
                        self._tool_index[tool_info.namespaced_name] = tool_info

                except Exception as e:
                    logger.warning(f"MCP refresh failed for '{state.config.name}': {e}")
                    state.connected = False
                    state.last_error = str(e)
            elif not state.connected:
                # Check if backoff allows reconnection attempt
                if state.backoff and not state.backoff.should_retry():
                    remaining = state.backoff.time_until_retry()
                    logger.debug(
                        f"MCP server '{state.config.name}' in backoff, "
                        f"next retry in {remaining:.1f}s"
                    )
                    continue

                # Try to reconnect
                logger.info(
                    f"MCP reconnecting to '{state.config.name}' "
                    f"(attempt {state.backoff.attempt_count + 1 if state.backoff else 1})..."
                )
                await self._connect_server(state)

    def _refilter_server(self, server_name: str) -> None:
        """Re-build state.tools + _tool_index from all_discovered_tools using current filter."""
        state = self._servers.get(server_name)
        if not state:
            return
        # Remove old entries from index
        for t in state.tools:
            self._tool_index.pop(t.namespaced_name, None)
        # Re-filter
        active = self._get_active_tools(state.config)
        allowed = set(active) if active else None
        state.tools = []
        for tool in state.all_discovered_tools:
            if allowed and tool.original_name not in allowed:
                continue
            state.tools.append(tool)
            self._tool_index[tool.namespaced_name] = tool

    async def load_tool_overrides(self, db) -> None:
        """Load per-server tool activation overrides from SystemSetting."""
        from sqlalchemy import select

        from models.database import SystemSetting

        for name in self._servers:
            key = f"mcp.{name}.active_tools"
            result = await db.execute(select(SystemSetting).where(SystemSetting.key == key))
            setting = result.scalar_one_or_none()
            if setting:
                self._tool_overrides[name] = json.loads(setting.value)
                logger.info(f"MCP tool override loaded for '{name}': {len(self._tool_overrides[name])} active tools")

    async def set_tool_override(self, server_name: str, active_tools: list[str] | None, db) -> None:
        """Update active tools for a server. None = reset to YAML default."""
        from sqlalchemy import select

        from models.database import SystemSetting

        key = f"mcp.{server_name}.active_tools"
        if active_tools is None:
            # Reset to default — delete override
            self._tool_overrides.pop(server_name, None)
            result = await db.execute(select(SystemSetting).where(SystemSetting.key == key))
            setting = result.scalar_one_or_none()
            if setting:
                await db.delete(setting)
        else:
            self._tool_overrides[server_name] = active_tools
            # Upsert SystemSetting
            result = await db.execute(select(SystemSetting).where(SystemSetting.key == key))
            setting = result.scalar_one_or_none()
            if setting:
                setting.value = json.dumps(active_tools)
            else:
                db.add(SystemSetting(key=key, value=json.dumps(active_tools)))
        await db.commit()
        # Re-apply filter to already-discovered tools
        self._refilter_server(server_name)

    def get_all_tools_with_status(self) -> list[dict]:
        """Return all discovered tools with active flag for admin UI."""
        result = []
        for state in self._servers.values():
            active_names = {t.namespaced_name for t in state.tools}
            for tool in state.all_discovered_tools:
                result.append({
                    "name": tool.namespaced_name,
                    "server": tool.server_name,
                    "original_name": tool.original_name,
                    "description": tool.description,
                    "input_schema": tool.input_schema,
                    "active": tool.namespaced_name in active_names,
                })
        return result

    async def start_refresh_loop(self) -> None:
        """Start background task for periodic health checks and tool refreshes."""
        async def _loop():
            while True:
                jitter = random.uniform(0.8, 1.2)
                await asyncio.sleep(settings.mcp_refresh_interval * jitter)
                try:
                    await self.refresh_tools()
                except Exception as e:
                    logger.error(f"MCP refresh loop error: {e}")

        self._refresh_task = asyncio.create_task(_loop())

    async def shutdown(self) -> None:
        """Close all MCP sessions and cancel background tasks."""
        if self._refresh_task:
            self._refresh_task.cancel()
            try:
                await self._refresh_task
            except asyncio.CancelledError:
                pass

        for state in self._servers.values():
            if state.exit_stack:
                try:
                    await state.exit_stack.__aexit__(None, None, None)
                except Exception as e:
                    logger.warning(f"MCP shutdown error for '{state.config.name}': {e}")
            state.connected = False
            state.session = None
            state.exit_stack = None

        self._tool_index.clear()
        logger.info("MCP manager shut down")
