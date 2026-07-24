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
    mcp_manager, extracted_value: str, *, names: list[str] | None = None, create: bool = True
) -> str | None:
    """Option A + guardrail: map a confidently-new extracted sender to a Paperless
    correspondent NAME the upload can resolve, creating it ONLY when it has no
    fuzzy-near match anywhere in the FULL taxonomy.

    The extractor only matches against a recency-pruned taxonomy window (top-N),
    so its non-exact verdict can be a false-new for a correspondent outside that
    window. We therefore re-check against the FULL correspondent list here before
    creating, to avoid duplicates on a large instance. Returns:

      - an existing canonical name when the sender STRONG-fuzzy matches one
        (recovers a pruned-window miss — reuse, never duplicate);
      - ``None`` when only a LOOSE fuzzy-near match exists (ambiguous → leave the
        field unset, honouring "auto-create only when no fuzzy-near match");
      - the (now-existing) name when the sender is genuinely new and was created;
      - ``None`` on any transport / create failure (caller does a bare upload).

    ``names`` lets a batch caller (the backfill) pass the full list once instead
    of this re-fetching it per document. ``create=False`` makes it side-effect
    free: a genuinely-new sender returns its name as a *preview* WITHOUT actually
    creating the correspondent (so a ``--dry-run`` doesn't mutate Paperless).

    Note: the Paperless MCP's name→id resolver also does a bidirectional
    *substring* match, so ``create_correspondent`` may answer ``already_exists``
    for a containment relationship our (Levenshtein) guardrail treated as new
    (e.g. "Telekom" ⊂ "Telekom Deutschland GmbH"). We reuse that existing name —
    intentional: it avoids a near-duplicate and is correct for the dominant
    recurring-sender case (the same substring resolution the upload would apply
    anyway). A spurious mid-token substring against an unrelated correspondent is
    a rare, accepted limitation (the LLM extracts full sender names).
    """
    value = (extracted_value or "").strip()
    if not value:
        return None
    if names is None:
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
    if not create:
        return value  # preview only (dry-run): report what WOULD be created
    created = _parse_paperless_result(
        await mcp_manager.execute_tool(
            "mcp.paperless.create_correspondent", {"name": value}
        )
    )
    if created.get("error") == "already_exists":
        return created.get("existing_name") or value  # MCP substring match → reuse
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


# ---------------------------------------------------------------------------
# Document-type + tag resolve-or-create (mirror of the correspondent path).
# Same guardrail: strong-fuzzy match in the FULL taxonomy → reuse; loose-near →
# skip (don't auto-create a near-duplicate); genuinely new → create. Extends the
# "resolve-or-create" behaviour correspondents already had to document_type/tags,
# so on a FRESH (or wiped) Paperless the taxonomy self-populates instead of the
# fields staying empty (the 2026-07 reset left doc_type/tags blank because the
# extractor only RESOLVES against the pre-curated taxonomy, which the wipe cleared).
# Config-gated (paperless_autocreate_document_type / _tags), default on.
# ---------------------------------------------------------------------------

_TAXONOMY_LIST_TOOL = {
    "document_type": "mcp.paperless.list_document_types",
    "tag": "mcp.paperless.list_tags",
}
_TAXONOMY_CREATE_TOOL = {
    "document_type": "mcp.paperless.create_document_type",
    "tag": "mcp.paperless.create_tag",
}


async def _fetch_taxonomy_names(mcp_manager, kind: str) -> list[str] | None:
    """FULL Paperless list of names for ``kind`` (document_type/tag). None on
    transport failure — the caller must then NOT guess (no auto-create half-blind)."""
    parsed = _parse_paperless_result(
        await mcp_manager.execute_tool(_TAXONOMY_LIST_TOOL[kind], {})
    )
    if parsed.get("error"):
        return None
    return [it["name"] for it in (parsed.get("items") or []) if it.get("name")]


async def resolve_or_create_taxonomy(
    mcp_manager, kind: str, extracted_value: str, *,
    names: list[str] | None = None, create: bool = True,
) -> str | None:
    """Map a confidently-new extracted document_type/tag to a Paperless NAME the
    upload can resolve, creating it ONLY when it has no fuzzy-near match in the
    FULL taxonomy (same guardrail + matchers as resolve_or_create_correspondent)."""
    value = (extracted_value or "").strip()
    if not value:
        return None
    if names is None:
        names = await _fetch_taxonomy_names(mcp_manager, kind)
    if names is None:
        return None  # couldn't read the taxonomy → don't risk a duplicate
    from services.paperless_metadata_extractor import _fuzzy_match, _fuzzy_top_candidates

    existing = _fuzzy_match(value, names)
    if existing:
        return existing
    if _fuzzy_top_candidates(value, names):
        return None  # fuzzy-near existing → don't auto-create
    if not create:
        return value  # dry-run preview
    created = _parse_paperless_result(
        await mcp_manager.execute_tool(_TAXONOMY_CREATE_TOOL[kind], {"name": value})
    )
    if created.get("error") == "already_exists":
        return created.get("existing_name") or value
    if created.get("id"):
        logger.info(f"folder-ingest paperless: auto-created {kind} {value!r} (id={created.get('id')})")
        return created.get("name") or value
    logger.warning(f"folder-ingest paperless: create {kind} failed for {value!r}: {created.get('error')}")
    return None


async def resolve_document_type_from_metadata(
    mcp_manager, metadata, *, names: list[str] | None = None, create: bool = True
) -> str | None:
    """Document-type NAME to file ``metadata`` under: the exact taxonomy hit if the
    extractor found one, else resolve-or-create the first non-exact extracted type."""
    from utils.config import settings

    if metadata.document_type:
        return metadata.document_type
    if not (create and settings.paperless_autocreate_document_type):
        return None
    new_type = next(
        (
            r.extracted_value
            for r in metadata.resolutions
            if r.field == "document_type" and r.status != "exact" and r.extracted_value
        ),
        None,
    )
    if not new_type:
        return None
    return await resolve_or_create_taxonomy(mcp_manager, "document_type", new_type, names=names)


async def resolve_tags_from_metadata(
    mcp_manager, metadata, *, names: list[str] | None = None, create: bool = True
) -> list[str]:
    """Tag NAMES for ``metadata``: the exact-resolved tags PLUS resolve-or-created
    ones for each non-exact extracted tag. De-duplicated, order-preserving."""
    from utils.config import settings

    result: list[str] = list(metadata.tags or [])
    new_tags = [
        r.extracted_value
        for r in metadata.resolutions
        if r.field == "tag" and r.status != "exact" and r.extracted_value
    ]
    # Only touch the Paperless taxonomy when there is actually a new tag to create
    # — a doc with only exact-resolved (or no) tags makes ZERO MCP calls here.
    if create and settings.paperless_autocreate_tags and new_tags:
        if names is None:
            names = await _fetch_taxonomy_names(mcp_manager, "tag")
        for value in new_tags:
            resolved = await resolve_or_create_taxonomy(
                mcp_manager, "tag", value, names=names
            )
            if resolved:
                result.append(resolved)
                if names is not None and resolved not in names:
                    names.append(resolved)  # so two new tags in one doc don't dup
    # de-dup, preserve order
    seen: set[str] = set()
    return [t for t in result if not (t in seen or seen.add(t))]


async def resolve_correspondent_from_metadata(
    mcp_manager, metadata, *, names: list[str] | None = None, create: bool = True
) -> str | None:
    """The correspondent NAME to file ``metadata`` under — the single source of
    truth shared by the live leg and the backfill, so they can't drift.

    An exact taxonomy hit already populated ``metadata.correspondent`` (the
    extractor never emits an "exact" resolution), so use it directly. Otherwise
    take the first NON-exact correspondent resolution's raw extracted name and
    let ``resolve_or_create_correspondent`` make the full-taxonomy
    reuse/skip/create decision — we deliberately do NOT pre-filter on the
    resolution ``status`` here, because that status was computed against the
    extractor's *pruned* window; the helper re-checks the full list.
    """
    if metadata.correspondent:
        return metadata.correspondent
    new_name = next(
        (
            r.extracted_value
            for r in metadata.resolutions
            if r.field == "correspondent" and r.status != "exact" and r.extracted_value
        ),
        None,
    )
    if not new_name:
        return None
    return await resolve_or_create_correspondent(
        mcp_manager, new_name, names=names, create=create
    )


def make_paperless_leg(
    mcp_manager,
    *,
    user_id: int | None = None,
    lang: str = "de",
    await_timeout_s: float | None = None,
) -> PaperlessLeg:
    """Build a ``PaperlessLeg`` closure over the MCP manager + owner.

    Runs in the **document-worker** as a ``post_document_ingest`` step (the OCR's
    home — Docling is memory-heavy and must not run in the always-on backend, which
    OOM'd when it did). The leg is given the worker's already-computed ``doc_text``
    (the best-quality Docling/OCR ∪ text-layer union) and:
      1. extracts Paperless metadata from that text (no re-OCR, no chunk shortcut);
      2. uploads the original file to Paperless;
      3. **writes ``doc_text`` back as the Paperless document's searchable content**
         (``update_document(content=…)``) so Paperless search uses Renfield's
         high-quality OCR, not its own weaker consume-time OCR.
    When ``doc_text`` is absent (the retry/refile path with no captured text), it
    falls back to a fresh Docling ``extract_from_file`` — still full quality, and
    still in the worker where Docling belongs."""

    async def _leg(
        db: AsyncSession,
        doc: Document,
        file_bytes: bytes,
        meta: IngestMeta,
        doc_text: str | None = None,
    ) -> bool:
        # Idempotent: a re-run for an already-settled doc (done OR terminally
        # failed) does nothing.
        if doc.paperless_state in _PAPERLESS_SETTLED:
            return True

        # 1. Metadata extraction (D5). Prefer the worker's already-extracted
        # high-quality OCR text (no second Docling pass, no chunk shortcut);
        # fall back to a fresh Docling extraction only when no text was supplied.
        # ``ocr_text`` is what we later transport into Paperless's content.
        upload_params: dict = {
            "title": meta.filename,
            "filename": meta.filename,
            "file_content_base64": base64.b64encode(file_bytes).decode("ascii"),
            # We drive the consume poll ourselves via await_consume_result, so
            # the upload returns immediately on accept.
            "wait_for_consume": False,
        }
        ocr_text: str = doc_text or ""
        try:
            from services.paperless_metadata_extractor import PaperlessMetadataExtractor

            extractor = PaperlessMetadataExtractor(mcp_manager=mcp_manager)
            if doc_text:
                extraction = await extractor.extract_from_doc_text(
                    doc_text, user_id=user_id, lang=lang
                )
            else:
                extraction = await extractor.extract_from_file(
                    doc.file_path, user_id=user_id, lang=lang
                )
            # Keep the OCR text the extractor actually used, for content transport.
            ocr_text = doc_text or extraction.doc_text or ""
            if extraction.error:
                logger.info(
                    f"paperless-leg: metadata extraction skipped "
                    f"({extraction.error}); bare upload for doc {doc.id}"
                )
            else:
                m = extraction.metadata
                if m.title:
                    upload_params["title"] = m.title
                # Existing match, or (Option A) resolve-or-create a new sender.
                correspondent = await resolve_correspondent_from_metadata(mcp_manager, m)
                if correspondent:
                    upload_params["correspondent"] = correspondent
                # Resolve-or-create (like correspondent) so document_type/tags
                # self-populate on a fresh/wiped Paperless instead of staying empty.
                # Best-effort + isolated: a taxonomy hiccup here must NOT discard the
                # already-resolved title/correspondent (fall through to upload them).
                try:
                    document_type = await resolve_document_type_from_metadata(mcp_manager, m)
                    if document_type:
                        upload_params["document_type"] = document_type
                    tags = await resolve_tags_from_metadata(mcp_manager, m)
                    if tags:
                        upload_params["tags"] = tags
                except Exception as tax_exc:  # noqa: BLE001 - taxonomy is best-effort
                    logger.warning(
                        f"paperless-leg: document_type/tags resolution failed for "
                        f"doc {doc.id} (uploading with title/correspondent only): {tax_exc}"
                    )
        except Exception as exc:  # noqa: BLE001 - extractor is best-effort
            logger.warning(
                f"paperless-leg: extractor error for doc {doc.id} "
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
                f"paperless-leg: doc {doc.id} {status} "
                f"(paperless_id={outcome.get('document_id')})"
            )
            # Transport Renfield's high-quality OCR into Paperless's searchable
            # content, overwriting Paperless's own weaker consume-time OCR. Only on
            # a freshly-filed 'success' with an id + text we have — a 'duplicate'
            # already exists (leave its content untouched). Best-effort: a failure
            # here does not un-settle the doc (it IS filed); search just keeps
            # Paperless's OCR.
            if status == "success" and pid and ocr_text.strip():
                try:
                    res = _parse_paperless_result(
                        await mcp_manager.execute_tool(
                            "mcp.paperless.update_document",
                            {"document_id": pid, "content": ocr_text},
                        )
                    )
                    if res.get("error"):
                        logger.warning(
                            f"paperless-leg: content transport failed for doc "
                            f"{doc.id} (paperless_id={pid}): {res.get('error')}"
                        )
                    else:
                        logger.info(
                            f"paperless-leg: transported OCR content ({len(ocr_text)} "
                            f"chars) into paperless_id={pid} for doc {doc.id}"
                        )
                except Exception as exc:  # noqa: BLE001 - transport is best-effort
                    logger.warning(
                        f"paperless-leg: content transport error for doc {doc.id}: {exc}"
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
