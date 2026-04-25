"""
paperless_commit_upload — second half of the cold-start confirm flow.

When ``forward_attachment_to_paperless`` runs extraction during the
cold-start window (user's first N uploads), it persists the extracted
metadata into ``paperless_pending_confirms`` and returns a confirm
token instead of firing the upload immediately. The user sees a
German confirm preview; their next chat message is their response.

This tool handles that second turn:

    user message "ja" / "nein" / "1: 2, 2: neu"
        │
        ▼
    paperless_commit_upload(confirm_token, user_response_text)
        │   1. SELECT pending_confirms row (session-scoped per #442)
        │   2. Parse user_response_text:
        │        - "ja" family → apply defaults to every resolution
        │          (pick #1 if near_matches non-empty, else "neu")
        │        - "nein" family → abort (delete ChatUpload + row, done)
        │        - "<idx>: <choice>" pairs → per-resolution decision
        │          where idx is the [N] number from the preview and
        │          choice is one of: a number from the candidate
        │          list, "neu", or "x" (skip the field).
        │   3. For each "neu" decision, call mcp.paperless.create_*
        │      and invalidate the extractor's taxonomy cache.
        │   4. Build final field values: post_fuzzy from extraction +
        │      resolved-by-user fields layered on top.
        │   5. Call mcp.paperless.upload_document with final fields.
        │   6. On success:
        │        - Write (doc_text, llm_output, user_approved) into
        │          paperless_extraction_examples (confirm-diff signal).
        │        - Increment users.paperless_confirms_used.
        │        - Delete the pending_confirms row.
        │   7. Return success with a user-facing message.
"""
from __future__ import annotations

import json
import re
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

# Per-resolution choice tokens. Only used as the right-hand side of a
# "<idx>: <choice>" pair, so the collision between "n" here and the
# top-level abort token "n" never fires (different parsing contexts).
# The preview marker "n. NEU anlegen" needs "n" to be valid here.
_NEW_TOKENS = {"n", "neu", "new", "anlegen", "create"}
_SKIP_TOKENS = {"x", "skip", "leer", "weglassen", "ueberspringen", "überspringen"}

# Pair format in the user reply: "1: 2, 2: neu, 3: x". Index is the
# [N] number from the confirm preview; value is a candidate index,
# "neu", or "x". Whitespace-tolerant. The lookahead also stops on the
# next "<digit>:" pair so phone-typed replies like "1:2 2:neu" (no
# comma) still split into two decisions instead of swallowing the
# second pair into the first value.
_CHOICE_RE = re.compile(
    r"\s*(\d+)\s*[:=]\s*([^,;\n]+?)\s*(?=,|;|$|\d+\s*[:=])"
)

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
    raw_response = user_response.strip()
    normalised = raw_response.lower()
    resolutions: list[dict[str, Any]] = list(pending.proposals or [])

    if normalised in _ABORT_TOKENS:
        return await _abort_pending(pending)

    if normalised in _APPROVE_TOKENS:
        # "ja" → defaults: pick #1 for resolutions with near matches,
        # "neu" for resolutions without. No-op when there are none.
        decisions = _default_decisions(resolutions)
        return await _commit_approved(
            pending,
            mcp_manager=mcp_manager,
            user_id=user_id,
            decisions=decisions,
        )

    # Try to parse per-field choices. Only meaningful when we actually
    # have resolutions to assign decisions to.
    if resolutions:
        decisions, parse_error = _parse_user_choices(raw_response, resolutions)
        if parse_error is None:
            return await _commit_approved(
                pending,
                mcp_manager=mcp_manager,
                user_id=user_id,
                decisions=decisions,
            )

    # Unrecognised response. Count the round, re-prompt if we still have
    # budget, otherwise force-abort.
    return await _handle_ambiguous_response(pending)


def _resolution_value(res: dict[str, Any]) -> str:
    """Extracted value of a resolution. Falls back to legacy ``value``
    so pending rows persisted under the old NewEntryProposal shape
    (``{field, value, reasoning}``) still carry signal — without this
    fallback the user's "ja" silently drops the LLM's pick because the
    new shape's ``extracted_value`` key is absent."""
    return (
        res.get("extracted_value")
        or res.get("value")
        or ""
    )


def _default_decisions(
    resolutions: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Build the default decision per resolution for the `ja` path.

    - near_matches non-empty → pick the first candidate (safe: the user
      sees it as the "(Vorschlag)" entry in the preview).
    - near_matches empty     → SKIP. Auto-creating a new taxonomy
      entry from a single OCR'd value without explicit user consent
      pollutes the user's Paperless instance with typos and one-off
      misreads forever. The user must explicitly type "<idx>: neu"
      to opt in to creating an entry.

    Each decision dict has shape:
        {"resolution": <res>, "action": "use" | "create" | "skip",
         "value": <str>}
    """
    out: list[dict[str, Any]] = []
    for res in resolutions:
        near = list(res.get("near_matches") or [])
        if near:
            out.append({
                "resolution": res,
                "action": "use",
                "value": near[0],
            })
        else:
            out.append({
                "resolution": res,
                "action": "skip",
                "value": "",
            })
    return out


def _parse_user_choices(
    text: str,
    resolutions: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], str | None]:
    """Parse a "1: 2, 2: neu, 3: x" reply into a decision per resolution.

    Resolutions not addressed in the user reply fall back to their
    default (same as the `ja` path). Indices outside 1..len(resolutions)
    or candidate indices outside 1..len(near_matches) → parse error
    (caller treats as ambiguous and re-prompts).
    """
    matches = _CHOICE_RE.findall(text)
    if not matches:
        return [], "no choices found"

    # Seed with defaults so unaddressed resolutions still get a value.
    decisions = _default_decisions(resolutions)
    by_idx = {i + 1: d for i, d in enumerate(decisions)}

    for raw_idx, raw_choice in matches:
        try:
            idx = int(raw_idx)
        except ValueError:
            return [], f"bad index {raw_idx!r}"
        if idx not in by_idx:
            return [], f"index {idx} out of range"
        decision = by_idx[idx]
        res = decision["resolution"]
        choice = raw_choice.strip().lower()

        if choice in _NEW_TOKENS:
            decision["action"] = "create"
            decision["value"] = _resolution_value(res)
            if not decision["value"]:
                return [], f"resolution {idx} has no value to create"
        elif choice in _SKIP_TOKENS:
            decision["action"] = "skip"
            decision["value"] = ""
        elif choice.isdigit():
            cand_idx = int(choice)
            near = list(res.get("near_matches") or [])
            if cand_idx < 1 or cand_idx > len(near):
                return [], f"candidate {cand_idx} out of range for field {idx}"
            decision["action"] = "use"
            decision["value"] = near[cand_idx - 1]
        else:
            return [], f"unrecognised choice {raw_choice!r} for field {idx}"

    return decisions, None


async def _commit_approved(
    pending,
    *,
    mcp_manager: Any,
    user_id: int | None,
    decisions: list[dict[str, Any]] | None = None,
) -> dict:
    """User approved. Fire creates for "create" decisions, set
    "use" decisions on the final field set, skip "skip" decisions,
    then upload_document, write the confirm-diff, bump the cold-start
    counter, and delete the pending row.

    ``decisions`` carries one entry per resolution from the pending row
    in the same order, with shape
    ``{"resolution": <dict>, "action": "use"|"create"|"skip", "value": str}``.
    None / empty when the pending row had no resolutions (every field
    resolved exactly during extraction).
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
    llm_output = pending.llm_output or {}
    decisions = decisions or []

    # Step 1 — fire create_* for every "create" decision. Singleton
    # fields (correspondent / document_type / storage_path) trigger
    # one create per decision; tag decisions trigger one create per
    # tag. Successful creates feed `resolved_fields` so the upload
    # picks up the new value.
    created_entries: dict[str, list[str]] = {}
    resolved_fields: dict[str, list[str]] = {
        "correspondent": [],
        "document_type": [],
        "storage_path": [],
        "tag": [],
    }

    for decision in decisions:
        action = decision.get("action")
        value = (decision.get("value") or "").strip()
        res = decision.get("resolution") or {}
        field = res.get("field")
        if not field or action == "skip" or not value:
            continue

        if action == "use":
            resolved_fields.setdefault(field, []).append(value)
            continue

        if action != "create":
            logger.warning("Unknown decision action, skipping: %r", action)
            continue

        # action == "create"
        create_tool = {
            "correspondent": "mcp.paperless.create_correspondent",
            "document_type": "mcp.paperless.create_document_type",
            "tag": "mcp.paperless.create_tag",
            "storage_path": "mcp.paperless.create_storage_path",
        }.get(field)
        if create_tool is None:
            logger.warning("Unknown decision field, skipping: %r", field)
            continue

        create_params: dict[str, Any] = {"name": value}
        if field == "storage_path":
            # Storage paths need a path template. Use the value for
            # both name and path; user can rename in Paperless UI.
            create_params["path"] = value

        try:
            result = await mcp_manager.execute_tool(create_tool, create_params)
        except Exception as exc:
            logger.warning("Failed to create %s %r: %s", field, value, exc)
            continue

        if not result or not result.get("success"):
            inner = (result or {}).get("message") or "no detail"
            if "already_exists" in str(inner):
                logger.info("%s %r already exists, reusing", field, value)
                resolved_fields.setdefault(field, []).append(value)
                created_entries.setdefault(field, []).append(value)
            else:
                logger.warning(
                    "Create %s %r failed: %s", field, value, str(inner)[:120],
                )
            continue
        resolved_fields.setdefault(field, []).append(value)
        created_entries.setdefault(field, []).append(value)

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

    # Build the final field set: post_fuzzy (the exact-resolved fields
    # from extraction) + resolved_fields (singletons set by user
    # decision OR newly-created entries). For singletons we take the
    # last value the user picked / created (only one decision per
    # field is meaningful). For tags we union the resolved set with
    # what came out of extraction.
    final_fields: dict[str, Any] = {}
    for key in ("correspondent", "document_type", "storage_path"):
        decided = resolved_fields.get(key) or []
        if decided:
            final_fields[key] = decided[-1]
        elif post_fuzzy.get(key):
            final_fields[key] = post_fuzzy[key]

    tags_out: list[str] = list(post_fuzzy.get("tags") or [])
    for tag_value in resolved_fields.get("tag") or []:
        if tag_value not in tags_out:
            tags_out.append(tag_value)
    if tags_out:
        final_fields["tags"] = tags_out

    if post_fuzzy.get("created_date"):
        final_fields["created_date"] = post_fuzzy["created_date"]

    tool_params: dict[str, Any] = {
        "title": post_fuzzy.get("title") or upload.filename,
        "filename": upload.filename,
        "file_content_base64": file_content_base64,
    }
    for key in ("correspondent", "document_type", "tags", "storage_path", "created_date"):
        val = final_fields.get(key)
        if val:
            tool_params[key] = val

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
    # across it would tie up the pool for no reason.
    user_approved = dict(post_fuzzy)
    user_approved["title"] = tool_params["title"]
    for key in ("correspondent", "document_type", "storage_path", "created_date"):
        if key in final_fields:
            user_approved[key] = final_fields[key]
        else:
            # User explicitly skipped (or extraction produced nothing).
            user_approved.pop(key, None)
    user_approved["tags"] = list(final_fields.get("tags") or [])

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
        flat: list[str] = []
        for values in created_entries.values():
            flat.extend(values)
        if flat:
            created_note = f" (Neu angelegt: {', '.join(sorted(set(flat)))})"

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
