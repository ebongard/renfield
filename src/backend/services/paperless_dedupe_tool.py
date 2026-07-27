"""`internal.paperless_dedupe` — agent-callable Paperless duplicate finder + remover.

Finds duplicate documents in Paperless and (by default) deletes the extras itself,
so a self-hosted instance keeps its own archive clean without a manual API script.
Backs "finde und lösche die Duplikate in Paperless", "räum die doppelten Dokumente auf",
"gibt es Dubletten?" (with dry_run).

Duplicate definition — a Paperless document is the SAME as another when ALL its
metadata is identical (correspondent, document_type, creation date, title, page
count) OR its OCR bytes are identical. Either is sufficient; the extra copies are
deleted (recoverable trash). Implementation, by ``paperless_dedupe_metadata_match_enabled``:
  * ON (default) — identity is the FULL intrinsic-metadata tuple ``(correspondent,
    document_type, date, title, page_count)``, read STRAIGHT FROM ``search_documents``
    (which returns page_count since paperless-mcp >= 1.10.0) — so NO per-document
    ``get_document`` is needed. Equal tuple ⇒ ALL metadata identical ⇒ the SAME
    document, even when the re-scanned OCR bytes differ (the "an Audi lease filed three
    times" case that byte-identical matching can never catch). The WHOLE tuple
    identifies the document — page_count alone does not; it is merely one field of it.
    Byte-identical copies share the whole tuple, so this criterion subsumes byte-id.
    (page_count ``None`` — Paperless omitted it — is a value, so all-metadata-identical
    still holds and dedupes; a genuinely DIFFERENT page_count keeps documents apart.)
    The search ``snippet`` is used only to REPORT which groups were re-scans (differing
    OCR) — never as a delete criterion.
  * OFF — legacy byte-identical only: a cheap candidate prefilter
    (correspondent/type/date/title) then the FULL OCR text is fetched per candidate
    (``get_document`` include_content=True, truncate=False) and compared; a member
    without OCR text can't be proven identical and is reported, never deleted.
  - The CANONICAL kept copy is the one with the LOWEST Paperless id (the original /
    earliest import). Every other same-identity copy is deleted.
  - Deletion goes through ``mcp.paperless.delete_document``, which on Paperless-ngx 2.x
    moves the document to the recoverable TRASH — so an over-eager pass is undoable.

``dry_run=True`` reports the groups + what WOULD be deleted without touching anything.

Destructive maintenance — **fail-closed**: with auth ON it requires an authenticated
``Permission.ADMIN``; an unidentified turn (``user_permissions=None``) is DENIED
(unlike the reversible maintenance tools, because a bulk archive delete has a larger
blast radius). Auth OFF (single-user household) skips the gate. Bounded sweep
(``SWEEP_CAP`` docs); a larger corpus is reported as partially swept.
"""
from __future__ import annotations

import hashlib
from typing import Any

from loguru import logger

from models.permissions import Permission, has_permission
from services.folder_ingest_paperless import _parse_paperless_result
from utils.config import settings

SWEEP_CAP = 500  # matches the Paperless MCP search_documents max_results ceiling

PAPERLESS_DEDUPE_TOOL: dict = {
    "internal.paperless_dedupe": {
        "description": (
            "Paperless-DUBLETTEN finden und aufräumen: durchsucht das Paperless-Archiv "
            "nach doppelten Dokumenten und LÖSCHT die überzähligen Kopien selbst (das "
            "älteste Dokument bleibt erhalten). Als Dublette gilt dasselbe Dokument, das "
            "mehrfach abgelegt wurde — erkannt an gleichem Titel, Datum, Korrespondent, "
            "Typ UND gleicher Seitenzahl (auch wenn der OCR-Text durch erneutes Scannen "
            "leicht abweicht), oder an byte-identischem Inhalt. Gelöschte landen im "
            "Paperless-Papierkorb (wiederherstellbar). Backt 'finde und lösche die "
            "Duplikate in Paperless', 'räum die doppelten Dokumente auf', 'gibt es "
            "Dubletten?' (mit nur_zeigen). Admin-Aktion."
        ),
        "parameters": {
            "dry_run": "true = nur finden und melden, NICHTS löschen (Default false = löschen)",
        },
    }
}


def _content_hash(text: str | None) -> str | None:
    if not text:
        return None
    return hashlib.sha256(text.encode("utf-8", "ignore")).hexdigest()


def _candidate_key(doc: dict) -> tuple:
    """Cheap grouping key from search metadata — narrows the set that needs a
    full-content fetch (byte-identical path only). Re-uploads share all four fields."""
    created = (doc.get("created") or "")[:10]  # date only
    return (
        (doc.get("correspondent") or "").strip().lower(),
        (doc.get("document_type") or "").strip().lower(),
        created,
        (doc.get("title") or "").strip().lower(),
    )


def _metadata_identity(doc: dict) -> tuple:
    """Full intrinsic-metadata identity: two documents with an equal tuple are the
    SAME document (all metadata identical), even if their OCR bytes differ. Every
    field comes from ``search_documents`` (which now returns ``page_count``), so the
    metadata-match path establishes identity with NO per-document ``get_document``.
    ``page_count`` None (Paperless omitted it) is a value, so all-metadata-identical
    still holds; a genuinely different page_count keeps documents apart."""
    return (*_candidate_key(doc), doc.get("page_count"))


async def paperless_dedupe(
    params: dict,
    mcp_manager: Any = None,
    user_id: int | None = None,
    user_permissions: list[str] | None = None,
) -> dict:
    """Find exact-duplicate Paperless documents and delete the extras (keep oldest)."""
    # Fail-closed: this trashes documents in bulk, so with auth ON it requires an
    # authenticated ADMIN. An unidentified turn (user_permissions=None — a device/
    # satellite token or unrecognized-voice turn) is DENIED, unlike the reversible
    # maintenance tools that allow None: a bulk archive delete is a higher blast
    # radius. Auth OFF (single-user household) skips the gate entirely.
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

    try:
        search = _parse_paperless_result(
            await mcp_manager.execute_tool(
                # Newest-first: a re-upload burst (the dominant duplicate source)
                # produces RECENT copies, so on a corpus larger than SWEEP_CAP the
                # newest window is where the dupes are. Copies from one burst are
                # time-adjacent → land in the same window together. (Full-corpus
                # coverage beyond SWEEP_CAP needs pagination — P3, see swept_note.)
                "mcp.paperless.search_documents",
                {"ordering": "-created", "max_results": SWEEP_CAP},
                # truncate=False: a 500-row result set can exceed the default
                # response cap; a truncated array would both drop candidate docs and
                # make len(docs) < SWEEP_CAP falsely read as "full corpus swept".
                truncate=False,
            )
        )
        if search.get("error"):
            return {
                "success": False,
                "message": f"Paperless-Suche fehlgeschlagen: {search.get('error')}",
                "action_taken": False,
            }
        docs = search.get("results") or []

        metadata_match = settings.paperless_dedupe_metadata_match_enabled

        groups_found = 0
        metadata_groups = 0  # dup groups matched by metadata whose OCR differs (re-scans)
        deleted_ids: list[int] = []
        kept_ids: list[int] = []
        skipped = 0

        # 1) Form duplicate groups — each a list of same-document ids.
        #    ON  → identity is the FULL metadata tuple, read straight from the search
        #          result (page_count included since paperless-mcp >= 1.10.0), so NO
        #          per-document get_document is needed. Equal tuple ⇒ ALL metadata
        #          identical ⇒ the SAME document, even when the re-scanned OCR bytes
        #          differ (the "filed three times" case byte-identical matching misses).
        #    OFF → legacy byte-identical: a cheap _candidate_key prefilter, then the
        #          FULL OCR text is fetched per candidate and compared; a member without
        #          OCR text can't be proven identical → skipped, never deleted.
        dup_groups: list[dict] = []  # {"ids": sorted[int], "text_differs": bool}

        if metadata_match:
            by_identity: dict[tuple, list[dict]] = {}
            for d in docs:
                if d.get("id") is None:
                    continue
                by_identity.setdefault(_metadata_identity(d), []).append(d)
            for members in by_identity.values():
                if len(members) < 2:
                    continue
                ids_sorted = sorted(m["id"] for m in members)
                # The snippet (from search) is a cheap OCR fingerprint: differing
                # snippets across the group mean the copies are re-scans that
                # byte-identical matching would have missed. Report-only — never a
                # delete criterion (metadata identity already decided the group).
                text_differs = len({(m.get("snippet") or "") for m in members}) > 1
                dup_groups.append({"ids": ids_sorted, "text_differs": text_differs})
        else:
            candidates: dict[tuple, list[dict]] = {}
            for d in docs:
                if d.get("id") is None:
                    continue
                candidates.setdefault(_candidate_key(d), []).append(d)
            for members in candidates.values():
                if len(members) < 2:
                    continue
                by_hash: dict[str, list[int]] = {}
                for m in members:
                    got = _parse_paperless_result(
                        await mcp_manager.execute_tool(
                            "mcp.paperless.get_document",
                            {"document_id": m["id"], "include_content": True},
                            # truncate=False: the byte-identical path MUST compare the
                            # FULL OCR text — default truncation would byte-cut a long doc
                            # and two different docs sharing the same first
                            # ~mcp_max_response_size bytes would hash-identical and one be
                            # wrongly deleted.
                            truncate=False,
                        )
                    )
                    if got.get("error"):
                        skipped += 1
                        continue
                    h = _content_hash(got.get("content"))
                    if h is None:  # no OCR text → can't prove identity → never delete
                        skipped += 1
                        continue
                    by_hash.setdefault(h, []).append(m["id"])
                for ids in by_hash.values():
                    if len(ids) >= 2:
                        dup_groups.append({"ids": sorted(ids), "text_differs": False})

        # 2) Delete the extras of each group (keep the lowest / oldest id).
        for g in dup_groups:
            groups_found += 1
            if g["text_differs"]:
                metadata_groups += 1
            ids_sorted = g["ids"]
            kept_ids.append(ids_sorted[0])  # lowest id = original / earliest import
            for dup_id in ids_sorted[1:]:
                if dry_run:
                    deleted_ids.append(dup_id)
                    continue
                res = _parse_paperless_result(
                    await mcp_manager.execute_tool(
                        "mcp.paperless.delete_document", {"document_id": dup_id}
                    )
                )
                if res.get("deleted"):
                    deleted_ids.append(dup_id)
                else:
                    logger.warning(
                        f"paperless_dedupe: delete_document({dup_id}) failed: {res.get('error')}"
                    )
                    skipped += 1

        swept_note = ""
        if len(docs) >= SWEEP_CAP:
            swept_note = f" (nur die ersten {SWEEP_CAP} Dokumente geprüft)"

        if groups_found == 0:
            return {
                "success": True,
                "message": f"Keine Duplikate in Paperless gefunden{swept_note}.",
                "action_taken": False,
                "data": {
                    "groups": 0,
                    "deleted": 0,
                    "kept": 0,
                    "skipped": skipped,
                    "metadata_groups": 0,
                    "dry_run": dry_run,
                },
            }

        verb = "würden gelöscht" if dry_run else "gelöscht"
        parts = [
            f"{groups_found} Duplikat-Gruppe(n)",
            f"{len(deleted_ids)} Kopie(n) {verb}",
            f"{len(kept_ids)} Original(e) behalten",
        ]
        if metadata_groups:
            parts.append(
                f"davon {metadata_groups} über gleiche Metadaten/Seitenzahl erkannt "
                "(Text nicht identisch — z. B. erneut eingescannt)"
            )
        if skipped:
            parts.append(f"{skipped} nicht eindeutig/übersprungen")
        return {
            "success": True,
            "message": "Paperless-Duplikate: " + ", ".join(parts) + swept_note + ".",
            "action_taken": bool(deleted_ids) and not dry_run,
            "data": {
                "groups": groups_found,
                "metadata_groups": metadata_groups,
                "deleted": len(deleted_ids),
                "deleted_ids": deleted_ids,
                "kept": len(kept_ids),
                "kept_ids": kept_ids,
                "skipped": skipped,
                "dry_run": dry_run,
            },
        }
    except Exception as e:  # noqa: BLE001
        logger.warning(f"paperless_dedupe failed: {e}")
        return {
            "success": False,
            "message": f"Aufräumen der Paperless-Duplikate fehlgeschlagen: {e!s}",
            "action_taken": False,
        }
