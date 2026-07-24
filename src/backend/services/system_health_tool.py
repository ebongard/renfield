"""`internal.system_health` — the agent-callable "what's broken right now?" tool.

Renfield's self-observability was split across three surfaces the chat agent could
NOT see: MCP transport health (get_status), the kiosk's internal-subsystem health,
and the ingest-pipeline status tool. None of them caught the 2026-07 class of
failure where a config value and its DB-stored counterpart diverge (a DB wipe left
the folder/email-ingest tokens and the Paperless API token stale → silent 403/401).

This tool bundles those signals PLUS a **config↔state consistency check** (the piece
that actually catches that class) into one read-only, ADMIN-gated answer:

1. config↔state   — a feature is enabled but its DB-stored token/target is missing
                    (would-be-silent misconfiguration — the root cause).
2. paperless      — documents stuck at ``paperless_state='pending'`` (the SYMPTOM of
                    a bad Paperless token — what actually happened).
3. MCP fleet      — get_status() → any server degraded/down.
4. subsystems     — the kiosk presence/knowledge/media health.
5. worker + queue — ingest worker liveness + live backlog.
6. infra          — DB / Redis reachability (Ollama is a shallow init check, matching
                    /health/ready — documented, not a real ping).

Every probe is isolated (one dead probe never blanks the answer). The message is
problems-first German; an all-green run says so. Read-only — it detects & reports,
it does not mutate (the self-heal lives in services/credential_reconciler).
"""
from __future__ import annotations

from typing import Any

from loguru import logger
from sqlalchemy import func, select, text

from models.database import Document, SETTING_EMAIL_INGEST_TOKEN, SETTING_FOLDER_INGEST_TOKEN
from models.permissions import Permission, has_permission
from services.database import AsyncSessionLocal
from services.ingest_common import get_ingest_token
from utils.config import settings

SYSTEM_HEALTH_TOOL: dict = {
    "internal.system_health": {
        "description": (
            "Systemgesundheit / Selbstdiagnose: prüft den LIVE-Betriebszustand und meldet, "
            "was gerade kaputt oder fehlkonfiguriert ist — MCP-Server-Gesundheit, "
            "Präsenz/Wissen/Medien-Subsysteme, Ingest-Worker + Warteschlange, "
            "Paperless-Ablage-Rückstau, Datenbank/Redis-Erreichbarkeit, und Config↔State-"
            "Konsistenz (Feature aktiviert, aber Token/Ziel fehlt). Backt Fragen wie "
            "'was ist kaputt?', 'ist alles gesund?', 'systemstatus', 'health check', "
            "'welche integration funktioniert nicht?', 'warum kommt nichts in Paperless an?'. "
            "Nur-lesend (repariert nicht). Admin-Berechtigung."
        ),
        "parameters": {},
    }
}


def _mark(health: str) -> str:
    return {"healthy": "OK", "degraded": "DEGRADED", "down": "DOWN", "off": "OFF"}.get(
        health, health
    )


async def _check_config_state(db) -> list[str]:
    """Feature enabled but its DB-stored provisioning missing (the root-cause class)."""
    problems: list[str] = []
    checks = [
        (settings.folder_ingest_enabled, SETTING_FOLDER_INGEST_TOKEN, "Folder-Ingest"),
        (settings.email_ingest_enabled, SETTING_EMAIL_INGEST_TOKEN, "Email-Ingest"),
    ]
    for enabled, key, label in checks:
        if not enabled:
            continue
        token = await get_ingest_token(db, key)
        if not token:
            problems.append(
                f"{label} ist aktiviert, aber kein Push-Token in der DB provisioniert "
                "(Pushes werden mit 403 abgelehnt)"
            )
    return problems


async def _check_paperless_backlog(db) -> str | None:
    """Documents stuck pending Paperless filing — the symptom of a bad Paperless token."""
    stuck = (
        await db.execute(
            select(func.count())
            .select_from(Document)
            .where(Document.paperless_state == "pending")
        )
    ).scalar_one()
    if stuck and stuck >= 3:
        return (
            f"{stuck} Dokument(e) hängen bei der Paperless-Ablage fest "
            "(paperless_state=pending) — möglicherweise ein ungültiger Paperless-API-Token"
        )
    return None


def _check_mcp(mcp_manager) -> tuple[list[str], list[str]]:
    """Returns (problem_lines, ok_names) from MCP get_status()."""
    problems: list[str] = []
    ok: list[str] = []
    if mcp_manager is None:
        return problems, ok
    st = mcp_manager.get_status()
    for s in st.get("servers", []):
        name = s.get("name", "?")
        health = s.get("health", "?")
        if health == "healthy":
            ok.append(name)
        else:
            extra = s.get("impaired_code") or s.get("last_error") or ""
            problems.append(f"MCP '{name}': {_mark(health)}" + (f" ({extra})" if extra else ""))
    return problems, ok


async def _check_subsystems() -> list[str]:
    """Kiosk internal-subsystem health (presence/knowledge/media)."""
    from api.websocket.kiosk_data import compute_internal_subsystem_health

    problems: list[str] = []
    for row in await compute_internal_subsystem_health():
        health = row.get("health")
        if health in ("degraded", "down"):
            code = row.get("impaired_code") or ""
            problems.append(
                f"Subsystem '{row.get('id')}': {_mark(health)}" + (f" ({code})" if code else "")
            )
    return problems


async def _check_infra() -> list[str]:
    """DB + Redis reachability (Ollama: shallow, see module docstring)."""
    problems: list[str] = []
    try:
        async with AsyncSessionLocal() as db:
            await db.execute(text("SELECT 1"))
    except Exception as e:  # noqa: BLE001
        problems.append(f"Datenbank nicht erreichbar: {e!s}")
    try:
        from services.redis_client import get_redis

        await get_redis().ping()
    except Exception as e:  # noqa: BLE001
        problems.append(f"Redis nicht erreichbar: {e!s}")
    return problems


async def system_health(
    params: dict,
    mcp_manager: Any = None,
    user_id: int | None = None,
    user_permissions: list[str] | None = None,
) -> dict:
    """Read-only aggregate health report. ADMIN-gated (auth-off/unidentified allowed)."""
    if settings.auth_enabled and user_permissions is not None:
        if not has_permission(user_permissions, Permission.ADMIN):
            return {
                "success": False,
                "message": "Für die Systemdiagnose fehlt die Berechtigung (admin).",
                "action_taken": False,
            }

    problems: list[str] = []
    data: dict = {}

    # Each probe isolated — one failure must not blank the whole answer.
    try:
        async with AsyncSessionLocal() as db:
            cfg = await _check_config_state(db)
            problems += cfg
            pl = await _check_paperless_backlog(db)
            if pl:
                problems.append(pl)
            data["config_state_problems"] = cfg
    except Exception as e:  # noqa: BLE001
        logger.warning(f"system_health: config/paperless probe failed: {e}")

    try:
        mcp_problems, mcp_ok = _check_mcp(mcp_manager)
        problems += mcp_problems
        data["mcp_ok"] = mcp_ok
        data["mcp_problems"] = mcp_problems
    except Exception as e:  # noqa: BLE001
        logger.warning(f"system_health: mcp probe failed: {e}")

    try:
        sub = await _check_subsystems()
        problems += sub
        data["subsystem_problems"] = sub
    except Exception as e:  # noqa: BLE001
        logger.warning(f"system_health: subsystem probe failed: {e}")

    try:
        from services.kb_maintenance_tool import ingest_worker_and_backlog

        worker_alive, backlog = await ingest_worker_and_backlog()
        data["worker_alive"] = worker_alive
        data["ingest_backlog"] = backlog
        if worker_alive is False:
            problems.append("Ingest-Worker ist NICHT erreichbar (Dokumentverarbeitung steht)")
        elif backlog and backlog > 50:
            problems.append(f"Ingest-Rückstau: {backlog} Aufgaben in der Warteschlange")
    except Exception as e:  # noqa: BLE001
        logger.warning(f"system_health: worker probe failed: {e}")

    try:
        problems += await _check_infra()
    except Exception as e:  # noqa: BLE001
        logger.warning(f"system_health: infra probe failed: {e}")

    data["problem_count"] = len(problems)
    if problems:
        message = "Probleme gefunden:\n- " + "\n- ".join(problems)
    else:
        ok = data.get("mcp_ok") or []
        message = "Alle geprüften Systeme sind gesund" + (
            f" (MCP: {', '.join(ok)})." if ok else "."
        )

    return {"success": True, "message": message, "action_taken": True, "data": data}
