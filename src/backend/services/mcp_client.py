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
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

import yaml
from loguru import logger

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
    import httpx

    for attempt in range(2):
        try:
            async with httpx.AsyncClient(timeout=8.0) as client:
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
    if len(text.encode('utf-8')) <= max_size:
        return text

    # Try smart JSON truncation: keep complete items in arrays
    try:
        import json
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
                result = json.dumps(data, ensure_ascii=False)
                if best < total:
                    result += f"\n\n[... Showing {best} of {total} results]"
                return result
    except (json.JSONDecodeError, TypeError, KeyError):
        pass

    # Fallback: byte-level truncation
    truncated = text.encode('utf-8')[:max_size - 50].decode('utf-8', errors='ignore')
    return truncated + "\n\n[... Response truncated (exceeded 10KB limit)]"


def _detect_inner_error(message: str) -> bool:
    """
    Detect application-level errors inside MCP response text.

    Some MCP servers (e.g. n8n-mcp) wrap all responses in a JSON envelope
    like ``{"success": false, "error": "..."}`` while the MCP protocol-level
    ``isError`` flag stays False.  This function parses the message to detect
    such inner failures.

    Returns True if the inner response indicates an error, False otherwise.
    """
    try:
        data = json.loads(message)
        if isinstance(data, dict) and "success" in data:
            return data["success"] is False
    except (json.JSONDecodeError, TypeError, ValueError):
        pass
    return False


class MCPTransportType(str, Enum):
    STREAMABLE_HTTP = "streamable_http"
    SSE = "sse"
    STDIO = "stdio"


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
                config = MCPServerConfig(
                    name=entry["name"],
                    url=_resolve_value(entry.get("url")),
                    transport=MCPTransportType(
                        _resolve_value(entry.get("transport", "streamable_http"))
                    ),
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

    async def execute_tool(self, namespaced_name: str, arguments: dict) -> dict:
        """
        Execute an MCP tool by its namespaced name.

        Includes:
        - Input validation against JSON schema
        - Rate limiting per server
        - Response truncation for large outputs

        Returns:
            {"success": bool, "message": str, "data": Any}
        """
        tool_info = self._tool_index.get(namespaced_name)
        if not tool_info:
            # Fuzzy fallback: LLM may hallucinate tool names (e.g. "get_current_weather"
            # when the actual tool is "weather_forecast"). Try matching by server prefix.
            parts = namespaced_name.split(".")
            if len(parts) >= 3 and parts[0] == "mcp":
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

        state = self._servers.get(tool_info.server_name)
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

        try:
            result = await asyncio.wait_for(
                state.session.call_tool(tool_info.original_name, arguments),
                timeout=settings.mcp_call_timeout,
            )

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

        except TimeoutError:
            logger.error(f"MCP tool call timeout: {namespaced_name}")
            return {
                "success": False,
                "message": f"Tool-Aufruf Timeout: {namespaced_name}",
                "data": None,
            }
        except Exception as e:
            logger.error(f"MCP tool call failed: {namespaced_name}: {e}")
            # Mark server as disconnected on session errors
            state.connected = False
            state.last_error = str(e)
            return {
                "success": False,
                "message": f"Tool-Aufruf fehlgeschlagen: {e}",
                "data": None,
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
                await asyncio.sleep(settings.mcp_refresh_interval)
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
