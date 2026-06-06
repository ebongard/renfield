"""Obligation → calendar reconciler (Calendar MCP).

Mirrors each user's OPEN obligations into their chosen calendar as events, and
keeps them in step: create on first sight, update when the date/summary changes,
delete when the obligation is handled (confirmed), drops out of the window, or
its fact is purged. Per-user opt-in — only users with an
``obligation_calendar_pref`` row are synced.

Design (mirrors the notifier/digest):
- Obligations are the source of truth; this is a stateless reconciler over the
  ``obligation_calendar_events`` ledger (fact → calendar event_id), so it is
  idempotent and restart-safe (run_at_boot).
- Owner-scoped: events are written to the obligation OWNER's calendar via the
  Calendar MCP, which enforces per-calendar write access by ``user_id``.
- Per-user advisory lock so two passes don't double-create.
- All-day events aren't supported by the MCP, so events are timed at
  ``obligation_calendar_event_hour``.
- MCP failures are non-fatal: the op is skipped and retried next pass (a failed
  create writes no ledger row; a failed delete keeps the row), so the ledger
  never claims a calendar state that didn't happen. A delete that reports the
  event already gone is treated as success (idempotent teardown).
- Turning sync OFF (clearing the calendar pref) tears the user's events down
  synchronously first (the route calls :meth:`teardown_user` before forgetting
  the event ids) — otherwise the reconciler, which keys off the pref, would
  never see the user again and the events would linger.

Known limitations (documented, not bugs):
- The MCP exposes no idempotency key, so a crash in the narrow window between a
  successful ``create_event`` and the ledger commit can leave a duplicate event
  (at-least-once, like the notifier). A pre-create marker-scan or MCP idempotency
  would close it — follow-up in TODOS.
- Events are timed at ``obligation_calendar_event_hour`` (all-day unsupported);
  the naive datetime is interpreted in the calendar backend's timezone (Europe/
  Berlin for the Google backend).
- A pref pointing at a calendar the user can no longer write to errors every
  pass (logged); it never self-heals until the pref is changed.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Any

from loguru import logger
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from models.database import (
    DOC_FACT_CATEGORY_OBLIGATION,
    OBLIGATION_MILESTONE_CONFIRMED,
    ObligationCalendarEvent,
)
from services.kg_reconciler_service import _resolve_lock_engine
from utils.config import settings

# pg_advisory_lock namespace (int4); objid is the user_id. Distinct from the
# notifier (0x4F42), digest (0x4F44), and KG reconciler (0x4B47).
_CAL_LOCK_NS = 0x4F43  # "OC"

_CREATE = "mcp.calendar.create_event"
_UPDATE = "mcp.calendar.update_event"
_DELETE = "mcp.calendar.delete_event"
# The reconciler acts on behalf of the obligation owner; per-calendar access is
# enforced by the MCP via user_id regardless of this list.
_PERMS = ["mcp.calendar.read", "mcp.calendar.manage"]


@dataclass
class CalSyncReport:
    user_id: int
    created: int = 0
    updated: int = 0
    deleted: int = 0
    errors: int = 0
    skipped_no_pref: bool = False
    notes: list[str] = field(default_factory=list)


def _summary(kind: str, amount: Any, currency: str | None, legal: bool) -> str:
    s = f"Frist: {kind}"
    if amount is not None:
        cur = (currency or "").strip()
        s += f" ({amount}{(' ' + cur) if cur else ''})"
    if legal:
        s = "⚠ " + s
    return s


def _event_times(d: date) -> tuple[str, str]:
    """(start, end) ISO strings for the (timed) event on date ``d`` — all-day
    isn't supported by the MCP, so place it at the configured hour for 30 min."""
    hour = min(23, max(0, settings.obligation_calendar_event_hour))
    start = datetime.combine(d, time(hour, 0))
    return start.isoformat(), (start + timedelta(minutes=30)).isoformat()


class ObligationCalendarSync:
    def __init__(self, db: AsyncSession, mcp_manager: Any):
        self.db = db
        self.mcp = mcp_manager

    # --- preferences -------------------------------------------------------
    async def get_pref(self, user_id: int) -> str | None:
        row = (await self.db.execute(
            text("SELECT calendar_name FROM obligation_calendar_pref WHERE user_id = :u"),
            {"u": user_id},
        )).first()
        return row[0] if row else None

    async def list_pref_user_ids(self) -> list[int]:
        rows = await self.db.execute(text("SELECT user_id FROM obligation_calendar_pref"))
        return [int(r[0]) for r in rows.fetchall()]

    # --- MCP call helper ---------------------------------------------------
    async def _mcp_event(self, tool: str, args: dict[str, Any], user_id: int) -> dict | None:
        """Call a calendar MCP tool; return the inner ``event`` dict (with ``id``)
        on success, or None on any failure (logged). delete returns no event, so
        callers treat a non-None / truthy result as success there."""
        if self.mcp is None:
            return None
        try:
            res = await self.mcp.execute_tool(
                tool, args, user_permissions=_PERMS, user_id=user_id,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning(f"calendar sync: {tool} raised: {e}")
            return None
        if not res or not res.get("success"):
            logger.warning(f"calendar sync: {tool} failed: {res.get('message') if res else 'no result'}")
            return None
        try:
            inner = json.loads(res.get("message") or "{}")
        except (json.JSONDecodeError, TypeError):
            return None
        if not isinstance(inner, dict) or inner.get("error") or inner.get("success") is False:
            logger.warning(f"calendar sync: {tool} inner error: {inner}")
            return None
        # create/update → {"success":true,"event":{...}}; delete → {"success":true,"message":...}
        return inner.get("event", inner)

    # --- reconcile ---------------------------------------------------------
    async def run_for_user(self, user_id: int, *, today: date | None = None) -> CalSyncReport:
        report = CalSyncReport(user_id=user_id)
        today = today or date.today()
        dialect = self.db.bind.dialect.name if self.db.bind is not None else ""
        if dialect != "postgresql":
            return await self._reconcile(user_id, today, report)
        lock_engine = _resolve_lock_engine(self.db.bind)
        if lock_engine is None:
            return await self._reconcile(user_id, today, report)
        async with lock_engine.connect() as lock_conn:
            got = (await lock_conn.execute(
                text("SELECT pg_try_advisory_lock(:ns, :uid)"),
                {"ns": _CAL_LOCK_NS, "uid": user_id},
            )).scalar()
            if not got:
                report.notes.append("skipped: another calendar-sync run holds this user's lock")
                return report
            try:
                return await self._reconcile(user_id, today, report)
            finally:
                await lock_conn.execute(
                    text("SELECT pg_advisory_unlock(:ns, :uid)"),
                    {"ns": _CAL_LOCK_NS, "uid": user_id},
                )

    async def _reconcile(self, user_id: int, today: date, report: CalSyncReport) -> CalSyncReport:
        calendar = await self.get_pref(user_id)
        if not calendar:
            report.skipped_no_pref = True
            return report

        horizon = max(0, settings.obligation_calendar_horizon_days)
        retain = max(0, settings.obligation_calendar_retain_past_days)
        min_date, max_date = today - timedelta(days=retain), today + timedelta(days=horizon)

        # Desired set: the owner's open (un-confirmed) dated obligations in window.
        rows = (await self.db.execute(
            text(
                "SELECT df.id, df.kind, df.obligation_date, df.legal_gate, "
                "       df.amount_value, df.amount_currency, df.value "
                "FROM document_facts df JOIN atoms a ON a.atom_id = df.atom_id "
                "WHERE a.owner_user_id = :uid AND df.category = :ob "
                "  AND df.obligation_date IS NOT NULL "
                "  AND df.obligation_date >= :min_date AND df.obligation_date <= :max_date "
                "  AND NOT EXISTS (SELECT 1 FROM obligation_acknowledgements oa "
                "                  WHERE oa.document_fact_id = df.id AND oa.user_id = :uid "
                "                    AND oa.milestone = :confirmed)"
            ),
            {"uid": user_id, "ob": DOC_FACT_CATEGORY_OBLIGATION,
             "min_date": min_date, "max_date": max_date,
             "confirmed": OBLIGATION_MILESTONE_CONFIRMED},
        )).fetchall()
        desired = {
            int(r[0]): {
                "kind": r[1], "date": r[2], "legal": bool(r[3]),
                "amount": r[4], "currency": r[5], "value": r[6],
            }
            for r in rows
        }

        ledger = {
            int(lr[1]) if lr[1] is not None else None: lr
            for lr in (await self.db.execute(
                text(
                    "SELECT id, document_fact_id, event_id, synced_obligation_date, synced_summary "
                    "FROM obligation_calendar_events WHERE user_id = :uid"
                ),
                {"uid": user_id},
            )).fetchall()
        }
        # Orphan rows (document_fact_id NULL) collapse to a single None key above;
        # fetch them as a list to delete each.
        orphans = [
            lr for lr in (await self.db.execute(
                text(
                    "SELECT id, event_id FROM obligation_calendar_events "
                    "WHERE user_id = :uid AND document_fact_id IS NULL"
                ),
                {"uid": user_id},
            )).fetchall()
        ]

        # create / update for desired obligations (capped per pass — a user with
        # hundreds of open obligations would otherwise fire hundreds of serial
        # MCP round-trips under the lock; the rest are picked up next pass).
        cap = max(1, settings.obligation_calendar_max_ops_per_run)
        ops = 0
        for fact_id, ob in desired.items():
            if ops >= cap:
                report.notes.append(f"op cap reached ({cap}); remaining deferred to next pass")
                break
            try:
                summary = _summary(ob["kind"], ob["amount"], ob["currency"], ob["legal"])
                start, end = _event_times(ob["date"])
                desc = str(ob["value"] or "")
                row = ledger.get(fact_id)
                if row is None:
                    ev = await self._mcp_event(_CREATE, {
                        "calendar": calendar, "title": summary,
                        "start": start, "end": end, "description": desc,
                    }, user_id)
                    ops += 1
                    if ev and ev.get("id"):
                        self.db.add(ObligationCalendarEvent(
                            document_fact_id=fact_id, user_id=user_id, calendar=calendar,
                            event_id=str(ev["id"]), synced_obligation_date=ob["date"],
                            synced_summary=summary,
                        ))
                        if await self._commit():
                            report.created += 1
                        else:
                            report.errors += 1
                    else:
                        report.errors += 1
                else:
                    _id, _fid, event_id, synced_date, synced_summary = row
                    changed = (synced_date != ob["date"]) or (synced_summary != summary)
                    if changed:
                        ev = await self._mcp_event(_UPDATE, {
                            "calendar": calendar, "event_id": event_id, "title": summary,
                            "start": start, "end": end, "description": desc,
                        }, user_id)
                        ops += 1
                        if ev is not None:
                            await self.db.execute(
                                text("UPDATE obligation_calendar_events SET "
                                     "synced_obligation_date = :d, synced_summary = :s, "
                                     "updated_at = NOW() WHERE id = :id"),
                                {"d": ob["date"], "s": summary, "id": _id},
                            )
                            if await self._commit():
                                report.updated += 1
                            else:
                                report.errors += 1
                        else:
                            report.errors += 1
            except Exception as e:  # noqa: BLE001
                report.errors += 1
                logger.warning(f"calendar sync: fact {fact_id} create/update failed: {e}")
                await self.db.rollback()

        # delete ledger rows whose obligation is no longer desired (confirmed,
        # out of window) + orphans (fact purged → SET NULL).
        stale = [lr for fid, lr in ledger.items() if fid is not None and fid not in desired]
        for lr in stale:
            await self._delete_row(calendar, lr[0], lr[2], user_id, report)
        for orow in orphans:
            await self._delete_row(calendar, orow[0], orow[1], user_id, report)
        return report

    async def _commit(self) -> bool:
        """Commit, rolling back on failure so one bad commit (e.g. a unique race
        or transient blip) doesn't poison the rest of the pass."""
        try:
            await self.db.commit()
            return True
        except Exception as e:  # noqa: BLE001
            logger.warning(f"calendar sync: commit failed: {e}")
            await self.db.rollback()
            return False

    async def _delete_event(self, calendar: str, event_id: str, user_id: int) -> str:
        """Delete a calendar event. Returns 'ok' (deleted), 'gone' (the calendar
        already has no such event → desired state reached, treat as done), or
        'fail' (transient → keep the ledger row and retry)."""
        if self.mcp is None:
            return "fail"
        try:
            res = await self.mcp.execute_tool(
                _DELETE, {"calendar": calendar, "event_id": event_id},
                user_permissions=_PERMS, user_id=user_id,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning(f"calendar sync: delete {event_id} raised: {e}")
            return "fail"
        if res and res.get("success"):
            return "ok"
        msg = ((res or {}).get("message") or "").lower()
        if "not found" in msg or "notfound" in msg or "no such" in msg:
            return "gone"  # user already removed it — don't retry forever (review F4)
        logger.warning(f"calendar sync: delete {event_id} failed: {msg or 'no result'}")
        return "fail"

    async def _delete_row(self, calendar: str, row_id: int, event_id: str,
                          user_id: int, report: CalSyncReport) -> None:
        status = await self._delete_event(calendar, event_id, user_id)
        if status in ("ok", "gone"):
            await self.db.execute(
                text("DELETE FROM obligation_calendar_events WHERE id = :id"), {"id": row_id},
            )
            if await self._commit():
                report.deleted += 1
            else:
                report.errors += 1
        else:
            report.errors += 1  # transient — keep the ledger row, retry next pass

    async def teardown_user(self, user_id: int) -> int:
        """Delete ALL of a user's synced calendar events + ledger rows (called
        when they turn sync off, BEFORE the pref row is removed — otherwise the
        reconciler, which keys off the pref, would never see them again and the
        events would linger; review F2). Best-effort: a transient delete failure
        keeps that row (rare orphan). Returns the count removed."""
        rows = (await self.db.execute(
            text("SELECT id, calendar, event_id FROM obligation_calendar_events WHERE user_id = :u"),
            {"u": user_id},
        )).fetchall()
        report = CalSyncReport(user_id=user_id)
        for r in rows:
            await self._delete_row(r[1], r[0], r[2], user_id, report)
        return report.deleted


async def reconcile_all_users(mcp_manager: Any) -> None:
    """Scheduler entry point: per-user session + reconcile for each user who has
    a calendar preference."""
    from services.database import AsyncSessionLocal

    async with AsyncSessionLocal() as enum_session:
        user_ids = await ObligationCalendarSync(enum_session, mcp_manager).list_pref_user_ids()
    for uid in user_ids:
        try:
            async with AsyncSessionLocal() as per_user_db:
                rep = await ObligationCalendarSync(per_user_db, mcp_manager).run_for_user(uid)
                if rep.created or rep.updated or rep.deleted or rep.errors:
                    logger.info(
                        f"Calendar sync user {uid}: +{rep.created} ~{rep.updated} "
                        f"-{rep.deleted} err={rep.errors}"
                    )
        except Exception as e:  # noqa: BLE001
            logger.warning(f"Calendar sync failed for user {uid}: {e}")
