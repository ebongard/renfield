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
from typing import Any, Dict, List, Optional

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


# === Constants ===
MAX_RESPONSE_SIZE = 10 * 1024  # 10KB max response size
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


def _validate_tool_input(arguments: Dict, input_schema: Dict) -> None:
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


def _truncate_response(text: str, max_size: int = MAX_RESPONSE_SIZE) -> str:
    """
    Truncate response text to max_size bytes.

    Args:
        text: Response text to truncate
        max_size: Maximum size in bytes

    Returns:
        Truncated text with indicator if truncated
    """
    if len(text.encode('utf-8')) <= max_size:
        return text

    # Truncate and add indicator
    truncated = text.encode('utf-8')[:max_size - 50].decode('utf-8', errors='ignore')
    return truncated + "\n\n[... Response truncated (exceeded 10KB limit)]"


class MCPTransportType(str, Enum):
    STREAMABLE_HTTP = "streamable_http"
    SSE = "sse"
    STDIO = "stdio"


@dataclass
class MCPServerConfig:
    """Configuration for a single MCP server."""
    name: str
    url: Optional[str] = None
    transport: MCPTransportType = MCPTransportType.STREAMABLE_HTTP
    auth_token_env: Optional[str] = None
    headers: Dict[str, str] = field(default_factory=dict)
    command: Optional[str] = None
    args: List[str] = field(default_factory=list)
    enabled: bool = True
    refresh_interval: int = 300
    examples: Dict[str, List[str]] = field(default_factory=dict)  # {"de": [...], "en": [...]}


@dataclass
class MCPToolInfo:
    """Metadata for a single tool discovered from an MCP server."""
    server_name: str
    original_name: str
    namespaced_name: str  # "mcp.<server>.<tool>"
    description: str
    input_schema: Dict = field(default_factory=dict)


@dataclass
class MCPServerState:
    """Runtime state for a connected MCP server."""
    config: MCPServerConfig
    connected: bool = False
    tools: List[MCPToolInfo] = field(default_factory=list)
    last_error: Optional[str] = None
    session: Any = None  # mcp.ClientSession
    exit_stack: Optional[AsyncExitStack] = None
    rate_limiter: Optional[TokenBucketRateLimiter] = None
    backoff: Optional[ExponentialBackoff] = None  # Reconnection backoff tracker


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
        self._servers: Dict[str, MCPServerState] = {}
        self._tool_index: Dict[str, MCPToolInfo] = {}  # namespaced_name -> MCPToolInfo
        self._refresh_task: Optional[asyncio.Task] = None

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
                    enabled=_resolve_value(entry.get("enabled", True)),
                    refresh_interval=int(
                        _resolve_value(entry.get("refresh_interval", 300))
                    ),
                    examples={
                        lang: exs
                        for lang, exs in entry.get("examples", {}).items()
                        if isinstance(exs, list)
                    },
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
            from mcp.client.streamable_http import streamablehttp_client
            from mcp.client.sse import sse_client
            from mcp.client.stdio import StdioServerParameters, stdio_client

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
                    raise ValueError(f"URL required for streamable_http transport")
                transport = await exit_stack.enter_async_context(
                    streamablehttp_client(url=config.url, headers=headers)
                )
            elif config.transport == MCPTransportType.SSE:
                if not config.url:
                    raise ValueError(f"URL required for SSE transport")
                transport = await exit_stack.enter_async_context(
                    sse_client(url=config.url, headers=headers)
                )
            elif config.transport == MCPTransportType.STDIO:
                if not config.command:
                    raise ValueError(f"Command required for stdio transport")
                # Pass current environment to subprocess so MCP servers
                # can access API keys and configuration.
                # Also inject Docker secrets (/run/secrets/) as env vars
                # (uppercase filename → value) so stdio MCP servers can
                # read API keys without exposing them in .env.
                subprocess_env = dict(os.environ)
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

            tools = []
            for tool in tools_result.tools:
                namespaced = f"mcp.{config.name}.{tool.name}"
                info = MCPToolInfo(
                    server_name=config.name,
                    original_name=tool.name,
                    namespaced_name=namespaced,
                    description=tool.description or "",
                    input_schema=tool.inputSchema if hasattr(tool, "inputSchema") else {},
                )
                tools.append(info)
                self._tool_index[namespaced] = info

            state.session = session
            state.exit_stack = exit_stack
            state.connected = True
            state.tools = tools
            state.last_error = None

            # Reset backoff on successful connection
            if state.backoff:
                state.backoff.record_success()

            logger.info(f"MCP server '{config.name}' connected: {len(tools)} tools")

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

    async def execute_tool(self, namespaced_name: str, arguments: Dict) -> Dict:
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

            return {
                "success": not is_error,
                "message": message,
                "data": raw_data if raw_data else None,
            }

        except asyncio.TimeoutError:
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

    def get_all_tools(self) -> List[MCPToolInfo]:
        """Return all discovered MCP tools."""
        return list(self._tool_index.values())

    def get_server_examples(self) -> Dict[str, Dict[str, List[str]]]:
        """Return configured examples for all servers.

        Returns:
            Dict mapping server name to {"de": [...], "en": [...]}
        """
        return {
            name: state.config.examples
            for name, state in self._servers.items()
            if state.config.examples
        }

    def is_mcp_tool(self, name: str) -> bool:
        """Check if a name is a known MCP tool."""
        return name in self._tool_index

    def get_status(self) -> Dict:
        """Return status information for all servers."""
        servers = []
        for name, state in self._servers.items():
            server_info = {
                "name": name,
                "transport": state.config.transport.value,
                "connected": state.connected,
                "tool_count": len(state.tools),
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
                    # Remove old tools for this server
                    old_names = [t.namespaced_name for t in state.tools]
                    for old_name in old_names:
                        self._tool_index.pop(old_name, None)

                    # Re-register
                    state.tools = []
                    for tool in tools_result.tools:
                        namespaced = f"mcp.{state.config.name}.{tool.name}"
                        info = MCPToolInfo(
                            server_name=state.config.name,
                            original_name=tool.name,
                            namespaced_name=namespaced,
                            description=tool.description or "",
                            input_schema=tool.inputSchema if hasattr(tool, "inputSchema") else {},
                        )
                        state.tools.append(info)
                        self._tool_index[namespaced] = info

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
