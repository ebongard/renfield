"""
Simba-Forward Tool — Platform-owned agent tool (xidra-only in practice).

Forwards a file the user attached in the chat to the **Simba tax portal**
document transfer (the `simba` MCP server, xidra). Reads the real file bytes
from server storage using the `attachment_id` and hands them to
`mcp.simba.upload_documents` as `content_base64` — so the LLM never sees or
fabricates base64 (the same architectural fix as
`internal.forward_attachment_to_paperless`).

Why this wrapper exists
-----------------------
`mcp.simba.upload_documents` takes a file as a local `path` / `url` /
`content_base64`. A CHAT-attached file is server-side bytes with none of those
the agent can reference, so the agent flailed (array-of-strings → hallucinated
`content_base64: "PLACEHOLDER"` → `attachment_id`) in a retry loop. This tool is
the bridge: attachment → real bytes → simba. `mcp.simba.upload_documents` is
therefore removed from the agent's `prompt_tools` (like `upload_document` for
Paperless); the agent uses THIS tool for chat attachments.

Safety: a real (irreversible) upload requires BOTH `dry_run=false` AND
`confirm=true`. The default is a dry-run; the tool relays the simba server's
`bestaetigung_erforderlich` guidance so the agent shows a preview and only
uploads for real after the user's explicit confirmation.
"""
from __future__ import annotations

import base64
from pathlib import Path

from loguru import logger

SIMBA_FORWARD_TOOL: dict = {
    "internal.forward_attachment_to_simba": {
        "description": (
            "Übertrage eine im Chat angehängte Datei an das Simba-Steuerportal "
            "(Dokumententransfer zur Steuerkanzlei). Liest die echten Datei-Bytes "
            "serverseitig anhand der attachment_id aus der UPLOADED DOCUMENT / "
            "Dokument-Sektion dieses Prompts — gib NIEMALS Datei-Bytes/base64 "
            "selbst an. Fehlt eine attachment_id (z. B. Folgenachricht), rufe OHNE "
            "attachment_id auf; der Server nimmt den letzten abgeschlossenen Upload "
            "dieser Sitzung. "
            "PFLICHT: category und type (gültige Kombination — bei Unsicherheit "
            "zuerst mcp.simba.list_categories). "
            "ABLAUF (unwiderroflicher Upload!): Rufe zuerst OHNE confirm auf "
            "(Probelauf) — der Server meldet, WAS übertragen würde. Zeige das dem "
            "Nutzer, und erst nach dessen ausdrücklicher Bestätigung rufe erneut "
            "mit dry_run=false UND confirm=true auf. Hochgeladene Dokumente lassen "
            "sich NICHT zurückziehen."
        ),
        "parameters": {
            "category": "Simba-Kategorie, z. B. 'Belege' oder 'Posteingang' (Pflicht).",
            "type": "Simba-Dokumenttyp, z. B. 'Ausgangsrechnung' (Pflicht).",
            "attachment_id": (
                "Integer-ID des Anhangs aus der Dokument-Sektion. OPTIONAL — weglassen, "
                "wenn keine ID gezeigt wird; der Server nimmt den letzten Upload dieser "
                "Sitzung. Niemals raten/erfinden."
            ),
            "month": "Buchungsmonat 1–12 (optional; Standard: aktueller Monat).",
            "year": "Buchungsjahr (optional; Standard: aktuelles Jahr).",
            "description": "Optionale Bezeichnung (max. 100 Zeichen).",
            "comment": "Optionaler Kommentar (max. 100 Zeichen).",
            "dry_run": (
                "Optional. Standard true (Probelauf, sendet nichts). Für den echten "
                "Upload false setzen — zusammen mit confirm=true."
            ),
            "confirm": (
                "Muss für einen ECHTEN Upload (dry_run=false) ausdrücklich true sein. "
                "Nur nach ausdrücklicher Nutzer-Bestätigung setzen."
            ),
        },
    },
}


async def forward_attachment_to_simba(
    params: dict,
    mcp_manager=None,
    session_id: str | None = None,
    user_id: int | None = None,
) -> dict:
    """Resolve the chat attachment, read its bytes, and forward to the simba MCP.

    `session_id`/`user_id` are injected by ActionExecutor. `category`/`type` are
    required (where in Simba the file goes). `dry_run` defaults to True and a real
    upload additionally needs `confirm=True` — both are passed through to the
    simba server, which enforces the confirm gate.
    """
    if mcp_manager is None:
        return {
            "success": False,
            "message": "MCP-Manager nicht verfügbar — Simba-MCP nicht eingebunden.",
            "action_taken": False,
        }

    category = (params.get("category") or "").strip()
    type_ = (params.get("type") or "").strip()
    if not category or not type_:
        return {
            "success": False,
            "message": (
                "category und type sind erforderlich (wohin im Simba-Portal). "
                "Nutze mcp.simba.list_categories für gültige Kombinationen."
            ),
            "action_taken": False,
        }

    # attachment_id is a HINT — parse leniently; an unusable id falls through to
    # the session fallback (most recent completed upload in THIS session).
    attachment_id_raw = params.get("attachment_id")
    attachment_id: int | None = None
    if attachment_id_raw is not None:
        try:
            attachment_id = int(attachment_id_raw)
        except (TypeError, ValueError):
            attachment_id = None

    if attachment_id is None and session_id is None:
        return {
            "success": False,
            "message": (
                "Kein hochgeladenes Dokument gefunden. Bitte hänge die Datei an und "
                "warte, bis der Upload abgeschlossen ist."
            ),
            "action_taken": False,
        }

    try:
        from sqlalchemy import select

        from models.database import ChatUpload
        from services.database import AsyncSessionLocal

        async with AsyncSessionLocal() as db:
            upload = None
            if attachment_id is not None:
                query = select(ChatUpload).where(ChatUpload.id == attachment_id)
                if session_id is not None:
                    query = query.where(ChatUpload.session_id == session_id)
                upload = (await db.execute(query)).scalar_one_or_none()

            # Session fallback — scoped to session_id, so it can never reach
            # another user's upload.
            if upload is None and session_id is not None:
                fb = (
                    select(ChatUpload)
                    .where(
                        ChatUpload.session_id == session_id,
                        ChatUpload.status == "completed",
                    )
                    .order_by(ChatUpload.id.desc())
                    .limit(1)
                )
                upload = (await db.execute(fb)).scalar_one_or_none()
                if upload is not None:
                    logger.info(
                        "forward_attachment_to_simba: no usable attachment_id (%r); "
                        "session fallback → upload %s (%s)",
                        attachment_id_raw, upload.id, upload.filename,
                    )

        if not upload:
            return {
                "success": False,
                "message": (
                    "In diesem Chat ist kein hochgeladenes Dokument vorhanden. Bitte "
                    "hänge die Datei an und warte, bis der Upload abgeschlossen ist."
                ),
                "action_taken": False,
            }

        if not upload.file_path or not Path(upload.file_path).is_file():
            return {
                "success": False,
                "message": (
                    f"Anhang {upload.id} ({upload.filename}) ist nicht mehr auf der "
                    "Platte verfügbar."
                ),
                "action_taken": False,
            }

        with open(upload.file_path, "rb") as f:
            content_base64 = base64.b64encode(f.read()).decode("ascii")

        dry_run = params.get("dry_run")
        dry_run = True if dry_run is None else bool(dry_run)
        confirm = bool(params.get("confirm"))

        tool_args: dict = {
            "category": category,
            "type": type_,
            "dry_run": dry_run,
            "confirm": confirm,
            "files": [
                {
                    "content_base64": content_base64,
                    # filename WITH extension → the simba server derives the type.
                    "filename": upload.filename,
                    **({"description": params["description"]} if params.get("description") else {}),
                    **({"comment": params["comment"]} if params.get("comment") else {}),
                }
            ],
        }
        for key in ("month", "year"):
            if params.get(key) is not None:
                try:
                    tool_args[key] = int(params[key])
                except (TypeError, ValueError):
                    pass

        mcp_result = await mcp_manager.execute_tool("mcp.simba.upload_documents", tool_args)

        if not mcp_result or not mcp_result.get("success"):
            detail = (mcp_result or {}).get("message") or "unbekannter Fehler"
            return {
                "success": False,
                "message": f"Simba-Upload fehlgeschlagen: {detail}",
                "action_taken": False,
            }

        # Relay the simba server's own message (dry-run preview /
        # bestaetigung_erforderlich / real-upload result) verbatim to the agent.
        real_upload_done = (not dry_run) and confirm
        return {
            "success": True,
            "message": mcp_result.get("message") or "OK",
            "action_taken": real_upload_done,
            "data": {
                "attachment_id": upload.id,
                "filename": upload.filename,
                "ziel": f"{category} / {type_}",
                "dry_run": dry_run,
            },
        }
    except Exception as e:
        logger.error(f"forward_attachment_to_simba error: {e}")
        return {
            "success": False,
            "message": f"Weiterleitung an Simba fehlgeschlagen: {e!s}",
            "action_taken": False,
        }
