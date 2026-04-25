"""
Chat-Upload Tool — Platform-owned agent tool.

Gives the agent a way to forward a file the user has attached in the chat
(via the paperclip icon → ``POST /api/chat_upload``) to Paperless-NGX, without
ever handling raw base64 bytes in the LLM scratchpad.

The agent sees the ``attachment_id`` in the ``document_context`` block of its
prompt. It calls this tool with the ID. The tool reads the real file bytes
from server storage, base64-encodes them, and invokes
``mcp.paperless.upload_document`` under the hood.

Why this wrapper exists
-----------------------
Before this module, the agent had direct access to ``mcp.paperless.upload_document``
in its tool list. Because the file bytes are never in the LLM context, the
agent hallucinated placeholder strings like
``"base64_encoded_content_of_the_invoice"`` and forwarded them to Paperless.
The resulting HTTP 400 trapped the agent in retry loops. See the deploy
chain around the Paperless-MIME and base64-validation PRs for the defensive
layers; this tool is the architectural fix that removes the hallucination
surface entirely.
"""
from __future__ import annotations

import base64
import json
import mimetypes
import re
import uuid
from pathlib import Path

from loguru import logger


# Cold-start window for the LLM-metadata confirm flow. Design doc § 5:
# first N uploads per user require explicit confirm; after that the
# system trusts itself and extraction runs silently. N=10 is
# conservative — covers "first two weeks of household use" at ~1
# upload/day without becoming permanent friction.
_COLD_START_CONFIRM_N = 10

# Skip-metadata heuristic. The agent can pass ``skip_metadata=True``
# explicitly when the user said "ohne Metadaten". The tool itself also
# defensively auto-skips for files that look like memes / screenshots
# / photos — the extraction pipeline adds latency that's pure loss on
# those.
_SCREENSHOT_FILENAME_RE = re.compile(
    r"^(?:screenshot|screen[\s_.-]?shot|img[_-]?\d+|photo|meme|selfie)",
    re.IGNORECASE,
)
_IMAGE_AUTOSKIP_SIZE_BYTES = 500 * 1024  # 500 KB

CHAT_UPLOAD_TOOLS: dict = {
    "internal.forward_attachment_to_paperless": {
        "description": (
            "Forward a file the user has attached to this chat to Paperless-NGX "
            "for OCR and archiving. Reads the file from server storage using the "
            "attachment_id shown in the UPLOADED DOCUMENT section of this prompt. "
            "Do NOT pass file_content_base64 — the tool does that internally from "
            "real file bytes. Preferred over mcp.paperless.upload_document for "
            "user-attached files. "
            "During the user's first 10 archives, this tool will return "
            "action_required=paperless_confirm with a preview for the user to "
            "approve — relay the message verbatim to the user, then on their "
            "next message call internal.paperless_commit_upload with the "
            "confirm_token and the user's response."
        ),
        "parameters": {
            "attachment_id": (
                "Integer ID of the attachment shown in the UPLOADED DOCUMENT "
                "section. Required."
            ),
            "title": (
                "Optional Paperless document title. Defaults to the attachment "
                "filename."
            ),
            "correspondent": "Optional Paperless correspondent name",
            "document_type": "Optional Paperless document type name",
            "tags": "Optional list of tag names",
            "skip_metadata": (
                "Optional boolean. Set True to skip LLM metadata extraction "
                "(e.g. for memes, screenshots, or when the user explicitly "
                "says 'ohne Metadaten'). Defaults to False — the tool also "
                "auto-skips based on file shape (small image files, "
                "screenshot-like filenames)."
            ),
        },
    },
    "internal.paperless_commit_upload": {
        "description": (
            "Second half of the cold-start confirm flow. Call this AFTER the "
            "user replies to a paperless_confirm preview produced by "
            "internal.forward_attachment_to_paperless. Reads the pending row "
            "by confirm_token, parses the user's response (ja / nein / inline "
            "edits like 'tags: foo, bar'), and finalises the upload to "
            "Paperless. Until this tool is wired into the agent registry, the "
            "cold-start confirm flow gets stuck after step 1 with "
            "'Unbekanntes Tool: internal.paperless_commit_upload'."
        ),
        "parameters": {
            "confirm_token": (
                "UUID returned in the data.confirm_token field of the "
                "preceding forward_attachment_to_paperless response. Required."
            ),
            "user_response_text": (
                "The user's verbatim reply to the confirm preview. Accepts "
                "'ja' / 'yes' to commit as-shown, 'nein' / 'no' to abort, or "
                "an inline edit like 'tags: rechnung, 2026' / "
                "'correspondent: Anthropic'. Required."
            ),
        },
    },
}


def _should_auto_skip_metadata(filename: str, file_size: int) -> bool:
    """Deterministic heuristic for skipping the extraction pipeline on
    files that clearly aren't archival documents.

    Triggers on:
      - small image files (likely photos / memes / screenshots)
      - filenames matching common screenshot / photo patterns
    Does NOT trigger on PDFs / docx / txt / md regardless of size —
    those are always real documents.
    """
    if _SCREENSHOT_FILENAME_RE.match(filename):
        return True
    mime, _ = mimetypes.guess_type(filename)
    if mime and mime.startswith("image/") and file_size < _IMAGE_AUTOSKIP_SIZE_BYTES:
        return True
    return False


async def forward_attachment_to_paperless(
    params: dict,
    mcp_manager=None,
    session_id: str | None = None,
    user_id: int | None = None,
) -> dict:
    """Forward a chat attachment to Paperless with optional LLM metadata
    extraction and cold-start confirm.

    Flow depends on the user's cold-start state and the ``skip_metadata``
    flag:

      1. ``skip_metadata=True`` (or auto-heuristic triggers) → bare
         upload, no extraction, no confirm. Identical behaviour to
         pre-PR-2b.
      2. User's ``paperless_confirms_used >= _COLD_START_CONFIRM_N`` →
         the system trusts itself: run extraction silently, upload
         with extracted metadata, no user confirm.
      3. User is inside the cold-start window → run extraction, persist
         ``paperless_pending_confirms`` row, return
         ``action_required=paperless_confirm`` with a German preview
         the agent relays to the user. The user's next message is
         their response; the agent should then call
         ``internal.paperless_commit_upload`` to finalise.

    Args:
        params: ``attachment_id`` (required), optional ``title``,
            ``correspondent`` / ``document_type`` / ``tags`` (passed
            through as-is if skipping), and ``skip_metadata`` bool.
        mcp_manager: MCPManager, injected by ActionExecutor.
        session_id: Chat session; scopes the DB lookup per #442.
        user_id: Authenticated user id; used to read/increment the
            cold-start counter. When ``None`` (single-user /
            auth-disabled), the cold-start check is bypassed and
            extraction always runs with confirm.
    """
    attachment_id_raw = params.get("attachment_id")
    if attachment_id_raw is None:
        return {
            "success": False,
            "message": "Parameter 'attachment_id' is required",
            "action_taken": False,
        }
    try:
        attachment_id = int(attachment_id_raw)
    except (TypeError, ValueError):
        return {
            "success": False,
            "message": f"'attachment_id' must be an integer, got: {attachment_id_raw!r}",
            "action_taken": False,
        }

    if mcp_manager is None:
        return {
            "success": False,
            "message": "MCP manager not available — Paperless MCP not wired in",
            "action_taken": False,
        }

    try:
        from sqlalchemy import select

        from models.database import ChatUpload, PaperlessPendingConfirm
        from services.database import AsyncSessionLocal

        async with AsyncSessionLocal() as db:
            query = select(ChatUpload).where(ChatUpload.id == attachment_id)
            if session_id is not None:
                query = query.where(ChatUpload.session_id == session_id)
            result = await db.execute(query)
            upload = result.scalar_one_or_none()

        if not upload:
            return {
                "success": False,
                "message": f"Attachment {attachment_id} not found",
                "action_taken": False,
            }

        if not upload.file_path or not Path(upload.file_path).is_file():
            return {
                "success": False,
                "message": (
                    f"Attachment {attachment_id} ({upload.filename}) is no "
                    "longer available on disk"
                ),
                "action_taken": False,
            }

        # Decide whether to skip extraction. Agent-passed flag wins;
        # auto-heuristic catches cases the agent didn't think to flag.
        skip_metadata = bool(params.get("skip_metadata"))
        if not skip_metadata:
            file_size = upload.file_size or 0
            if _should_auto_skip_metadata(upload.filename, file_size):
                skip_metadata = True
                logger.debug(
                    "Auto-skipping metadata extraction for %s (size=%d)",
                    upload.filename, file_size,
                )

        # Pass-through params that override extraction output (agent
        # already knows the answer for these — no need to ask the LLM).
        agent_overrides = {
            k: params[k] for k in ("title", "correspondent", "document_type", "tags")
            if params.get(k)
        }

        if skip_metadata:
            # Path 1: bare upload, no extraction. Identical to pre-PR-2b
            # behaviour. Used for memes / screenshots / user-explicit opt-out.
            return await _direct_upload(
                upload=upload,
                mcp_manager=mcp_manager,
                params=agent_overrides,
            )

        # Run extraction.
        extraction_result = await _run_extraction(
            attachment_id=attachment_id,
            session_id=session_id,
            user_id=user_id,
            mcp_manager=mcp_manager,
            user_lang=_infer_lang(upload),
        )
        if extraction_result is None or extraction_result.error:
            # Extraction failed — fall back to bare upload with a
            # user-visible note. Matches the design's fallback-on-everything
            # philosophy.
            err = extraction_result.error if extraction_result else "Extractor unavailable"
            logger.warning(
                f"Metadata extraction failed for attachment {attachment_id}: "
                f"{err} — bare upload"
            )
            direct = await _direct_upload(
                upload=upload, mcp_manager=mcp_manager, params=agent_overrides,
            )
            # Annotate the success message so the user knows.
            if direct.get("success"):
                direct["message"] = (
                    f"{direct['message']} "
                    f"(ohne Metadaten-Extraktion: {err})"
                )
            return direct

        # Merge agent overrides into the extracted metadata (agent
        # wins). `mode="json"` serialises pydantic dates to ISO
        # strings so the dict is safe to write into a JSON column
        # downstream (paperless_pending_confirms.post_fuzzy_output).
        # Without it, `created_date` comes back as a datetime.date
        # which SQLAlchemy's default JSON encoder can't handle and
        # the whole INSERT fails.
        post_fuzzy = extraction_result.metadata.model_dump(mode="json")
        post_fuzzy.update(agent_overrides)

        # Cold-start gate. User is inside the window → confirm
        # required; otherwise silent upload with extracted metadata.
        confirms_used = await _get_confirms_used(user_id)
        if confirms_used >= _COLD_START_CONFIRM_N:
            # Path 2: trusted silent upload with extracted metadata.
            return await _direct_upload(
                upload=upload,
                mcp_manager=mcp_manager,
                params=_extraction_to_upload_params(post_fuzzy),
                track_for_sweep=True,
                user_id=user_id,
                doc_text=extraction_result.doc_text,
            )

        # Path 3: cold-start confirm. Persist a pending row and return
        # the preview — agent relays to user, who answers in the next
        # turn and the agent calls paperless_commit_upload.
        confirm_token = str(uuid.uuid4())
        # Same `mode="json"` reason as post_fuzzy above — these dicts
        # flow into the paperless_pending_confirms.llm_output /
        # .proposals JSON columns; any datetime.date inside would kill
        # the INSERT.
        llm_output_for_persist = extraction_result.metadata.model_dump(mode="json")
        # Stash doc_text alongside llm_output so the commit tool can
        # write it into paperless_extraction_examples without another
        # extraction run.
        llm_output_for_persist["_doc_text"] = extraction_result.doc_text

        async with AsyncSessionLocal() as db:
            pending = PaperlessPendingConfirm(
                confirm_token=confirm_token,
                attachment_id=attachment_id,
                session_id=session_id or "unknown",
                # user_id is nullable — None lands when AUTH_ENABLED=false.
                # The counter-increment on successful commit guards against
                # this (paperless_commit_tool skips when user_id is None).
                user_id=user_id,
                llm_output=llm_output_for_persist,
                post_fuzzy_output=post_fuzzy,
                proposals=[
                    p.model_dump(mode="json")
                    for p in extraction_result.metadata.new_entry_proposals
                ],
            )
            db.add(pending)
            await db.commit()

        preview_text = _render_confirm_message(
            filename=upload.filename,
            metadata=post_fuzzy,
            proposals=extraction_result.metadata.new_entry_proposals,
        )

        return {
            "success": True,
            "message": preview_text,
            "action_taken": False,  # upload not yet committed
            "data": {
                "action_required": "paperless_confirm",
                "confirm_token": confirm_token,
                "attachment_id": attachment_id,
                "filename": upload.filename,
            },
        }
    except Exception as e:
        logger.error(f"forward_attachment_to_paperless error: {e}")
        return {
            "success": False,
            "message": f"Forward to Paperless failed: {e!s}",
            "action_taken": False,
        }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _direct_upload(
    upload,
    *,
    mcp_manager,
    params: dict,
    track_for_sweep: bool = False,
    user_id: int | None = None,
    doc_text: str | None = None,
) -> dict:
    """Bare Paperless upload via the MCP tool. Used by the skip-metadata
    path and by the post-cold-start silent path.

    When *track_for_sweep* is True (silent-past-cap path), and the upload
    succeeds with a document_id, persist a ``paperless_upload_tracking``
    row so the PR 4 UI-edit sweeper can later detect and learn from user
    edits in the Paperless UI. Skip-metadata + extraction-failed paths
    leave this off — there's no extraction to compare against, so no
    training signal to capture."""
    with open(upload.file_path, "rb") as f:
        file_bytes = f.read()
    file_content_base64 = base64.b64encode(file_bytes).decode("ascii")

    tool_params: dict = {
        "title": params.get("title") or upload.filename,
        "filename": upload.filename,
        "file_content_base64": file_content_base64,
    }
    for key in ("correspondent", "document_type", "tags",
                "storage_path", "created_date", "custom_fields"):
        val = params.get(key)
        if val:
            tool_params[key] = val

    mcp_result = await mcp_manager.execute_tool(
        "mcp.paperless.upload_document", tool_params
    )

    if not mcp_result or not mcp_result.get("success"):
        detail = (mcp_result or {}).get("message") or "unknown error"
        return {
            "success": False,
            "message": f"Paperless upload failed: {detail}",
            "action_taken": False,
        }

    # Parse the envelope to extract task_id etc.
    task_id: str | None = None
    document_id: int | None = None
    patch_state: str | None = None
    inner_msg = mcp_result.get("message")
    if isinstance(inner_msg, str):
        try:
            inner = json.loads(inner_msg)
            if isinstance(inner, dict):
                task_id = inner.get("task_id")
                document_id = inner.get("document_id")
                patch_state = inner.get("post_upload_patch")
        except (json.JSONDecodeError, TypeError):
            pass

    # PR 4: persist tracking row on the silent-past-cap path so the
    # UI-edit sweeper has a baseline to diff against. Skipped when
    # track_for_sweep=False (skip-metadata + extraction-failed paths —
    # no extracted metadata, nothing to compare against).
    if track_for_sweep and document_id is not None:
        from models.database import PaperlessUploadTracking
        from services.database import AsyncSessionLocal
        try:
            async with AsyncSessionLocal() as db:
                db.add(PaperlessUploadTracking(
                    chat_upload_id=upload.id,
                    paperless_document_id=int(document_id),
                    user_id=user_id,
                    original_metadata=dict(params),
                    doc_text=doc_text,
                ))
                await db.commit()
        except Exception as exc:
            # Tracking-row persistence must not block the user's upload
            # success path — it's purely a learning-loop concern.
            logger.warning(f"Upload-tracking persist failed: {exc}")

    # Poll Paperless's task endpoint to learn the REAL ingestion outcome.
    #
    # Without this, the MCP returned "task accepted" (HTTP 200 + task_id)
    # gets blindly reported as "Sent to Paperless" — even though the
    # consume task can fail asynchronously with e.g.
    # "Not consuming X.pdf: It is a duplicate of <existing> (#NNN)".
    # The user then sees "successfully uploaded" but the doc never lands.
    # That was the prod symptom on 2026-04-25.
    #
    # We only poll when (a) we got a task_id back AND (b) the MCP didn't
    # already produce a document_id (it does that itself when PATCH-style
    # metadata is set, by polling internally). Skip the poll without a
    # task_id (nothing to look up).
    consume_failure: str | None = None
    if task_id and document_id is None:
        consume_failure, polled_doc_id = await _poll_paperless_task(task_id)
        if polled_doc_id is not None:
            document_id = polled_doc_id

    if consume_failure:
        # The POST landed but the consume queue rejected it. Surface the
        # exact reason to the user so the agent's final answer doesn't
        # claim success when Paperless silently dropped the doc.
        return {
            "success": False,
            "message": (
                f"Paperless lehnte das Dokument ab: {consume_failure}"
            ),
            "action_taken": False,
            "data": {
                "attachment_id": upload.id,
                "filename": upload.filename,
                "task_id": task_id,
                "rejection_reason": consume_failure,
            },
        }

    return {
        "success": True,
        "message": f"Sent to Paperless: {upload.filename}",
        "action_taken": True,
        "data": {
            "attachment_id": upload.id,
            "filename": upload.filename,
            "task_id": task_id,
            "document_id": document_id,
            "post_upload_patch": patch_state,
        },
    }


async def _poll_paperless_task(
    task_id: str, *, timeout_s: float = 30.0, interval_s: float = 1.0,
) -> tuple[str | None, int | None]:
    """Poll Paperless's /api/tasks/?task_id= until terminal state.

    Returns (failure_reason, document_id). Failure reason is the
    `result` field from Paperless when status=FAILURE (e.g.
    "It is a duplicate of <other> (#1661)"). Document id is the
    `related_document` field from Paperless when status=SUCCESS.
    Either or both may be None on timeout / unexpected shape.

    Reads PAPERLESS_API_URL + PAPERLESS_API_TOKEN from env (the same
    way the MCP server resolves them, since chat_upload_tool runs
    in-process and shares the env).
    """
    import asyncio
    import os

    import httpx

    base = os.environ.get("PAPERLESS_API_URL")
    token = os.environ.get("PAPERLESS_API_TOKEN")
    if not (base and token):
        return None, None

    url = f"{base.rstrip('/')}/api/tasks/?task_id={task_id}"
    headers = {"Authorization": f"Token {token}"}
    deadline = asyncio.get_running_loop().time() + timeout_s

    async with httpx.AsyncClient(verify=False, timeout=10.0) as client:  # noqa: S501
        while asyncio.get_running_loop().time() < deadline:
            try:
                r = await client.get(url, headers=headers)
                r.raise_for_status()
                data = r.json()
                tasks = data if isinstance(data, list) else data.get("results", [])
                if tasks:
                    t = tasks[0]
                    status = (t.get("status") or "").upper()
                    if status == "SUCCESS":
                        rd = t.get("related_document")
                        return None, int(rd) if rd is not None else None
                    if status == "FAILURE":
                        return (t.get("result") or "Paperless rejected the upload"), None
                    # Still PENDING / STARTED → keep polling.
            except (httpx.HTTPError, ValueError) as exc:
                logger.warning(
                    f"Paperless task poll {task_id} transient error: {exc}"
                )
            await asyncio.sleep(interval_s)
    return None, None


async def _run_extraction(
    *,
    attachment_id: int,
    session_id: str | None,
    user_id: int | None,
    mcp_manager,
    user_lang: str,
):
    """Run the PaperlessMetadataExtractor; return its ExtractionResult
    or None if the extractor module can't be loaded."""
    try:
        from services.paperless_metadata_extractor import PaperlessMetadataExtractor
    except Exception as exc:  # pragma: no cover
        logger.warning(f"Extractor module import failed: {exc}")
        return None
    extractor = PaperlessMetadataExtractor(mcp_manager=mcp_manager)
    try:
        return await extractor.extract(
            attachment_id=attachment_id,
            session_id=session_id,
            user_id=user_id,
            lang=user_lang,
        )
    except Exception as exc:
        logger.warning(f"Extractor call failed: {exc}")
        return None


async def _get_confirms_used(user_id: int | None) -> int:
    """Read ``users.paperless_confirms_used`` for the cold-start check.

    Returns 0 when ``user_id`` is None (auth-disabled dev) so extraction
    always runs with confirm in that mode — the operator can decide to
    flip the flag via the admin UI once they're comfortable.
    """
    if user_id is None:
        return 0
    from sqlalchemy import select

    from models.database import User
    from services.database import AsyncSessionLocal

    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(User.paperless_confirms_used).where(User.id == user_id)
        )
        value = result.scalar()
    return int(value or 0)


def _extraction_to_upload_params(post_fuzzy: dict) -> dict:
    """Translate extractor output to the params shape
    ``mcp.paperless.upload_document`` expects."""
    out: dict = {}
    for key in ("title", "correspondent", "document_type", "storage_path",
                "created_date"):
        val = post_fuzzy.get(key)
        if val:
            out[key] = val
    tags = post_fuzzy.get("tags") or []
    if tags:
        out["tags"] = list(tags)
    return out


def _infer_lang(upload) -> str:
    """Pick a prompt language. v1 defaults to German because the
    household audience is DE-primary; English doc filenames get EN.
    Crude heuristic — doesn't matter much, the LLM handles either."""
    filename_lower = (upload.filename or "").lower()
    en_hints = ("invoice", "receipt", "statement", "contract", "letter")
    if any(h in filename_lower for h in en_hints):
        return "en"
    return "de"


def _render_confirm_message(
    *,
    filename: str,
    metadata: dict,
    proposals: list,
) -> str:
    """Build the German confirm preview the agent relays to the user.

    Matches the shape documented in the design doc § 5 confirm UX.
    Missing fields render as "—" so the user sees what the LLM couldn't
    decide and can either approve the gaps or abort.
    """
    def _display(value):
        if value is None or value == "":
            return "—"
        if isinstance(value, list):
            return ", ".join(str(v) for v in value) if value else "—"
        return str(value)

    proposals_line = "keine"
    if proposals:
        parts = []
        for p in proposals:
            field = p.field if hasattr(p, "field") else p.get("field")
            value = p.value if hasattr(p, "value") else p.get("value")
            parts.append(f"{field}: {value}")
        proposals_line = "; ".join(parts)

    return (
        f"Ich möchte das Dokument so ablegen:\n"
        f"\n"
        f"  Datei:             {filename}\n"
        f"  Titel:             {_display(metadata.get('title'))}\n"
        f"  Korrespondent:     {_display(metadata.get('correspondent'))}\n"
        f"  Dokumenttyp:       {_display(metadata.get('document_type'))}\n"
        f"  Tags:              {_display(metadata.get('tags'))}\n"
        f"  Speicherpfad:      {_display(metadata.get('storage_path'))}\n"
        f"  Ausstellungsdatum: {_display(metadata.get('created_date'))}\n"
        f"\n"
        f"  Neu anzulegen:     {proposals_line}\n"
        f"\n"
        f"Passt das so? Antworte mit `ja` zum Ablegen oder `nein` zum Abbrechen."
    )
