"""`internal.reextract_paperless_metadata` — agent-callable Paperless metadata backfill.

Re-runs the Paperless metadata extraction over already-FILED documents and PATCHes
the Paperless document with any GAP fields (correspondent / document_type / tags)
that are currently empty — using the same resolve-or-create helpers as the live
filing leg (so a fresh/wiped Paperless self-populates its taxonomy, and existing
docs filed before those fields were populated get backfilled).

This is the agent-facing wrapper of ``bin/backfill_paperless_metadata.py`` — so a
user can just ask "re-extrahiere die Paperless-Metadaten" / "fülle die fehlenden
Felder in Paperless" instead of an operator running a script. Gap-fill only: it
never overwrites a field a human already set. Write/maintenance — gated on
``Permission.RAG_MANAGE`` (auth-off / unidentified-voice turns allowed).
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from loguru import logger
from sqlalchemy import select

from models.database import PAPERLESS_STATE_DONE, Document
from models.permissions import Permission, has_permission
from services.database import AsyncSessionLocal
from services.folder_ingest_paperless import (
    _parse_paperless_result,
    resolve_correspondent_from_metadata,
    resolve_document_type_from_metadata,
    resolve_tags_from_metadata,
)
from utils.config import settings

REEXTRACT_DEFAULT_CAP = 100
REEXTRACT_MAX_CAP = 500

PAPERLESS_REEXTRACT_TOOL: dict = {
    "internal.reextract_paperless_metadata": {
        "description": (
            "Paperless-Metadaten NACHZIEHEN: extrahiert für bereits abgelegte Dokumente "
            "die Metadaten neu und füllt LEERE Felder in Paperless (Korrespondent, "
            "Dokumenttyp, Tags) per resolve-or-create nach — überschreibt nie manuell "
            "Gesetztes. Backt 're-extrahiere die Paperless-Metadaten', 'fülle die "
            "fehlenden Felder / Dokumenttypen / Tags in Paperless', 'setze Typ und Tags "
            "für die abgelegten Dokumente'. Admin/RAG-Verwaltung."
        ),
        "parameters": {
            "limit": "Max. Anzahl Dokumente (Default 100, max 500)",
        },
    }
}


def _cap(params: dict) -> int:
    raw = params.get("limit")
    try:
        n = int(raw) if raw is not None else REEXTRACT_DEFAULT_CAP
    except (TypeError, ValueError):
        n = REEXTRACT_DEFAULT_CAP
    return max(1, min(n, REEXTRACT_MAX_CAP))


async def reextract_paperless_metadata(
    params: dict,
    mcp_manager: Any = None,
    user_id: int | None = None,
    user_permissions: list[str] | None = None,
) -> dict:
    """Gap-fill Paperless metadata (correspondent/document_type/tags) on filed docs."""
    if settings.auth_enabled and user_permissions is not None:
        if not has_permission(user_permissions, Permission.RAG_MANAGE):
            return {
                "success": False,
                "message": "Zum Nachziehen der Paperless-Metadaten fehlt die Berechtigung (RAG-Verwaltung).",
                "action_taken": False,
            }
    if mcp_manager is None:
        return {"success": False, "message": "Paperless nicht verfügbar (kein MCP).", "action_taken": False}

    from services.paperless_metadata_extractor import PaperlessMetadataExtractor

    extractor = PaperlessMetadataExtractor(mcp_manager=mcp_manager)
    limit = _cap(params)
    fixed = skipped = already = unmatched = 0

    try:
        async with AsyncSessionLocal() as db:
            docs = (
                await db.execute(
                    select(Document)
                    .where(Document.paperless_state == PAPERLESS_STATE_DONE)
                    .order_by(Document.id.desc())
                    .limit(limit)
                )
            ).scalars().all()

            for doc in docs:
                pid = doc.paperless_document_id
                if pid is None:
                    unmatched += 1
                    continue
                got = _parse_paperless_result(
                    await mcp_manager.execute_tool("mcp.paperless.get_document", {"document_id": pid})
                )
                if got.get("error"):
                    unmatched += 1
                    continue
                # Which fields are still empty in Paperless? (gap-fill only)
                needs_corr = not (got.get("correspondent") or "")
                needs_type = not (got.get("document_type") or "")
                needs_tags = not (got.get("tags") or [])
                if not (needs_corr or needs_type or needs_tags):
                    already += 1
                    continue
                if not doc.file_path or not Path(doc.file_path).exists():
                    skipped += 1
                    continue

                result = await extractor.extract_from_file(
                    doc.file_path, user_id=getattr(doc, "user_id", None), lang="de"
                )
                if result.error:
                    skipped += 1
                    continue
                m = result.metadata

                patch: dict = {"document_id": pid}
                if needs_corr:
                    corr = await resolve_correspondent_from_metadata(mcp_manager, m)
                    if corr:
                        patch["correspondent"] = corr
                if needs_type:
                    dt = await resolve_document_type_from_metadata(mcp_manager, m)
                    if dt:
                        patch["document_type"] = dt
                if needs_tags:
                    tags = await resolve_tags_from_metadata(mcp_manager, m)
                    if tags:
                        patch["tags"] = tags

                if len(patch) == 1:  # only document_id → nothing resolved
                    skipped += 1
                    continue

                res = _parse_paperless_result(
                    await mcp_manager.execute_tool("mcp.paperless.update_document", patch)
                )
                if res.get("error"):
                    logger.warning(f"reextract: update_document({pid}) failed: {res.get('error')}")
                    skipped += 1
                    continue
                fixed += 1

        parts = [f"{fixed} Dokument(e) nachgezogen"]
        if already:
            parts.append(f"{already} bereits vollständig")
        if skipped:
            parts.append(f"{skipped} übersprungen")
        if unmatched:
            parts.append(f"{unmatched} ohne Paperless-Zuordnung")
        return {
            "success": True,
            "message": "Paperless-Metadaten: " + ", ".join(parts) + ".",
            "action_taken": fixed > 0,
            "data": {"fixed": fixed, "already": already, "skipped": skipped, "unmatched": unmatched},
        }
    except Exception as e:  # noqa: BLE001
        logger.warning(f"reextract_paperless_metadata failed: {e}")
        return {
            "success": False,
            "message": f"Nachziehen der Paperless-Metadaten fehlgeschlagen: {e!s}",
            "action_taken": False,
        }
