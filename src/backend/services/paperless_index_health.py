"""Paperless search-index health check + self-heal (Fix B).

The 2026-08 re-ingest loop left a Paperless whose full-text (Tantivy) index held
far fewer documents than its database: documents existed but full-text search
could not find them. Nothing told anyone.

**What Paperless offers** (verified against paperless-ngx source, 2026-09):
a full rebuild is only the ``document_index reindex`` management command on the
Paperless host — there is no REST endpoint for it (``/api/tasks/run/`` accepts
only train_classifier / sanity_check). ``/api/status/`` says whether the index
can be *opened*, not whether it is *complete*. A document PATCH, however,
re-indexes that single document. So:

* **Detection** lives in the Paperless MCP (``search_index_health``): one page of
  document ids from the database (index-independent) is probed against the index
  id by id. A miss only counts as a proven degradation when other documents on the
  same page WERE found (so the probe itself is known to work).
* **Healing** is a non-destructive re-save (PATCH of the unchanged title), bounded
  per run, and only when ``paperless_index_heal_enabled`` is on.

**No alerting of its own** (same pattern as ``services/watchdog.py``): this
function RAISES when the index is proven degraded and not healed, when healing
had no effect, or when Paperless reports the index cannot be opened. The
scheduled-task engine turns a streak of those raises into ONE ``ops_alert`` to the
owner admin, with re-alert TTL and a recovery notice.

**Walking the archive.** A Redis cursor holds the next page. It advances only past
a page that is not left degraded, so an unhealed degradation re-checks the SAME
page each run — the repeated raise is what builds the failure streak. A lost
cursor (Redis flush) just restarts at page 1.

Weak signals never act: an ``inconclusive`` result (nothing found at all, a probe
Paperless rejects, a sample cut short) is logged and reported, never raised and
never healed beyond the MCP's single canary re-save.
"""
from __future__ import annotations

from typing import Any

from loguru import logger

from services.folder_ingest_paperless import _parse_paperless_result
from services.redis_client import get_redis
from utils.config import settings

TOOL = "mcp.paperless.search_index_health"
CURSOR_KEY = "paperless:index_health:page"


def looks_like_index_result(res: dict) -> bool:
    """True iff ``res`` is a real ``search_index_health`` answer. MCPManager
    fuzzy-falls-back an UNKNOWN tool to another paperless tool instead of erroring,
    so an older MCP would hand back a foreign payload — never read that as healthy."""
    return res.get("index_check") is True and isinstance(res.get("verdict"), str)


async def _load_cursor() -> int:
    try:
        raw = await get_redis().get(CURSOR_KEY)
        page = int(raw) if raw is not None else 1
    except Exception as e:  # noqa: BLE001 — a lost cursor only restarts the walk
        logger.debug(f"paperless-index-health: cursor read failed ({e}); page 1")
        return 1
    return page if page >= 1 else 1


async def _save_cursor(page: int) -> None:
    try:
        await get_redis().set(CURSOR_KEY, str(page))
    except Exception as e:  # noqa: BLE001
        logger.debug(f"paperless-index-health: cursor write failed ({e})")


async def run_index_health_check(mcp_manager: Any) -> str:
    """Probe one page of the Paperless archive; heal if allowed. Returns a short
    detail line; RAISES ``RuntimeError`` on a condition an operator must see."""
    heal = bool(settings.paperless_index_heal_enabled)  # re-read every run (H4)
    page = await _load_cursor()

    res = _parse_paperless_result(await mcp_manager.execute_tool(
        TOOL,
        {
            "sample_size": settings.paperless_index_check_sample_size,
            "page": page,
            "heal": heal,
            "max_touch": settings.paperless_index_heal_max_touch,
            "min_age_seconds": settings.paperless_index_check_min_age_seconds,
        },
        truncate=False,
        call_timeout=settings.paperless_index_check_call_timeout_s,
    ))
    if res.get("error"):
        raise RuntimeError(f"search_index_health fehlgeschlagen: {res['error']}")
    if not looks_like_index_result(res):
        raise RuntimeError(
            "search_index_health nicht verfügbar (Paperless-MCP veraltet / Fuzzy-Fallback)"
        )

    verdict = res["verdict"]
    sampled = int(res.get("sampled") or 0)
    missing = int(res.get("missing") or 0)
    healed = int(res.get("healed") or 0)
    still_missing = list(res.get("still_missing_ids") or [])
    next_page = res.get("next_page")
    detail = f"page={page} verdict={verdict} sampled={sampled} missing={missing}"
    if res.get("heal_attempted"):
        detail += f" touched={int(res.get('touched') or 0)} healed={healed}"

    if verdict == "index_error":
        # Cursor stays: nothing on this page was judged.
        raise RuntimeError(
            "Paperless meldet, dass der Suchindex nicht geöffnet werden kann — "
            "`document_index reindex` auf dem Paperless-Host ausführen"
        )

    if verdict == "degraded":
        if not heal:
            raise RuntimeError(
                f"Paperless-Suchindex unvollständig: {missing} von {sampled} geprüften "
                f"Dokumenten fehlen (Seite {page}); Selbstheilung ist aus "
                f"(PAPERLESS_INDEX_HEAL_ENABLED)"
            )
        if still_missing:
            raise RuntimeError(
                f"Selbstheilung des Paperless-Suchindex wirkungslos: {len(still_missing)} "
                f"Dokument(e) fehlen nach dem Neu-Speichern weiterhin (Seite {page}) — "
                f"`document_index reindex` auf dem Paperless-Host prüfen"
            )
        if healed < missing:
            # Progress, but the per-run touch budget left misses on this page:
            # stay on it so the next run continues here.
            logger.warning(f"paperless-index-health: {detail} (budget exhausted, page kept)")
            return detail + " (Seite wird fortgesetzt)"
        logger.warning(f"paperless-index-health: healed a degraded page — {detail}")

    if verdict == "inconclusive":
        logger.warning(f"paperless-index-health: inconclusive — {detail}: {res.get('message')}")

    await _save_cursor(int(next_page) if next_page else 1)
    return detail
