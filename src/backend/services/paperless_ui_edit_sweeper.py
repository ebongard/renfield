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

import json
from datetime import datetime, timedelta
from typing import Any

from loguru import logger

# Fields we diff between what we uploaded and what's in Paperless now.
# Order matters for the deterministic doc-diff summary — covers every
# field ``PaperlessMetadata`` persists minus the metadata-only ones
# (``confidence``, ``new_entry_proposals``).
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

# Don't look further back than this. Tracking rows older than the cap
# are swept as-is (no MCP call) and marked done — the 1 h edit window
# has long since closed and anything later is taxonomy drift, not an
# extraction correction.
_MAX_AGE_FOR_SWEEP = timedelta(hours=24)

# Query batch size. Small to keep each tick bounded — household scale
# rarely sees more than a handful of uploads per hour, so this is
# defensive against bursts or backlog recovery after downtime.
_SWEEP_BATCH_SIZE = 50


async def run_sweep_tick(
    *,
    mcp_manager: Any,
    now: datetime | None = None,
) -> dict[str, int]:
    """Run one sweep pass. Returns counts for telemetry/logging.

    The function is a single atomic tick — the caller decides cadence
    (lifecycle.py registers an hourly loop). Safe to run manually for
    testing.
    """
    from sqlalchemy import select, update as sqla_update

    from models.database import (
        PaperlessExtractionExample,
        PaperlessUploadTracking,
    )
    from services.database import AsyncSessionLocal

    current = now or datetime.utcnow()
    oldest_swept = current - _MAX_AGE_FOR_SWEEP
    newest_swept = current - _MIN_AGE_BEFORE_SWEEP

    counters = {"candidates": 0, "edits_detected": 0, "errors": 0, "expired": 0}

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
            )
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

    async with AsyncSessionLocal() as db:
        for row in example_rows:
            db.add(row)
        if swept_ids:
            await db.execute(
                sqla_update(PaperlessUploadTracking)
                .where(PaperlessUploadTracking.id.in_(swept_ids))
                .values(swept_at=current)
            )
        await db.commit()

    if counters["edits_detected"] or counters["errors"]:
        logger.info(
            "ui-edit sweep: %d candidates → %d edits, %d expired, %d errors",
            counters["candidates"], counters["edits_detected"],
            counters["expired"], counters["errors"],
        )
    return counters


async def _detect_edit(
    *,
    mcp_manager: Any,
    document_id: int,
    original: dict[str, Any],
) -> dict[str, Any] | None:
    """Return the current Paperless metadata if it differs from
    *original*, or ``None`` if they match (or if we can't compare)."""
    result = await mcp_manager.execute_tool(
        "mcp.paperless.get_document", {"document_id": document_id},
    )
    if not result or not result.get("success"):
        return None

    inner_msg = result.get("message")
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

    # The MCP get_document returns resolved names for
    # correspondent/document_type and a list of tag names.
    # Shape matches original_metadata directly, so field-by-field diff
    # works without remapping.
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
    shape PR 3's retriever expects."""
    from models.database import PaperlessExtractionExample
    return PaperlessExtractionExample(
        doc_text=tracking.doc_text or "",
        llm_output=dict(tracking.original_metadata or {}),
        user_approved=dict(current),
        source="paperless_ui_sweep",
        user_id=tracking.user_id,
        # doc_text_embedding is filled in Stage 3 (see run_sweep_tick).
        doc_text_embedding=None,
    )


async def run_abandoned_confirm_sweep(
    *,
    now: datetime | None = None,
    max_age_hours: int = 24,
) -> int:
    """Delete ``paperless_pending_confirms`` rows older than
    *max_age_hours*. The FK cascade on ``chat_uploads`` doesn't help
    here — the ChatUpload itself is still valid (the user may upload
    it again); only the stale confirm-state needs to go.

    Returns the number of rows deleted. Designed to be safely re-run.
    """
    from sqlalchemy import delete

    from models.database import PaperlessPendingConfirm
    from services.database import AsyncSessionLocal

    current = now or datetime.utcnow()
    cutoff = current - timedelta(hours=max_age_hours)

    async with AsyncSessionLocal() as db:
        result = await db.execute(
            delete(PaperlessPendingConfirm)
            .where(PaperlessPendingConfirm.created_at < cutoff)
        )
        await db.commit()
        deleted = result.rowcount or 0

    if deleted:
        logger.info(
            "abandoned-confirm sweep: %d pending_confirms older than %d h purged",
            deleted, max_age_hours,
        )
    return deleted
