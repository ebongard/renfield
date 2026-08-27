"""`internal.paperless_dedupe` — agent-callable Paperless duplicate finder + remover.

**Thin caller** (#1137, Review D9): the actual duplicate-detection + deletion
logic lives in the Paperless MCP server (`mcp.paperless.dedupe_documents`,
renfield-mcp-paperless >= 1.12.0) — so it ships with the Paperless integration
and an instance without Paperless carries none of it, and there is ONE
implementation of the destructive delete op instead of a backend fork. This
module only: enforces the ADMIN gate, calls the MCP tool, and maps its result
to the German user-facing message the chat/UI expect. The same MCP tool backs
the autonomous `paperless_dedupe` scheduled task (services/scheduled_tasks).

Duplicate identity (implemented in the MCP): a document is the SAME as another
when its file CHECKSUM is identical, OR all its metadata is identical
(correspondent, document_type, date, title, page_count), OR its OCR is
byte-identical. The lowest-id copy is kept; extras go to Paperless trash
(recoverable). The MCP sweeps the FULL archive index-independently and deletes a
bounded batch per call (rate-limit-safe), reporting how many remain so the
caller re-runs until 0.

Destructive maintenance — **fail-closed**: with auth ON it requires an
authenticated ``Permission.ADMIN``; an unidentified turn (``user_permissions=None``)
is DENIED (a bulk archive delete has a larger blast radius than the reversible
tools). Auth OFF (single-user household) skips the gate.
"""
from __future__ import annotations

from typing import Any

from loguru import logger

from models.permissions import Permission, has_permission
from services.folder_ingest_paperless import _parse_paperless_result
from utils.config import settings

PAPERLESS_DEDUPE_TOOL: dict = {
    "internal.paperless_dedupe": {
        "description": (
            "Paperless-DUBLETTEN finden und aufräumen: durchsucht das GESAMTE "
            "Paperless-Archiv nach doppelten Dokumenten und LÖSCHT die überzähligen "
            "Kopien selbst (das älteste Dokument bleibt erhalten). Als Dublette gilt "
            "dasselbe Dokument, das mehrfach abgelegt wurde — erkannt an gleichem "
            "Titel, Datum, Korrespondent, Typ UND gleicher Seitenzahl (auch wenn der "
            "OCR-Text durch erneutes Scannen leicht abweicht), oder an byte-identischem "
            "Inhalt. Wegen des Paperless-Rate-Limits wird portionsweise gelöscht: das "
            "Tool meldet, wie viele gelöscht wurden und wie viele NOCH VERBLEIBEN — "
            "bei verbleibenden Duplikaten ERNEUT aufrufen, bis 0 übrig sind. Gelöschte "
            "landen im Paperless-Papierkorb (wiederherstellbar). Backt 'finde und "
            "lösche die Duplikate in Paperless', 'räum die doppelten Dokumente auf', "
            "'gibt es Dubletten?' (mit nur_zeigen). Admin-Aktion."
        ),
        "parameters": {
            "dry_run": "true = nur finden und melden, NICHTS löschen (Default false = löschen)",
        },
    }
}

# The dedupe_documents result's own marker keys. MCPManager fuzzy-falls-back an
# UNKNOWN tool to another paperless tool (e.g. search_documents) instead of
# erroring, so a response lacking these means the tool is unavailable (Paperless
# MCP too old) — report that rather than misread a foreign response.
_DEDUPE_MARKER_KEYS = ("scanned", "complete", "duplicate_copies")


def _looks_like_dedupe_result(res: dict) -> bool:
    return all(k in res for k in _DEDUPE_MARKER_KEYS)


async def paperless_dedupe(
    params: dict,
    mcp_manager: Any = None,
    user_id: int | None = None,
    user_permissions: list[str] | None = None,
) -> dict:
    """Find + delete a rate-limit-safe batch of duplicate Paperless documents via
    the Paperless MCP, and report how many remain. Keep the oldest of each group."""
    # Fail-closed: this trashes documents in bulk, so with auth ON it requires an
    # authenticated ADMIN. An unidentified turn (user_permissions=None) is DENIED.
    # Auth OFF (single-user household) skips the gate entirely.
    if settings.auth_enabled:
        if user_permissions is None or not has_permission(user_permissions, Permission.ADMIN):
            return {
                "success": False,
                "message": "Zum Aufräumen der Paperless-Duplikate fehlt die Berechtigung (Admin).",
                "action_taken": False,
            }
    if mcp_manager is None:
        return {"success": False, "message": "Paperless nicht verfügbar (kein MCP).", "action_taken": False}

    dry_run = bool(params.get("dry_run"))
    max_delete = max(1, int(settings.paperless_dedupe_delete_batch))
    metadata_match = bool(settings.paperless_dedupe_metadata_match_enabled)

    try:
        # truncate=False: the dedupe result can be large; a truncated payload would
        # be unparseable and misread as a transport error.
        res = _parse_paperless_result(await mcp_manager.execute_tool(
            "mcp.paperless.dedupe_documents",
            {"dry_run": dry_run, "max_delete": max_delete, "metadata_match": metadata_match},
            truncate=False,
        ))
    except Exception as e:  # noqa: BLE001
        logger.warning(f"paperless_dedupe failed: {e}")
        return {
            "success": False,
            "message": f"Aufräumen der Paperless-Duplikate fehlgeschlagen: {e!s}",
            "action_taken": False,
        }

    if res.get("error"):
        return {
            "success": False,
            "message": f"Aufräumen der Paperless-Duplikate fehlgeschlagen: {res['error']}",
            "action_taken": False,
        }
    if not _looks_like_dedupe_result(res):
        # Fuzzy-fallback / old MCP: never claim clean off a foreign response.
        return {
            "success": False,
            "message": (
                "Die Paperless-Dedupe-Funktion ist nicht verfügbar "
                "(Paperless-MCP-Server veraltet, benötigt >= 1.12.0)."
            ),
            "action_taken": False,
        }

    scanned = int(res.get("scanned") or 0)
    groups = int(res.get("groups") or 0)
    metadata_groups = int(res.get("metadata_groups") or 0)
    total_extras = int(res.get("duplicate_copies") or 0)
    kept = int(res.get("kept") or 0)
    deleted = int(res.get("deleted") or 0)
    remaining = int(res.get("remaining") or 0)
    skipped = int(res.get("skipped") or 0)
    complete = bool(res.get("complete"))

    data = {
        "documents_scanned": scanned,
        "sweep_complete": complete,
        "groups": groups,
        "metadata_groups": metadata_groups,
        "duplicate_copies": total_extras,
        "kept": kept,
        "skipped": skipped,
        "deleted": deleted,
        "remaining": remaining,
        "dry_run": dry_run,
    }

    # Only a COMPLETE sweep may claim the archive is clean; a partial sweep must
    # disclose it and never report "clean".
    partial_note = (
        ""
        if complete
        else (
            " ACHTUNG: das Archiv wurde nur TEILWEISE durchsucht (Rate-Limit oder "
            "sehr großer Bestand) — erneut aufrufen, um den Rest zu prüfen"
        )
    )

    if groups == 0:
        return {
            "success": True,
            "message": f"Keine Duplikate in den {scanned} geprüften Dokumenten gefunden{partial_note}.",
            "action_taken": False,
            "data": data,
        }

    if dry_run:
        note = ""
        if metadata_groups:
            note = (
                f" Davon {metadata_groups} über gleiche Metadaten/Seitenzahl erkannt "
                "(OCR-Text nicht identisch — z. B. erneut eingescannt)."
            )
        return {
            "success": True,
            "message": (
                f"{groups} Duplikat-Gruppe(n) mit insgesamt {total_extras} "
                f"überzähligen Kopien gefunden ({scanned} Dokumente geprüft, "
                f"{kept} Originale bleiben erhalten).{note} Nichts gelöscht "
                f"(nur_zeigen).{partial_note}"
            ),
            "action_taken": False,
            "data": data,
        }

    # Deletion happened in the MCP; map its counts to the message.
    if remaining == 0 and complete and skipped == 0:
        message = (
            f"{deleted} Duplikat(e) in den Papierkorb verschoben — alle "
            f"{groups} Gruppen bereinigt, {kept} Originale behalten "
            f"(wiederherstellbar im Papierkorb)."
        )
    else:
        parts = [f"{deleted} Duplikat(e) in den Papierkorb verschoben"]
        if remaining > 0:
            parts.append(
                f"{remaining} von {total_extras} Kopien verbleiben "
                "(portionsweise wegen des Paperless-Rate-Limits)"
            )
        if skipped:
            parts.append(f"{skipped} Dokument(e) konnten nicht geprüft/gelöscht werden")
        if not complete:
            parts.append("das Archiv wurde nur teilweise durchsucht")
        message = ". ".join(parts) + ". Sag erneut 'räum die Duplikate auf', um fortzufahren."

    return {
        "success": True,
        "message": message,
        "action_taken": deleted > 0,
        "data": {**data, "deleted_ids": res.get("deleted_ids") or []},
    }
