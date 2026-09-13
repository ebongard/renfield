"""MCP health self-detection + proactive alerting (Phase 1).

The gap this closes: an MCP failure that renfield already *knows about* but never
*surfaces* — so the user has to notice the symptom (e.g. "why isn't the import
running?") and go digging in logs. Two sources feed it:

* **Plane-A** (MCP client connections): read ``MCPManager.get_status()`` — any
  server whose folded ``health`` is ``degraded``/``down``. This already captures
  disconnect + plugin-load + no-tools; it does NOT yet capture "connected but the
  upstream resource is dead" (that's Phase 2 functional health).
* **Plane-B** (ingest push MCPs — filesystem / email-ingest): they DETECT their own
  failures (SMB-auth, IMAP-drop, retry-exhausted, fatal token) and fire an
  ``OPERATOR-NOTIFY`` that, without a webhook, dead-ends in container logs. We point
  that webhook at ``POST /api/mcp-health/report`` → ``ingest_report`` here.

Both converge on ONE user-facing action: a **privacy-aware proactive notification**
to the admin/owner (``NotificationService.process_webhook``), deduped by a per-issue
ledger with a re-alert TTL so an ongoing problem doesn't spam but a recurrence still
alerts. Detect-and-notify ONLY — healing is Phase 2 (mirrors the ``system_health`` /
``credential_reconciler`` split: this module never mutates an MCP).
"""
from __future__ import annotations

import asyncio
import time
from typing import Any

from loguru import logger

from services import ops_alert
from utils.config import settings

# Plane-B reports keyed by "source" (the reporting MCP), latest event only.
_reports: dict[str, dict[str, Any]] = {}


# The alert ledger + admin resolution + delivery now live in services/ops_alert.py
# (shared with the scheduled-task failure alerts and the HTTP watchdog — ONE alert
# path, ONE admin resolution). These thin delegates keep the call sites here
# readable and remain the seam tests monkeypatch.
def _should_alert(key: str) -> bool:
    """True if this issue-key hasn't been alerted, or the re-alert TTL has elapsed."""
    return ops_alert.should_alert(key)


def _clear_alert(key: str) -> None:
    ops_alert.clear_alert(key)


async def _notify(title: str, message: str, dedup_key: str, data: dict) -> None:
    """Fire ONE privacy-aware proactive notification to the admin/owner."""
    await ops_alert.notify_admin(
        title=title,
        message=message,
        dedup_key=dedup_key,
        data=data,
        event_type="mcp_health",
        source="mcp_health_monitor",
    )


# --- Plane-B: ingest MCPs push their own failures here -----------------------

_PLANE_B_EVENT_LABELS = {
    "fatal": "hat einen fatalen Fehler",
    "disconnect": "hat die Verbindung verloren",
    "failure": "kann Dateien nicht verarbeiten",
}


async def ingest_report(payload: dict[str, Any]) -> None:
    """Record + alert on an OPERATOR-NOTIFY pushed by an ingest MCP.

    Payload (WebhookNotifier): ``{source, event, reason, root?, relpath?}``. Stored
    as the source's latest health event; a NEW/changed/expired problem fires a
    proactive alert. Recovery isn't reported by the MCP, so the ledger TTL is what
    eventually re-arms a repeat alert."""
    source = str(payload.get("source") or "unknown-mcp")
    event = str(payload.get("event") or "failure")
    reason = str(payload.get("reason") or "")
    root = payload.get("root")
    _reports[source] = {
        "source": source,
        "event": event,
        "reason": reason,
        "root": root,
        "relpath": payload.get("relpath"),
        "at": time.time(),
    }
    if not settings.mcp_health_monitor_enabled:
        return
    key = f"planeb:{source}:{event}:{root or ''}:{reason}"
    if not _should_alert(key):
        return
    label = _PLANE_B_EVENT_LABELS.get(event, "meldet einen Fehler")
    where = f" ({root})" if root else ""
    await _notify(
        title="Dokument-Import gestört",
        message=f"{source}{where} {label}: {reason or event}. Dokumente werden ggf. nicht importiert.",
        dedup_key=key,
        data={"plane": "B", "source": source, "event": event, "reason": reason, "root": root},
    )


# --- Plane-A: poll MCPManager.get_status() -----------------------------------

# Fixed share of the probe hang-guard floor beyond the 3x transport/init/list
# window: probe (2s) + bounded teardown (5s) + re-probe (2s), padded. Module
# constant (not config) so tests can shrink it.
_PROBE_GUARD_OVERHEAD_S = 15.0


async def _self_heal(mcp_manager, problem_names: list[str]) -> set[str]:
    """Phase 2 self-heal: actively probe each degraded/down server, which drives a
    single-shot reconnect (``probe_server``). Returns the set of names we attempted
    (so the alert pass can say "Selbstheilung versucht"). The caller re-reads
    get_status() afterwards, so a server the reconnect fixed simply drops out of the
    problem set and never alerts — while a plugin_failed/upstream-dead server that a
    reconnect can't fix stays degraded and alerts."""
    attempted: set[str] = set()
    probe = getattr(mcp_manager, "probe_server", None)
    if not (settings.mcp_health_self_heal_enabled and callable(probe)):
        return attempted
    healed = 0
    # The guard must stay ABOVE a worst-case honest reconnect (probe + bounded
    # teardown + transport/init/tools-list at mcp_connect_timeout each + re-probe),
    # else raising MCP_CONNECT_TIMEOUT alone would make every slow-but-honest
    # heal get cancelled mid-connect and self-heal permanently fail. The floor
    # tracks the connect timeout so the two knobs can't be misconfigured apart.
    probe_guard_s = max(
        settings.mcp_health_self_heal_probe_timeout,
        settings.mcp_connect_timeout * 3 + _PROBE_GUARD_OVERHEAD_S,
    )
    for name in problem_names[: settings.mcp_health_self_heal_max_per_tick]:
        try:
            # Hard hang-guard: the probe drives a reconnect, and a wedged
            # transport used to hang here forever — freezing the WHOLE monitor
            # loop (the scheduler awaits each tick), so a down server never
            # alerted and never healed (#1107). asyncio.timeout keeps the
            # cancellation in this task (anyio-scope-safe).
            async with asyncio.timeout(probe_guard_s):
                res = await probe(name)
            attempted.add(name)
            if isinstance(res, dict) and res.get("ok"):
                healed += 1
        except TimeoutError:
            attempted.add(name)  # we DID try — the alert may say "Selbstheilung versucht"
            logger.warning(
                f"mcp_health: self-heal probe for '{name}' exceeded "
                f"{probe_guard_s:.0f}s hang-guard — aborted"
            )
        except Exception as e:  # noqa: BLE001 — a probe failure must not break the tick
            logger.warning(f"mcp_health: self-heal probe failed for '{name}': {e}")
    if attempted:
        logger.info(
            f"mcp_health: self-heal probed {len(attempted)} server(s), "
            f"{healed} recovered on reconnect"
        )
    return attempted


async def _bespoke_probe_search() -> tuple[bool, str | None]:
    """The `search` server's purpose-built probe (services/search_health.py, #1162).

    A generic "did the tool return rows" probe is a DOCUMENTED false-green here:
    Wikipedia answers almost any query, so a non-empty result set hides a total
    scraper outage. That module counts distinct contributing general engines
    instead — the right signal, already written, already tested. It just had no way
    to reach anyone: its verdict surfaced only in `internal.system_health`, i.e.
    only if a human thought to ask. This is the wire.

    `unknown` (probe disabled, no URL, HTTP failure) yields no verdict at all —
    absence of evidence must not read as evidence of failure.
    """
    from services.search_health import probe_search_functional

    result = await probe_search_functional()
    verdict = result.get("verdict")
    if verdict == "unknown":
        return True, None  # no signal; caller skips recording
    return verdict == "healthy", result.get("reason")


# Servers whose health cannot be judged by a generic tool call. Name → coroutine
# returning (ok, detail). Checked BEFORE the YAML stanza, so a bespoke probe always
# wins; a server listed here needs no `health_probe` in mcp_servers.yaml.
_BESPOKE_PROBES = {
    "search": _bespoke_probe_search,
}


def _probe_detail(mcp_manager, name: str) -> str | None:
    """The last probe's failure reason for a server, if we still have it."""
    try:
        state = getattr(mcp_manager, "_servers", {}).get(name)
        return getattr(state, "last_probe_detail", None) if state else None
    except Exception:  # noqa: BLE001
        return None


async def _run_probes(mcp_manager) -> list[str]:
    """Run the due functional probes. Returns the names actually probed.

    Runs AFTER the self-heal pass on purpose: a wedged session is reconnected
    first, so the probe judges the SERVICE on a fresh transport rather than
    re-reporting a transport problem the heal already fixed.
    """
    if not settings.mcp_health_probe_enabled:
        return []
    # Duck-type the manager the same way _self_heal does with probe_server: a
    # manager that does not support probing is skipped, not assumed. Without this
    # the pass would crash on any stand-in that answers every attribute.
    due_fn = getattr(mcp_manager, "health_probe_due", None)
    record_fn = getattr(mcp_manager, "record_external_probe", None)
    if not callable(due_fn):
        return []
    try:
        due = due_fn()
    except Exception as e:  # noqa: BLE001
        logger.warning(f"mcp_health: probe due-check failed: {e}")
        return []
    if not isinstance(due, list):
        return []

    # Bespoke-probe servers are due on the same cadence but have no YAML stanza, so
    # health_probe_due() does not list them. Fold them in, respecting the interval.
    if callable(record_fn):
        servers = getattr(mcp_manager, "_servers", None)
        for name in _BESPOKE_PROBES:
            state = servers.get(name) if isinstance(servers, dict) else None
            if state is None or not getattr(state, "connected", False) or name in due:
                continue
            last = getattr(state, "last_probe_at", 0.0)
            if not isinstance(last, (int, float)):
                continue
            if last and (time.monotonic() - last) < settings.mcp_health_probe_interval:
                continue
            due.append(name)

    probed: list[str] = []
    for name in due[: settings.mcp_health_probe_max_per_tick]:
        try:
            # Same hang-guard discipline as the self-heal pass (#1107): the call has
            # its own timeout, but a wedged transport can hang elsewhere and a frozen
            # monitor loop is the failure mode we already paid for once.
            async with asyncio.timeout(settings.mcp_health_probe_guard_timeout):
                bespoke = _BESPOKE_PROBES.get(name)
                if bespoke is not None:
                    ok, detail = await bespoke()
                    if ok and detail is None:
                        # "unknown" — no evidence either way, record nothing.
                        continue
                    record_fn(name, ok, detail)
                else:
                    await mcp_manager.run_health_probe(name)
            probed.append(name)
        except TimeoutError:
            logger.warning(
                f"mcp_health: probe for '{name}' exceeded "
                f"{settings.mcp_health_probe_guard_timeout:.0f}s hang-guard — aborted"
            )
        except Exception as e:  # noqa: BLE001 — a probe must never break the tick
            logger.warning(f"mcp_health: probe failed for '{name}': {e}")
    if probed:
        logger.debug(f"mcp_health: probed {len(probed)} server(s): {', '.join(probed)}")
    return probed


async def monitor_tick(app) -> None:
    """One poll of the MCP client fleet: self-heal (probe+reconnect) degraded/down
    servers, then alert on those STILL broken (NEW problems only), and clear the
    ledger on recovery (so a later re-failure re-alerts promptly)."""
    if not settings.mcp_health_monitor_enabled:
        return
    mcp_manager = getattr(app.state, "mcp_manager", None)
    if mcp_manager is None:
        return
    # Tick heartbeat (#1107): a stuck monitor loop used to be indistinguishable
    # from "all healthy". The COUNTER ticks in the finally (so a live loop whose
    # get_status keeps failing still reads as alive — WARNs tell the rest), the
    # problem GAUGE is only set when a tick actually completed with a verdict.
    try:
        await _monitor_tick_body(mcp_manager)
    finally:
        from utils.metrics import record_mcp_health_tick

        record_mcp_health_tick(_last_tick_problem_count)


_last_tick_problem_count: int | None = None


async def _monitor_tick_body(mcp_manager) -> None:
    global _last_tick_problem_count
    _last_tick_problem_count = None
    try:
        status = mcp_manager.get_status()
    except Exception as e:  # noqa: BLE001
        logger.warning(f"mcp_health: get_status failed: {e}")
        return

    # Self-heal pass: probe+reconnect the problem servers, then re-read health so we
    # only alert on the ones the self-heal could NOT fix. Skip `plugin_failed` — a
    # reconnect provably can't reload a failed startup plugin, so probing it just
    # wastes an RPC and would make the alert falsely claim "Selbstheilung versucht".
    problem_names = [
        s.get("name")
        for s in status.get("servers", [])
        if s.get("health") in ("degraded", "down")
        and s.get("impaired_code") != "plugin_failed"
    ]
    healed_attempted: set[str] = set()
    if problem_names:
        healed_attempted = await _self_heal(mcp_manager, problem_names)
        if healed_attempted:
            try:
                status = mcp_manager.get_status()  # post-heal health
            except Exception as e:  # noqa: BLE001
                logger.warning(f"mcp_health: post-heal get_status failed: {e}")

    # Functional probes (A1) run AFTER the self-heal so they judge the SERVICE on a
    # freshly reconnected transport, then the health is re-read so the verdicts they
    # just wrote reach the SAME alert pass everything else uses. No second alert path.
    probed = await _run_probes(mcp_manager)
    if probed:
        try:
            status = mcp_manager.get_status()  # post-probe health
        except Exception as e:  # noqa: BLE001
            logger.warning(f"mcp_health: post-probe get_status failed: {e}")

    current_problems: set[str] = set()
    for srv in status.get("servers", []):
        name = srv.get("name")
        health = srv.get("health")
        if health in ("degraded", "down"):
            key = f"planea:{name}:{health}"
            current_problems.add(key)
            if _should_alert(key):
                reason = srv.get("impaired_code") or srv.get("last_error") or health
                if srv.get("impaired_code") == "probe_failed":
                    # Say WHAT failed, not just that something did — "Funktionstest
                    # fehlgeschlagen" alone would send the reader back to the logs,
                    # which is the dead end this whole feature exists to close.
                    detail = _probe_detail(mcp_manager, name)
                    reason = f"Funktionstest fehlgeschlagen{f': {detail}' if detail else ''}"
                verb = "ist nicht erreichbar" if health == "down" else "ist eingeschränkt"
                tried = " (Selbstheilung versucht, ohne Erfolg)" if name in healed_attempted else ""
                await _notify(
                    title=f"MCP-Dienst {name} {verb}",
                    message=(
                        f"Der MCP-Dienst '{name}' {verb} ({reason}){tried}. "
                        "Betroffene Funktionen können ausfallen."
                    ),
                    dedup_key=key,
                    data={
                        "plane": "A", "server": name, "health": health,
                        "reason": reason, "self_heal_attempted": name in healed_attempted,
                    },
                )
    # Recovery: any Plane-A ledger key no longer a current problem → clear it.
    for key in ops_alert.alerted_keys("planea:"):
        if key not in current_problems:
            _clear_alert(key)

    _last_tick_problem_count = len(current_problems)
    logger.debug(
        f"mcp_health: tick complete — {len(status.get('servers', []))} servers, "
        f"{len(current_problems)} problem(s), {len(healed_attempted)} heal attempt(s)"
    )


# A Plane-B report older than this with no newer one is treated as recovered — the
# ingest MCPs re-fire OPERATOR-NOTIFY on an ONGOING failure (per file / per reconnect
# attempt), so silence means the problem cleared. They never send an explicit
# "recovered" signal, so freshness is the only recovery proxy.
_PLANE_B_STALE_SECONDS = 900.0


def plane_b_reports(fresh_only: bool = True) -> list[dict[str, Any]]:
    """The latest failure report pushed by each ingest MCP (Plane-B). Read-only —
    ``internal.system_health`` surfaces these (they never reach get_status()).
    ``fresh_only`` drops reports older than the staleness window (assumed recovered)."""
    if not fresh_only:
        return list(_reports.values())
    cutoff = time.time() - _PLANE_B_STALE_SECONDS
    return [r for r in _reports.values() if r.get("at", 0) >= cutoff]
