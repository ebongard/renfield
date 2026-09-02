"""Functional health probe for the SearXNG-backed `search` MCP (#1162).

MCP health folds connectivity + tool-presence (``MCPManager.get_status()``): a
SearXNG instance that is UP and exposes ``web_search`` shows GREEN even when every
upstream scraper engine is CAPTCHA-blocked from the datacenter IP and it returns 0
real results. This is the "green-reachable-but-functionally-dead" class already
called out for MCP nodes, applied to search.

A raw result-count check is a false-green trap: Wikipedia (and a few other engines)
answer almost any query, so ``N results > 0`` hides a total scraper outage. The
correct signal is the number of **distinct _general_ engines that contributed** to a
fixed neutral probe, PLUS SearXNG's top-level ``unresponsive_engines`` list. We mark
``degraded`` (never a hard ``down`` — the Bing/Google-CSE backbone still works) when
too few distinct general engines contribute OR when the backbone is unresponsive.

We probe SearXNG DIRECTLY over its JSON API (``settings.searxng_api_url``), NOT
through the third-party MCP, which may strip the per-result engine metadata the
signal depends on. Any error/timeout → ``unknown`` (best-effort, never crashes the
caller). Gated by ``settings.search_functional_probe_enabled`` (dark by default).
"""
from __future__ import annotations

from typing import Any

import httpx
from loguru import logger

from utils.config import settings

# Fixed, neutral, high-recall probe query — a common word every healthy general
# engine answers, so a low contributing-engine count means a real scraper outage,
# not a query that happens to be obscure. Module-level constant, NOT user config.
PROBE_QUERY = "wikipedia"

# The search backbone: if SearXNG reports these as unresponsive, general web search
# is effectively dark regardless of how many niche engines still answer.
BACKBONE_ENGINES = frozenset({"bing", "google", "google-cse", "duckduckgo"})

_PROBE_TIMEOUT_S = 8.0


def _distinct_general_engines(results: list[dict[str, Any]]) -> set[str]:
    """Collect distinct engine names that contributed results.

    SearXNG carries per-result attribution either as ``engines`` (a list, when the
    same result was returned by several engines) or ``engine`` (a scalar). We union
    both shapes. We deliberately do NOT count results with no engine attribution.
    """
    engines: set[str] = set()
    for r in results:
        if not isinstance(r, dict):
            continue
        multi = r.get("engines")
        if isinstance(multi, list):
            engines.update(str(e).lower() for e in multi if e)
        single = r.get("engine")
        if single:
            engines.add(str(single).lower())
    return engines


def _classify(payload: dict[str, Any]) -> dict[str, Any]:
    """Pure verdict logic over a parsed SearXNG JSON payload."""
    results = payload.get("results")
    if not isinstance(results, list):
        results = []
    unresponsive_raw = payload.get("unresponsive_engines") or []
    # unresponsive_engines entries are typically [engine_name, reason]; be liberal.
    unresponsive: list[str] = []
    for entry in unresponsive_raw:
        if isinstance(entry, (list, tuple)) and entry:
            unresponsive.append(str(entry[0]))
        elif entry:
            unresponsive.append(str(entry))

    contributing = _distinct_general_engines(results)
    min_engines = settings.search_functional_min_engines

    backbone_down = BACKBONE_ENGINES.intersection(e.lower() for e in unresponsive)

    if len(contributing) < min_engines:
        verdict = "degraded"
        reason = (
            f"nur {len(contributing)} von mind. {min_engines} allgemeinen Engines "
            "haben zur Testsuche beigetragen (Scraper vermutlich CAPTCHA-blockiert)"
        )
    elif backbone_down:
        verdict = "degraded"
        reason = (
            "Suchmaschinen-Backbone nicht erreichbar: "
            + ", ".join(sorted(backbone_down))
        )
    else:
        verdict = "healthy"
        reason = f"{len(contributing)} allgemeine Engines aktiv"

    return {
        "verdict": verdict,
        "contributing_engines": len(contributing),
        "unresponsive": unresponsive,
        "reason": reason,
    }


async def probe_search_functional() -> dict[str, Any]:
    """Probe SearXNG's JSON API and return a functional verdict.

    Returns ``{verdict, contributing_engines, unresponsive, reason}`` where verdict is
    ``"healthy"``/``"degraded"``/``"unknown"``. Never raises: any error (probe disabled,
    no configured URL, HTTP/timeout/parse failure) → ``"unknown"``.
    """
    if not settings.search_functional_probe_enabled:
        return {
            "verdict": "unknown",
            "contributing_engines": 0,
            "unresponsive": [],
            "reason": "Funktionaler Suchtest deaktiviert",
        }

    base = settings.searxng_api_url
    if not base:
        return {
            "verdict": "unknown",
            "contributing_engines": 0,
            "unresponsive": [],
            "reason": "Keine SearXNG-URL konfiguriert",
        }

    url = base.rstrip("/") + "/search"
    params = {"q": PROBE_QUERY, "format": "json", "categories": "general"}
    try:
        async with httpx.AsyncClient(timeout=_PROBE_TIMEOUT_S) as client:
            resp = await client.get(url, params=params)
            resp.raise_for_status()
            payload = resp.json()
        if not isinstance(payload, dict):
            raise ValueError("SearXNG-Antwort ist kein JSON-Objekt")
        return _classify(payload)
    except Exception as e:  # noqa: BLE001 — best-effort, never crash the caller
        logger.warning(f"search_health: functional probe failed: {e}")
        return {
            "verdict": "unknown",
            "contributing_engines": 0,
            "unresponsive": [],
            "reason": f"Testsuche fehlgeschlagen: {e!s}",
        }
