"""External HTTP watchdog (A3) — "who notices that Renfield itself is gone?"

The 21.5-hour outage of 2026-09-11 is the uncomfortable case: both backends sat
in ``CrashLoopBackOff``. **A system that is down cannot report itself.** So this
module does not watch us — it watches OTHER endpoints, and the peer instance
watches ours. The two deployments are fully independent (separate namespaces,
separate databases), so mutual observation needs no new service.

**No alerting machinery of its own.** The check runs as a scheduled task and
simply RAISES when a target is unreachable, so the failure-streak alerting in
``scheduled_tasks.engine`` provides the threshold, the durable ledger, the
re-alert TTL and the recovery notice. That reuse also settles multi-replica
behaviour for free: the engine holds a per-task advisory lock, so exactly one
replica probes per tick.

**Probe ``/health/ready``, never ``/health``.** The latter is a load-balancer
ping that answers ``{"status": "ok"}`` with a completely dead database — it
would have stayed green through the entire outage this exists to catch.

**Honest limit (A3c).** Mutual watching is silent when both instances die at the
same moment, which is exactly what one ``iscsid`` restart did to both databases.
Only an observer outside the cluster covers that; this is the floor, not the
ceiling.

Configuration is ``WATCHDOG_TARGETS`` — comma-separated ``name=url``. Empty (the
default) makes the whole thing inert, so an instance that has not been given
anything to watch does no work and never alerts.
"""
from __future__ import annotations

from dataclasses import dataclass

import httpx
from loguru import logger

from utils.config import settings


@dataclass(frozen=True)
class WatchdogTarget:
    name: str
    url: str


def parse_targets(raw: str | None) -> list[WatchdogTarget]:
    """Parse ``name=url,name=url`` into targets, skipping malformed entries.

    A malformed entry is logged and dropped rather than raising: one typo in an
    operator's env var must not take down the watch on the other targets.
    """
    targets: list[WatchdogTarget] = []
    seen: set[str] = set()
    for chunk in (raw or "").split(","):
        entry = chunk.strip()
        if not entry:
            continue
        name, sep, url = entry.partition("=")
        name, url = name.strip(), url.strip()
        if not sep or not name or not url:
            logger.warning(f"watchdog: ignoring malformed target '{entry}' (expected name=url)")
            continue
        if not url.startswith(("http://", "https://")):
            logger.warning(f"watchdog: ignoring target '{name}' — url must be http(s)")
            continue
        if name in seen:
            logger.warning(f"watchdog: duplicate target name '{name}' ignored")
            continue
        seen.add(name)
        targets.append(WatchdogTarget(name=name, url=url))
    return targets


async def _check(client: httpx.AsyncClient, target: WatchdogTarget) -> str | None:
    """Probe one target. Returns None when healthy, else a short failure reason.

    Any non-2xx counts as a failure — ``/health/ready`` answers 503 with a JSON
    body naming the broken dependency, and that IS the signal we want.
    """
    try:
        response = await client.get(target.url)
    except Exception as e:  # noqa: BLE001 — unreachable in any form is the signal
        return f"{type(e).__name__}"
    if response.status_code >= 300:
        return f"HTTP {response.status_code}"
    return None


async def run_watchdog() -> str:
    """Probe every configured target once.

    Returns a human-readable detail line on success. **Raises** ``RuntimeError``
    naming the failing targets otherwise — that raise is the whole alerting
    integration (see the module docstring).
    """
    targets = parse_targets(settings.watchdog_targets)
    if not targets:
        return "keine Ziele konfiguriert"

    failures: list[str] = []
    async with httpx.AsyncClient(
        timeout=settings.watchdog_timeout, follow_redirects=False
    ) as client:
        for target in targets:
            reason = await _check(client, target)
            if reason is not None:
                failures.append(f"{target.name} ({reason})")

    if failures:
        # Named in the message so the A2 alert says WHICH target is gone. Note the
        # trade-off recorded in tasks/todo.md: a second target failing during an
        # existing streak does not produce a second alert.
        raise RuntimeError("nicht erreichbar: " + ", ".join(failures))

    return f"{len(targets)} Ziel(e) erreichbar"
