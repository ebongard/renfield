"""Paperless search-index health check + self-heal (Fix B).

The 2026-08 re-ingest loop left a Paperless whose full-text (Tantivy) index held
far fewer documents than its database: documents existed but full-text search
could not find them. Nothing told anyone.

**What Paperless offers** (verified against paperless-ngx source, 2026-09):
a full rebuild is only the ``document_index reindex`` management command on the
Paperless host — there is no REST endpoint for it (``/api/tasks/run/`` accepts
only train_classifier / sanity_check). ``/api/status/`` says whether the index
can be *opened*, not whether it is *complete*. ``DocumentViewSet.update`` however
calls ``get_backend().add_or_update(doc)`` unconditionally, so even an EMPTY
partial PATCH re-indexes that single document — and it also always sends
``document_updated``, which runs every enabled "Document Updated" workflow.

* **Detection** lives in the Paperless MCP (``search_index_health``): one page of
  document ids from the database (index-independent) is probed against the index.
  Misses count as a proven degradation when the probe is known to work — because
  another document on the page was found, because a positive control (a recent
  document from page 1) was found in the same call, or because an earlier run
  proved it (``PROOF_KEY``, persisted here). That last two cover the 2026-08 shape:
  an index that lost its OLD documents, where every old page is entirely missing.
* **Healing** is an empty PATCH per missing document, bounded per run, only with
  ``paperless_index_heal_enabled``. The MCP refuses to heal while enabled
  Document-Updated workflows exist (or cannot be verified) unless
  ``paperless_index_heal_allow_workflows`` is set.

**Alerting.** Like ``services/watchdog.py`` this mostly RAISES and lets the
scheduled-task engine's failure streak send ONE ``ops_alert``: on ``index_error``,
on a heal blocked by workflows, on a proven degradation with healing off, and on a
heal that left documents missing that still have attempts left. In those cases the
Redis page cursor stays put, so the next run re-checks the same page and the streak
can build. Documents that exhaust ``paperless_index_heal_max_attempts`` are given
up: excluded from further re-saves, reported by a direct (rate-limited)
``ops_alert``, and the walk moves on — one unindexable document can no longer
freeze the walk or cause hourly re-saves of its neighbours.

Weak signals never act: an ``inconclusive`` result is logged, never raised.
"""
from __future__ import annotations

from typing import Any

from loguru import logger

from services import ops_alert
from services.folder_ingest_paperless import _parse_paperless_result
from services.redis_client import get_redis
from utils.config import settings

TOOL = "mcp.paperless.search_index_health"
CURSOR_KEY = "paperless:index_health:page"
PROOF_KEY = "paperless:index_health:probe_proven"
ATTEMPTS_KEY = "paperless:index_health:heal_attempts"
EXHAUSTED_KEY = "paperless:index_health:heal_exhausted"
UNHEALABLE_ALERT_KEY = "paperless_index:unhealable"

# A proof that the id probe works on this Paperless expires, so a Paperless upgrade
# that changed the query syntax cannot keep flagging a healthy index forever.
PROOF_TTL_SECONDS = 7 * 86400
# Ledger entries of documents deleted in Paperless must not linger forever.
LEDGER_TTL_SECONDS = 30 * 86400
MAX_EXCLUDE_IDS = 1000


def looks_like_index_result(res: dict) -> bool:
    """True iff ``res`` is a real ``search_index_health`` answer. MCPManager
    fuzzy-falls-back an UNKNOWN tool to another paperless tool instead of erroring,
    so an older MCP would hand back a foreign payload — never read that as healthy."""
    return res.get("index_check") is True and isinstance(res.get("verdict"), str)


def _ids(value: Any) -> list[int]:
    out: list[int] = []
    for v in value or []:
        try:
            out.append(int(v))
        except (TypeError, ValueError):
            continue
    return out


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


async def _probe_proven() -> bool:
    try:
        return bool(await get_redis().get(PROOF_KEY))
    except Exception:  # noqa: BLE001 — no proof on record = the conservative default
        return False


async def _remember_proof() -> None:
    try:
        await get_redis().set(PROOF_KEY, "1", ex=PROOF_TTL_SECONDS)
    except Exception as e:  # noqa: BLE001
        logger.debug(f"paperless-index-health: proof write failed ({e})")


async def _exhausted_ids() -> set[int]:
    try:
        return set(_ids(await get_redis().smembers(EXHAUSTED_KEY)))
    except Exception as e:  # noqa: BLE001
        logger.debug(f"paperless-index-health: ledger read failed ({e})")
        return set()


async def _clear_ledger(ids: list[int]) -> None:
    """A document found in (or healed into) the index is no longer a failure."""
    if not ids:
        return
    try:
        r = get_redis()
        keys = [str(i) for i in ids]
        await r.hdel(ATTEMPTS_KEY, *keys)
        await r.srem(EXHAUSTED_KEY, *keys)
    except Exception as e:  # noqa: BLE001
        logger.debug(f"paperless-index-health: ledger clear failed ({e})")


async def _record_failures(ids: list[int]) -> list[int]:
    """Count one more failed heal per id; return the ids that just hit the limit."""
    if not ids:
        return []
    limit = settings.paperless_index_heal_max_attempts
    newly: list[int] = []
    try:
        r = get_redis()
        for i in ids:
            count = int(await r.hincrby(ATTEMPTS_KEY, str(i), 1))
            if count >= limit:
                await r.sadd(EXHAUSTED_KEY, str(i))
                newly.append(i)
        await r.expire(ATTEMPTS_KEY, LEDGER_TTL_SECONDS)
        await r.expire(EXHAUSTED_KEY, LEDGER_TTL_SECONDS)
    except Exception as e:  # noqa: BLE001 — without a ledger the heal just retries
        logger.warning(f"paperless-index-health: ledger write failed ({e})")
    return newly


async def _alert_unhealable(ids: list[int]) -> None:
    """Direct ops_alert for given-up documents. Not a raise: the walk must continue,
    and a single raise would never reach the engine's streak threshold."""
    if not ids or not ops_alert.should_alert(
        UNHEALABLE_ALERT_KEY, realert_seconds=settings.scheduled_task_failure_realert_seconds
    ):
        return
    preview = ", ".join(str(i) for i in sorted(ids)[:20])
    delivered = await ops_alert.notify_admin(
        title="Paperless-Suchindex: Dokumente nicht heilbar",
        message=(
            f"{len(ids)} Dokument(e) fehlen nach "
            f"{settings.paperless_index_heal_max_attempts} Heilversuchen weiterhin im "
            f"Paperless-Suchindex und werden nicht mehr neu gespeichert "
            f"(Paperless-IDs: {preview}). `document_index reindex` auf dem "
            f"Paperless-Host ausführen."
        ),
        dedup_key=UNHEALABLE_ALERT_KEY,
        data={"paperless_ids": sorted(ids)[:50]},
        event_type="ops_health",
        source="paperless_index_health",
    )
    if not delivered:
        ops_alert.clear_alert(UNHEALABLE_ALERT_KEY)  # retry on the next run


async def run_index_health_check(mcp_manager: Any) -> str:
    """Probe one page of the Paperless archive; heal if allowed. Returns a short
    detail line; RAISES ``RuntimeError`` on a condition an operator must see."""
    # Runtime flags are re-read every run (H4).
    heal = bool(settings.paperless_index_heal_enabled)
    page = await _load_cursor()
    proven_before = await _probe_proven()
    exhausted = await _exhausted_ids()

    res = _parse_paperless_result(await mcp_manager.execute_tool(
        TOOL,
        {
            "sample_size": settings.paperless_index_check_sample_size,
            "page": page,
            "heal": heal,
            "max_touch": settings.paperless_index_heal_max_touch,
            "min_age_seconds": settings.paperless_index_check_min_age_seconds,
            "probe_proven": proven_before,
            "exclude_ids": sorted(exhausted)[:MAX_EXCLUDE_IDS],
            "allow_workflows": bool(settings.paperless_index_heal_allow_workflows),
        },
        truncate=False,
        call_timeout=settings.paperless_index_check_call_timeout_s,
    ))
    if res.get("error"):
        raise RuntimeError(f"search_index_health fehlgeschlagen: {res['error']}")
    if not looks_like_index_result(res):
        raise RuntimeError(
            "search_index_health nicht verfügbar (Paperless-MCP veraltet, benötigt >= 1.13.0)"
        )

    verdict = res["verdict"]
    sampled = int(res.get("sampled") or 0)
    missing = int(res.get("missing") or 0)
    healed_ids = _ids(res.get("healed_ids"))
    still_missing = _ids(res.get("still_missing_ids"))
    skipped = _ids(res.get("skipped_ids"))
    excluded = _ids(res.get("excluded_ids"))
    unverified = _ids(res.get("unverified_ids"))
    next_page = res.get("next_page")

    if res.get("probe_proven"):
        await _remember_proof()
    await _clear_ledger(_ids(res.get("found_ids")) + healed_ids)
    newly_exhausted = await _record_failures(still_missing) if res.get("heal_attempted") else []

    detail = f"page={page} verdict={verdict} sampled={sampled} missing={missing}"
    if res.get("heal_attempted"):
        detail += f" touched={int(res.get('touched') or 0)} healed={len(healed_ids)}"
    if skipped:
        detail += f" skipped={len(skipped)}"
    if excluded:
        detail += f" given_up={len(excluded)}"

    if verdict == "index_error":
        # Cursor stays: nothing on this page was judged.
        raise RuntimeError(
            "Paperless meldet, dass der Suchindex nicht geöffnet werden kann — "
            "`document_index reindex` auf dem Paperless-Host ausführen"
        )

    blocked = res.get("heal_blocked")
    if blocked:
        if blocked == "workflows":
            why = (
                f"{int(res.get('blocking_workflows') or 0)} aktive Paperless-Workflow(s) "
                f"mit Auslöser 'Dokument aktualisiert' würden bei jedem Neu-Speichern laufen"
            )
        else:
            why = "die Paperless-Workflows ließen sich nicht prüfen"
        raise RuntimeError(
            f"Selbstheilung des Paperless-Suchindex blockiert: {why} "
            f"({missing} fehlende Dokument(e) auf Seite {page}). Workflows prüfen oder "
            f"PAPERLESS_INDEX_HEAL_ALLOW_WORKFLOWS setzen"
        )

    if verdict == "degraded":
        if not heal:
            raise RuntimeError(
                f"Paperless-Suchindex unvollständig: {missing} von {sampled} geprüften "
                f"Dokumenten fehlen (Seite {page}); Selbstheilung ist aus "
                f"(PAPERLESS_INDEX_HEAL_ENABLED)"
            )
        given_up = sorted(set(excluded) | set(newly_exhausted))
        retryable = [i for i in still_missing if i not in set(newly_exhausted)]
        if given_up:
            await _alert_unhealable(given_up)
        if retryable:
            raise RuntimeError(
                f"Selbstheilung des Paperless-Suchindex wirkungslos: {len(retryable)} "
                f"Dokument(e) fehlen nach dem Neu-Speichern weiterhin (Seite {page}); "
                f"erneuter Versuch beim nächsten Lauf, nach "
                f"{settings.paperless_index_heal_max_attempts} Versuchen aufgegeben"
            )
        accounted = len(healed_ids) + len(still_missing) + len(skipped) + len(excluded)
        if unverified or accounted < missing:
            # The touch budget or the time budget left work on this page.
            logger.warning(f"paperless-index-health: {detail} (page kept)")
            return detail + " (Seite wird fortgesetzt)"
        logger.warning(f"paperless-index-health: degraded page handled — {detail}")

    if verdict == "inconclusive":
        logger.warning(f"paperless-index-health: inconclusive — {detail}: {res.get('message')}")

    await _save_cursor(int(next_page) if next_page else 1)
    return detail
