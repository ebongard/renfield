"""
paperless_ui_edit_sweeper — hourly sweep that turns user edits in the
Paperless UI into extraction-learning examples.

Pairs with PR 3's confirm-diff signal. Some users edit metadata in the
Paperless UI after upload instead of through the chat confirm flow
(common past the cold-start window, or when the silent upload landed
something slightly wrong). Without this sweep, those corrections are
invisible to the learning loop.

Flow per tick:

    1. Pull ``paperless_upload_tracking`` rows with ``swept_at IS NULL``
       whose ``uploaded_at`` is old enough that the 1 h edit window
       has closed (prevents catching the user mid-edit) but still
       within a reasonable recent-past cap.
    2. For each row, call ``mcp.paperless.get_document`` and diff the
       live metadata against ``original_metadata``.
    3. When the fields differ AND the first edit landed within 1 h of
       upload (best-effort — Paperless's ``modified`` timestamp covers
       the LATEST edit, so the 1 h check is a proxy), persist a
       ``paperless_extraction_examples`` row with
       ``source='paperless_ui_sweep'`` and the doc_text embedding so
       future retrievals can surface it.
    4. Mark the tracking row ``swept_at = now`` regardless of outcome
       so we don't re-process it.

Design reference: docs/design/paperless-llm-metadata.md (PR 4).

Scope cut for v1:
- No-re-edit filter (``superseded=true`` on later re-edits) is deferred.
  The 1 h time filter catches most taxonomy-drift cases at household
  scale. If noise shows up in real use, PR 4b adds the re-sweep.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from typing import Any

from loguru import logger

# Serialise sweep ticks within a single process. The hourly lifecycle
# loop is already serial with itself, but admin-triggered manual runs
# could otherwise overlap a scheduled tick — both would SELECT the
# same unswept rows, both would MCP-fetch, both would persist. The
# lock is cheap and prevents that class of double-processing.
#
# Multi-replica deployments (k8s) would need a Postgres advisory lock
# to coordinate across processes. Out of v1 scope because household-
# scale Renfield runs a single backend replica; if that changes, swap
# this for ``pg_try_advisory_lock`` inside the transaction.
_sweep_lock = asyncio.Lock()

# Fields we diff between what we uploaded and what's in Paperless now.
# Order matters for the deterministic doc-diff summary — covers every
# field ``PaperlessMetadata`` persists minus the metadata-only ones
# (``confidence``, ``resolutions``).
_TRACKED_FIELDS: tuple[str, ...] = (
    "title",
    "correspondent",
    "document_type",
    "tags",
    "storage_path",
    "created_date",
)

# Wait at least this long after upload before sweeping. Gives the user
# time to make + settle on their edit within the 1 h window.
_MIN_AGE_BEFORE_SWEEP = timedelta(hours=1, minutes=5)

# Paperless's ``modified`` timestamp must land within this window of
# ``uploaded_at`` for the diff to count as an extraction correction.
# Beyond it, we treat the edit as taxonomy drift and don't learn from
# it. The window is slightly wider than _MIN_AGE_BEFORE_SWEEP so an
# edit that barely slipped past the 1 h window isn't missed by ~5 min
# clock skew.
_EDIT_WINDOW_AFTER_UPLOAD = timedelta(hours=1, minutes=15)

# Don't look further back than this. Tracking rows older than the cap
# are swept as-is (no MCP call) and marked done — the 1 h edit window
# has long since closed and anything later is taxonomy drift, not an
# extraction correction.
_MAX_AGE_FOR_SWEEP = timedelta(hours=24)

# Query batch size. Small to keep each tick bounded — household scale
# rarely sees more than a handful of uploads per hour, so this is
# defensive against bursts or backlog recovery after downtime.
_SWEEP_BATCH_SIZE = 50

# Substring emitted by ``_truncate_response`` in ``mcp_client.py`` when
# the response exceeds ``mcp_max_response_size`` (10 KB default) and
# can't be slimmed to fit. A truncated ``get_document`` response means
# we literally cannot compare against the original metadata — the
# mismatch could be real or it could be a byte-truncation artefact.
# The proper fix lives on the MCP server side (a narrow
# ``get_document_metadata`` tool without the OCR content, or an
# ``include_content=False`` flag). Until that lands, we detect and
# skip rather than pollute the learning corpus with partial-data
# diffs.
_TRUNCATION_MARKER = "[... Response truncated"


class _TruncatedResponseError(RuntimeError):
    """MCP truncated the ``get_document`` response. Raised by
    ``_detect_edit`` so the caller can count + stamp ``swept_at``
    (don't retry — truncation is deterministic for that doc)."""


async def run_sweep_tick(
    *,
    mcp_manager: Any,
    now: datetime | None = None,
) -> dict[str, int]:
    """Run one sweep pass. Returns counts for telemetry/logging.

    The function is a single atomic tick — the caller decides cadence
    (lifecycle.py registers an hourly loop). Safe to run manually for
    testing. Concurrent invocations are serialised via ``_sweep_lock``;
    a second caller that arrives while a tick is in flight returns an
    empty counters dict with ``skipped=1`` rather than double-processing
    the candidate set.
    """
    if _sweep_lock.locked():
        logger.info("ui-edit sweep tick skipped — another tick is in flight")
        return {
            "candidates": 0, "edits_detected": 0, "errors": 0,
            "expired": 0, "truncated": 0, "skipped": 1,
        }

    async with _sweep_lock:
        return await _run_sweep_tick_locked(mcp_manager=mcp_manager, now=now)


async def _run_sweep_tick_locked(
    *,
    mcp_manager: Any,
    now: datetime | None = None,
) -> dict[str, int]:
    """Internal body of ``run_sweep_tick`` — assumes the caller holds
    ``_sweep_lock``. Split out so the lock behaviour is visible at a
    glance and the body is still directly testable."""
    from sqlalchemy import select, update as sqla_update

    from models.database import (
        PaperlessExtractionExample,
        PaperlessUploadTracking,
    )
    from services.database import AsyncSessionLocal

    current = now or datetime.utcnow()
    oldest_swept = current - _MAX_AGE_FOR_SWEEP
    newest_swept = current - _MIN_AGE_BEFORE_SWEEP

    counters = {
        "candidates": 0,
        "edits_detected": 0,
        "errors": 0,
        "expired": 0,
        "truncated": 0,
    }

    # Stage 1 — candidate selection. Oldest first so we drain the
    # backlog predictably after downtime.
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(PaperlessUploadTracking)
            .where(PaperlessUploadTracking.swept_at.is_(None))
            .where(PaperlessUploadTracking.uploaded_at <= newest_swept)
            .order_by(PaperlessUploadTracking.uploaded_at.asc())
            .limit(_SWEEP_BATCH_SIZE)
        )
        candidates: list[PaperlessUploadTracking] = list(result.scalars().all())

    counters["candidates"] = len(candidates)
    if not candidates:
        return counters

    # Stage 2 — per-row diff. Done outside the DB session so we don't
    # hold connections across MCP round-trips (the same pattern PR 3
    # learned for embedding calls).
    example_rows: list[PaperlessExtractionExample] = []
    swept_ids: list[int] = []
    for tracking in candidates:
        if tracking.uploaded_at < oldest_swept:
            # Too old to learn from. Stamp + move on; no MCP call.
            counters["expired"] += 1
            swept_ids.append(tracking.id)
            continue

        try:
            diff = await _detect_edit(
                mcp_manager=mcp_manager,
                document_id=tracking.paperless_document_id,
                original=tracking.original_metadata or {},
                uploaded_at=tracking.uploaded_at,
            )
        except _TruncatedResponseError as exc:
            # Deterministic: the doc is just too big for the current
            # MCP response cap. Retrying next hour won't help — stamp
            # swept_at to drain it from the candidate queue.
            logger.warning("ui-edit sweep: %s (skipping)", exc)
            counters["truncated"] += 1
            swept_ids.append(tracking.id)
            continue
        except Exception as exc:
            logger.warning(
                "ui-edit sweep: get_document for paperless doc %d failed: %s",
                tracking.paperless_document_id, exc,
            )
            counters["errors"] += 1
            # Don't stamp swept_at on errors — retry next tick. MCP
            # outages are transient; we'd lose signal otherwise.
            continue

        if diff is not None:
            counters["edits_detected"] += 1
            example_rows.append(_build_example_row(tracking=tracking, current=diff))
        swept_ids.append(tracking.id)

    # Stage 3 — optional embed + persist. Embeds happen one-at-a-time
    # to avoid saturating Ollama; each call is bounded by the retriever's
    # 5 s wait_for. At household scale the batch is tiny (≤ 10 rows in
    # practice).
    if example_rows:
        from services.paperless_example_retriever import embed_doc_text
        for row in example_rows:
            if row.doc_text:
                try:
                    row.doc_text_embedding = await embed_doc_text(row.doc_text)
                except Exception as exc:
                    # Persist without embedding — same fallback PR 3
                    # uses. Row is still useful for future backfill.
                    logger.warning("ui-edit sweep embed failed: %s", exc)

    # Per-row persist: if one example row violates a constraint (bad
    # JSON shape, FK race, etc.), we don't want to roll back the whole
    # batch — that would also roll back the swept_at stamps and push
    # every candidate into an infinite re-sweep loop next tick.
    for row in example_rows:
        try:
            async with AsyncSessionLocal() as db:
                db.add(row)
                await db.commit()
        except Exception as exc:
            logger.warning("ui-edit sweep persist failed for row: %s", exc)
            counters["errors"] += 1

    # Swept_at stamps land in their own transaction so a persist failure
    # above doesn't block the queue drain. Bulk UPDATE is still fine
    # here — these are all the same column write, no per-row constraints.
    if swept_ids:
        async with AsyncSessionLocal() as db:
            await db.execute(
                sqla_update(PaperlessUploadTracking)
                .where(PaperlessUploadTracking.id.in_(swept_ids))
                .values(swept_at=current)
            )
            await db.commit()

    if counters["edits_detected"] or counters["errors"] or counters["truncated"]:
        logger.info(
            "ui-edit sweep: %d candidates → %d edits, %d expired, "
            "%d truncated, %d errors",
            counters["candidates"], counters["edits_detected"],
            counters["expired"], counters["truncated"], counters["errors"],
        )
    return counters


async def _detect_edit(
    *,
    mcp_manager: Any,
    document_id: int,
    original: dict[str, Any],
    uploaded_at: datetime | None = None,
) -> dict[str, Any] | None:
    """Return the current Paperless metadata if it differs from
    *original*, or ``None`` if they match (or if we can't compare).

    When *uploaded_at* is supplied and the Paperless ``modified``
    timestamp is outside ``_EDIT_WINDOW_AFTER_UPLOAD``, the diff is
    dropped as taxonomy drift (the design intent of the 1 h window).
    """
    result = await mcp_manager.execute_tool(
        "mcp.paperless.get_document", {"document_id": document_id},
    )
    if not result or not result.get("success"):
        return None

    inner_msg = result.get("message")
    if isinstance(inner_msg, str) and _TRUNCATION_MARKER in inner_msg:
        # MCP cut the response — any diff we computed would be noise.
        # See _TRUNCATION_MARKER comment for the long-term fix.
        raise _TruncatedResponseError(
            f"get_document response for doc {document_id} exceeded MCP cap"
        )
    current: dict[str, Any] = {}
    if isinstance(inner_msg, str):
        try:
            parsed = json.loads(inner_msg)
        except (json.JSONDecodeError, TypeError):
            parsed = None
        if isinstance(parsed, dict):
            current = parsed
    elif isinstance(inner_msg, dict):
        current = inner_msg

    if not current or current.get("error"):
        return None

    # Field-name remap: the MCP ``upload_document`` tool accepts
    # ``created_date`` but ``get_document`` returns ``created`` (ISO
    # timestamp like ``2026-02-14T00:00:00Z``). Without this mapping
    # every sweep would see "original.created_date=2026-02-14" vs
    # "current.created_date=None" and write a phantom ui_sweep row
    # claiming the user blanked the date — poisoning the learning
    # corpus.
    if "created_date" not in current:
        raw_created = current.get("created")
        if isinstance(raw_created, str):
            # Take the YYYY-MM-DD prefix — original_metadata stores the
            # date only, not the timestamp.
            current = {**current, "created_date": raw_created[:10]}

    # 1 h edit-window filter (design: extraction corrections land in
    # the first hour; anything later is taxonomy drift). The proxy is
    # imperfect — ``modified`` reflects the LATEST edit, so a user who
    # edited at T+30 min and again at T+20 h will be filtered out
    # here. We accept the false-negative rather than the corpus
    # poisoning that drift edits would cause.
    if uploaded_at is not None:
        raw_modified = current.get("modified")
        modified_dt = _parse_iso_datetime(raw_modified) if isinstance(raw_modified, str) else None
        if modified_dt is not None:
            if modified_dt - uploaded_at > _EDIT_WINDOW_AFTER_UPLOAD:
                return None

    # The MCP get_document returns resolved names for
    # correspondent/document_type and a list of tag names.
    # Shape matches original_metadata for the tracked fields after
    # the ``created`` remap above.
    normalised = {
        field: _normalise_field(field, current.get(field))
        for field in _TRACKED_FIELDS
    }
    original_norm = {
        field: _normalise_field(field, original.get(field))
        for field in _TRACKED_FIELDS
    }
    if normalised == original_norm:
        return None
    return normalised


def _resolve_editor_user_id(*, tracking: Any, current: dict[str, Any]) -> int | None:
    """Attribution seam for multi-user households.

    Ideal: pick the Paperless editor (``owner`` on the Paperless
    document), map them to a Renfield user, return that user_id. The
    MCP ``get_document`` tool does not currently forward ``owner``,
    and no user-mapping table exists yet — so this helper returns the
    uploader today. When either of those lands, extend this function;
    the rest of the sweeper is already calling through it.
    """
    owner = current.get("owner")
    if isinstance(owner, int):
        # Forward-compat branch: once Renfield has a Paperless-user →
        # Renfield-user mapping, resolve *owner* here. Until then, the
        # owner ID is in a different user-ID space than Renfield, so
        # using it raw would be worse than the uploader fallback.
        # Leave the branch visible so the next hand knows where to
        # wire the mapping in.
        pass
    return tracking.user_id


def _parse_iso_datetime(value: str) -> datetime | None:
    """Parse a Paperless ``modified`` / ``created`` ISO timestamp into
    a naive-UTC ``datetime`` so it can be compared against the tracking
    row's ``uploaded_at`` (also naive UTC — see migration rev
    pc20260426). Returns ``None`` if the string isn't parseable."""
    if not value:
        return None
    try:
        # Accept both "2026-02-14T00:00:00Z" and "2026-02-14T00:00:00+00:00".
        normalized = value.replace("Z", "+00:00")
        parsed = datetime.fromisoformat(normalized)
    except (ValueError, TypeError):
        return None
    if parsed.tzinfo is not None:
        # Convert to UTC then drop the tz info — the rest of the
        # sweeper works with naive-UTC (see migration comment).
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def _normalise_field(field: str, value: Any) -> Any:
    """Field-level normalisation before diffing. Lists get sorted so
    tag-order swaps don't register as edits; None and empty-string are
    treated as equal."""
    if value in (None, "", []):
        return None
    if field == "tags":
        return sorted(v for v in value if v)
    return value


def _build_example_row(
    *,
    tracking: Any,
    current: dict[str, Any],
):
    """Construct a ``paperless_extraction_examples`` row from a detected
    edit. The ``llm_output`` is the original upload metadata (what the
    LLM had proposed + we committed); ``user_approved`` is the current
    Paperless state (what the user actually landed on). Matches the
    shape PR 3's retriever expects.

    Attribution caveat: ``user_id`` is set to the ORIGINAL UPLOADER,
    not the Paperless UI editor. Paperless-ngx exposes ``owner`` on
    its document endpoint, but the MCP ``get_document`` tool doesn't
    currently forward it, and Renfield has no Paperless-user→Renfield-
    user mapping. In a multi-user household this means a correction
    made by user B (who edited in Paperless) is attributed to user A
    (who uploaded) — so user A sees the example in their retriever
    context, user B doesn't. Acceptable for v1 (single-user product
    today). The forward path is (a) expose ``owner`` in MCP, (b) add
    the user-mapping table, (c) resolve the editor here and attribute
    to them instead."""
    from models.database import PaperlessExtractionExample
    return PaperlessExtractionExample(
        doc_text=tracking.doc_text or "",
        llm_output=dict(tracking.original_metadata or {}),
        user_approved=dict(current),
        source="paperless_ui_sweep",
        user_id=_resolve_editor_user_id(tracking=tracking, current=current),
        # doc_text_embedding is filled in Stage 3 (see run_sweep_tick).
        doc_text_embedding=None,
    )


async def run_abandoned_confirm_sweep(
    *,
    now: datetime | None = None,
    max_age_hours: int = 24,
) -> int:
    """Reap confirm flows the user walked away from.

    Design contract (``docs/design/paperless-llm-metadata.md`` §
    "Abandoned-confirm cleanup" + eng-review test plan): a
    ``paperless_pending_confirms`` row older than *max_age_hours* means
    the user started the cold-start confirm flow but never answered
    ja/nein. We delete the ``ChatUpload`` row AND unlink the bytes on
    disk; the pending_confirm row itself disappears via the CASCADE on
    ``pending_confirms.attachment_id → chat_uploads.id``.

    We don't reuse the generic chat-upload retention loop here. That
    loop fires on ``retention_days`` (30 d default), leaving abandoned
    confirm files on disk far longer than the 24 h policy calls for.
    Explicit reap makes the window predictable.

    Returns the count of ChatUploads reaped (== pending_confirms
    cascaded). Safe to re-run; per-row errors are logged and skipped.
    """
    from pathlib import Path

    from sqlalchemy import select

    from models.database import ChatUpload, PaperlessPendingConfirm
    from services.database import AsyncSessionLocal

    current = now or datetime.utcnow()
    cutoff = current - timedelta(hours=max_age_hours)

    reaped = 0
    file_errors = 0
    async with AsyncSessionLocal() as db:
        # Join so we can unlink the file before the cascade drops the
        # ChatUpload row. Ordering: file first, DB row second — better
        # to orphan a DB row than a file we forgot about.
        stale = await db.execute(
            select(ChatUpload, PaperlessPendingConfirm)
            .join(
                PaperlessPendingConfirm,
                PaperlessPendingConfirm.attachment_id == ChatUpload.id,
            )
            .where(PaperlessPendingConfirm.created_at < cutoff)
        )
        for upload, _pending in stale.all():
            if upload.file_path:
                try:
                    p = Path(upload.file_path)
                    if p.is_file():
                        p.unlink()
                except Exception as exc:
                    # Keep reaping the DB row even if the disk unlink
                    # fails — the file may be on a remounted volume,
                    # already-gone, or permission-locked. A stranded
                    # file is cheaper than a stranded DB row that
                    # blocks future sweeps.
                    logger.warning(
                        "abandoned-confirm sweep: unlink %s failed: %s",
                        upload.file_path, exc,
                    )
                    file_errors += 1
            await db.delete(upload)
            reaped += 1
        if reaped:
            await db.commit()

    if reaped:
        logger.info(
            "abandoned-confirm sweep: %d ChatUploads + pending_confirms "
            "older than %d h purged (%d file-unlink errors)",
            reaped, max_age_hours, file_errors,
        )
    return reaped
