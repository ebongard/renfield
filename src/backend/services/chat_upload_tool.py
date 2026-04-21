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
from pathlib import Path

from loguru import logger


CHAT_UPLOAD_TOOLS: dict = {
    "internal.forward_attachment_to_paperless": {
        "description": (
            "Forward a file the user has attached to this chat to Paperless-NGX "
            "for OCR and archiving. Reads the file from server storage using the "
            "attachment_id shown in the UPLOADED DOCUMENT section of this prompt. "
            "Do NOT pass file_content_base64 — the tool does that internally from "
            "real file bytes. Preferred over mcp.paperless.upload_document for "
            "user-attached files."
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
        },
    },
}


async def forward_attachment_to_paperless(
    params: dict,
    mcp_manager=None,
    session_id: str | None = None,
) -> dict:
    """Read a ChatUpload from server storage and forward it to Paperless.

    Args:
        params: Tool parameters from the agent (attachment_id required; title,
            correspondent, document_type, tags optional).
        mcp_manager: MCPManager instance, injected by ActionExecutor. Needed
            to invoke ``mcp.paperless.upload_document`` with real base64.
        session_id: Chat session the request came from. When provided, the
            DB lookup is scoped to attachments uploaded within the same
            session — prevents a crafted prompt from referencing an
            attachment_id belonging to another user's conversation. When
            ``None`` (single-user dev setup, auth disabled), the check is
            skipped.

    Returns a standard agent-tool result dict: ``success``, ``message``,
    ``action_taken``, and ``data`` with ``task_id``, ``attachment_id``, and
    ``filename`` on success.
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

        from models.database import ChatUpload
        from services.database import AsyncSessionLocal

        async with AsyncSessionLocal() as db:
            query = select(ChatUpload).where(ChatUpload.id == attachment_id)
            # Session scoping: when the caller provides a session_id, the
            # attachment must belong to that session. A mismatch is reported
            # as "not found" rather than "forbidden" — the agent doesn't
            # need to distinguish, and the softer message avoids leaking
            # the existence of attachments belonging to other sessions.
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

        # Synchronous read is fine here — typical chat attachments are small
        # enough (< 10 MB) that blocking for a few ms is simpler than the
        # aiofiles dep tree, and this tool is already inside an async context
        # that can tolerate the brief pause.
        with open(upload.file_path, "rb") as f:
            file_bytes = f.read()
        file_content_base64 = base64.b64encode(file_bytes).decode("ascii")

        tool_params: dict = {
            "title": params.get("title") or upload.filename,
            "filename": upload.filename,
            "file_content_base64": file_content_base64,
        }
        if params.get("correspondent"):
            tool_params["correspondent"] = params["correspondent"]
        if params.get("document_type"):
            tool_params["document_type"] = params["document_type"]
        if params.get("tags"):
            tool_params["tags"] = params["tags"]

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

        # Paperless MCP returns {"task_id": ..., "title": ..., "filename": ...}
        # wrapped inside MCPManager.execute_tool's ``message`` field as JSON.
        task_id: str | None = None
        inner_msg = mcp_result.get("message")
        if isinstance(inner_msg, str):
            try:
                inner = json.loads(inner_msg)
                if isinstance(inner, dict):
                    task_id = inner.get("task_id")
            except (json.JSONDecodeError, TypeError):
                pass

        return {
            "success": True,
            "message": f"Sent to Paperless: {upload.filename}",
            "action_taken": True,
            "data": {
                "attachment_id": attachment_id,
                "filename": upload.filename,
                "task_id": task_id,
            },
        }
    except Exception as e:
        logger.error(f"forward_attachment_to_paperless error: {e}")
        return {
            "success": False,
            "message": f"Forward to Paperless failed: {e!s}",
            "action_taken": False,
        }
