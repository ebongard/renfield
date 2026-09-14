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
from collections import deque
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

# Exponential Backoff constants for reconnection — read from Settings
# (see mcp_backoff_* fields in utils/config.py).
BACKOFF_INITIAL_DELAY = settings.mcp_backoff_initial_delay
BACKOFF_MAX_DELAY = settings.mcp_backoff_max_delay
BACKOFF_MULTIPLIER = settings.mcp_backoff_multiplier
BACKOFF_JITTER = settings.mcp_backoff_jitter


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


# Bound for tearing down a (half-)broken transport stack. anyio teardown of a
# wedged streamable_http session can hang on stream drain; every teardown site
# below is bounded by this so no caller (esp. one holding reconnect_lock) can
# freeze on cleanup (#1107).
_TEARDOWN_TIMEOUT_S = 5.0


# Strong references to detached cleanup tasks: the event loop only holds weak
# refs, so a fire-and-forget close task could be garbage-collected before it
# runs ("Task was destroyed but it is pending!"). Done tasks self-remove.
_detached_cleanup_tasks: set["asyncio.Task"] = set()


async def _close_stack_bounded(stack: AsyncExitStack) -> None:
    """Bounded best-effort close of a transport stack (shared idiom for every
    teardown site — a wedged anyio teardown must never hold a caller). Swallows
    teardown failures/timeouts; an OUTER cancellation still propagates."""
    try:
        async with asyncio.timeout(_TEARDOWN_TIMEOUT_S):
            await stack.__aexit__(None, None, None)
    except asyncio.CancelledError:
        raise
    except BaseException:  # teardown of a half-broken stack — nothing to surface
        pass


def _close_stack_detached(stack: AsyncExitStack) -> None:
    """Schedule a detached best-effort close (used from a CANCELLED connect,
    where awaiting anything would insta-cancel). Keeps a strong task reference
    so the closer can't be GC'd before it runs; fully silent — cross-task anyio
    cancel-scope errors are expected here."""

    async def _run() -> None:
        try:
            async with asyncio.timeout(_TEARDOWN_TIMEOUT_S):
                await stack.__aexit__(None, None, None)
        except BaseException:  # detached cleanup — nothing to surface
            pass

    task = asyncio.get_running_loop().create_task(_run())
    _detached_cleanup_tasks.add(task)
    task.add_done_callback(_detached_cleanup_tasks.discard)


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

    # Wrap flat LLM args INTO a single required object property the schema expects
    # (the inverse of the unwrap above). Some MCP servers — notably the Digital.ai
    # Release MCP — take every tool's parameters as one required `request` object,
    # so a flat {"active": true} from the LLM fails validation with
    # "'request' is a required property". When the schema has exactly one property,
    # it is an object and required, and the LLM's keys are NOT top-level schema
    # properties (i.e. they belong inside the wrapper), nest them. Handles the empty
    # {} → {"request": {}} case too. Skipped when the wrapper key is already present,
    # so a correctly-wrapped call is never double-wrapped.
    if len(properties) == 1:
        (wrap_key, wrap_schema), = properties.items()
        # A single required "wrapper" property is object-like when it declares an
        # object type OR references a model — FastMCP renders a Pydantic model
        # parameter (e.g. release-mcp's `request: ListReleasesRequest`) as a bare
        # $ref with no inline "type", so keying only on type=="object" misses it.
        wrap_is_object = (
            wrap_schema.get("type") == "object"
            or "$ref" in wrap_schema
            or "allOf" in wrap_schema
            or "anyOf" in wrap_schema
            or "oneOf" in wrap_schema
            or "properties" in wrap_schema
        )
        if (
            wrap_key not in coerced
            and wrap_is_object
            and wrap_key in input_schema.get("required", [])
            and all(k not in properties for k in coerced)
        ):
            logger.info(f"🔄 Wrapping flat args into '{wrap_key}': {list(coerced.keys())}")
            coerced = {wrap_key: coerced}

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
    # Geocoding only applies to dict-shaped arguments against a schema that
    # declares lat/lon. A non-dict payload (a malformed tool call) passes
    # through untouched so the real validation error surfaces downstream
    # instead of an AttributeError raised from inside geocoding.
    if not input_schema or not isinstance(arguments, dict):
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


# Upstream-throttle recognition (Phase 3). Matched ONLY against results that are
# already errors (isError / inner-error envelope) or app-level exceptions — never
# against a successful result, whose payload may legitimately contain these words
# (a document about rate limits is not a throttle). "429" alone is NOT enough: an
# app error naming invoice or document 429 must not read as a throttle, so the bare
# number only counts next to an HTTP/status marker; structured JSON is checked by key.
_RATE_LIMIT_TEXT = re.compile(
    r"too many requests|rate[\s_-]?limit|(?:http|status)\W{0,12}429\b",
    re.IGNORECASE,
)
_RETRY_AFTER_TEXT = re.compile(r"retry[\s_-]?after\W{0,4}(\d{1,6}(?:\.\d+)?)", re.IGNORECASE)
_RATE_LIMIT_STATUS_KEYS = ("status", "status_code", "statusCode", "code", "http_status")
_RETRY_AFTER_KEYS = ("retry_after", "retry-after", "retryAfter")


def _parse_retry_after(value: Any) -> float | None:
    """Seconds from a Retry-After value. The HTTP-date form is not honoured — guessing
    across clock skew would be worse than ignoring it."""
    if value is None or isinstance(value, bool):
        return None
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return None
    return seconds if 0 < seconds < 1_000_000 else None


def _classify_rate_limit(
    message: str | None,
    status_code: int | None = None,
    retry_after_header: Any = None,
) -> tuple[bool, float | None]:
    """Is this ERROR an upstream throttle? Returns ``(limited, retry_after_seconds)``.

    Real shapes this has to recognise: an MCP server relaying httpx's
    ``Client error '429 Too Many Requests' for url …``, a JSON envelope carrying a
    429 status, or plain "rate limit exceeded" prose. A transport-level 429 from the
    MCP endpoint itself cannot be classified here: the SDK raises it inside a
    background task, so it surfaces as a timeout or a dead session instead.
    """
    text = message or ""
    limited = status_code == 429
    retry_after = _parse_retry_after(retry_after_header)
    try:
        payload = json.loads(text)
    except (ValueError, TypeError):
        payload = None
    if isinstance(payload, dict):
        if any(payload.get(k) in (429, "429") for k in _RATE_LIMIT_STATUS_KEYS):
            limited = True
        if retry_after is None:
            retry_after = next(
                (v for v in (_parse_retry_after(payload.get(k)) for k in _RETRY_AFTER_KEYS) if v),
                None,
            )
    if not limited and _RATE_LIMIT_TEXT.search(text):
        limited = True
    if not limited:
        return False, None
    if retry_after is None:
        match = _RETRY_AFTER_TEXT.search(text)
        if match:
            retry_after = _parse_retry_after(match.group(1))
    return True, retry_after


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

# Exact message the MCP streamable_http client returns once the server has
# invalidated our session id — which is what happens when that server's
# pod/process restarts (mcp/client/streamable_http.py raises this as an McpError
# with JSON-RPC code 32600). It is NOT one of the transport exceptions above, so
# without special-casing it the call is mistaken for an application error and
# never retried.
_SESSION_TERMINATED_MARKER = "session terminated"


def _is_session_dead(exc: BaseException) -> bool:
    """True if `exc` means the transport/session is dead (vs. an app error).

    Two cases:
    - the typed transport exceptions (stream closed / connection reset);
    - the streamable_http "Session terminated" ``McpError`` raised after the
      server restarts — the case that left the agent unable to reach a bounced
      MCP server until a manual backend restart.

    Deliberately NARROW: the message check is gated on the exception actually
    being an ``McpError`` and matches only the SDK's exact "session terminated"
    signal — NOT a free-text substring over arbitrary exceptions. A genuine
    application error whose message merely mentions a "session" (e.g. an
    email/IMAP or API tool surfacing an upstream "session expired") must NOT be
    treated as transport death, because that would reconnect-and-retry and could
    double-execute a mutating tool.
    """
    if isinstance(exc, _SESSION_DEAD_EXCEPTIONS):
        return True
    try:
        from mcp.shared.exceptions import McpError
    except Exception:  # noqa: BLE001 - SDK shape guard
        return False
    if not isinstance(exc, McpError):
        return False
    err = getattr(exc, "error", None)
    msg = (getattr(err, "message", None) or str(exc) or "").lower()
    return _SESSION_TERMINATED_MARKER in msg


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


# --- Per-user auth resolver seam (per-user data scoping) ----------------------
# A plugin (Reva) registers a resolver via `set_user_auth_resolver()`. Given a
# server name + authenticated renfield `user_id`, it returns the **HTTP headers**
# to attach to THIS user's request — typically `{"Authorization": "Bearer <tok>"}`,
# but a dict so a provider needing more than one header is expressible without a
# core change (e.g. mcp-atlassian's multi-user mode requires BOTH
# `Authorization: Bearer <oauth>` AND `X-Atlassian-Cloud-Id: <cloud_id>`). It
# returns None (or an empty dict) when the user has no provisioned credential.
# Renfield stays scheme-agnostic — the plugin owns the credential store, the
# scheme, and any provider-specific companion headers. This is the ONLY seam
# through which per-user credentials cross into the MCP core; the core never
# sees a plaintext store.
_USER_AUTH_RESOLVER = None  # type: ignore[var-annotated]


def set_user_auth_resolver(fn) -> None:
    """Register (or clear, with None) the per-user MCP auth resolver.

    `fn` is `async (server_name: str, user_id: int) -> dict[str, str] | None`
    returning the request headers to attach for that user. Idempotent.
    """
    global _USER_AUTH_RESOLVER
    _USER_AUTH_RESOLVER = fn


async def _resolve_user_auth_headers(server_name: str, user_id: int):
    """Return the per-user request headers dict, or None/empty if unresolved."""
    if _USER_AUTH_RESOLVER is None:
        return None
    return await _USER_AUTH_RESOLVER(server_name, user_id)


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
    # Output-provider stanza (docs/design/output-providers.md). When present, this
    # MCP server is a room-output target source. Shape:
    #   {capabilities: [audio|video|power|transport|queue],
    #    discover/play/control/status: <tool_name>, boot_timeout?: <seconds>}
    # Consumed by ha_glue/services/output_providers.py to build McpOutputProvider
    # entries. None => not an output provider (the default for every other server).
    output_provider: dict | None = None
    streaming: bool = False  # Opt-in: server emits progress notifications via MCP progress_callback.
                              # When true, execute_tool_streaming wires an asyncio.Queue to capture
                              # notifications and yield ProgressChunks. First consumer: federation
                              # query_brain (F3). Non-streaming servers ignore this flag — the
                              # progress queue stays empty and only the final result is yielded.

    # Per-user auth (per-user data scoping). When True, a tool call for this
    # server does NOT ride the shared connection-level credential; each call
    # opens a short-lived per-user session whose `Authorization` is resolved
    # from the registered user-auth resolver (`set_user_auth_resolver`) for
    # `(name, user_id)`. FAIL-CLOSED: an authenticated user with no resolvable
    # credential — or an unidentified `user_id=None` turn — is DENIED, never
    # silently downgraded to the shared operator credential (that downgrade is
    # exactly the confused-deputy the per-user model exists to remove). Only
    # streamable_http / sse transports are eligible. Default False = the
    # byte-identical legacy shared-session path.
    per_user_auth: bool = False

    # Functional health probe (A1). Optional per-server stanza naming ONE cheap,
    # read-only tool call that must succeed. Shape:
    #   {enabled: bool, tool: str, args: dict, interval: int, timeout: float,
    #    expect: {min_items: int, path: str|None}}
    # Why this exists alongside the Phase-2 `calls_failing` signal: that one counts
    # only TIMEOUTS, because an app-level error (device off, parcel not found) says
    # nothing about the SERVER's health. A probe escapes that bind — WE choose a call
    # that must succeed, so its failure IS a health signal. And it works on a server
    # nobody has called, which produces no samples at all.
    # None => not probed (the default; the honest limit is in the YAML, not the flag).
    health_probe: dict | None = None

    # Tool-call timeout override, in seconds. Either one number for every tool of
    # the server, or a mapping `{tool_name: seconds, default: seconds}` so ONE
    # long tool does not stretch the timeout of the quick ones (a status query
    # must not hang for minutes because a sibling tool legitimately runs long).
    # None => the global `settings.mcp_call_timeout`. Motivated by the scanner,
    # whose scan once ran inside the call (2026-09-14) — it now returns at once
    # and needs no override, but the mechanism stays for tools that are slow by
    # nature.
    call_timeout: float | dict[str, float] | None = None

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
    # Deadline (monotonic start + that call's own timeout) of every tool call
    # running on this session. While one is still WITHIN its deadline, the
    # background refresh and the self-heal probe leave the session alone: a server
    # busy answering a long call can be slow to answer list_tools, and treating
    # that as death used to tear the running call down (review 2026-09-14). A call
    # past its deadline shields nothing — otherwise steady traffic of hung calls
    # would keep a dead server looking busy, and never healed, indefinitely.
    inflight_deadlines: list = field(default_factory=list)

    @property
    def inflight_calls(self) -> int:
        return len(self.inflight_deadlines)

    def shielded_by_inflight_call(self, now: float | None = None) -> bool:
        """True while at least one running call is still inside its timeout."""
        now = time.monotonic() if now is None else now
        return any(deadline > now for deadline in self.inflight_deadlines)
    # Functional-health signal (Phase 2): rolling window of recent tool-call
    # outcomes that are HEALTH-CORRELATED — True on a clean result, False on a
    # timeout (server/upstream didn't respond). Deliberately NOT recorded:
    # app-level errors (isError / a device-off / not-found envelope — an application
    # outcome, not the server's health), caller rejects (permission/validation/rate-
    # limit), and session-death (already flips connected=False → "down"). Reset on
    # (re)connect (fresh session).
    recent_outcomes: deque = field(
        default_factory=lambda: deque(maxlen=max(1, settings.mcp_health_call_window))
    )

    # Functional-probe verdict (A1). Kept SEPARATE from recent_outcomes on purpose:
    # that window records only timeouts from whatever the agent happened to call,
    # while this records a call WE chose that must succeed. Folding them would
    # re-import the app-error ambiguity the Phase-2 review deliberately excluded.
    probe_consecutive_failures: int = 0
    last_probe_at: float = 0.0           # monotonic; 0 = never probed
    last_probe_ok: bool | None = None    # None = no verdict yet
    last_probe_detail: str | None = None

    # No-tools grace (Phase 3). Monotonic time this server was first seen exposing
    # zero tools; None while it has tools, or before any discovery. The `no_tools`
    # verdict is reported at once (the kiosk stays honest) but the ALERT waits out
    # `mcp_health_no_tools_grace_seconds`, so a server whose tools register a moment
    # after connect is not reported broken on every boot. Deliberately NOT reset by a
    # reconnect that still finds nothing — a flapping server must still age into an
    # alert.
    no_tools_since: float | None = None

    # Upstream rate-limit signal (Phase 3). Monotonic timestamps of ERROR results the
    # upstream throttled (HTTP 429 / "too many requests"). SEPARATE from both
    # recent_outcomes (timeouts) and the probe verdict: a throttle is neither a dead
    # server nor a failed functional check. Windowed — events age out after
    # mcp_health_rate_limit_window_seconds, so a burst can never pin a server red.
    rate_limit_events: deque = field(default_factory=lambda: deque(maxlen=1000))
    # Per-TOOL Retry-After horizon (monotonic). Per tool, not per server: one server
    # can front several upstreams (tracking talks to one API per carrier), and one
    # throttled upstream must not block the others.
    rate_limited_until: dict = field(default_factory=dict)

    def note_discovered_tools(self, now: float | None = None) -> None:
        """Update the no-tools clock after (re)discovering the tool list."""
        if self.all_discovered_tools:
            self.no_tools_since = None
        elif self.no_tools_since is None:
            self.no_tools_since = time.monotonic() if now is None else now

    def no_tools_age(self, now: float | None = None) -> float | None:
        """Seconds this server has exposed zero tools, or None if unknown/has tools."""
        if self.no_tools_since is None:
            return None
        now = time.monotonic() if now is None else now
        return max(0.0, now - self.no_tools_since)

    def _prune_rate_limit_events(self, now: float) -> None:
        window = settings.mcp_health_rate_limit_window_seconds
        while self.rate_limit_events and now - self.rate_limit_events[0] > window:
            self.rate_limit_events.popleft()

    def record_rate_limit(
        self, tool: str, retry_after: float | None, now: float | None = None
    ) -> None:
        """Record one upstream throttle for `tool`, honouring a Retry-After if given."""
        now = time.monotonic() if now is None else now
        self.rate_limit_events.append(now)
        self._prune_rate_limit_events(now)
        if retry_after is not None and retry_after > 0:
            horizon = now + min(retry_after, settings.mcp_rate_limit_max_backoff_seconds)
            # Never shorten a horizon the upstream already gave us.
            self.rate_limited_until[tool] = max(horizon, self.rate_limited_until.get(tool, 0.0))

    def rate_limit_count(self, now: float | None = None) -> int:
        """Throttle events still inside the window."""
        self._prune_rate_limit_events(time.monotonic() if now is None else now)
        return len(self.rate_limit_events)

    def rate_limit_failing(self, now: float | None = None) -> bool:
        """True once enough throttles fall inside the window to be believed."""
        return self.rate_limit_count(now) >= max(1, settings.mcp_health_rate_limit_min_events)

    def rate_limit_retry_in(self, tool: str, now: float | None = None) -> float | None:
        """Seconds until `tool` may be called again, or None when not throttled."""
        until = self.rate_limited_until.get(tool)
        if until is None:
            return None
        remaining = until - (time.monotonic() if now is None else now)
        if remaining <= 0:
            self.rate_limited_until.pop(tool, None)
            return None
        return remaining

    def clear_rate_limit(self, tool: str) -> None:
        """A clean result: the upstream accepts this tool's calls again."""
        self.rate_limited_until.pop(tool, None)

    def probe_failing(self) -> bool:
        """True once the probe has failed enough times in a row to be believed.

        A single failure is not a verdict — an upstream hiccup, a rate-limit, a
        restart window. The threshold is what keeps this from being noisier than
        the silence it replaces.
        """
        return self.probe_consecutive_failures >= max(
            1, settings.mcp_health_probe_fail_threshold
        )

    def record_probe_outcome(self, ok: bool, detail: str | None = None) -> None:
        """Record one functional-probe result."""
        self.last_probe_at = time.monotonic()
        self.last_probe_ok = ok
        self.last_probe_detail = None if ok else detail
        self.probe_consecutive_failures = 0 if ok else self.probe_consecutive_failures + 1

    def record_call_outcome(self, ok: bool) -> None:
        """Record one real tool-call outcome for functional-health folding."""
        self.recent_outcomes.append(bool(ok))

    def calls_failing(self) -> bool:
        """True when enough recent calls have been made AND the failure share is at
        or above the configured ratio — 'connected but the calls are dying'."""
        n = len(self.recent_outcomes)
        if n < settings.mcp_health_call_min_samples:
            return False
        failures = sum(1 for ok in self.recent_outcomes if not ok)
        return (failures / n) >= settings.mcp_health_call_fail_ratio


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


def _server_call_timeout(state: Any, tool_name: str | None = None) -> float:
    """The timeout for one tool call: the tool's own entry, then the server's
    (number or `default`), then the global setting."""
    override = getattr(getattr(state, "config", None), "call_timeout", None)
    if isinstance(override, dict):
        override = override[tool_name] if tool_name in override else override.get("default")
    return override if override is not None else settings.mcp_call_timeout


_CALL_TIMEOUT_MIN_S = 1.0
_CALL_TIMEOUT_MAX_S = 3600.0

# The MCP SDK's HTTP transports carry their OWN read timeout (streamable_http and
# sse both default to 300s). A call_timeout above it would still die at the
# transport, as an opaque transport error instead of our clean timeout. Keep the
# transport strictly longer than the call, so the call timeout always fires first.
_SDK_TRANSPORT_READ_TIMEOUT_S = 300.0
_TRANSPORT_READ_MARGIN_S = 30.0


def _transport_read_timeout(config: "MCPServerConfig") -> float:
    """HTTP transport read timeout for a server — never shorter than its
    longest call (one session carries every tool of the server)."""
    configured = config.call_timeout
    if isinstance(configured, dict):
        configured = max(configured.values(), default=None)
    if configured is None:
        return _SDK_TRANSPORT_READ_TIMEOUT_S
    return max(_SDK_TRANSPORT_READ_TIMEOUT_S, configured + _TRANSPORT_READ_MARGIN_S)


def _parse_timeout_seconds(raw: Any, label: str) -> float | None:
    """One timeout value in seconds (env-substituted), or None when unusable."""
    value = _resolve_value(raw)
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        # _resolve_value turns "1"/"0" into booleans; float(True) would silently
        # become a 1-second timeout.
        logger.warning(f"{label}: ignoring boolean-like value {raw!r}")
        return None
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        logger.warning(f"{label}: ignoring unparseable value {value!r}")
        return None
    if not _CALL_TIMEOUT_MIN_S <= seconds <= _CALL_TIMEOUT_MAX_S:
        logger.warning(
            f"{label}: {seconds}s outside [{_CALL_TIMEOUT_MIN_S:.0f}, "
            f"{_CALL_TIMEOUT_MAX_S:.0f}] — using the global default"
        )
        return None
    return seconds


def _parse_call_timeout(raw: Any) -> float | dict[str, float] | None:
    """Parse a server's optional ``call_timeout``: a number, or a mapping
    ``{tool_name: seconds, default: seconds}``.

    Absent, unparseable or out-of-range values => not set (the global default).
    A typo must cost only that override, never the server — same stance as
    ``_parse_health_probe``."""
    if isinstance(raw, dict):
        parsed = {}
        for name, value in raw.items():
            seconds = _parse_timeout_seconds(value, f"call_timeout.{name}")
            if seconds is not None:
                parsed[str(name)] = seconds
        return parsed or None
    return _parse_timeout_seconds(raw, "call_timeout")


def _parse_health_probe(raw: dict | None) -> dict | None:
    """Parse + validate the per-server ``health_probe`` stanza (A1).

    A malformed or disabled stanza yields ``None`` (= not probed) rather than
    raising: a typo in one server's probe config must never stop the whole MCP
    fleet from loading.
    """
    if not raw or not isinstance(raw, dict):
        return None
    if not _resolve_value(raw.get("enabled", True)):
        return None
    tool = raw.get("tool")
    if not tool or not isinstance(tool, str):
        logger.warning("health_probe stanza without a 'tool' name — ignored")
        return None
    expect_raw = raw.get("expect") or {}
    if not isinstance(expect_raw, dict):
        expect_raw = {}
    def _num(value, default, cast):
        """Coerce a numeric option, falling back on anything unparseable.

        These three coercions used to raise on a value like ``interval: 10m``, and
        the exception escaped into the per-entry config try/except — which drops
        the WHOLE server. A typo in a probe's interval would have silently removed
        Paperless from the fleet: no tools, no health entry, nothing. The docstring
        promised otherwise; now the code keeps the promise.
        """
        try:
            return cast(_resolve_value(value)) if value not in (None, "") else default
        except (TypeError, ValueError):
            logger.warning(
                f"health_probe: ignoring unparseable value {value!r}, using {default}"
            )
            return default

    interval = _num(raw.get("interval"), settings.mcp_health_probe_interval, int)
    timeout = _num(raw.get("timeout"), settings.mcp_health_probe_timeout, float)
    min_items = _num(expect_raw.get("min_items"), 0, int)
    return {
        "enabled": True,
        "tool": tool,
        "args": raw.get("args") if isinstance(raw.get("args"), dict) else {},
        # Floor the interval: a probe every few seconds against a real upstream is
        # a load generator, not a check.
        "interval": max(30, interval or settings.mcp_health_probe_interval),
        "timeout": timeout or settings.mcp_health_probe_timeout,
        "expect": {
            # 0 = only "the call came back without an error envelope".
            "min_items": max(0, min_items),
            # Which field to count; None = count the payload itself if it is a list.
            "path": expect_raw.get("path") or None,
        },
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
        # Strong refs to fire-and-forget kiosk tool_health broadcasts scheduled
        # from _set_connected (a sync funnel called from both sync and async
        # contexts), so a task isn't GC'd before it runs.
        self._health_bg_tasks: set[asyncio.Task] = set()

    def _set_connected(self, state: "MCPServerState", connected: bool) -> None:
        """Single funnel for every server-connection flip.

        Sets ``state.connected`` AND, on an actual TRANSITION (not a redundant
        re-assign of the same value), fire-and-forget pushes a content-free
        ``tool_health_changed`` delta to the kiosk hub — so a wall display learns
        a server dropped or a reconnect healed it within one WS round-trip,
        instead of decaying frozen tool-health status. Fire-and-forget: a hub
        failure must never affect MCP operation. Every ``state.connected =`` site
        routes through here so no transition is missed."""
        if state.connected == connected:
            return
        state.connected = connected
        # Check for a running loop BEFORE creating the coroutine, so the
        # config-time / sync path doesn't leave an un-awaited coroutine.
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return  # no running loop (config-time / sync test path)
        task = loop.create_task(
            self._broadcast_tool_health(state.config.name, connected)
        )
        self._health_bg_tasks.add(task)
        task.add_done_callback(self._health_bg_tasks.discard)

    async def _broadcast_tool_health(self, server_name: str, connected: bool) -> None:
        try:
            from api.websocket.kiosk_handler import broadcast_kiosk_event

            # Recompute the folded connectivity+functionality health via the same
            # helper get_status() uses, so a live flip carries the same `health`
            # AND `impaired_code` the snapshot does (the FE reconciles on both).
            state = self._servers.get(server_name)
            if state is not None:
                health, code = self._server_health(server_name, state)
            else:
                health, code = ("healthy" if connected else "down"), None

            await broadcast_kiosk_event(
                {
                    "type": "tool_health_changed",
                    "server": server_name,
                    "connected": connected,
                    "health": health,
                    "impaired_code": code,
                }
            )
        except Exception as e:
            logger.debug(f"kiosk tool_health broadcast failed: {e}")

    def load_config(
        self,
        path: str,
        only: set[str] | None = None,
        overlay_dir: str | None = None,
    ) -> None:
        """Load MCP server configuration from YAML file.

        ``only`` restricts loading to the named servers — used by the
        document-worker's minimal single-server Paperless client so it can spin
        up just that one stdio subprocess without the full 10-server lifecycle
        (see services/paperless_worker_client.py).

        ``overlay_dir`` (default ``settings.mcp_config_overlay_dir``) holds
        instance-local stanzas appended after ``path`` — see
        ``_read_overlay_entries``."""
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

        entries: list[dict] = []
        config_path = Path(path)
        if not config_path.exists():
            logger.warning(f"MCP config file not found: {path}")
        else:
            try:
                with open(config_path) as f:
                    raw = yaml.safe_load(f)
            except Exception as e:
                logger.error(f"Failed to parse MCP config: {e}")
                return
            if raw and raw.get("servers"):
                entries.extend(raw["servers"])

        entries.extend(
            self._read_overlay_entries(
                Path(overlay_dir if overlay_dir is not None else settings.mcp_config_overlay_dir),
                base_names={e.get("name") for e in entries if isinstance(e, dict)},
            )
        )

        if not entries:
            logger.info("MCP config loaded but no servers defined")
            return

        for entry in entries:
            try:
                if only is not None and entry.get("name") not in only:
                    continue
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
                    output_provider=(
                        entry.get("output_provider")
                        if isinstance(entry.get("output_provider"), dict)
                        else None
                    ),
                    streaming=bool(_resolve_value(entry.get("streaming", False))),
                    per_user_auth=bool(_resolve_value(entry.get("per_user_auth", False))),
                    health_probe=_parse_health_probe(entry.get("health_probe")),
                    call_timeout=_parse_call_timeout(entry.get("call_timeout")),
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

    @staticmethod
    def _read_overlay_entries(overlay_dir: Path, base_names: set) -> list[dict]:
        """Server entries from the instance-local overlay directory.

        Each ``*.yaml``/``*.yml`` file (sorted, dotfiles ignored — a mounted
        ConfigMap directory also holds ``..data`` bookkeeping entries) carries its
        own ``servers:`` list. The overlay exists so a server that belongs to ONE
        installation survives the wholesale swap of the shared mcp_servers.yaml on
        every deploy. It must never silently change a shared server, so an entry
        whose name is already defined (in the base file or an earlier overlay
        file) is skipped with an error. A broken file is skipped on its own; the
        other files still load."""
        if not overlay_dir.is_dir():
            return []
        entries: list[dict] = []
        seen = set(base_names)
        files = sorted(
            p for p in overlay_dir.iterdir()
            if p.suffix in (".yaml", ".yml") and not p.name.startswith(".") and p.is_file()
        )
        for file in files:
            try:
                with open(file) as f:
                    raw = yaml.safe_load(f)
            except Exception as e:
                logger.error(f"Failed to parse MCP overlay config {file.name}: {e}")
                continue
            servers = raw.get("servers") if isinstance(raw, dict) else None
            if not isinstance(servers, list):
                logger.error(f"MCP overlay config {file.name} has no 'servers' list, skipping")
                continue
            for entry in servers:
                name = entry.get("name") if isinstance(entry, dict) else None
                if name in seen:
                    logger.error(
                        f"MCP overlay config {file.name}: server '{name}' is already "
                        f"defined, overlay entry skipped"
                    )
                    continue
                seen.add(name)
                entries.append(entry)
            logger.info(f"MCP overlay config loaded: {file.name}")
        return entries

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
        exit_stack: AsyncExitStack | None = None
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

            # Connect based on transport type. The WHOLE transport establishment
            # is bounded by a same-task asyncio.timeout: init/list_tools below were
            # always wait_for-bounded, but the transport __aenter__ itself was not —
            # a pathological upstream could wedge here indefinitely while holding
            # the reconnect_lock, silencing every reconnect path incl. the health
            # monitor's self-heal tick (#1107). asyncio.timeout (not wait_for)
            # keeps the cancellation in THIS task, so anyio cancel scopes entered
            # by the transport contexts stay task-consistent for later teardown.
            if config.transport == MCPTransportType.STREAMABLE_HTTP:
                if not config.url:
                    raise ValueError("URL required for streamable_http transport")
                async with asyncio.timeout(settings.mcp_connect_timeout):
                    transport = await exit_stack.enter_async_context(
                        streamablehttp_client(
                        url=config.url, headers=headers,
                        sse_read_timeout=_transport_read_timeout(config),
                    )
                    )
            elif config.transport == MCPTransportType.SSE:
                if not config.url:
                    raise ValueError("URL required for SSE transport")
                async with asyncio.timeout(settings.mcp_connect_timeout):
                    transport = await exit_stack.enter_async_context(
                        sse_client(
                        url=config.url, headers=headers,
                        sse_read_timeout=_transport_read_timeout(config),
                    )
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
                async with asyncio.timeout(settings.mcp_connect_timeout):
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

            # A STALE stack from a prior session can still be set here (direct
            # refresh_tools reconnects skip the teardown in _reconnect_server) —
            # close it bounded before overwriting, else its transport leaks.
            if state.exit_stack is not None and state.exit_stack is not exit_stack:
                await _close_stack_bounded(state.exit_stack)
            state.session = session
            state.exit_stack = exit_stack
            self._set_connected(state, True)
            state.all_discovered_tools = all_tools
            state.note_discovered_tools()
            state.last_error = None
            # Fresh session → drop the old session's failure history so a reconnect
            # that fixed the upstream isn't left falsely flagged calls_failing.
            state.recent_outcomes.clear()
            # The probe verdict is NOT cleared here. A reconnect proves the
            # transport works; it proves nothing about the service behind it, and
            # Paperless answering HTTP 500 for three days did so across many
            # healthy reconnects. Only a successful probe clears a probe verdict.
            # last_probe_at is left alone too, so a reconnect loop cannot starve
            # the probe by continually resetting its due-time.

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

        except asyncio.CancelledError:
            # Cancelled mid-connect (self-heal hang-guard or shutdown): mark the
            # state honestly and hand the partially-entered transport — plus a
            # possibly still-live STALE stack from a prior session (direct
            # refresh_tools reconnects don't tear down first) — to a detached
            # best-effort closer; awaiting here would insta-cancel, and a rare
            # leak beats a frozen loop. The same slow-upstream condition must
            # advance backoff like an inner-timeout failure does.
            self._set_connected(state, False)
            state.last_error = "connect cancelled (timeout/shutdown)"
            if state.backoff:
                state.backoff.record_failure()
            if state.exit_stack is not None and state.exit_stack is not exit_stack:
                _close_stack_detached(state.exit_stack)
            state.exit_stack = None
            if exit_stack is not None:
                _close_stack_detached(exit_stack)
            raise
        except Exception as e:
            self._set_connected(state, False)
            # str(TimeoutError()) is "" — always keep a meaningful error text.
            state.last_error = str(e) or type(e).__name__

            # Record failure for exponential backoff
            if state.backoff:
                next_delay = state.backoff.record_failure()
                logger.warning(
                    f"MCP server '{config.name}' connection failed: {e} "
                    f"(attempt {state.backoff.attempt_count}, next retry in {next_delay:.1f}s)"
                )
            else:
                logger.warning(f"MCP server '{config.name}' connection failed: {e}")

            # Clean up BOTH stacks on failure, bounded: the LOCAL partially-
            # entered one (previously leaked — only state.exit_stack was closed,
            # which the reconnect path has already torn down), AND a possibly
            # still-live STALE state.exit_stack from a prior session — direct
            # refresh_tools reconnects (a list_tools failure flips connected
            # without teardown) reach here with the old stack still set, and
            # dropping it unclosed would orphan its transport/subprocess.
            if exit_stack is not None:
                await _close_stack_bounded(exit_stack)
            if state.exit_stack is not None and state.exit_stack is not exit_stack:
                await _close_stack_bounded(state.exit_stack)
            state.exit_stack = None
            state.session = None

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
            # (the resource is half-broken — that's why we're here). Bounded:
            # anyio teardown of a broken streamable_http session can hang on
            # stream drain, and this runs under reconnect_lock — an unbounded
            # hang here would freeze every reconnect path for the server.
            if state.exit_stack is not None:
                await _close_stack_bounded(state.exit_stack)
                state.exit_stack = None
                state.session = None
            logger.info(f"MCP reconnecting to '{state.config.name}'...")
            await self._connect_server(state)
            return state.connected

    async def _ensure_connected(self, state: "MCPServerState | None") -> bool:
        """Best-effort on-demand reconnect immediately before a tool call.

        Root-cause fix for "MCP Server X nicht verbunden" after that server's
        pod/subprocess restarted: rather than bailing out and waiting for the
        background refresh tick (or a manual backend restart) to heal the
        session, a tool call attempts ONE reconnect right here. Mirrors
        ``probe_server``'s reconnect-on-failure.

        Transport-agnostic: ``_reconnect_server`` → ``_connect_server`` respawns
        a stdio subprocess or re-establishes a streamable_http session as
        appropriate, so this works for every configured MCP server. Federation
        servers have no session and are dispatched before this is reached.

        Returns True iff the server now has a live session.
        """
        if state is None:
            return False
        if state.connected and state.session is not None:
            return True
        logger.info(
            f"MCP '{state.config.name}' not connected at call time; "
            f"attempting on-demand reconnect"
        )
        return await self._reconnect_server(state)

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
        if state.shielded_by_inflight_call():
            # A call is running inside its timeout, and a probe failure here would
            # reconnect the session underneath it. Leave it alone — but report
            # "skipped", not healthy: a running call is no proof the server works.
            return {"ok": None, "latency_ms": None, "detail": "skipped: call in flight"}

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
        self._set_connected(state, False)
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
            self._set_connected(state, False)
            state.last_error = detail
        return {"ok": ok, "latency_ms": latency, "detail": detail}

    def health_probe_due(self, now: float | None = None) -> list[str]:
        """Names of connected servers whose configured probe interval has elapsed.

        A server with no stanza is never probed — the blast radius of this feature
        is the YAML, not the flag.
        """
        now = time.monotonic() if now is None else now
        due: list[str] = []
        for name, state in self._servers.items():
            probe = state.config.health_probe
            if not probe or not state.connected:
                continue
            if state.config.transport == MCPTransportType.FEDERATION:
                continue
            # per_user_auth servers deny a user_id=None call FAIL-CLOSED, so a probe
            # would report a permanent false failure. Deliberately unprobeable.
            if state.config.per_user_auth:
                continue
            if state.last_probe_at and (now - state.last_probe_at) < probe["interval"]:
                continue
            due.append(name)
        return due

    @staticmethod
    def _evaluate_probe_expectation(message: str | None, expect: dict) -> tuple[bool | None, str | None]:
        """Check a probe result against its declared minimum.

        Returns a TRI-STATE: ``True`` = met, ``False`` = not met, ``None`` = cannot
        be judged (a misconfiguration). The third state matters — inventing a
        failure out of "I could not tell" would fire a critical alert about a
        healthy server, in a subsystem whose whole purpose is making green mean
        green.

        Note what is parsed: ``execute_tool`` returns ``data`` as the list of raw
        MCP *content parts* (``[{"type": ..., "text": ...}]``), NOT the payload. The
        payload lives in the joined ``message`` text, so that is what we parse. An
        earlier version counted the content parts, which made every ``min_items``
        above 1 fail permanently against a perfectly healthy server.

        ``min_items`` of 0 (the default, and what all shipped stanzas use) means
        "the call came back without an error envelope" and needs no payload at all.
        """
        min_items = expect.get("min_items", 0)
        if min_items <= 0:
            return True, None

        if not message:
            return None, "leere Antwort — Mindestanzahl nicht prüfbar"
        try:
            payload = json.loads(message)
        except (ValueError, TypeError):
            # A server answering prose rather than JSON cannot be counted. That is a
            # configuration mistake (wrong tool, or min_items on a prose tool), not
            # evidence of ill health.
            return None, "Antwort ist kein JSON — Mindestanzahl nicht prüfbar"

        container = payload
        path = expect.get("path")
        if path:
            if not isinstance(payload, dict):
                return False, f"erwartetes Feld '{path}' fehlt (Antwort ist kein Objekt)"
            container = payload.get(path)
            if container is None:
                return False, f"erwartetes Feld '{path}' fehlt in der Antwort"
        count = len(container) if isinstance(container, (list, dict, str)) else None
        if count is None:
            return None, "Antwort ist nicht zählbar — Mindestanzahl nicht prüfbar"
        if count < min_items:
            return False, f"nur {count} Einträge, erwartet mindestens {min_items}"
        return True, None

    async def run_health_probe(self, server_name: str) -> dict:
        """Run one server's functional probe and record the verdict on its state.

        Returns ``{"ok": bool, "detail": str | None, "skipped": bool}``. Never
        raises — a probe that explodes must not break the monitor tick that ran it.

        The call goes through ``execute_tool`` with ``user_permissions=None``
        (system call, no user) and ``user_id=None``, which also keeps it out of the
        per-user ``ToolOutcomeStat`` telemetry the kiosk reads — a probe must not
        colour the tool-health numbers it exists to make honest.
        """
        state = self._servers.get(server_name)
        if state is None or not state.config.health_probe:
            return {"ok": True, "detail": None, "skipped": True}
        probe = state.config.health_probe
        namespaced = f"mcp.{server_name}.{probe['tool']}"

        # Resolve EXACTLY. execute_tool has a deliberate fuzzy fallback that
        # substitutes a similar tool when a name misses — helpful for the agent,
        # poison here: a probe that quietly calls a DIFFERENT tool either answers
        # fine while the intended check never ran (false green), or fails schema
        # validation forever (false red). Both defeat the point. A missing tool is a
        # misconfiguration, reported as such, never as ill health.
        if state.all_discovered_tools and not any(
            t.original_name == probe["tool"] for t in state.all_discovered_tools
        ):
            logger.warning(
                f"mcp_health: probe tool '{probe['tool']}' does not exist on "
                f"'{server_name}' — check the health_probe stanza"
            )
            return {"ok": True, "detail": "Sondenwerkzeug existiert nicht", "skipped": True}

        try:
            result = await self.execute_tool(
                namespaced,
                dict(probe.get("args") or {}),
                user_permissions=None,
                user_id=None,
                call_timeout=probe["timeout"],
            )
        except Exception as e:  # noqa: BLE001 — a probe never breaks its caller
            state.record_probe_outcome(False, f"{type(e).__name__}: {e}"[:200])
            logger.warning(f"mcp_health: probe '{namespaced}' raised: {e}")
            return {"ok": False, "detail": state.last_probe_detail, "skipped": False}

        if not result.get("success"):
            raw = str(result.get("message") or "")
            if (
                settings.mcp_health_rate_limit_signal_enabled
                or settings.mcp_rate_limit_backoff_enabled
            ) and _classify_rate_limit(raw)[0]:
                # A throttled upstream is not a dead service. With either Phase-3 flag
                # on, the throttle is owned by that machinery (execute_tool already
                # recorded it), so the probe records no verdict — two failed probes
                # would otherwise report probe_failed for a server that is merely busy.
                # With the backoff gate on this is not optional: the "failure" may be
                # our OWN Retry-After refusal, and counting it would be self-inflicted red.
                # The cadence still advances: re-probing a throttled upstream every
                # tick would only deepen the throttle.
                state.last_probe_at = time.monotonic()
                logger.info(f"mcp_health: probe '{namespaced}' throttled upstream — no verdict")
                return {"ok": True, "detail": "Upstream drosselt — Sonde ohne Urteil", "skipped": True}
            detail = (raw or "Aufruf fehlgeschlagen")[:200]
            state.record_probe_outcome(False, detail)
            return {"ok": False, "detail": detail, "skipped": False}

        verdict, reason = self._evaluate_probe_expectation(
            result.get("message"), probe["expect"]
        )
        if verdict is None:
            # Cannot judge → record NOTHING. Neither a false failure nor a false
            # success; the warning is the signal, and the stanza needs fixing.
            logger.warning(f"mcp_health: probe '{namespaced}' inconclusive: {reason}")
            return {"ok": True, "detail": reason, "skipped": True}
        state.record_probe_outcome(verdict, reason)
        return {"ok": verdict, "detail": reason, "skipped": False}

    @staticmethod
    def _note_rate_limit(
        state: "MCPServerState", tool: str, text: str | None, exc: BaseException | None = None
    ) -> None:
        """Record an upstream throttle if this ERROR is one (Phase 3).

        Both flags off → records nothing, so the default path stays byte-identical.
        The signal flag needs the events for the health verdict; the backoff flag
        needs the Retry-After horizon — either one is reason enough to record.
        """
        if not (
            settings.mcp_health_rate_limit_signal_enabled
            or settings.mcp_rate_limit_backoff_enabled
        ):
            return
        response = getattr(exc, "response", None)
        status = getattr(response, "status_code", None)
        headers = getattr(response, "headers", None)
        header = headers.get("retry-after") if hasattr(headers, "get") else None
        limited, retry_after = _classify_rate_limit(
            text,
            status_code=status if isinstance(status, int) else None,
            retry_after_header=header,
        )
        if not limited:
            return
        state.record_rate_limit(tool, retry_after)
        logger.warning(
            f"MCP upstream rate limit: mcp.{state.config.name}.{tool}"
            + (f" (Retry-After {retry_after:.0f}s)" if retry_after else "")
        )

    def record_external_probe(self, server_name: str, ok: bool, detail: str | None) -> None:
        """Record a verdict from a PURPOSE-BUILT probe that lives outside this class.

        Some servers cannot be honestly probed by a generic tool call. ``search`` is
        the worked example: a bare result count is a documented false-green because
        Wikipedia answers almost anything, so ``services/search_health.py`` probes
        SearXNG's JSON API directly and counts distinct contributing engines. That
        verdict used to dead-end in ``internal.system_health``, i.e. it was only ever
        seen if a human thought to ask. This funnels it into the SAME state the
        generic probe writes, so one channel feeds the kiosk, the alert and the
        health tool alike.
        """
        state = self._servers.get(server_name)
        if state is None:
            return
        state.record_probe_outcome(ok, detail)

    def _check_tool_permission(
        self,
        tool_info: MCPToolInfo,
        user_permissions: list[str] | None,
    ) -> str | None:
        """
        Check if user has permission to call this MCP tool.

        Returns None if allowed, or an error message string if denied.

        Permission resolution order:
        1. user_permissions is None → allow. None means "no permission model in
           effect": AUTH_ENABLED=false (single-user) OR an intentionally
           unidentified caller (anonymous/guest voice turn, device/satellite)
           that ha_glue deliberately keeps allowed so spoken commands work. A
           permission *load failure* is NOT represented as None — the caller
           substitutes `[]` (no permissions → denied below), so the fail-open
           path here can't be reached by an error. (#690)
        2. "mcp.*" in user_permissions → allow (admin wildcard)
        3. tool_permissions has mapping for this tool → check specific permission
        4. permissions defined (server-level) → check if user has at least one
        5. Nothing defined → convention: check "mcp.<server_name>" in user_permissions
        6. No match / empty list → denied
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

    async def _call_tool_per_user_session(
        self, state: "MCPServerState", tool_name: str, arguments: dict, auth_headers: dict
    ):
        """Open a short-lived per-user MCP session (its OWN request headers) and
        call one tool, then tear it down.

        The MCP SDK binds auth at connection init — there is no per-request
        header override on a shared session — so a per-user call needs its own
        session (design note: modelcontextprotocol/python-sdk#1434). Correctness
        over reuse: the session is per-call, never cached, because a cached
        per-user session would serve a stale/rotated token — a security bug, not
        a perf win. A bounded per-user session cache is a deferred optimization.
        Only `streamable_http` / `sse` are eligible (a per-user stdio subprocess
        is nonsensical). `auth_headers` (from the resolver) are merged over the
        server's static headers — typically `Authorization`, plus any companion
        header a provider needs (e.g. `X-Atlassian-Cloud-Id`)."""
        from mcp import ClientSession
        from mcp.client.sse import sse_client
        from mcp.client.streamable_http import streamablehttp_client

        config = state.config
        headers = dict(config.headers)
        headers.update(auth_headers)

        async with AsyncExitStack() as stack:
            if config.transport == MCPTransportType.STREAMABLE_HTTP:
                if not config.url:
                    raise ValueError("URL required for streamable_http transport")
                transport = await stack.enter_async_context(
                    streamablehttp_client(
                        url=config.url, headers=headers,
                        sse_read_timeout=_transport_read_timeout(config),
                    )
                )
            elif config.transport == MCPTransportType.SSE:
                if not config.url:
                    raise ValueError("URL required for SSE transport")
                transport = await stack.enter_async_context(
                    sse_client(
                        url=config.url, headers=headers,
                        sse_read_timeout=_transport_read_timeout(config),
                    )
                )
            else:
                raise ValueError(
                    f"per_user_auth requires streamable_http/sse, got "
                    f"{config.transport}"
                )
            # (read, write) or (read, write, get_session_id) — mirror _connect_server
            if len(transport) == 3:
                read_stream, write_stream, _ = transport
            else:
                read_stream, write_stream = transport
            session = await stack.enter_async_context(
                ClientSession(read_stream, write_stream)
            )
            await session.initialize()
            return await session.call_tool(tool_name, arguments)

    async def execute_tool(
        self,
        namespaced_name: str,
        arguments: dict,
        user_permissions: list[str] | None = None,
        user_id: int | None = None,
        progress_sink: ProgressSink | None = None,
        truncate: bool = True,
        call_timeout: float | None = None,
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
            truncate: Cap the response at ``mcp_max_response_size`` (default
                True). Truncation exists to protect the LLM context window; a
                programmatic caller fetching binary payloads (e.g.
                ``download_document``'s base64 file bytes) must pass
                ``truncate=False`` or the JSON envelope is byte-cut mid-payload
                and becomes unparseable for any non-trivial file.

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

        # Self-heal a session that went down since the last call (e.g. the
        # server's pod restarted) instead of failing the call outright.
        if not await self._ensure_connected(state):
            return {
                "success": False,
                "message": f"MCP Server '{tool_info.server_name}' nicht verbunden",
                "data": None,
            }

        # === Upstream Retry-After (Phase 3, dark) ===
        # Honour a Retry-After the upstream gave for THIS tool: refuse at once rather
        # than send a request we already know will be rejected. Deliberately NO
        # transparent wait-and-retry: a 429 inside a tool can follow side effects of
        # that same call, and re-running a mutating tool is the double execution
        # _is_session_dead is careful to avoid. Not recorded as a new throttle event
        # either — our own refusal is not evidence from the upstream, and counting it
        # would let the gate keep the server red by itself.
        if settings.mcp_rate_limit_backoff_enabled:
            retry_in = state.rate_limit_retry_in(tool_info.original_name)
            if retry_in is not None:
                return {
                    "success": False,
                    "message": (
                        f"Upstream-Rate-Limit für {namespaced_name}: "
                        f"erneut versuchen in {max(1, round(retry_in))} s"
                    ),
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

        # Per-call timeout override for deliberately-blocking poll tools (e.g.
        # paperless await_consume_result, which waits out a slow Paperless consume
        # that can exceed the default 30s — the timeout that drove the 2026-07
        # duplicate-upload loop). Then the server's own `call_timeout`, then the
        # global setting.
        effective_timeout = (
            call_timeout
            if call_timeout is not None
            else _server_call_timeout(state, tool_info.original_name)
        )

        # Per-user auth (per-user data scoping). When the server opts in, the
        # call must run under THIS user's credential, not the shared operator
        # one. FAIL-CLOSED: no identity or no provisioned credential → deny,
        # never fall through to the shared session (that fall-through would be
        # the confused deputy the whole model removes). The headers are resolved
        # once here; `_do_call` opens a short-lived per-user session with them.
        per_user_headers: dict | None = None
        if state.config.per_user_auth:
            if user_id is None:
                logger.warning(
                    f"🔒 per-user MCP: {namespaced_name} needs an identified "
                    f"caller — denying unauthenticated turn"
                )
                return {
                    "success": False,
                    "message": "Authentication required for this tool.",
                    "data": None,
                }
            try:
                per_user_headers = await _resolve_user_auth_headers(
                    state.config.name, user_id
                )
            except Exception as e:  # noqa: BLE001 — resolver must never wedge a call
                logger.error(
                    f"per-user MCP: auth resolver failed for "
                    f"{state.config.name}/user={user_id}: {e}"
                )
                per_user_headers = None
            if not per_user_headers:
                logger.warning(
                    f"🔒 per-user MCP: no credential for user={user_id} on "
                    f"'{state.config.name}' — denying {namespaced_name}"
                )
                return {
                    "success": False,
                    "message": (
                        "No credential is connected for this service. "
                        "Connect it in your account settings to continue."
                    ),
                    "data": None,
                }

        async def _do_call() -> Any:
            # Counted in flight so refresh_tools / probe_server do not reconnect
            # this session underneath the call; released on every exit path.
            deadline = time.monotonic() + effective_timeout
            state.inflight_deadlines.append(deadline)
            try:
                if per_user_headers:
                    return await asyncio.wait_for(
                        self._call_tool_per_user_session(
                            state, tool_info.original_name, arguments, per_user_headers
                        ),
                        timeout=effective_timeout,
                    )
                return await asyncio.wait_for(
                    state.session.call_tool(tool_info.original_name, arguments),
                    timeout=effective_timeout,
                )
            finally:
                state.inflight_deadlines.remove(deadline)

        # Try once; on a session-death signal (transport exception OR the
        # streamable_http "Session terminated" McpError after a server bounce —
        # see _is_session_dead) reconnect and retry once. Genuine application
        # errors (other McpError, validation) and timeouts fall through
        # immediately: reconnecting wouldn't help and would tear down a healthy
        # session over a malformed argument.
        result = None
        last_exc: BaseException | None = None
        for attempt in range(2):
            try:
                result = await _do_call()
                last_exc = None
                break
            except TimeoutError:
                logger.error(f"MCP tool call timeout: {namespaced_name}")
                # Functional-health signal: a timeout means the server/upstream did
                # not respond — health-correlated (unlike an app-level error result).
                state.record_call_outcome(False)
                return {
                    "success": False,
                    "message": f"Tool-Aufruf Timeout: {namespaced_name}",
                    "data": None,
                }
            except Exception as e:  # noqa: BLE001 - bubble in last_exc
                last_exc = e
                if not _is_session_dead(e):
                    # Application-level error (McpError, schema, etc.). The
                    # session is fine; just surface the failure.
                    logger.error(f"MCP tool call failed: {namespaced_name}: {e}")
                    state.last_error = str(e)
                    self._note_rate_limit(state, tool_info.original_name, str(e), exc=e)
                    break
                if attempt == 0:
                    logger.warning(
                        f"MCP session died on {namespaced_name}: {type(e).__name__}: {e}; "
                        f"reconnecting and retrying once"
                    )
                    self._set_connected(state, False)
                    state.last_error = str(e)
                    reconnected = await self._reconnect_server(state)
                    if not reconnected:
                        break
                    continue
                logger.error(
                    f"MCP tool call failed after reconnect: {namespaced_name}: {e}"
                )
                self._set_connected(state, False)
                state.last_error = str(e)

        if result is None:
            # NOT recorded as a health failure: this path is either an app-level
            # exception (session is fine — an app error, not the server's health) or
            # a session death that already flipped connected=False (→ "down" via
            # connectivity). Counting it would flag a healthy server whose tool just
            # errored. Only timeouts + genuine successes drive calls_failing.
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
                content_parts.append(_truncate_response(text) if truncate else text)
            raw_data.append(
                {"type": getattr(item, "type", "unknown"), "text": text}
            )

        message = "\n".join(content_parts) if content_parts else "Tool executed"

        # Truncate final message if still too large
        if truncate:
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

        # Functional-health signal: record ONLY a clean result as a success. An
        # isError result (protocol or inner-JSON) is an APPLICATION outcome — a
        # device off, a parcel not found, a workflow that returned success:false —
        # NOT a statement about the server's health, so it is deliberately not
        # recorded (else a burst of legitimate app errors would falsely flag the
        # server calls_failing). Only a timeout is counted as a health failure.
        if not is_error:
            state.record_call_outcome(True)
            # The upstream accepts this tool again — lift any Retry-After horizon.
            state.clear_rate_limit(tool_info.original_name)
        else:
            # Phase 3: an ERROR result may be an upstream throttle. Classified only
            # here and on app exceptions, never on a success, whose payload may
            # legitimately talk about rate limits.
            self._note_rate_limit(state, tool_info.original_name, message)

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
            # F-ID-1: pass the authenticated asker so the asker side can attach
            # the person-scoped querier_ref (dark unless the link + flag exist).
            async for item in asker.query_peer(peer, query_text, user_id=user_id):
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

        # Self-heal a session that went down since the last call (e.g. the
        # server's pod restarted) instead of failing the call outright.
        if not await self._ensure_connected(state):
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
            asyncio.wait_for(
                call_coro, timeout=_server_call_timeout(state, tool_info.original_name)
            )
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
            self._set_connected(state, False)
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
        """Return status information for all servers.

        Each server carries a synthesized ``health`` ∈ {healthy, degraded, down}
        that folds CONNECTIVITY and FUNCTIONALITY together, so the kiosk shows a
        node connected-but-impaired (e.g. a backing plugin failed to load, or the
        server exposes zero tools) as *degraded* rather than a green *healthy*.
        ``impaired_reason`` carries a short human string when degraded-while-up.
        """
        plugin_failed = self._impaired_servers()
        servers = []
        for name, state in self._servers.items():
            health, code = self._server_health(name, state, plugin_failed)
            server_info = {
                "name": name,
                "transport": state.config.transport.value,
                "connected": state.connected,
                "tool_count": len(state.tools),
                "total_tool_count": len(state.all_discovered_tools),
                "last_error": state.last_error,
                "health": health,
            }
            if code is not None:
                server_info["impaired_code"] = code
            # Evidence the alert path needs to judge the verdict (additive fields).
            if code == "no_tools":
                age = state.no_tools_age()
                if age is not None:
                    server_info["no_tools_for_seconds"] = round(age, 1)
            elif code == "rate_limited":
                server_info["rate_limit_events"] = state.rate_limit_count()
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

    def _server_health(
        self, name: str, state: "MCPServerState", plugin_failed: set[str] | None = None
    ) -> tuple[str, str | None]:
        """Fold CONNECTIVITY and FUNCTIONALITY into (health, impaired_code).

        health ∈ {healthy, degraded, down}; impaired_code is a stable machine code
        (the frontend localizes it — never a human string, per the i18n rule) set
        only when degraded-while-connected: ``plugin_failed`` (a bound startup
        plugin didn't load) or ``no_tools`` (the server DISCOVERED no tools —
        checked against all_discovered_tools, NOT the active-tool filter, so a
        healthy server whose prompt_tools/override filtered everything out is not
        falsely flagged). Federation servers manage their single tool out of band
        → exempt from no_tools. Shared by get_status() and _broadcast_tool_health.
        """
        if not state.connected:
            return "down", None
        if plugin_failed is None:
            plugin_failed = self._impaired_servers()
        if name in plugin_failed:
            return "degraded", "plugin_failed"
        if state.config.transport != MCPTransportType.FEDERATION and not state.all_discovered_tools:
            return "degraded", "no_tools"
        # Functional probe (A1) — a call WE chose, that must succeed. Checked BEFORE
        # calls_failing because it is the more trustworthy signal: calls_failing is
        # inferred from whatever the agent happened to call and counts only timeouts,
        # while a failed probe is direct evidence that the service behind a green
        # transport is not working. This is the signal that would have caught
        # Paperless answering HTTP 500 on its first day instead of its fourth.
        if state.probe_failing():
            return "degraded", "probe_failed"
        # Upstream rate-limit (Phase 3, dark) — direct evidence from the upstream, so
        # it outranks the inferred calls_failing; below probe_failed, because a dead
        # service is the worse news. Windowed, so it clears once throttling stops.
        if (
            settings.mcp_health_rate_limit_signal_enabled
            and state.config.transport != MCPTransportType.FEDERATION
            and state.rate_limit_failing()
        ):
            return "degraded", "rate_limited"
        # Functional health (Phase 2): connected + list_tools present, but the recent
        # real tool calls are mostly failing → the transport is green but the upstream
        # is dead. Federation exempt (its single tool is managed out of band).
        if state.config.transport != MCPTransportType.FEDERATION and state.calls_failing():
            return "degraded", "calls_failing"
        return "healthy", None

    def _impaired_servers(self) -> set[str]:
        """MCP-server names whose bound startup plugin FAILED to load. Driven by
        ``settings.plugin_mcp_bindings`` ("plugin_prefix=server_name", comma-
        separated — ``=`` not ``:`` because plugin specs themselves contain a
        colon, so ``:`` mis-splits a full spec used as the prefix). Empty by
        default → no impairments from this source (public build unaffected).
        The prefix is matched via spec.startswith(), so either the module prefix
        (``twin_adapter``) or the full spec (``twin_adapter.plugin:register``)
        works as the left side."""
        raw = (settings.plugin_mcp_bindings or "").strip()
        if not raw:
            return set()
        try:
            from api.lifecycle import failed_plugins
        except Exception:
            return set()
        failed = failed_plugins()
        if not failed:
            return set()
        impaired: set[str] = set()
        for pair in raw.split(","):
            if "=" not in pair:
                continue
            prefix, server = (p.strip() for p in pair.split("=", 1))
            if not prefix or not server:
                continue
            if any(spec.startswith(prefix) for spec in failed):
                impaired.add(server)
        return impaired

    async def refresh_tools(self) -> None:
        """Refresh tool lists from all connected servers and reconnect failed ones."""
        for state in self._servers.values():
            # Federation-transport servers have no MCP session; their
            # single `query_brain` tool is managed by PeerMCPRegistry,
            # not discovered via list_tools. Skip explicitly so future
            # refactors don't accidentally include them.
            if state.config.transport == MCPTransportType.FEDERATION:
                continue
            if state.shielded_by_inflight_call():
                # A call is running on this session, still inside its timeout. A server busy answering it
                # may be slow to answer list_tools too; reading that as a dead
                # session would disconnect it and tear the call down. The next
                # tick checks it again.
                logger.debug(
                    f"MCP refresh skipped for '{state.config.name}': "
                    f"{state.inflight_calls} call(s) in flight"
                )
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
                    state.note_discovered_tools()

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
                    self._set_connected(state, False)
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
                # Bounded like every other teardown site: a session wedged on
                # stream drain (the #1107 failure mode) must not block graceful
                # shutdown past the k8s grace period — later shutdown steps
                # (plugin hooks, cleanup) still have to run.
                await _close_stack_bounded(state.exit_stack)
            self._set_connected(state, False)
            state.session = None
            state.exit_stack = None

        self._tool_index.clear()
        logger.info("MCP manager shut down")
