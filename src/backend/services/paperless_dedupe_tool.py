"""`internal.paperless_dedupe` — agent-callable Paperless duplicate finder + remover.

Finds duplicate documents in Paperless and (by default) deletes the extras itself,
so a self-hosted instance keeps its own archive clean without a manual API script.
Backs "finde und lösche die Duplikate in Paperless", "räum die doppelten Dokumente auf",
"gibt es Dubletten?" (with dry_run).

Duplicate definition — a Paperless document is the SAME as another when its file
CHECKSUM is identical (byte-identical original — the exact signal, catches re-upload-
loop copies even if their metadata drifted), OR ALL its metadata is identical
(correspondent, document_type, creation date, title, page count), OR its OCR bytes
are identical. Any is sufficient; the extra copies are deleted (recoverable trash).
Enumeration is INDEX-INDEPENDENT (``list_all_documents``: DB-ordered, fully paginated,
carries the checksum) so a stale/partial Paperless search index cannot hide copies —
the failure that made a dry-run see only ~564 of ~3616 real duplicates on xidra. "All metadata identical" requires those fields to be
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
import time
from typing import Any

from loguru import logger

from models.permissions import Permission, has_permission
from services.folder_ingest_paperless import _parse_paperless_result
from utils.config import settings

_SEARCH_PAGE = 500       # Paperless MCP search_documents max_results ceiling per call
_MAX_SWEEP_PAGES = 40    # safety cap on the paginated sweep (40 * 500 = 20k docs)
_RATE_RETRY = 6          # retries when the MCP rate-limits a call (search/fetch/delete)
_RATE_SLEEP = 1.2        # seconds to wait for the 60/min token bucket to refill
_MAX_DELETE_SECONDS = 75  # wall-clock budget for the delete loop, so one tool call
#                           can't block past the agent step timeout under heavy throttle


async def _call_with_retry(mcp_manager: Any, tool: str, params: dict, **kw) -> dict:
    """Execute an MCP tool and parse it, retrying ONLY on a rate-limit rejection (the
    60/min token bucket refills ~1/s) — shared by the sweep, the OCR fetch and the
    delete so all three survive throttling, not just deletes. Returns the parsed dict;
    a non-rate-limit error is returned as-is for the caller to interpret."""
    for _ in range(_RATE_RETRY):
        res = _parse_paperless_result(await mcp_manager.execute_tool(tool, params, **kw))
        err = str(res.get("error") or "")
        if err and "rate limit" in err.lower():
            await asyncio.sleep(_RATE_SLEEP)
            continue
        return res
    return {"error": "rate_limited"}  # exhausted retries — surfaced to the caller

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


async def _gather_all_documents(
    mcp_manager: Any,
) -> tuple[list[dict], bool, str | None]:
    """Enumerate the WHOLE archive, index-INDEPENDENT, via ``list_all_documents``
    (DB-ordered, fully paginated, carries ``checksum``). Returns ``(docs, complete,
    error)``; ``complete`` is False when the returned count is short of the DB
    ``total_count`` (never claim clean on a partial enumeration).

    Falls back to the legacy ``created_before`` date-window sweep if
    ``list_all_documents`` is unavailable (an older MCP / version skew) — so dedup
    still works, degraded. That fallback CANNOT reach a duplicate group whose >500
    copies share one creation date (the failure that hid the 2289-copy group);
    ``list_all_documents`` fixes it by walking an id cursor, not a date window."""
    res = await _call_with_retry(mcp_manager, "mcp.paperless.list_all_documents", {})
    # Accept ONLY a genuine list_all_documents response — it is the only paperless
    # tool whose summary carries ``total_count``. Critical: on an OLD MCP that lacks
    # this tool, MCPManager does NOT error — it FUZZY-FALLS-BACK to another paperless
    # tool (search_documents), returning a success-shaped result with a DIFFERENT
    # summary (``total_matching``, newest-100 only, no checksum). Treating that as our
    # result would dedupe only the newest slice and falsely report the archive clean —
    # the exact silent failure this fix kills. So gate on the ``total_count`` contract
    # marker; anything else (real error OR a fuzzy-fallback search result) degrades to
    # the legacy date-window sweep, which discloses partial coverage and never
    # false-cleans. (Also: a missing total means UNKNOWN completeness, never complete.)
    summary = res.get("summary")
    if not res.get("error") and isinstance(summary, dict) and "total_count" in summary:
        docs = res.get("results") or []
        total = summary["total_count"]
        complete = total is not None and len(docs) >= total and not summary.get("truncated")
        return docs, complete, None
    logger.info(
        f"paperless_dedupe: list_all_documents unavailable/unrecognized "
        f"({res.get('error') or 'no total_count in summary'}); falling back to the "
        "date-window sweep"
    )
    return await _gather_via_date_windows(mcp_manager)


async def _gather_via_date_windows(
    mcp_manager: Any,
) -> tuple[list[dict], bool, str | None]:
    """LEGACY fallback: page the archive via ``created_before`` date windows (the MCP
    caps each search at 500). Returns ``(docs, complete, error)``:
      * ``complete`` is True ONLY when the sweep reached the natural end of the archive
        (a page returned < 500). It is False whenever coverage may be partial — a
        search errored mid-sweep, the _MAX_SWEEP_PAGES cap was hit, or the date window
        stalled (>500 docs share the oldest date, so it can't advance). Callers MUST NOT
        claim the archive is clean when ``complete`` is False.
      * ``error`` is set only when the VERY FIRST search failed (zero docs). A later
        page erroring returns the partial corpus with complete=False and error=None.
    Dedups by id (adjacent windows overlap on the boundary date), bounded by
    _MAX_SWEEP_PAGES. Rate-limited searches are retried (``_call_with_retry``)."""
    docs_by_id: dict[int, dict] = {}
    before: str | None = None
    complete = False
    for _ in range(_MAX_SWEEP_PAGES):
        params: dict = {"ordering": "-created", "max_results": _SEARCH_PAGE}
        if before:
            # created__date__lte — inclusive, so the boundary date's docs reappear in
            # the next window and are deduped by id (no gap even if a date straddles
            # the 500-row page boundary).
            params["created_before"] = before
        # truncate=False: a 500-row page can exceed the default response cap; truncation
        # would drop candidate docs and corrupt the sweep.
        res = await _call_with_retry(
            mcp_manager, "mcp.paperless.search_documents", params, truncate=False
        )
        if res.get("error"):
            # first page failed → nothing to work with; later page failed → partial.
            return list(docs_by_id.values()), False, (res.get("error") if not docs_by_id else None)
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
            complete = True  # reached the natural end of the archive
            break
        if oldest is None or oldest == before:
            # can't advance the date window (>500 docs share one date) → older docs are
            # unreachable via this API; report the sweep as INCOMPLETE, don't claim clean.
            break
        before = oldest  # next window: that date and older
    return list(docs_by_id.values()), complete, None


async def _build_dup_groups(
    docs: list[dict], mcp_manager: Any, metadata_match: bool
) -> tuple[list[dict], int]:
    """Group docs into duplicate sets. Returns (dup_groups, skipped) where each group
    is {"ids": sorted[int], "text_differs": bool}. See module docstring for identity."""
    skipped = 0
    dup_groups: list[dict] = []

    # PASS 1 — exact checksum groups (byte-identical files). The strongest, cheapest
    # signal: same MD5 ⇒ same document, independent of any metadata/date drift, and it
    # needs no per-doc fetch. This is what catches the re-ingest-loop copies the
    # metadata key missed (their title/date could vary while the bytes are identical).
    # Docs without a checksum (older MCP, or genuinely absent) fall through to pass 2.
    by_checksum: dict[str, list[int]] = {}
    for d in docs:
        if d.get("id") is None:
            continue
        cs = d.get("checksum")
        if cs:
            by_checksum.setdefault(cs, []).append(d["id"])
    claimed: set[int] = set()
    for ids in by_checksum.values():
        if len(ids) >= 2:
            dup_groups.append({"ids": sorted(ids), "text_differs": False})
            claimed.update(ids)

    # PASS 2 — the metadata / OCR-hash passes on everything NOT already a byte-identical
    # checksum dup. Catches RE-SCANS (same document scanned twice → DIFFERENT bytes/
    # checksum, but same correspondent/type/date/title/page_count). Unique-checksum docs
    # stay in here for exactly that reason.
    candidates: dict[tuple, list[dict]] = {}
    for d in docs:
        if d.get("id") is None or d["id"] in claimed:
            continue
        candidates.setdefault(_candidate_key(d), []).append(d)

    async def _byte_identical_groups(members: list[dict]) -> list[dict]:
        nonlocal skipped
        by_hash: dict[str, list[int]] = {}
        for m in members:
            # truncate=False: MUST compare the FULL OCR text — a byte-cut would make two
            # different docs sharing the same prefix hash-identical. _call_with_retry so
            # a rate-limited fetch doesn't silently drop the group.
            got = await _call_with_retry(
                mcp_manager,
                "mcp.paperless.get_document",
                {"document_id": m["id"], "include_content": True},
                truncate=False,
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

    # (dup_groups already holds the pass-1 checksum groups; pass 2 appends to it.)
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
    """Delete one document (rate-limit retried via _call_with_retry). Returns True if
    deleted, False on a non-rate-limit error or after exhausting rate-limit retries."""
    res = await _call_with_retry(mcp_manager, "mcp.paperless.delete_document", {"document_id": doc_id})
    if res.get("deleted"):
        return True
    logger.warning(f"paperless_dedupe: delete_document({doc_id}) failed: {res.get('error')}")
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
        docs, sweep_complete, err = await _gather_all_documents(mcp_manager)
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

        # Only a COMPLETE sweep may claim the archive is clean; a partial sweep (search
        # error / page cap / date-window stall) must disclose it and never report "clean".
        partial_note = (
            ""
            if sweep_complete
            else (
                " ACHTUNG: das Archiv wurde nur TEILWEISE durchsucht (Rate-Limit oder "
                "sehr großer Bestand) — erneut aufrufen, um den Rest zu prüfen"
            )
        )

        def _data(skipped_val: int, deleted: int, remaining: int) -> dict:
            return {
                "documents_scanned": len(docs),
                "sweep_complete": sweep_complete,
                "groups": groups_found,
                "metadata_groups": metadata_groups,
                "duplicate_copies": total_extras,
                "kept": kept,
                "skipped": skipped_val,
                "deleted": deleted,
                "remaining": remaining,
                "dry_run": dry_run,
            }

        if groups_found == 0:
            return {
                "success": True,
                "message": f"Keine Duplikate in den {len(docs)} geprüften Dokumenten gefunden{partial_note}.",
                "action_taken": False,
                "data": _data(skipped, 0, 0),
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
                    f"{kept} Originale bleiben erhalten).{note} Nichts gelöscht "
                    f"(nur_zeigen).{partial_note}"
                ),
                "action_taken": False,
                "data": _data(skipped, 0, total_extras),
            }

        # Delete a rate-limit-safe batch; the rest is left for the next call. A
        # wall-clock budget bounds the loop so one tool call can't block past the agent
        # step timeout under heavy throttling — anything not reached counts as remaining.
        deadline = time.monotonic() + _MAX_DELETE_SECONDS
        deleted_ids: list[int] = []
        delete_failed = 0
        for doc_id in extras[:batch_cap]:
            if time.monotonic() >= deadline:
                break
            if await _delete_one(mcp_manager, doc_id):
                deleted_ids.append(doc_id)
            else:
                delete_failed += 1
        remaining = total_extras - len(deleted_ids)
        total_skipped = skipped + delete_failed  # grouping-time skips + delete failures

        # "clean" ONLY when the full archive was swept, nothing remains, and nothing was
        # skipped — otherwise report honestly and tell the caller to re-run.
        if remaining == 0 and sweep_complete and total_skipped == 0:
            message = (
                f"{len(deleted_ids)} Duplikat(e) in den Papierkorb verschoben — alle "
                f"{groups_found} Gruppen bereinigt, {kept} Originale behalten "
                f"(wiederherstellbar im Papierkorb)."
            )
        else:
            parts = [f"{len(deleted_ids)} Duplikat(e) in den Papierkorb verschoben"]
            if remaining > 0:
                parts.append(
                    f"{remaining} von {total_extras} Kopien verbleiben "
                    "(portionsweise wegen des Paperless-Rate-Limits)"
                )
            if total_skipped:
                parts.append(f"{total_skipped} Dokument(e) konnten nicht geprüft/gelöscht werden")
            if not sweep_complete:
                parts.append("das Archiv wurde nur teilweise durchsucht")
            message = ". ".join(parts) + ". Sag erneut 'räum die Duplikate auf', um fortzufahren."

        return {
            "success": True,
            "message": message,
            "action_taken": bool(deleted_ids),
            "data": {
                **_data(total_skipped, len(deleted_ids), remaining),
                "deleted_ids": deleted_ids,
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
