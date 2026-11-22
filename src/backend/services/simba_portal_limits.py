"""The Simba portal's upload limits, as the portal itself states them (#1609).

The simba MCP reads the limits from the portal's uploader state after login
and reports them in ``check_connection`` (``maxDateigroesseMB``). The portal is
the authority: Renfield never hard-codes 15 MB, it asks.

Used to tell the owner BEFORE the confirm that a proposal can never land (the
review list and the send overlay show the reason instead of a confirm button)
and to refuse such a confirm with a validation error instead of letting the
upload fail as a bad gateway. It is a pre-check, not the gate: the MCP still
validates every upload, so unknown limits (MCP down, old MCP, unparsable
answer) fail OPEN -- no pre-check, the upload path behaves as before.

``check_connection`` logs into the portal (seconds when cold), so the answer is
cached: an hour when known, a minute when not, and one call in flight at a time.
"""
from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass

from loguru import logger

from utils.log_safety import error_text_without_content

_TOOL = "mcp.simba.check_connection"
_KNOWN_TTL_SECONDS = 3600.0
_UNKNOWN_TTL_SECONDS = 60.0
_BYTES_PER_MB = 1024 * 1024  # the MCP's own unit (validate.ts: MB * 1024 * 1024)


@dataclass(frozen=True)
class PortalLimits:
    max_file_mb: float

    @property
    def max_file_bytes(self) -> int:
        return int(self.max_file_mb * _BYTES_PER_MB)


_cached: tuple[float, PortalLimits | None] | None = None  # (expires_at, value)
_inflight: asyncio.Task | None = None


def reset_for_tests() -> None:
    global _cached, _inflight
    _cached = None
    _inflight = None


def parse_limits(result) -> PortalLimits | None:
    """The limits from a ``check_connection`` envelope, or None when it does not
    state a usable one (a failed call, an error text, an older MCP)."""
    if not isinstance(result, dict) or not result.get("success"):
        return None
    raw = result.get("message")
    try:
        inner = json.loads(raw) if isinstance(raw, str) else None
    except (TypeError, ValueError):
        return None
    if not isinstance(inner, dict) or inner.get("status") != "ok":
        return None
    mb = inner.get("maxDateigroesseMB")
    if isinstance(mb, bool) or not isinstance(mb, (int, float)) or mb <= 0:
        return None
    return PortalLimits(max_file_mb=float(mb))


async def _fetch(mcp_manager) -> PortalLimits | None:
    global _cached
    limits: PortalLimits | None = None
    try:
        limits = parse_limits(await mcp_manager.execute_tool(_TOOL, {}))
    except Exception as e:
        logger.warning(f"simba limits: {_TOOL} failed: {error_text_without_content(e)}")
    if limits is None:
        logger.info("simba limits: the portal stated none -- no size pre-check for now")
    ttl = _KNOWN_TTL_SECONDS if limits else _UNKNOWN_TTL_SECONDS
    _cached = (time.monotonic() + ttl, limits)
    return limits


async def portal_limits(mcp_manager, *, wait: float | None = None) -> PortalLimits | None:
    """The portal's limits, cached. ``wait`` bounds how long a caller waits for a
    cold fetch (the list does not hold the page for a portal login); the fetch
    itself runs on and fills the cache. None = unknown."""
    global _inflight
    if mcp_manager is None:
        return None
    if _cached is not None and _cached[0] > time.monotonic():
        return _cached[1]
    if _inflight is None or _inflight.done():
        _inflight = asyncio.create_task(_fetch(mcp_manager))
    try:
        return await asyncio.wait_for(asyncio.shield(_inflight), timeout=wait)
    except TimeoutError:
        return None


@dataclass(frozen=True)
class TooLarge:
    """A file over the portal's limit. Data, not text: the UI renders it in the
    viewer's language; ``message()`` is the German fallback for an API detail."""

    size_mb: float
    max_mb: float
    code: str = "too_large"

    def message(self) -> str:
        size = f"{self.size_mb:.1f}".replace(".", ",")
        limit = f"{self.max_mb:g}".replace(".", ",")
        return (
            f"Die Datei ist {size} MB groß, das Simba-Portal nimmt höchstens {limit} MB an. "
            "Bitte verkleinern (z. B. mit geringerer Scan-Auflösung) oder aufteilen."
        )


def too_large(size_bytes: int, limits: PortalLimits | None) -> TooLarge | None:
    """Why a file of ``size_bytes`` cannot go to Simba, or None when it fits or
    the limits are unknown. Carries no file name."""
    if limits is None or size_bytes <= limits.max_file_bytes:
        return None
    return TooLarge(size_mb=round(size_bytes / _BYTES_PER_MB, 1), max_mb=limits.max_file_mb)
