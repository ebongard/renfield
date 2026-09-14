"""Backfill the Paperless ``created`` date for filed documents whose post-consume
PATCH never ran — the testable core of ``bin/backfill_paperless_metadata.py
--mode created-date``.

**Who is affected.** With ``wait_for_consume=False`` Paperless cannot set
``created`` until the consume task yields a document id, so the folder-ingest leg
reapplies the extracted date in a post-consume ``update_document``. Documents that
settled via the idempotent task_id RE-POLL (``_settle_from_outcome``, Fix A) or
were resolved by checksum never get that PATCH, so Paperless keeps the consume-time
date. No column records which path settled a document, so the backfill identifies
the *symptom* instead of the path.

**Conservative by construction:**

* Source of truth is ``documents.document_date`` (the document's own date, already
  derived at Schicht-A extraction) — no LLM, no OCR, no Docling in this process.
* Only documents with ``paperless_state='done'``, a linked
  ``paperless_document_id`` and a ``document_date``.
* A Paperless document linked from several KB rows that disagree on the date is
  **ambiguous** and skipped.
* A PATCH happens ONLY when Paperless ``created`` still equals the date the document
  was ``added`` — i.e. Paperless fell back to the consume date. Any other value may
  be a human edit or Paperless's own date parse and is never overwritten.
* Idempotent: a patched document then reads ``created == document_date`` and is
  counted ``already_correct`` on the next run.
* The DB session is closed before the Paperless loop (no pooled connection held
  across slow MCP calls).

**Rate-limit-safe.** The MCPManager token bucket is 60/min and REJECTS rather than
waits, so every call is paced below it (``RatePacer``) and a rejection is retried
with linear backoff (``call_paced``). The batch is capped and resumable via
``after_pid``.

Output is counts and ids only — never titles, filenames or content.
"""
from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select

from models.database import PAPERLESS_STATE_DONE, Document
from services.folder_ingest_paperless import _parse_paperless_result

DEFAULT_RATE_PER_MINUTE = 50  # below the MCPManager's 60/min bucket
DEFAULT_LIMIT = 200
MAX_LIMIT = 1000
RATE_LIMIT_RETRIES = 5
RATE_LIMIT_BACKOFF_S = 15.0

Sleep = Callable[[float], Awaitable[None]]


class RatePacer:
    """Space calls at least ``60 / per_minute`` seconds apart."""

    def __init__(
        self,
        per_minute: int = DEFAULT_RATE_PER_MINUTE,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        self.interval = 60.0 / max(1, int(per_minute))
        self._clock = clock
        self._sleep = sleep
        self._next: float | None = None

    async def wait(self) -> None:
        now = self._clock()
        if self._next is not None and now < self._next:
            await self._sleep(self._next - now)
            now = self._next
        self._next = now + self.interval


def _is_rate_limited(raw: dict | None) -> bool:
    """The MCPManager's rejection envelope: ``success=False`` + "Rate limit exceeded"."""
    if not raw or raw.get("success"):
        return False
    return "rate limit" in str(raw.get("message") or "").lower()


async def call_paced(
    mcp_manager: Any,
    tool: str,
    params: dict,
    pacer: RatePacer,
    *,
    sleep: Sleep = asyncio.sleep,
    retries: int = RATE_LIMIT_RETRIES,
    backoff_s: float = RATE_LIMIT_BACKOFF_S,
) -> dict:
    """Paced ``execute_tool`` that retries a rate-limit rejection with linear
    backoff. Returns the parsed tool dict, or ``{"error": "rate_limited"}``."""
    for attempt in range(retries + 1):
        await pacer.wait()
        raw = await mcp_manager.execute_tool(tool, params)
        if not _is_rate_limited(raw):
            return _parse_paperless_result(raw)
        if attempt < retries:
            await sleep(backoff_s * (attempt + 1))
    return {"error": "rate_limited"}


@dataclass
class CreatedDateReport:
    commit: bool
    candidates: int = 0
    ambiguous: list[int] = field(default_factory=list)
    already_correct: int = 0
    would_patch: list[int] = field(default_factory=list)
    patched: list[int] = field(default_factory=list)
    skipped_not_consume_date: list[int] = field(default_factory=list)
    unreachable: list[int] = field(default_factory=list)
    failed: list[int] = field(default_factory=list)
    last_pid: int = 0

    def summary(self) -> dict:
        return {
            "mode": "commit" if self.commit else "dry-run",
            "candidates": self.candidates,
            "ambiguous": len(self.ambiguous),
            "already_correct": self.already_correct,
            "would_patch": len(self.would_patch),
            "patched": len(self.patched),
            "skipped_not_consume_date": len(self.skipped_not_consume_date),
            "unreachable": len(self.unreachable),
            "failed": len(self.failed),
            "last_pid": self.last_pid,
        }


def _date_part(value: Any) -> str | None:
    """``YYYY-MM-DD`` of a Paperless date/datetime string (``created`` is a date on
    Paperless 2.x+, a datetime on older versions; ``added`` is a datetime)."""
    if isinstance(value, str) and len(value) >= 10:
        return value[:10]
    return None


async def select_candidates(
    session_factory: Callable[[], Any], *, after_pid: int, limit: int
) -> tuple[list[tuple[int, str]], list[int]]:
    """``([(paperless_id, target_date_iso)], ambiguous_pids)`` — one entry per
    Paperless document, ordered by id, ``> after_pid``, capped at ``limit``."""
    async with session_factory() as db:
        rows = (
            await db.execute(
                select(Document.paperless_document_id, Document.document_date).where(
                    Document.paperless_state == PAPERLESS_STATE_DONE,
                    Document.paperless_document_id.is_not(None),
                    Document.document_date.is_not(None),
                    Document.paperless_document_id > after_pid,
                )
            )
        ).all()
    dates: dict[int, set[str]] = {}
    for pid, doc_date in rows:
        dates.setdefault(int(pid), set()).add(doc_date.isoformat())
    ambiguous = sorted(pid for pid, ds in dates.items() if len(ds) > 1)
    clean = sorted((pid, next(iter(ds))) for pid, ds in dates.items() if len(ds) == 1)
    return clean[: max(1, min(int(limit), MAX_LIMIT))], ambiguous


async def backfill_created_dates(
    session_factory: Callable[[], Any],
    mcp_manager: Any,
    *,
    commit: bool = False,
    limit: int = DEFAULT_LIMIT,
    after_pid: int = 0,
    pacer: RatePacer | None = None,
    sleep: Sleep = asyncio.sleep,
) -> CreatedDateReport:
    """Dry-run (default) or apply the created-date backfill. See the module docstring."""
    pacer = pacer or RatePacer(sleep=sleep)
    report = CreatedDateReport(commit=commit, last_pid=after_pid)
    batch, ambiguous = await select_candidates(session_factory, after_pid=after_pid, limit=limit)
    report.ambiguous = ambiguous
    report.candidates = len(batch)

    for pid, target in batch:
        report.last_pid = pid
        got = await call_paced(
            mcp_manager,
            "mcp.paperless.get_document",
            {"document_id": pid, "include_content": False},
            pacer,
            sleep=sleep,
        )
        if got.get("error"):
            report.unreachable.append(pid)
            continue
        current = _date_part(got.get("created"))
        added = _date_part(got.get("added"))
        if current == target:
            report.already_correct += 1
            continue
        if current is None or added is None or current != added:
            # Not the consume-date fallback — possibly a human edit. Never overwrite.
            report.skipped_not_consume_date.append(pid)
            continue
        if not commit:
            report.would_patch.append(pid)
            continue
        res = await call_paced(
            mcp_manager,
            "mcp.paperless.update_document",
            {"document_id": pid, "created_date": target},
            pacer,
            sleep=sleep,
        )
        (report.failed if res.get("error") else report.patched).append(pid)

    return report
