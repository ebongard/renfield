"""`internal.paperless_dedupe` — agent-callable Paperless duplicate finder + remover.

Finds duplicate documents in Paperless and (by default) deletes the extras itself,
so a self-hosted instance keeps its own archive clean without a manual API script.
Backs "finde und lösche die Duplikate in Paperless", "räum die doppelten Dokumente auf",
"gibt es Dubletten?" (with dry_run).

Duplicate definition — a Paperless document is the SAME as another when ALL its
metadata is identical (correspondent, document_type, creation date, title, page
count) OR its OCR bytes are identical. Either is sufficient; the extra copies are
deleted (recoverable trash). "All metadata identical" requires those fields to be
PRESENT and equal — an absent field (empty title / missing page_count) is NOT
"identical", so it drops to the byte-identical rule (never delete on a weak signal).
Per candidate group (already sharing correspondent/type/date/title):
  * METADATA identity (``paperless_dedupe_metadata_match_enabled`` ON, NON-EMPTY title,
    AND every member carries a page_count) — sub-group by page_count. Same page_count ⇒
    the full tuple ``(correspondent, document_type, date, title, page_count)`` matches ⇒
    the SAME document, even when the re-scanned OCR bytes differ (the "an Audi lease
    filed three times" case that byte-identical matching can never catch). The identity
    is read STRAIGHT FROM ``search_documents`` (page_count included since paperless-mcp
    >= 1.10.0), so NO per-document ``get_document`` is needed. The search ``snippet`` is
    used only to REPORT which groups were re-scans — never to delete.
  * BYTE-IDENTICAL fallback (setting OFF, empty title, OR any member missing page_count)
    — the FULL OCR text is fetched per member and compared; only byte-identical copies
    are deleted, and a member without OCR text can't be proven identical → never deleted.
  - The CANONICAL kept copy is the one with the LOWEST Paperless id (the original /
    earliest import). Every other same-identity copy is deleted.

FULL-ARCHIVE sweep + RATE-LIMIT-SAFE batched delete. The Paperless MCP caps each
search at 500 results and is rate-limited (60/min token bucket). So each invocation:
  1. pages the WHOLE archive via ``created_before`` date windows (dedup by id), not
     just the newest 500 — a re-ingested copy keeps its original date, so duplicates
     are scattered across the corpus, not clustered at the newest end;
  2. deletes up to ``paperless_dedupe_delete_batch`` extras (default 50), each with
     retry/backoff on a rate-limit rejection so the batch actually completes;
  3. reports how many were deleted and how many REMAIN, so the caller re-runs to
     continue. It NEVER claims the archive is clean while copies remain.

``dry_run=True`` reports the full duplicate scope + what WOULD be deleted, deleting
nothing.

Destructive maintenance — **fail-closed**: with auth ON it requires an authenticated
``Permission.ADMIN``; an unidentified turn (``user_permissions=None``) is DENIED
(unlike the reversible maintenance tools, because a bulk archive delete has a larger
blast radius). Auth OFF (single-user household) skips the gate.
"""
from __future__ import annotations

import asyncio
import hashlib
from typing import Any

from loguru import logger

from models.permissions import Permission, has_permission
from services.folder_ingest_paperless import _parse_paperless_result
from utils.config import settings

_SEARCH_PAGE = 500       # Paperless MCP search_documents max_results ceiling per call
_MAX_SWEEP_PAGES = 40    # safety cap on the paginated sweep (40 * 500 = 20k docs)
_RATE_RETRY = 6          # per-delete retries when the MCP rate-limits the call
_RATE_SLEEP = 1.2        # seconds to wait for the 60/min token bucket to refill

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


async def _gather_all_documents(mcp_manager: Any) -> tuple[list[dict], str | None]:
    """Page the WHOLE archive via ``created_before`` date windows (the MCP caps each
    search at 500). Returns (docs, error). Dedups by id (adjacent windows overlap on
    the boundary date), bounded by _MAX_SWEEP_PAGES. A partial corpus (a later page
    erroring after earlier pages succeeded) is still returned — better to dedupe what
    we have than nothing; the error is only surfaced when we got zero docs."""
    docs_by_id: dict[int, dict] = {}
    before: str | None = None
    for _ in range(_MAX_SWEEP_PAGES):
        params: dict = {"ordering": "-created", "max_results": _SEARCH_PAGE}
        if before:
            # created__date__lte — inclusive, so the boundary date's docs reappear in
            # the next window and are deduped by id (no gap even if a date straddles
            # the 500-row page boundary).
            params["created_before"] = before
        res = _parse_paperless_result(
            await mcp_manager.execute_tool(
                "mcp.paperless.search_documents",
                params,
                # truncate=False: a 500-row page can exceed the default response cap;
                # truncation would drop candidate docs and corrupt the sweep.
                truncate=False,
            )
        )
        if res.get("error"):
            return list(docs_by_id.values()), (None if docs_by_id else res.get("error"))
        batch = res.get("results") or []
        oldest: str | None = None
        for d in batch:
            if d.get("id") is None:
                continue
            docs_by_id.setdefault(d["id"], d)
            cd = (d.get("created") or "")[:10]
            if cd and (oldest is None or cd < oldest):
                oldest = cd
        if len(batch) < _SEARCH_PAGE:
            break  # reached the end of the archive
        if oldest is None or oldest == before:
            break  # can't advance the date window (>500 docs share one date) — stop
        before = oldest  # next window: that date and older
    return list(docs_by_id.values()), None


async def _build_dup_groups(
    docs: list[dict], mcp_manager: Any, metadata_match: bool
) -> tuple[list[dict], int]:
    """Group docs into duplicate sets. Returns (dup_groups, skipped) where each group
    is {"ids": sorted[int], "text_differs": bool}. See module docstring for identity."""
    skipped = 0
    candidates: dict[tuple, list[dict]] = {}
    for d in docs:
        if d.get("id") is None:
            continue
        candidates.setdefault(_candidate_key(d), []).append(d)

    async def _byte_identical_groups(members: list[dict]) -> list[dict]:
        nonlocal skipped
        by_hash: dict[str, list[int]] = {}
        for m in members:
            got = _parse_paperless_result(
                await mcp_manager.execute_tool(
                    "mcp.paperless.get_document",
                    {"document_id": m["id"], "include_content": True},
                    # truncate=False: MUST compare the FULL OCR text — a byte-cut would
                    # make two different docs sharing the same prefix hash-identical.
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
        return [
            {"ids": sorted(ids), "text_differs": False}
            for ids in by_hash.values()
            if len(ids) >= 2
        ]

    dup_groups: list[dict] = []
    for meta_key, members in candidates.items():
        if len(members) < 2:
            continue
        title = meta_key[3]  # _candidate_key = (correspondent, type, date, title)
        strong_metadata = (
            metadata_match
            and bool(title)
            and all(m.get("page_count") is not None for m in members)
        )
        if not strong_metadata:
            dup_groups.extend(await _byte_identical_groups(members))
            continue
        by_page: dict[Any, list[dict]] = {}
        for m in members:
            by_page.setdefault(m["page_count"], []).append(m)
        for grp in by_page.values():
            if len(grp) < 2:
                continue
            ids_sorted = sorted(m["id"] for m in grp)
            text_differs = len({(m.get("snippet") or "") for m in grp}) > 1
            dup_groups.append({"ids": ids_sorted, "text_differs": text_differs})
    return dup_groups, skipped


async def _delete_one(mcp_manager: Any, doc_id: int) -> bool:
    """Delete one document, retrying on an MCP rate-limit rejection (the 60/min token
    bucket refills ~1/s). Returns True if deleted, False on a non-rate-limit error or
    after exhausting retries."""
    for _ in range(_RATE_RETRY):
        res = _parse_paperless_result(
            await mcp_manager.execute_tool("mcp.paperless.delete_document", {"document_id": doc_id})
        )
        if res.get("deleted"):
            return True
        err = str(res.get("error") or "")
        if "rate limit" in err.lower():
            await asyncio.sleep(_RATE_SLEEP)  # let the token bucket refill, then retry
            continue
        logger.warning(f"paperless_dedupe: delete_document({doc_id}) failed: {err}")
        return False
    logger.warning(f"paperless_dedupe: delete_document({doc_id}) gave up after rate-limit retries")
    return False


async def paperless_dedupe(
    params: dict,
    mcp_manager: Any = None,
    user_id: int | None = None,
    user_permissions: list[str] | None = None,
) -> dict:
    """Find duplicate Paperless documents and delete a rate-limit-safe batch of the
    extras (keep the oldest of each group). Reports how many remain."""
    # Fail-closed: this trashes documents in bulk, so with auth ON it requires an
    # authenticated ADMIN. An unidentified turn (user_permissions=None) is DENIED —
    # a bulk archive delete has a higher blast radius than the reversible tools.
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
    metadata_match = settings.paperless_dedupe_metadata_match_enabled
    batch_cap = max(1, int(settings.paperless_dedupe_delete_batch))

    try:
        docs, err = await _gather_all_documents(mcp_manager)
        if err:
            return {
                "success": False,
                "message": f"Paperless-Suche fehlgeschlagen: {err}",
                "action_taken": False,
            }

        dup_groups, skipped = await _build_dup_groups(docs, mcp_manager, metadata_match)
        groups_found = len(dup_groups)
        metadata_groups = sum(1 for g in dup_groups if g["text_differs"])
        kept = groups_found  # one canonical original kept per group
        # every extra copy across every group (the lowest id of each group is kept)
        extras = [doc_id for g in dup_groups for doc_id in g["ids"][1:]]
        total_extras = len(extras)

        base_data = {
            "documents_scanned": len(docs),
            "groups": groups_found,
            "metadata_groups": metadata_groups,
            "duplicate_copies": total_extras,
            "kept": kept,
            "skipped": skipped,
            "dry_run": dry_run,
        }

        if groups_found == 0:
            return {
                "success": True,
                "message": f"Keine Duplikate in Paperless gefunden ({len(docs)} Dokumente geprüft).",
                "action_taken": False,
                "data": {**base_data, "deleted": 0, "remaining": 0},
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
                    f"{groups_found} Duplikat-Gruppe(n) mit insgesamt {total_extras} "
                    f"überzähligen Kopien gefunden ({len(docs)} Dokumente geprüft, "
                    f"{kept} Originale bleiben erhalten).{note} Nichts gelöscht (nur_zeigen)."
                ),
                "action_taken": False,
                "data": {**base_data, "deleted": 0, "remaining": total_extras},
            }

        # Delete a rate-limit-safe batch; the rest is left for the next call.
        deleted_ids: list[int] = []
        for doc_id in extras[:batch_cap]:
            if await _delete_one(mcp_manager, doc_id):
                deleted_ids.append(doc_id)
            else:
                skipped += 1
        remaining = total_extras - len(deleted_ids)

        if remaining > 0:
            message = (
                f"{len(deleted_ids)} Duplikat(e) in den Papierkorb verschoben. "
                f"Es verbleiben noch {remaining} von {total_extras} überzähligen Kopien "
                f"(portionsweise wegen des Paperless-Rate-Limits). Sag erneut "
                f"'räum die Duplikate auf', um fortzufahren."
            )
        else:
            message = (
                f"{len(deleted_ids)} Duplikat(e) in den Papierkorb verschoben — alle "
                f"{groups_found} Gruppen bereinigt, {kept} Originale behalten "
                f"(wiederherstellbar im Papierkorb)."
            )

        return {
            "success": True,
            "message": message,
            "action_taken": bool(deleted_ids),
            "data": {
                **base_data,
                "deleted": len(deleted_ids),
                "deleted_ids": deleted_ids,
                "remaining": remaining,
                "batch_cap": batch_cap,
            },
        }
    except Exception as e:  # noqa: BLE001
        logger.warning(f"paperless_dedupe failed: {e}")
        return {
            "success": False,
            "message": f"Aufräumen der Paperless-Duplikate fehlgeschlagen: {e!s}",
            "action_taken": False,
        }
