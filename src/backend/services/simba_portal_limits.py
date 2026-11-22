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

The portal states the limit per UPLOAD CONTEXT (Kategorie + Typ: the MCP reads
it from that context's uploader state, and validates an upload against the
context it goes to), so it is asked and cached per (category, type) -- a
confirm is judged against the category the owner chose, never another one.

``check_connection`` logs into the portal (seconds when cold), so the answer is
cached: an hour when known, a minute when not, one call in flight per context.
Every caller waits at most ``DEFAULT_WAIT_SECONDS`` for a cold fetch (the fetch
runs on and fills the cache); a slower portal means unknown, i.e. no pre-check.
"""
from __future__ import annotations

import asyncio
import json
import math
import time
from dataclasses import dataclass

from loguru import logger

from utils.log_safety import error_text_without_content

_TOOL = "mcp.simba.check_connection"
_KNOWN_TTL_SECONDS = 3600.0
_UNKNOWN_TTL_SECONDS = 60.0
_BYTES_PER_MB = 1024 * 1024  # the MCP's own unit (validate.ts: MB * 1024 * 1024)
#: How long a request waits for a cold fetch (a portal login). The MCP call's
#: own timeout is far longer; a request must not hang on it.
DEFAULT_WAIT_SECONDS = 5.0


@dataclass(frozen=True)
class PortalLimits:
    max_file_mb: float
    #: The upload context the limit was stated for.
    category: str
    type: str

    @property
    def max_file_bytes(self) -> int:
        return int(self.max_file_mb * _BYTES_PER_MB)


_Context = tuple[str, str]
_cached: dict[_Context, tuple[float, PortalLimits | None]] = {}  # (expires_at, value)
_inflight: dict[_Context, asyncio.Task] = {}


def reset_for_tests() -> None:
    _cached.clear()
    _inflight.clear()


def parse_limits(result, category: str, type_: str) -> PortalLimits | None:
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
    return PortalLimits(max_file_mb=float(mb), category=category, type=type_)


async def _fetch(mcp_manager, context: _Context) -> PortalLimits | None:
    category, type_ = context
    limits: PortalLimits | None = None
    try:
        limits = parse_limits(
            await mcp_manager.execute_tool(_TOOL, {"category": category, "type": type_}), category, type_,
        )
    except Exception as e:
        logger.warning(f"simba limits: {_TOOL} failed: {error_text_without_content(e)}")
    if limits is None:
        logger.info("simba limits: the portal stated none -- no size pre-check for now")
    ttl = _KNOWN_TTL_SECONDS if limits else _UNKNOWN_TTL_SECONDS
    _cached[context] = (time.monotonic() + ttl, limits)
    return limits


async def portal_limits(
    mcp_manager, category: str | None, type_: str | None, *, wait: float | None = None,
) -> PortalLimits | None:
    """The portal's limits for uploads to ``category`` / ``type_``, cached.
    ``wait`` (default ``DEFAULT_WAIT_SECONDS``, never unbounded) is how long a
    caller waits for a cold fetch; the fetch itself
    runs on and fills the cache. None = unknown -- also without a manager or
    without a context to ask for (no category chosen yet)."""
    if mcp_manager is None or not (category or "").strip() or not (type_ or "").strip():
        return None
    context = (category.strip(), type_.strip())
    hit = _cached.get(context)
    if hit is not None and hit[0] > time.monotonic():
        return hit[1]
    task = _inflight.get(context)
    if task is None or task.done():
        task = _inflight[context] = asyncio.create_task(_fetch(mcp_manager, context))
    try:
        return await asyncio.wait_for(
            asyncio.shield(task), timeout=DEFAULT_WAIT_SECONDS if wait is None else wait,
        )
    except TimeoutError:
        return None


@dataclass(frozen=True)
class TooLarge:
    """A file over the portal's limit. Data, not text: the UI renders it in the
    viewer's language; ``message()`` is the German fallback for an API detail."""

    size_mb: float
    max_mb: float
    #: The upload context it was judged against (the limit is per context).
    category: str
    type: str
    code: str = "too_large"

    def as_detail(self) -> dict:
        """The body of the 422 -- the same data the review list carries, plus
        the German fallback sentence for a client that cannot render it."""
        return {
            "code": self.code, "size_mb": self.size_mb, "max_mb": self.max_mb,
            "category": self.category, "type": self.type, "message": self.message(),
        }

    def message(self) -> str:
        size = f"{self.size_mb:.1f}".replace(".", ",")
        limit = f"{self.max_mb:g}".replace(".", ",")
        return (
            f"Die Datei ist {size} MB groß, das Simba-Portal nimmt höchstens {limit} MB an. "
            "Bitte verkleinern (z. B. mit geringerer Scan-Auflösung) oder aufteilen."
        )


def too_large(size_bytes: int, limits: PortalLimits | None) -> TooLarge | None:
    """Why a file of ``size_bytes`` cannot go to Simba, or None when it fits or
    the limits are unknown. Carries no file name. The size is rounded UP to a
    tenth, so a file just over the limit never reads as equal to it."""
    if limits is None or size_bytes <= limits.max_file_bytes:
        return None
    return TooLarge(
        size_mb=math.ceil(size_bytes / _BYTES_PER_MB * 10) / 10, max_mb=limits.max_file_mb,
        category=limits.category, type=limits.type,
    )
