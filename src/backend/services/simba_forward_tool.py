"""
Simba-Forward Tools — Platform-owned agent tools (xidra-only in practice).

Two-step, human-gated bridge for sending a chat-attached file to the **Simba tax
portal** (`simba` MCP). A real upload to the tax accountant is irreversible, so
it CANNOT happen in a single agent turn:

  1. ``internal.forward_attachment_to_simba`` — resolves the ChatUpload, runs a
     DRY-RUN (validates, sends nothing), persists a short-lived pending-confirm
     record in Redis, and returns a preview + ``confirm_token``. It NEVER
     uploads. The agent must relay the preview and STOP.
  2. ``internal.simba_commit_upload`` — called ONLY after the user replies in a
     NEW message. Reads the pending record, parses ja/nein, and on confirmation
     performs the real upload (``mcp.simba.upload_documents`` with
     ``content_base64``, ``dry_run=false``, ``confirm=true``). The agent can't
     fabricate the user's reply, so a real upload requires a genuine human turn.

Bytes are read from server storage and handed over as base64 — the LLM never
sees or fabricates them (same fix as ``forward_attachment_to_paperless``).
``mcp.simba.upload_documents`` is dropped from the agent's ``prompt_tools``.
"""
from __future__ import annotations

import base64
import json
import uuid
from pathlib import Path

from loguru import logger

_PENDING_PREFIX = "simba:pending:"
_PENDING_TTL_SECONDS = 900  # 15 minutes to answer the confirm

_AFFIRMATIVE = {
    "ja", "jau", "jo", "yes", "y", "passt", "ok", "okay", "okey", "genau",
    "mach", "machen", "klar", "senden", "hochladen", "übertragen", "uebertragen",
    "bestätige", "bestaetige", "bestätigt", "bestaetigt", "confirm",
}
_NEGATIVE = {
    "nein", "ne", "nö", "no", "n", "abbrechen", "abbruch", "stop", "stopp",
    "nicht", "cancel", "verwerfen", "doch nicht",
}


SIMBA_FORWARD_TOOL: dict = {
    "internal.forward_attachment_to_simba": {
        "description": (
            "SCHRITT 1 (lädt NICHTS hoch): Bereite die Übertragung einer im Chat "
            "angehängten Datei ans Simba-Steuerportal vor. Macht einen Probelauf, "
            "validiert die Datei und gibt eine VORSCHAU + ein confirm_token zurück. "
            "ZEIGE dem Nutzer die Vorschau (Kategorie/Typ, Zeitraum, Dateiname) und "
            "WARTE auf seine Antwort — rufe hier NICHTS weiter auf. Erst wenn der "
            "Nutzer in einer NEUEN Nachricht bestätigt, rufe "
            "internal.simba_commit_upload mit dem confirm_token und der wörtlichen "
            "Antwort des Nutzers auf. "
            "Liest die Bytes serverseitig anhand der attachment_id — gib NIEMALS "
            "base64/Bytes an. Fehlt die attachment_id, ruf OHNE sie auf (letzter "
            "Upload der Sitzung). PFLICHT: category und type (bei Unsicherheit "
            "zuerst mcp.simba.list_categories)."
        ),
        "parameters": {
            "category": "Simba-Kategorie, z. B. 'Belege' oder 'Posteingang' (Pflicht).",
            "type": "Simba-Dokumenttyp, z. B. 'Ausgangsrechnung' (Pflicht).",
            "attachment_id": (
                "Integer-ID des Anhangs. OPTIONAL — weglassen für den letzten "
                "Upload der Sitzung. Niemals raten/erfinden."
            ),
            "month": "Buchungsmonat 1–12 (optional; Standard: aktueller Monat).",
            "year": "Buchungsjahr (optional; Standard: aktuelles Jahr).",
            "description": "Optionale Bezeichnung (max. 100 Zeichen).",
            "comment": "Optionaler Kommentar (max. 100 Zeichen).",
        },
    },
    "internal.simba_commit_upload": {
        "description": (
            "SCHRITT 2 (ECHTER, UNWIDERRUFLICHER Upload): Führt die Übertragung ans "
            "Simba-Portal aus, NACHDEM der Nutzer die Vorschau aus "
            "internal.forward_attachment_to_simba bestätigt hat. Rufe dies NUR auf, "
            "nachdem der Nutzer in einer NEUEN Nachricht geantwortet hat — erfinde "
            "die Antwort NICHT. 'ja'/'passt' → Upload; 'nein' → Abbruch."
        ),
        "parameters": {
            "confirm_token": (
                "Das confirm_token aus der forward_attachment_to_simba-Vorschau (Pflicht)."
            ),
            "user_response_text": (
                "Die wörtliche Antwort des Nutzers auf die Vorschau (Pflicht): "
                "'ja'/'passt' zum Übertragen, 'nein' zum Abbrechen."
            ),
        },
    },
}


async def _resolve_upload(attachment_id_raw, session_id: str | None):
    """Resolve the ChatUpload by id or session fallback. Returns (upload, error)."""
    attachment_id: int | None = None
    if attachment_id_raw is not None:
        try:
            attachment_id = int(attachment_id_raw)
        except (TypeError, ValueError):
            attachment_id = None

    if attachment_id is None and session_id is None:
        return None, "Kein hochgeladenes Dokument gefunden. Bitte hänge die Datei an."

    from sqlalchemy import select

    from models.database import ChatUpload
    from services.database import AsyncSessionLocal

    async with AsyncSessionLocal() as db:
        upload = None
        if attachment_id is not None:
            q = select(ChatUpload).where(ChatUpload.id == attachment_id)
            if session_id is not None:
                q = q.where(ChatUpload.session_id == session_id)
            upload = (await db.execute(q)).scalar_one_or_none()
        if upload is None and session_id is not None:
            fb = (
                select(ChatUpload)
                .where(ChatUpload.session_id == session_id, ChatUpload.status == "completed")
                .order_by(ChatUpload.id.desc())
                .limit(1)
            )
            upload = (await db.execute(fb)).scalar_one_or_none()

    if not upload:
        return None, (
            "In diesem Chat ist kein hochgeladenes Dokument vorhanden. Bitte hänge "
            "die Datei an und warte, bis der Upload abgeschlossen ist."
        )
    if not upload.file_path or not Path(upload.file_path).is_file():
        return None, f"Anhang {upload.id} ({upload.filename}) ist nicht mehr verfügbar."
    return upload, None


def _read_base64(path: str) -> str:
    with open(path, "rb") as f:
        return base64.b64encode(f.read()).decode("ascii")


def _period(params: dict) -> dict:
    out = {}
    for key in ("month", "year"):
        if params.get(key) is not None:
            try:
                out[key] = int(params[key])
            except (TypeError, ValueError):
                pass
    return out


def _interpret_upload_result(mcp_result: dict) -> dict:
    """Honest outcome of a REAL mcp.simba.upload_documents call — success ONLY if
    at least one file actually transferred, else the concrete failure reason."""
    if not mcp_result or not mcp_result.get("success"):
        detail = (mcp_result or {}).get("message") or "unbekannter Fehler"
        return {"success": False, "message": f"Simba-Upload fehlgeschlagen: {detail}", "action_taken": False}

    raw = mcp_result.get("message")
    inner: dict = {}
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                inner = parsed
        except (ValueError, TypeError):
            inner = {}

    def _int(v):
        try:
            return int(v)
        except (TypeError, ValueError):
            return None

    uebertragen = _int(inner.get("uebertragen"))
    fehlgeschlagen = _int(inner.get("fehlgeschlagen"))
    if uebertragen is not None and uebertragen > 0 and not (fehlgeschlagen or 0):
        return {"success": True, "message": raw or "Übertragen.", "action_taken": True}

    res0 = (inner.get("ergebnisse") or [{}])
    res0 = res0[0] if res0 else {}
    errors = inner.get("fehler") or []
    reason = ""
    if errors:
        reason = errors[0].get("error") if isinstance(errors[0], dict) else str(errors[0])
    elif res0:
        status = res0.get("status")
        resp = (res0.get("response") or "")[:300]
        reason = f"HTTP {status}: {resp}" if status else resp
    return {
        "success": False,
        "message": (
            "Der Upload an Simba ist NICHT angekommen (0 Dateien übertragen)"
            + (f": {reason}" if reason else ".")
            + " Bitte dem Nutzer melden — behaupte KEINEN Erfolg."
        ),
        "action_taken": False,
        "data": {"raw": raw},
    }


async def forward_attachment_to_simba(
    params: dict,
    mcp_manager=None,
    session_id: str | None = None,
    user_id: int | None = None,
) -> dict:
    """STEP 1: dry-run + persist a pending-confirm record. Never uploads."""
    if mcp_manager is None:
        return {"success": False, "message": "MCP-Manager nicht verfügbar.", "action_taken": False}

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

    try:
        upload, err = await _resolve_upload(params.get("attachment_id"), session_id)
        if err:
            return {"success": False, "message": err, "action_taken": False}

        content_base64 = _read_base64(upload.file_path)
        period = _period(params)

        # Dry-run: validate + confirm the portal accepts it. Sends nothing.
        dry_args = {
            "category": category,
            "type": type_,
            "dry_run": True,
            "files": [
                {
                    "content_base64": content_base64,
                    "filename": upload.filename,
                    **({"description": params["description"]} if params.get("description") else {}),
                    **({"comment": params["comment"]} if params.get("comment") else {}),
                }
            ],
            **period,
        }
        dry = await mcp_manager.execute_tool("mcp.simba.upload_documents", dry_args)
        if not dry or not dry.get("success"):
            detail = (dry or {}).get("message") or "unbekannter Fehler"
            return {"success": False, "message": f"Probelauf fehlgeschlagen: {detail}", "action_taken": False}

        # Detect a dry-run validation failure (e.g. file too big / wrong type).
        inner: dict = {}
        if isinstance(dry.get("message"), str):
            try:
                inner = json.loads(dry["message"])
            except (ValueError, TypeError):
                inner = {}
        if inner.get("fehler"):
            e0 = inner["fehler"][0]
            reason = e0.get("error") if isinstance(e0, dict) else str(e0)
            return {
                "success": False,
                "message": f"Die Datei kann nicht übertragen werden: {reason}",
                "action_taken": False,
            }

        # Persist the pending record (metadata only — bytes are re-read at commit).
        confirm_token = str(uuid.uuid4())
        record = {
            "attachment_id": upload.id,
            "filename": upload.filename,
            "category": category,
            "type": type_,
            "description": params.get("description"),
            "comment": params.get("comment"),
            "session_id": session_id,
            "user_id": user_id,
            **period,
        }
        from services.redis_client import get_redis

        await get_redis().setex(
            f"{_PENDING_PREFIX}{confirm_token}", _PENDING_TTL_SECONDS, json.dumps(record)
        )

        now_period = period.get("month") and period.get("year")
        zeitraum = f"{period['month']:02d}/{period['year']}" if now_period else "aktueller Monat"
        preview = (
            "Ich würde folgendes an die Steuerkanzlei (Simba) übertragen:\n"
            f"  Datei:     {upload.filename}\n"
            f"  Ablage:    {category} / {type_}\n"
            f"  Zeitraum:  {zeitraum}\n"
            + (f"  Bezeichn.: {params['description']}\n" if params.get("description") else "")
            + (f"  Kommentar: {params['comment']}\n" if params.get("comment") else "")
            + "\nSoll ich das ENDGÜLTIG übertragen? (nicht widerrufbar) — "
            "antworte mit ja oder nein."
        )
        return {
            "success": True,
            "message": preview,
            "action_taken": False,  # nothing uploaded yet — STOP and wait for the user
            "data": {
                "action_required": "simba_confirm",
                "confirm_token": confirm_token,
                "attachment_id": upload.id,
                "filename": upload.filename,
                "ziel": f"{category} / {type_}",
            },
        }
    except Exception as e:
        logger.error(f"forward_attachment_to_simba error: {e}")
        return {"success": False, "message": f"Vorbereitung fehlgeschlagen: {e!s}", "action_taken": False}


async def simba_commit_upload(
    params: dict,
    mcp_manager=None,
    session_id: str | None = None,
    user_id: int | None = None,
) -> dict:
    """STEP 2: the real upload, only after the user confirmed the preview."""
    if mcp_manager is None:
        return {"success": False, "message": "MCP-Manager nicht verfügbar.", "action_taken": False}

    token = (params.get("confirm_token") or "").strip()
    response_text = (params.get("user_response_text") or "").strip()
    if not token:
        return {"success": False, "message": "confirm_token fehlt.", "action_taken": False}

    from services.redis_client import get_redis

    redis = get_redis()
    key = f"{_PENDING_PREFIX}{token}"
    raw = await redis.get(key)
    if not raw:
        return {
            "success": False,
            "message": (
                "Die Vorschau ist abgelaufen oder unbekannt. Bitte die Datei erneut "
                "mit internal.forward_attachment_to_simba vorbereiten."
            ),
            "action_taken": False,
        }
    try:
        record = json.loads(raw)
    except (ValueError, TypeError):
        await redis.delete(key)
        return {"success": False, "message": "Vorschau-Datensatz beschädigt.", "action_taken": False}

    # Scope the token to the session that created it.
    if record.get("session_id") and session_id and record["session_id"] != session_id:
        return {"success": False, "message": "confirm_token gehört zu einer anderen Sitzung.", "action_taken": False}

    # Interpret the user's reply. Anything not clearly yes/no → ask again (no upload).
    words = {w.strip(".,!?").lower() for w in response_text.split()}
    said_yes = bool(words & _AFFIRMATIVE)
    said_no = bool(words & _NEGATIVE)
    if said_no and not said_yes:
        await redis.delete(key)
        return {
            "success": True,
            "message": "Abgebrochen — es wurde NICHTS an Simba übertragen.",
            "action_taken": False,
        }
    if not said_yes:
        return {
            "success": True,
            "message": (
                "Ich habe keine klare Bestätigung erkannt. Bitte den Nutzer um ein "
                "eindeutiges 'ja' (übertragen) oder 'nein' (abbrechen) bitten. "
                "Es wurde nichts übertragen."
            ),
            "action_taken": False,
        }

    # Confirmed → real upload.
    try:
        upload, err = await _resolve_upload(record.get("attachment_id"), record.get("session_id") or session_id)
        if err:
            await redis.delete(key)
            return {"success": False, "message": err, "action_taken": False}

        content_base64 = _read_base64(upload.file_path)
        tool_args = {
            "category": record["category"],
            "type": record["type"],
            "dry_run": False,
            "confirm": True,
            "files": [
                {
                    "content_base64": content_base64,
                    "filename": upload.filename,
                    **({"description": record["description"]} if record.get("description") else {}),
                    **({"comment": record["comment"]} if record.get("comment") else {}),
                }
            ],
        }
        for key_ in ("month", "year"):
            if record.get(key_) is not None:
                tool_args[key_] = record[key_]

        mcp_result = await mcp_manager.execute_tool("mcp.simba.upload_documents", tool_args)
        await redis.delete(key)  # single-use token

        outcome = _interpret_upload_result(mcp_result)
        outcome.setdefault("data", {})
        outcome["data"].update({
            "attachment_id": upload.id,
            "filename": upload.filename,
            "ziel": f"{record['category']} / {record['type']}",
        })
        return outcome
    except Exception as e:
        logger.error(f"simba_commit_upload error: {e}")
        return {"success": False, "message": f"Upload fehlgeschlagen: {e!s}", "action_taken": False}
