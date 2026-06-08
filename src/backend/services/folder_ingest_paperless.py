"""The folder-ingest Paperless leg (T5/T10) — the real ``PaperlessLeg`` the
push route wires into ``folder_ingest.ingest_document`` when
``folder_ingest_to_paperless`` is on.

It files the document into Paperless and records the outcome on
``Document.paperless_state`` so the D2 dedup matrix never re-runs a settled leg:

  - extract metadata (best-effort, D5) from the document's persisted recovery
    copy via the now-ChatUpload-decoupled ``PaperlessMetadataExtractor``;
  - ``mcp.paperless.upload_document(wait_for_consume=False)`` — returns fast on
    accept (Paperless decides duplicate/parse-failure later, in consume);
  - ``mcp.paperless.await_consume_result(task_id)`` — the MCP polls the consume
    task and reports the terminal outcome, classifying Paperless's duplicate
    marker (D10) so the marker-string knowledge lives in the MCP, not here.

State mapping (``paperless_state``):
  - success / duplicate  → ``done``   (filed, or already there) — leg settled.
  - non-duplicate failure → ``failed`` (Paperless rejected it; retry won't help)
                                       — leg settled (terminal, prevents a loop).
  - upload error / pending-timeout → left un-set — NOT settled, retried later.

The leg returns True when settled (done or failed), False when it should be
retried. Best-effort throughout: a Paperless hiccup never fails the KB ingest
(the bridge already enqueued the document).
"""

from __future__ import annotations

import base64
import json

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from models.database import (
    PAPERLESS_STATE_DONE,
    PAPERLESS_STATE_FAILED,
    Document,
)
from services.folder_ingest import _PAPERLESS_SETTLED, IngestMeta, PaperlessLeg


def _parse_paperless_result(mcp_result: dict | None) -> dict:
    """Unwrap the MCPManager envelope to the paperless tool's own dict.

    The tool's return (``{task_id, ...}`` / ``{status, ...}`` / ``{error}``) is
    JSON-encoded in ``mcp_result["message"]``; ``mcp_result["success"]`` is the
    transport-level envelope flag. Returns ``{"error": ...}`` on a transport
    failure or an unparseable body."""
    if not mcp_result or not mcp_result.get("success"):
        return {"error": (mcp_result or {}).get("message") or "mcp_transport_error"}
    msg = mcp_result.get("message")
    if isinstance(msg, str):
        try:
            inner = json.loads(msg)
            if isinstance(inner, dict):
                return inner
        except (json.JSONDecodeError, TypeError):
            pass
    return {"error": "unparseable_paperless_response"}


async def _fetch_correspondent_names(mcp_manager) -> list[str] | None:
    """The FULL Paperless correspondent list (names). None on transport failure
    — the caller must then NOT guess (no auto-create on a half-known taxonomy)."""
    parsed = _parse_paperless_result(
        await mcp_manager.execute_tool("mcp.paperless.list_correspondents", {})
    )
    if parsed.get("error"):
        return None
    return [it["name"] for it in (parsed.get("items") or []) if it.get("name")]


async def resolve_or_create_correspondent(
    mcp_manager, extracted_value: str
) -> str | None:
    """Option A + guardrail: map a confidently-new extracted sender to a Paperless
    correspondent NAME the upload can resolve, creating it ONLY when it has no
    fuzzy-near match anywhere in the FULL taxonomy.

    The extractor only matches against a recency-pruned taxonomy window (top-N),
    so its "new sender" verdict (a ``status=="none"`` resolution) can be a
    false-new for a correspondent outside that window. We therefore re-check
    against the FULL correspondent list here before creating, to avoid duplicates
    on a large instance. Returns:

      - an existing canonical name when the sender STRONG-fuzzy matches one
        (recovers a pruned-window miss — reuse, never duplicate);
      - ``None`` when only a LOOSE fuzzy-near match exists (ambiguous → leave the
        field unset, honouring "auto-create only when no fuzzy-near match");
      - the (now-existing) name when the sender is genuinely new and was created;
      - ``None`` on any transport / create failure (caller does a bare upload).
    """
    value = (extracted_value or "").strip()
    if not value:
        return None
    names = await _fetch_correspondent_names(mcp_manager)
    if names is None:
        return None  # couldn't read the taxonomy → don't risk a duplicate
    # Reuse the extractor's own matchers so "existing" means the same thing here
    # as it does inside extraction.
    from services.paperless_metadata_extractor import _fuzzy_match, _fuzzy_top_candidates

    existing = _fuzzy_match(value, names)
    if existing:
        return existing  # strong match in the full list → reuse (pruned-window recovery)
    if _fuzzy_top_candidates(value, names):
        return None  # fuzzy-near existing → guardrail: don't auto-create
    created = _parse_paperless_result(
        await mcp_manager.execute_tool(
            "mcp.paperless.create_correspondent", {"name": value}
        )
    )
    if created.get("error") == "already_exists":
        return created.get("existing_name") or value  # raced / exact-dup → reuse
    if created.get("id"):
        logger.info(
            f"folder-ingest paperless: auto-created correspondent {value!r} "
            f"(id={created.get('id')})"
        )
        return created.get("name") or value
    logger.warning(
        f"folder-ingest paperless: create_correspondent failed for {value!r}: "
        f"{created.get('error')}"
    )
    return None


def make_paperless_leg(
    mcp_manager,
    *,
    user_id: int | None = None,
    lang: str = "de",
    await_timeout_s: float | None = None,
) -> PaperlessLeg:
    """Build a ``PaperlessLeg`` closure over the MCP manager + owner. The route
    passes the result to ``ingest_document(paperless_leg=...)`` when Paperless
    filing is enabled; ``None`` is passed when it is off."""

    async def _leg(
        db: AsyncSession, doc: Document, file_bytes: bytes, meta: IngestMeta
    ) -> bool:
        # Idempotent: a re-push of an already-settled leg (done OR terminally
        # failed) does nothing. In practice classify_existing already routes a
        # settled doc to DUPLICATE so the leg isn't called — this is defence in
        # depth for any direct caller.
        if doc.paperless_state in _PAPERLESS_SETTLED:
            return True

        # 1. Best-effort metadata extraction (D5) from the persisted recovery
        # copy. Failure → bare upload (filename title only), never fatal.
        upload_params: dict = {
            "title": meta.filename,
            "filename": meta.filename,
            "file_content_base64": base64.b64encode(file_bytes).decode("ascii"),
            # We drive the consume poll ourselves via await_consume_result, so
            # the upload returns immediately on accept.
            "wait_for_consume": False,
        }
        try:
            from services.paperless_metadata_extractor import PaperlessMetadataExtractor

            extractor = PaperlessMetadataExtractor(mcp_manager=mcp_manager)
            extraction = await extractor.extract_from_file(
                doc.file_path, user_id=user_id, lang=lang
            )
            if extraction.error:
                logger.info(
                    f"folder-ingest paperless: metadata extraction skipped "
                    f"({extraction.error}); bare upload for doc {doc.id}"
                )
            else:
                m = extraction.metadata
                if m.title:
                    upload_params["title"] = m.title
                if m.correspondent:
                    upload_params["correspondent"] = m.correspondent
                else:
                    # Option A: a confidently-new sender (a ``status=="none"``
                    # correspondent resolution = no near match even in the
                    # extractor's pruned window) → resolve-or-create against the
                    # FULL taxonomy, with the no-fuzzy-near guardrail.
                    new_name = next(
                        (
                            r.extracted_value
                            for r in m.resolutions
                            if r.field == "correspondent"
                            and r.status == "none"
                            and r.extracted_value
                        ),
                        None,
                    )
                    if new_name:
                        resolved = await resolve_or_create_correspondent(
                            mcp_manager, new_name
                        )
                        if resolved:
                            upload_params["correspondent"] = resolved
                if m.document_type:
                    upload_params["document_type"] = m.document_type
                if m.tags:
                    upload_params["tags"] = m.tags
        except Exception as exc:  # noqa: BLE001 - extractor is best-effort
            logger.warning(
                f"folder-ingest paperless: extractor error for doc {doc.id} "
                f"(bare upload): {exc}"
            )

        # 2. Upload (non-blocking). Distinguish a TRANSPORT failure (MCP
        # unreachable — envelope success=False) from a TOOL-LEVEL rejection
        # (Paperless said no: bad field / 4xx / config — a body with `error`).
        # Transport → transient, leave unset, retry. Tool rejection → terminal:
        # re-sending the same bytes+metadata yields the same answer, so record
        # FAILED (settled) rather than looping forever on PAPERLESS_ONLY re-pushes.
        upload_raw = await mcp_manager.execute_tool(
            "mcp.paperless.upload_document", upload_params
        )
        if not upload_raw or not upload_raw.get("success"):
            logger.warning(
                f"folder-ingest paperless: MCP transport failure uploading "
                f"doc {doc.id}; will retry"
            )
            return False
        upload = _parse_paperless_result(upload_raw)
        task_id = upload.get("task_id")
        if upload.get("error") or not task_id:
            doc.paperless_state = PAPERLESS_STATE_FAILED
            await db.commit()
            logger.warning(
                f"folder-ingest paperless: Paperless rejected doc {doc.id} "
                f"({upload.get('error') or 'no task_id'}); recorded failed"
            )
            return True

        # 3. Await the consume verdict via the MCP (it owns the duplicate-marker
        # knowledge — D10).
        outcome = _parse_paperless_result(
            await mcp_manager.execute_tool(
                "mcp.paperless.await_consume_result",
                {"task_id": task_id, "timeout_s": await_timeout_s},
            )
        )
        status = outcome.get("status")

        if status in ("success", "duplicate"):
            doc.paperless_state = PAPERLESS_STATE_DONE
            # Persist the filed Paperless id so a later re-tag / backfill can
            # address it directly (a "duplicate" may carry no id — keep NULL).
            pid = outcome.get("document_id")
            if pid:
                doc.paperless_document_id = pid
            await db.commit()
            logger.info(
                f"folder-ingest paperless: doc {doc.id} {status} "
                f"(paperless_id={outcome.get('document_id')})"
            )
            return True

        if status == "failure":
            # Paperless rejected the document for a non-duplicate reason — it
            # won't succeed on retry, so record a terminal FAILED state. Settled
            # (don't loop); the KB still has the document, only Paperless filing
            # is skipped (observable in Paperless's own failed-task log).
            doc.paperless_state = PAPERLESS_STATE_FAILED
            await db.commit()
            logger.warning(
                f"folder-ingest paperless: doc {doc.id} consume FAILED "
                f"(non-duplicate): {outcome.get('detail')}"
            )
            return True

        # pending (consume still running past the timeout) or an error reaching
        # the task endpoint → not settled; leave paperless_state unset so a later
        # pass retries.
        logger.info(
            f"folder-ingest paperless: doc {doc.id} consume not terminal "
            f"({status or outcome.get('error')}); will retry"
        )
        return False

    return _leg
