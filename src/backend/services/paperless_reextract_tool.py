"""`internal.reextract_paperless_metadata` — agent-callable Paperless metadata backfill.

Re-derives Paperless metadata for already-FILED documents and PATCHes any GAP fields
(correspondent / document_type / tags) that are currently empty — using the same
resolve-or-create helpers as the live filing leg (so a fresh/wiped Paperless
self-populates its taxonomy, and docs filed before those fields were populated get
backfilled). The agent-facing wrapper of ``bin/backfill_paperless_metadata.py``, so a
user can just ask "re-extrahiere die Paperless-Metadaten".

Backend-safe by construction (this runs in the always-on backend, not the worker):
  - **No Docling OCR** — re-uses the document's ALREADY-STORED chunk text
    (extract_from_doc_text), never extract_from_file, so it can't OOM the backend
    the way the worker-only filing leg would.
  - **No held DB connection** — one short session up front to gather the work list +
    stored text, then the slow per-doc Paperless MCP loop runs with NO pooled
    connection checked out (Design-Z pool-starvation avoidance).
  - **Taxonomy fetched ONCE** and passed to every resolver (batch-reuse), staying
    under the Paperless MCP 60/min rate limit.
  - ``get_document(include_content=False)`` so a large-OCR doc's response can't be
    truncated and mis-bucketed as unmatched.
  - Filed-as-duplicate docs (paperless_document_id NULL) are recovered via a
    filename index, matching the backfill script.

Gap-fill only (never overwrites a human-set field). Bounded batch. Write/maintenance
— gated on ``Permission.RAG_MANAGE`` (auth-off / unidentified-voice turns allowed).
"""
from __future__ import annotations

from typing import Any

from loguru import logger
from sqlalchemy import select

from models.database import (
    PAPERLESS_STATE_DONE,
    Document,
    DocumentChunk,
)
from models.permissions import Permission, has_permission
from services.database import AsyncSessionLocal
from services.folder_ingest_paperless import (
    _fetch_correspondent_names,
    _fetch_taxonomy_names,
    _parse_paperless_result,
    resolve_correspondent_from_metadata,
    resolve_document_type_from_metadata,
    resolve_tags_from_metadata,
)
from utils.config import settings

REEXTRACT_DEFAULT_CAP = 50
REEXTRACT_MAX_CAP = 200
_MAX_TEXT_CHARS = 8000  # metadata lives at the top of the doc; the extractor truncates anyway

PAPERLESS_REEXTRACT_TOOL: dict = {
    "internal.reextract_paperless_metadata": {
        "description": (
            "Paperless-Metadaten NACHZIEHEN: leitet für bereits abgelegte Dokumente die "
            "Metadaten neu ab (aus dem gespeicherten Text) und füllt LEERE Felder in "
            "Paperless (Korrespondent, Dokumenttyp, Tags) per resolve-or-create nach — "
            "überschreibt nie manuell Gesetztes. Backt 're-extrahiere die Paperless-"
            "Metadaten', 'fülle die fehlenden Dokumenttypen / Tags in Paperless', 'setze "
            "Typ und Tags für die abgelegten Dokumente'. Admin/RAG-Verwaltung."
        ),
        "parameters": {"limit": f"Max. Dokumente pro Lauf (Default {REEXTRACT_DEFAULT_CAP}, max {REEXTRACT_MAX_CAP})"},
    }
}


def _cap(params: dict) -> int:
    try:
        n = int(params.get("limit")) if params.get("limit") is not None else REEXTRACT_DEFAULT_CAP
    except (TypeError, ValueError):
        n = REEXTRACT_DEFAULT_CAP
    return max(1, min(n, REEXTRACT_MAX_CAP))


async def _gather_worklist(limit: int) -> list[dict]:
    """ONE short session: filed docs + their stored chunk text. No connection is
    held past this — the slow MCP loop runs connection-free."""
    async with AsyncSessionLocal() as db:
        docs = (
            await db.execute(
                select(Document)
                .where(Document.paperless_state == PAPERLESS_STATE_DONE)
                .order_by(Document.id.desc())
                .limit(limit)
            )
        ).scalars().all()
        work: list[dict] = []
        for doc in docs:
            chunks = (
                await db.execute(
                    select(DocumentChunk.content)
                    .where(DocumentChunk.document_id == doc.id)
                    .order_by(DocumentChunk.id)
                )
            ).scalars().all()
            text = "\n".join(c for c in chunks if c)[:_MAX_TEXT_CHARS]
            work.append({
                "id": doc.id,
                "filename": (doc.filename or "").strip().lower(),
                "paperless_id": doc.paperless_document_id,
                "user_id": getattr(doc, "user_id", None),
                "text": text,
            })
        return work


async def _build_filename_index(mcp_manager) -> dict[str, int]:
    """``{original_file_name(lower): paperless_id}`` for the recently-added docs, so a
    doc filed as 'duplicate' (paperless_document_id NULL) can still be matched.
    include_content=False keeps each response small (can't be truncated)."""
    search = _parse_paperless_result(
        await mcp_manager.execute_tool(
            "mcp.paperless.search_documents", {"ordering": "-added", "max_results": 200}
        )
    )
    index: dict[str, int] = {}
    for r in search.get("results") or []:
        pid = r.get("id")
        if pid is None:
            continue
        got = _parse_paperless_result(
            await mcp_manager.execute_tool(
                "mcp.paperless.get_document", {"document_id": pid, "include_content": False}
            )
        )
        ofn = (got.get("original_file_name") or "").strip().lower()
        if ofn and ofn not in index:
            index[ofn] = pid
    return index


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
    fixed = skipped = already = unmatched = 0

    try:
        work = await _gather_worklist(_cap(params))
        # Taxonomy fetched ONCE, reused for every doc (rate-limit friendly).
        corr_names = await _fetch_correspondent_names(mcp_manager)
        type_names = await _fetch_taxonomy_names(mcp_manager, "document_type")
        tag_names = await _fetch_taxonomy_names(mcp_manager, "tag")
        fname_index = (
            await _build_filename_index(mcp_manager)
            if any(w["paperless_id"] is None for w in work)
            else {}
        )

        for w in work:
            pid = w["paperless_id"] or fname_index.get(w["filename"])
            if pid is None:
                unmatched += 1
                continue
            got = _parse_paperless_result(
                await mcp_manager.execute_tool(
                    "mcp.paperless.get_document", {"document_id": pid, "include_content": False}
                )
            )
            if got.get("error"):
                unmatched += 1
                continue
            needs_corr = not (got.get("correspondent") or "")
            needs_type = not (got.get("document_type") or "")
            needs_tags = not (got.get("tags") or [])
            if not (needs_corr or needs_type or needs_tags):
                already += 1
                continue
            if not w["text"]:
                skipped += 1  # no stored text to re-derive from
                continue

            result = await extractor.extract_from_doc_text(
                w["text"], user_id=w["user_id"], lang="de"
            )
            if result.error:
                skipped += 1
                continue
            m = result.metadata

            patch: dict = {"document_id": pid}
            if needs_corr:
                corr = await resolve_correspondent_from_metadata(mcp_manager, m, names=corr_names)
                if corr:
                    patch["correspondent"] = corr
            if needs_type:
                dt = await resolve_document_type_from_metadata(mcp_manager, m, names=type_names)
                if dt:
                    patch["document_type"] = dt
            if needs_tags:
                tags = await resolve_tags_from_metadata(mcp_manager, m, names=tag_names)
                if tags:
                    patch["tags"] = tags

            if len(patch) == 1:  # nothing resolved
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
