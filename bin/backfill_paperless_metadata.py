#!/usr/bin/env python3
"""
Backfill Paperless metadata for folder-ingested documents. Two modes, chosen
explicitly with ``--mode``; every run is a DRY RUN unless ``--commit`` is given.

``--mode created-date`` — the Paperless ``created`` date (Ausstellungsdatum)
  Documents that settled via the idempotent task_id RE-POLL (or a checksum id
  lookup) never received the post-consume ``update_document``, so Paperless kept
  the consume-time date. Logic + guards live in
  ``services/paperless_metadata_backfill.py``:
    - source = ``documents.document_date`` (no LLM, no OCR);
    - only ``paperless_state='done'`` docs with a linked ``paperless_document_id``;
    - PATCHes ONLY when Paperless ``created`` still equals its ``added`` date (the
      consume-date fallback) — anything else may be a human edit and is left alone;
    - skips a Paperless doc linked from KB rows that disagree on the date;
    - idempotent, paced below the 60/min MCP rate limit with backoff on rejection,
      batch-capped (``--limit``, default 200, max 1000) and resumable
      (``--after-pid``; the summary prints ``last_pid``);
    - prints counts and Paperless ids only.
  Renfield's OCR text is NOT re-transported: the full text is not stored outside
  the chunk table, and re-deriving it needs Docling (worker-only).

``--mode correspondent`` — the original correspondent gap-fill
  Re-runs metadata extraction and resolves-or-creates the correspondent with the
  same full-taxonomy guardrail as the live leg; sets ONLY the correspondent and
  skips docs that already have one. (Unchanged behaviour; it lifts the MCP rate
  limiter for its one-pass filename index.)

Running it:
  The backend is located by itself — ``$RENFIELD_BACKEND_DIR``, else the repo layout
  (``bin/../src/backend``), else the image layout (``/app``) — so a copy of this file
  anywhere in the backend pod runs without ``PYTHONPATH``. Only the ``paperless`` MCP
  server is started. Exit code: 0 on success, 1 on an error (incl. Paperless MCP not
  connected, or any failed PATCH in created-date mode), 2 if the backend is not found.
  Every teardown step is time-bounded, so the process terminates after the summary.

Usage:
    python bin/backfill_paperless_metadata.py --mode created-date            # dry run
    python bin/backfill_paperless_metadata.py --mode created-date --commit
    python bin/backfill_paperless_metadata.py --mode created-date --commit --after-pid 1234
    python bin/backfill_paperless_metadata.py --mode correspondent --commit --limit 50
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import threading
from collections.abc import Coroutine, Mapping
from pathlib import Path
from typing import Any

# --- backend import path (identical block in every bin/backfill_*.py) ---------------
# These scripts are copied into the backend pod on their own, so this cannot live in a
# shared module: it is what makes the shared modules importable in the first place.


def _find_backend_dir(
    script: Path, env: Mapping[str, str] = os.environ, image_root: Path = Path("/app")
) -> Path:
    """The Renfield backend root: ``$RENFIELD_BACKEND_DIR`` (exclusive when set), else
    the repo layout ``bin/../src/backend``, else the image layout ``/app``."""
    override = env.get("RENFIELD_BACKEND_DIR")
    candidates = (
        [Path(override)] if override
        else [script.resolve().parent.parent / "src" / "backend", image_root]
    )
    for candidate in candidates:
        if (candidate / "services" / "__init__.py").is_file() and (candidate / "utils" / "config.py").is_file():
            return candidate
    print(
        f"{script.name}: Renfield backend not found (tried: {', '.join(map(str, candidates))}). "
        "Set RENFIELD_BACKEND_DIR to the directory holding services/ and utils/ "
        "(repo: src/backend, image: /app).",
        file=sys.stderr,
    )
    raise SystemExit(2)


_BACKEND = _find_backend_dir(Path(__file__))
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))
# --- end backend import path ---------------------------------------------------------

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
logger = logging.getLogger("backfill_paperless_metadata")

# Upper bound for MCPManager.shutdown(). Its per-stack teardown is already bounded
# (5 s each), so this only fires on a wedge outside those bounds.
MCP_SHUTDOWN_TIMEOUT_S = 20.0
# Upper bound for each event-loop teardown step (leftover tasks, async generators,
# default executor) after the work is done.
LOOP_TEARDOWN_TIMEOUT_S = 5.0


class BackfillError(RuntimeError):
    """A run that cannot proceed (reported, exit code 1)."""


def _backend_path(value: str) -> Path:
    """Settings paths are relative to the backend root (the image's cwd ``/app``);
    resolve them there when the script runs from another cwd."""
    path = Path(value)
    if path.is_absolute() or path.exists():
        return path
    return _BACKEND / path


async def _await_bounded(awaitable: Any, timeout: float, what: str) -> bool:
    """Await with a HARD upper bound. ``asyncio.wait_for`` is not one: on timeout it
    cancels and then waits for the cancellation to be acknowledged, which a wedged
    transport teardown never does. On timeout the task is cancelled and abandoned."""
    task = asyncio.ensure_future(awaitable)
    done, _ = await asyncio.wait({task}, timeout=timeout)
    if not done:
        task.cancel()
        logger.warning("%s did not finish within %.0f s — abandoned", what, timeout)
        return False
    if not task.cancelled() and task.exception() is not None:
        logger.warning("%s failed: %s", what, task.exception())
        return False
    return True


async def _shutdown(manager, timeout: float | None = None) -> None:
    await _await_bounded(
        manager.shutdown(),
        MCP_SHUTDOWN_TIMEOUT_S if timeout is None else timeout,
        "MCP shutdown",
    )


def _run(coro: Coroutine[Any, Any, Any], teardown_timeout: float | None = None) -> Any:
    """``asyncio.run`` with a bounded teardown. ``asyncio.run`` cancels every leftover
    task and then waits for ALL of them without a limit (and joins the default
    executor without one), so a single MCP transport task that does not honour its
    cancellation keeps the process alive after the work is done."""
    timeout = LOOP_TEARDOWN_TIMEOUT_S if teardown_timeout is None else teardown_timeout
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(coro)
    finally:
        try:
            leftovers = [t for t in asyncio.all_tasks(loop) if not t.done()]
            for task in leftovers:
                task.cancel()
            if leftovers:
                _, pending = loop.run_until_complete(asyncio.wait(leftovers, timeout=timeout))
                if pending:
                    logger.warning(
                        "%d background task(s) ignored cancellation for %.0f s — abandoned: %s",
                        len(pending), timeout,
                        ", ".join(sorted({getattr(t.get_coro(), "__qualname__", "?") for t in pending})),
                    )
            loop.run_until_complete(_await_bounded(loop.shutdown_asyncgens(), timeout, "async generator shutdown"))
            loop.run_until_complete(_await_bounded(loop.shutdown_default_executor(), timeout, "executor shutdown"))
        finally:
            asyncio.set_event_loop(None)
            loop.close()
        stuck = [t.name for t in threading.enumerate() if t is not threading.main_thread() and not t.daemon and t.is_alive()]
        if stuck:
            logger.warning("non-daemon thread(s) still alive at exit: %s", ", ".join(stuck))


async def _build_mcp_manager(*, lift_rate_limit: bool):
    """A MCPManager connected to the ``paperless`` server ONLY — the one server these
    backfills use (same as ``services/paperless_worker_client``). Raises
    ``BackfillError`` when it is not configured or does not connect; without this a
    missing server made every document look ``unreachable`` and the run exit 0.

    ``lift_rate_limit`` is only for the correspondent mode, whose one-pass filename
    index bursts hundreds of get_document calls (the 60/min bucket REJECTS over the
    cap). The created-date mode keeps the limiter and paces itself below it."""
    from services.mcp_client import MCPManager
    from utils.config import settings

    config_path = _backend_path(settings.mcp_config_path)
    manager = MCPManager()
    manager.load_config(
        str(config_path),
        only={"paperless"},
        overlay_dir=str(_backend_path(settings.mcp_config_overlay_dir)),
    )
    state = getattr(manager, "_servers", {}).get("paperless")
    if state is None:
        raise BackfillError(
            f"paperless MCP server not configured/enabled in {config_path} (PAPERLESS_ENABLED?)"
        )
    await manager.connect_all()
    if not state.connected:
        await _shutdown(manager)
        raise BackfillError(f"paperless MCP server did not connect: {state.last_error}")
    if lift_rate_limit:
        limiter = getattr(state, "rate_limiter", None)
        if limiter is not None:
            limiter.rate = 100_000
            limiter.max_tokens = 100_000.0
            limiter.tokens = 100_000.0
    return manager


async def _run_created_date(*, commit: bool, limit: int, after_pid: int, rate: int) -> bool:
    from services.database import AsyncSessionLocal
    from services.paperless_metadata_backfill import RatePacer, backfill_created_dates

    manager = await _build_mcp_manager(lift_rate_limit=False)
    try:
        report = await backfill_created_dates(
            AsyncSessionLocal,
            manager,
            commit=commit,
            limit=limit,
            after_pid=after_pid,
            pacer=RatePacer(rate),
        )
    finally:
        await _shutdown(manager)
    print(json.dumps(report.summary(), indent=2))
    ids = {
        "patched": report.patched,
        "would_patch": report.would_patch,
        "failed": report.failed,
        "unreachable": report.unreachable,
        "ambiguous": report.ambiguous,
        "skipped_not_consume_date": report.skipped_not_consume_date,
    }
    print(json.dumps({"paperless_ids": {k: v for k, v in ids.items() if v}}, indent=2), flush=True)
    if not commit:
        print("DRY-RUN — nothing was written. Re-run with --commit to apply.", flush=True)
    return not report.failed


async def _build_filename_index(manager) -> dict[str, int]:
    """``{original_file_name(lower): paperless_id}`` over the most recently ADDED
    Paperless documents (one pass). Keyed on the upload filename — the leg sends
    ``title``+``filename = meta.filename`` — which sidesteps the document-date vs
    ingest-date mismatch a ``created`` window would suffer (Paperless's ``created``
    is the parsed *document* date, not when we filed it). Bounded by max_results,
    so the backfill targets recent ingests (older docs need the stored id)."""
    from services.folder_ingest_paperless import _parse_paperless_result

    search = _parse_paperless_result(
        await manager.execute_tool(
            "mcp.paperless.search_documents", {"ordering": "-added", "max_results": 500}
        )
    )
    index: dict[str, int] = {}
    for r in search.get("results") or []:
        pid = r.get("id")
        if pid is None:
            continue
        got = _parse_paperless_result(
            await manager.execute_tool("mcp.paperless.get_document", {"document_id": pid})
        )
        ofn = (got.get("original_file_name") or "").strip().lower()
        if ofn and ofn not in index:  # most-recently-added wins on a duplicate filename
            index[ofn] = pid
    return index


async def _run_correspondent(*, commit: bool, limit: int | None) -> bool:
    from sqlalchemy import select

    from models.database import PAPERLESS_STATE_DONE, Document
    from services.database import AsyncSessionLocal
    from services.folder_ingest_paperless import (
        _fetch_correspondent_names,
        _parse_paperless_result,
        resolve_correspondent_from_metadata,
    )
    from services.paperless_metadata_extractor import PaperlessMetadataExtractor

    manager = await _build_mcp_manager(lift_rate_limit=True)
    extractor = PaperlessMetadataExtractor(mcp_manager=manager)
    fixed = skipped = no_corr = unmatched = already = 0
    try:
        names = await _fetch_correspondent_names(manager)  # full taxonomy, fetched once
        async with AsyncSessionLocal() as db:
            stmt = (
                select(Document)
                .where(Document.paperless_state == PAPERLESS_STATE_DONE)
                .order_by(Document.id.desc())
            )
            if limit:
                stmt = stmt.limit(limit)
            docs = (await db.execute(stmt)).scalars().all()
            logger.info("%d filed folder-ingest document(s) to check", len(docs))

            # Build the filename→id index once, only if some doc lacks a stored id.
            index: dict[str, int] = {}
            if any(d.paperless_document_id is None for d in docs):
                index = await _build_filename_index(manager)

            for doc in docs:
                pid = doc.paperless_document_id or index.get((doc.filename or "").strip().lower())
                if pid is None:
                    unmatched += 1
                    logger.info("  doc %s (%s): no Paperless match — skipped", doc.id, doc.filename)
                    continue

                got = _parse_paperless_result(
                    await manager.execute_tool("mcp.paperless.get_document", {"document_id": pid})
                )
                if got.get("error"):
                    unmatched += 1
                    logger.info("  doc %s: get_document(%s) error: %s", doc.id, pid, got.get("error"))
                    continue
                if (got.get("correspondent") or "").strip():
                    already += 1
                    continue  # gap-fill only — never overwrite an existing correspondent

                if not doc.file_path or not Path(doc.file_path).exists():
                    skipped += 1
                    logger.info("  doc %s: recovery file missing (%s) — skipped", doc.id, doc.file_path)
                    continue

                result = await extractor.extract_from_file(
                    doc.file_path, user_id=getattr(doc, "user_id", None), lang="de"
                )
                if result.error:
                    skipped += 1
                    logger.info("  doc %s: extraction failed (%s) — skipped", doc.id, result.error)
                    continue

                # Same resolve-or-create path as the live leg (shared helper);
                # full taxonomy passed in so it isn't re-fetched per document.
                # create=commit so a dry run previews new correspondents
                # without actually creating them in Paperless.
                corr = await resolve_correspondent_from_metadata(
                    manager, result.metadata, names=names, create=commit
                )
                if not corr:
                    no_corr += 1
                    logger.info("  doc %s (paperless %s): no correspondent resolved — left blank", doc.id, pid)
                    continue

                logger.info("  doc %s (paperless %s): set correspondent -> %r", doc.id, pid, corr)
                if commit:
                    patch = _parse_paperless_result(
                        await manager.execute_tool(
                            "mcp.paperless.update_document",
                            {"document_id": pid, "correspondent": corr},
                        )
                    )
                    if patch.get("error"):
                        logger.warning("    update_document(%s) failed: %s", pid, patch.get("error"))
                        continue
                    if doc.paperless_document_id != pid:
                        doc.paperless_document_id = pid  # backfill the linkage (filename-matched only)
                        await db.commit()
                fixed += 1

        logger.info(
            "Done: %d set, %d already-had, %d no-correspondent, %d unmatched, %d skipped%s",
            fixed, already, no_corr, unmatched, skipped, "" if commit else "  (DRY-RUN — no writes)",
        )
    finally:
        await _shutdown(manager)
    return True


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Backfill Paperless metadata for folder-ingested documents.")
    p.add_argument(
        "--mode", choices=["created-date", "correspondent"], required=True,
        help="Which metadata to backfill.",
    )
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="Preview only (the default).")
    mode.add_argument("--commit", action="store_true", help="Apply the changes to Paperless.")
    p.add_argument(
        "--limit", type=int, default=None,
        help="Cap the batch (created-date: default 200, max 1000; correspondent: unlimited).",
    )
    p.add_argument(
        "--after-pid", type=int, default=0,
        help="created-date: resume after this Paperless id (the previous run's last_pid).",
    )
    p.add_argument(
        "--rate", type=int, default=50,
        help="created-date: max MCP calls per minute (keep below the 60/min limit).",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.mode == "created-date":
            from services.paperless_metadata_backfill import DEFAULT_LIMIT

            ok = _run(_run_created_date(
                commit=args.commit,
                limit=args.limit or DEFAULT_LIMIT,
                after_pid=args.after_pid,
                rate=args.rate,
            ))
        else:
            ok = _run(_run_correspondent(commit=args.commit, limit=args.limit))
    except BackfillError as exc:
        logger.error("%s", exc)
        return 1
    except Exception:
        logger.exception("backfill failed")
        return 1
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
