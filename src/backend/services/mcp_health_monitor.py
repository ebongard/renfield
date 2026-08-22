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

from utils.config import settings

# Plane-B reports keyed by "source" (the reporting MCP), latest event only.
_reports: dict[str, dict[str, Any]] = {}
# Alert ledger: issue-key → last-alert monotonic time. Re-alert only after the TTL,
# so an ongoing problem doesn't spam but a recurrence (or a new problem) does.
_alerted: dict[str, float] = {}


def _should_alert(key: str) -> bool:
    """True if this issue-key hasn't been alerted, or the re-alert TTL has elapsed."""
    now = time.monotonic()
    last = _alerted.get(key)
    if last is None or (now - last) >= settings.mcp_health_realert_seconds:
        _alerted[key] = now
        return True
    return False


def _clear_alert(key: str) -> None:
    _alerted.pop(key, None)


async def _resolve_admin_user_id(db) -> int | None:
    """The ops target for a health alert = the owner admin (lowest-id active user
    whose role grants ADMIN), falling back to the first user by id. Returns None on
    an empty users table (auth-off single-user install — the notification then isn't
    per-user-scoped, which is correct for that mode)."""
    try:
        from sqlalchemy import select

        from models.database import User
        from services.auth_service import active_admin_ids

        admin_ids = await active_admin_ids(db)
        if admin_ids:
            return min(admin_ids)
        # No admin-granting role (auth-off / dev): fall back to the first user.
        return (
            await db.execute(select(User.id).order_by(User.id).limit(1))
        ).scalar_one_or_none()
    except Exception:  # noqa: BLE001
        return None


async def _notify(title: str, message: str, dedup_key: str, data: dict) -> None:
    """Fire ONE privacy-aware proactive notification to the admin/owner. Best-effort:
    a dedup/suppression ValueError or any failure is swallowed (never breaks a tick)."""
    if not settings.proactive_enabled:
        return
    try:
        from services.database import AsyncSessionLocal
        from services.notification_service import NotificationService

        # Dedicated session — process_webhook commits its own db.
        async with AsyncSessionLocal() as db:
            target = await _resolve_admin_user_id(db)
            await NotificationService(db).process_webhook(
                event_type="mcp_health",
                title=title,
                message=message,
                urgency="critical",
                source="mcp_health_monitor",
                privacy="personal",
                target_user_id=target,
                data={"dedup_key": dedup_key, **data},
            )
    except ValueError:
        pass  # deduped / suppressed by the notification pipeline
    except Exception as e:  # noqa: BLE001
        logger.warning(f"mcp_health: notify failed for {dedup_key}: {e}")


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

    current_problems: set[str] = set()
    for srv in status.get("servers", []):
        name = srv.get("name")
        health = srv.get("health")
        if health in ("degraded", "down"):
            key = f"planea:{name}:{health}"
            current_problems.add(key)
            if _should_alert(key):
                reason = srv.get("impaired_code") or srv.get("last_error") or health
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
    for key in [k for k in _alerted if k.startswith("planea:")]:
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
