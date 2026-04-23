"""
paperless_commit_upload — second half of the cold-start confirm flow.

When ``forward_attachment_to_paperless`` runs extraction during the
cold-start window (user's first N uploads), it persists the extracted
metadata into ``paperless_pending_confirms`` and returns a confirm
token instead of firing the upload immediately. The user sees a
German confirm preview; their next chat message is their response.

This tool handles that second turn:

    user message "ja" / "nein" / "ändere X"
        │
        ▼
    paperless_commit_upload(confirm_token, user_response_text)
        │   1. SELECT pending_confirms row (session-scoped per #442)
        │   2. Parse user_response_text:
        │        - "ja" family → approve as-is
        │        - "nein" family → abort (delete ChatUpload + row, done)
        │        - anything else (v1) → ask again, bump edit_rounds
        │   3. If approved with new-entry proposals:
        │        - For each proposal, call mcp.paperless.create_*
        │        - Invalidate the extractor's taxonomy cache so the
        │          next extraction sees the new entries
        │   4. Call mcp.paperless.upload_document with final fields
        │   5. On success:
        │        - Write (doc_text, llm_output, user_approved) into
        │          paperless_extraction_examples if the user's
        │          approved fields differ from the LLM's post-fuzzy
        │          output (confirm-diff signal for PR 3).
        │        - Increment users.paperless_confirms_used.
        │        - Delete the pending_confirms row.
        │   6. Return success with a user-facing message.
"""
from __future__ import annotations

import json
from typing import Any

from loguru import logger

# NB: SQLAlchemy + ORM-model + session-factory imports are done lazily
# inside each function. Importing ``services.database`` at module level
# triggers engine creation at import time, which breaks clean-env unit
# tests and mirrors the exact anti-pattern PR #428 fixed for
# ``_init_mcp``.


# Approve / abort token families. Case-insensitive, whitespace-stripped.
_APPROVE_TOKENS = {"ja", "j", "ok", "okay", "passt", "passt so", "yes", "y", "sure"}
_ABORT_TOKENS = {"nein", "n", "abbrechen", "stopp", "stop", "no", "cancel"}

# v1 keeps edit-parsing dumb: ja/nein only; anything else gets an error
# message. Smarter correction parsing lands with PR 5 (interactive card)
# or a separate follow-up. The cold-start confirm is capped at 10
# uploads per user so this rough UX has a bounded impact.
_MAX_EDIT_ROUNDS = 3


PAPERLESS_COMMIT_TOOLS: dict = {
    "internal.paperless_commit_upload": {
        "description": (
            "Finalise or abort a pending Paperless upload. Call this tool "
            "when the previous assistant turn returned "
            "action_required=paperless_confirm and the user has now "
            "responded. Pass the confirm_token from the previous turn AND "
            "the user's response text verbatim. Supports 'ja' to approve, "
            "'nein' to abort."
        ),
        "parameters": {
            "confirm_token": (
                "UUID from the action_required=paperless_confirm response. "
                "Required."
            ),
            "user_response_text": (
                "The user's response message verbatim. Required."
            ),
        },
    },
}


async def paperless_commit_upload(
    params: dict,
    mcp_manager=None,
    session_id: str | None = None,
    user_id: int | None = None,
) -> dict:
    """Finalise or abort a pending-confirm upload.

    Returns a standard agent-tool result dict with ``success``,
    ``message``, ``action_taken``, and ``data``.
    """
    confirm_token = params.get("confirm_token")
    user_response = params.get("user_response_text") or params.get("user_response") or ""

    if not confirm_token:
        return {
            "success": False,
            "message": "Parameter 'confirm_token' is required",
            "action_taken": False,
        }
    if not isinstance(confirm_token, str):
        return {
            "success": False,
            "message": f"'confirm_token' must be a string, got {type(confirm_token).__name__}",
            "action_taken": False,
        }
    if mcp_manager is None:
        return {
            "success": False,
            "message": "MCP manager not available — Paperless MCP not wired in",
            "action_taken": False,
        }

    # Lazy imports — see module-level note on why.
    from sqlalchemy import select

    from models.database import PaperlessPendingConfirm
    from services.database import AsyncSessionLocal

    # Look up the pending row (session-scoped).
    async with AsyncSessionLocal() as db:
        query = select(PaperlessPendingConfirm).where(
            PaperlessPendingConfirm.confirm_token == confirm_token
        )
        if session_id is not None:
            query = query.where(PaperlessPendingConfirm.session_id == session_id)
        result = await db.execute(query)
        pending = result.scalar_one_or_none()

    if pending is None:
        # Soft-404 — don't distinguish cross-session probe from
        # genuinely-unknown-token. Same privacy treatment as #442.
        return {
            "success": False,
            "message": (
                "Die Bestätigung ist abgelaufen oder unbekannt. Bitte lade "
                "das Dokument erneut hoch."
            ),
            "action_taken": False,
        }

    # Classify the user's response.
    normalised = user_response.strip().lower()
    if normalised in _APPROVE_TOKENS:
        return await _commit_approved(
            pending, mcp_manager=mcp_manager, user_id=user_id,
        )
    if normalised in _ABORT_TOKENS:
        return await _abort_pending(pending)

    # Unrecognised response. v1 behaviour: count the round, re-prompt if
    # we still have budget, otherwise force-abort.
    return await _handle_ambiguous_response(pending)


async def _commit_approved(
    pending,
    *,
    mcp_manager: Any,
    user_id: int | None,
) -> dict:
    """User said 'ja'. Fire creates for approved proposals, then
    upload_document, then write the confirm-diff, increment the
    cold-start counter, and delete the pending row.
    """
    from sqlalchemy import delete, update

    from models.database import (
        ChatUpload,
        PaperlessExtractionExample,
        PaperlessPendingConfirm,
        User,
    )
    from services.database import AsyncSessionLocal

    post_fuzzy = pending.post_fuzzy_output or {}
    proposals = pending.proposals or []
    llm_output = pending.llm_output or {}

    # Step 1 — approve new-entry proposals by calling the matching
    # create_* MCP tool. Each success invalidates the extractor's
    # taxonomy cache so subsequent extractions see the new entry.
    created_entries: dict[str, str] = {}
    for proposal in proposals:
        field = proposal.get("field")
        value = proposal.get("value")
        if not field or not value:
            continue
        create_tool = {
            "correspondent": "mcp.paperless.create_correspondent",
            "document_type": "mcp.paperless.create_document_type",
            "tag": "mcp.paperless.create_tag",
            "storage_path": "mcp.paperless.create_storage_path",
        }.get(field)
        if create_tool is None:
            logger.warning("Unknown proposal field, skipping: %r", field)
            continue

        create_params: dict[str, Any] = {"name": value}
        if field == "storage_path":
            # Storage paths need a path template. Use the LLM's suggested
            # value for both name and path — crude v1 fallback, user can
            # rename in Paperless UI if needed.
            create_params["path"] = value

        try:
            result = await mcp_manager.execute_tool(create_tool, create_params)
        except Exception as exc:
            logger.warning(
                "Failed to create %s %r: %s", field, value, exc,
            )
            continue

        if not result or not result.get("success"):
            inner = (result or {}).get("message") or "no detail"
            if "already_exists" in str(inner):
                # Raced against another extraction — someone else already
                # created this entry. Treat as success.
                logger.info("%s %r already exists, reusing", field, value)
                created_entries[field] = value
            else:
                logger.warning(
                    "Create %s %r failed: %s", field, value, str(inner)[:120],
                )
            continue
        created_entries[field] = value

    # Flush the extractor's taxonomy cache so future extractions see
    # the new entries.
    if created_entries:
        try:
            from services.paperless_metadata_extractor import _invalidate_taxonomy_cache
            _invalidate_taxonomy_cache()
        except Exception as exc:  # pragma: no cover — defensive
            logger.debug("Could not flush taxonomy cache: %s", exc)

    # Step 2 — fetch the ChatUpload bytes + build the upload call.
    async with AsyncSessionLocal() as db:
        upload = await db.get(ChatUpload, pending.attachment_id)

    if upload is None or not upload.file_path:
        # ChatUpload vanished (manual purge, sweeper, or FK cascade
        # elsewhere). The FK is ON DELETE CASCADE so the pending row
        # would normally be gone too — but if the row survived via a
        # broken file_path on disk, drop it explicitly so we don't
        # strand orphaned pending_confirms.
        async with AsyncSessionLocal() as db:
            await db.execute(
                delete(PaperlessPendingConfirm).where(
                    PaperlessPendingConfirm.confirm_token == pending.confirm_token
                )
            )
            await db.commit()
        return {
            "success": False,
            "message": "Anhang nicht mehr verfügbar.",
            "action_taken": False,
        }

    try:
        from pathlib import Path as _Path
        import base64 as _base64
        with open(upload.file_path, "rb") as f:
            file_bytes = f.read()
        file_content_base64 = _base64.b64encode(file_bytes).decode("ascii")
    except Exception as exc:
        return {
            "success": False,
            "message": f"Konnte Datei nicht lesen: {exc!s}",
            "action_taken": False,
        }

    tool_params: dict[str, Any] = {
        "title": post_fuzzy.get("title") or upload.filename,
        "filename": upload.filename,
        "file_content_base64": file_content_base64,
    }
    for key in ("correspondent", "document_type", "tags", "storage_path"):
        val = post_fuzzy.get(key)
        if val:
            tool_params[key] = val
    created = post_fuzzy.get("created_date")
    if created:
        tool_params["created_date"] = created

    try:
        mcp_result = await mcp_manager.execute_tool(
            "mcp.paperless.upload_document", tool_params
        )
    except Exception as exc:
        return {
            "success": False,
            "message": f"Paperless-Upload fehlgeschlagen: {exc!s}",
            "action_taken": False,
        }

    if not mcp_result or not mcp_result.get("success"):
        detail = (mcp_result or {}).get("message") or "unknown"
        return {
            "success": False,
            "message": f"Paperless-Upload fehlgeschlagen: {detail}",
            "action_taken": False,
        }

    # Parse the MCP envelope to pull out post_upload_patch status.
    inner_msg = mcp_result.get("message")
    inner: dict[str, Any] = {}
    if isinstance(inner_msg, str):
        try:
            parsed = json.loads(inner_msg)
        except (json.JSONDecodeError, TypeError):
            parsed = None
        if isinstance(parsed, dict):
            inner = parsed
    elif isinstance(inner_msg, dict):
        inner = inner_msg

    task_id = inner.get("task_id")
    document_id = inner.get("document_id")
    patch_state = inner.get("post_upload_patch")

    # Step 3 — compute the approved field set + embed the doc_text
    # BEFORE opening the write session. The embed call is up to 5 s
    # (see retriever ``_EMBED_TIMEOUT_S``); holding a DB connection
    # across it would tie up the pool for no reason. Post-PR-3 the
    # session is only open for the actual writes.
    user_approved = dict(post_fuzzy)
    for field, value in created_entries.items():
        if field == "tag":
            # Tag proposals feed the tags list — append if not already
            # there. Post-fuzzy tags list carries the taxonomy-hit
            # tags; the new tag is one we just created.
            existing_tags = list(user_approved.get("tags") or [])
            if value not in existing_tags:
                existing_tags.append(value)
            user_approved["tags"] = existing_tags
        else:
            user_approved[field] = value

    diff_row: PaperlessExtractionExample | None = None
    if user_approved != post_fuzzy:
        doc_text = _truncate_doc_text(pending)
        # PR 3: embed at write time so the row is retrievable as a
        # learning example on subsequent extractions. Embed failure
        # is non-fatal — the row still captures the raw diff signal,
        # just won't surface via similarity until a backfill runs.
        #
        # Strip the ``_doc_text`` scratchpad key from ``llm_output``
        # before persisting: leaving it in would double the document
        # inside every future prompt (once as the snippet, once inside
        # the rendered LLM-proposal JSON) and leak the untruncated
        # text — the snippet is already capped at 600 chars.
        from services.paperless_example_retriever import embed_doc_text
        doc_text_embedding = await embed_doc_text(doc_text)
        llm_output_for_persist = {
            k: v for k, v in llm_output.items() if k != "_doc_text"
        }
        diff_row = PaperlessExtractionExample(
            doc_text=doc_text,
            llm_output=llm_output_for_persist,
            user_approved=user_approved,
            source="confirm_diff",
            doc_text_embedding=doc_text_embedding,
            user_id=user_id,
        )

    # Step 3.5 — build the upload-tracking row (PR 4). Only if we
    # actually got a document_id back; without it the sweeper has
    # nothing to fetch. ``doc_text`` here feeds the future ui_sweep
    # row if the user edits in the Paperless UI within 1 h.
    tracking_row = None
    if document_id is not None:
        from models.database import PaperlessUploadTracking
        tracking_row = PaperlessUploadTracking(
            chat_upload_id=pending.attachment_id,
            paperless_document_id=int(document_id),
            user_id=user_id,
            original_metadata=dict(user_approved),
            doc_text=_truncate_doc_text(pending) if pending else None,
        )

    async with AsyncSessionLocal() as db:
        if diff_row is not None:
            db.add(diff_row)
        if tracking_row is not None:
            db.add(tracking_row)

        # Step 4 — increment the cold-start counter. Design: increment
        # ONLY on successful upload, and only when we actually know the
        # user. Resolution from session → conversation → user is done
        # by the caller and passed as user_id.
        if user_id is not None:
            await db.execute(
                update(User)
                .where(User.id == user_id)
                .values(
                    paperless_confirms_used=User.paperless_confirms_used + 1
                )
            )

        # Step 5 — delete the pending confirm row.
        await db.execute(
            delete(PaperlessPendingConfirm).where(
                PaperlessPendingConfirm.confirm_token == pending.confirm_token
            )
        )
        await db.commit()

    # Step 6 — user-facing response.
    if patch_state == "success":
        suffix = " mit allen Metadaten."
    elif patch_state in ("timed_out", "retries_exhausted", "client_error"):
        suffix = (
            " — aber der Speicherpfad konnte nicht gesetzt werden. "
            "Bitte in Paperless selbst anpassen."
        )
    else:
        suffix = "."

    created_note = ""
    if created_entries:
        names = ", ".join(sorted(created_entries.values()))
        created_note = f" (Neu angelegt: {names})"

    return {
        "success": True,
        "message": f"Im Paperless abgelegt: {upload.filename}{suffix}{created_note}",
        "action_taken": True,
        "data": {
            "task_id": task_id,
            "document_id": document_id,
            "post_upload_patch": patch_state,
            "created_entries": created_entries,
        },
    }


async def _abort_pending(pending) -> dict:
    """User said 'nein'. Delete the ChatUpload bytes + row + the
    pending confirm. Nothing lands in Paperless."""
    from sqlalchemy import delete

    from models.database import ChatUpload, PaperlessPendingConfirm
    from services.database import AsyncSessionLocal

    attachment_id = pending.attachment_id

    async with AsyncSessionLocal() as db:
        # Delete the pending row first (it has FK → chat_uploads with
        # CASCADE, but we want explicit control over the order for
        # clarity in logs).
        await db.execute(
            delete(PaperlessPendingConfirm).where(
                PaperlessPendingConfirm.confirm_token == pending.confirm_token
            )
        )
        # Soft-delete the ChatUpload. Bytes on disk get cleaned up by
        # the existing chat-upload sweeper. Fallback: if the ChatUpload
        # row is fully removed elsewhere, the cascade already fired.
        upload = await db.get(ChatUpload, attachment_id)
        if upload is not None:
            # Mark as deleted but keep the row for audit trail.
            # (If the ChatUpload model doesn't have is_deleted, leave
            # it alone — disk cleanup handles the rest.)
            if hasattr(upload, "is_deleted"):
                upload.is_deleted = True
        await db.commit()

    return {
        "success": True,
        "message": "Abgebrochen. Das Dokument wurde nicht abgelegt.",
        "action_taken": True,
        "data": {"aborted": True, "attachment_id": attachment_id},
    }


async def _handle_ambiguous_response(pending) -> dict:
    """User said something that wasn't 'ja' or 'nein'.

    v1 keeps it simple: bump edit_rounds, if budget remains ask again;
    otherwise force-abort. Real correction parsing lands in a later PR.
    """
    from sqlalchemy import update

    from models.database import PaperlessPendingConfirm
    from services.database import AsyncSessionLocal

    async with AsyncSessionLocal() as db:
        await db.execute(
            update(PaperlessPendingConfirm)
            .where(PaperlessPendingConfirm.confirm_token == pending.confirm_token)
            .values(edit_rounds=PaperlessPendingConfirm.edit_rounds + 1)
        )
        await db.commit()
        # Re-fetch to get the new edit_rounds.
        refreshed = await db.get(PaperlessPendingConfirm, pending.confirm_token)
        rounds = refreshed.edit_rounds if refreshed else _MAX_EDIT_ROUNDS + 1

    if rounds > _MAX_EDIT_ROUNDS:
        # Give up, abort the upload entirely.
        return await _abort_pending(pending)

    return {
        "success": False,
        "message": (
            "Ich habe deine Antwort nicht verstanden. Bitte antworte mit "
            "`ja` zum Ablegen oder `nein` zum Abbrechen."
        ),
        "action_taken": False,
        "data": {
            "confirm_token": pending.confirm_token,
            "action_required": "paperless_confirm",
        },
    }


def _truncate_doc_text(pending, max_chars: int = 8000) -> str:
    """Doc text lives in the pending row's post_fuzzy_output metadata
    indirectly. For v1 we pull it from the llm_output's original
    payload, which the extractor persists alongside the metadata.

    If the pending row doesn't carry doc_text (shape evolved), fall
    back to an empty string — the example row is still useful for the
    user_approved diff, just less effective for future prompt
    augmentation.
    """
    llm = pending.llm_output or {}
    text = llm.get("_doc_text") or ""
    if not isinstance(text, str):
        return ""
    return text[:max_chars]
