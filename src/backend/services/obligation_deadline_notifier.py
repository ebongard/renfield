"""Obligation-deadline notifier — Schicht A proactive deadline reminders.

Turns dated ``document_facts`` obligations into owner-targeted reminders. Design
per the cross-model learning ``schicht-a-obligations-source-of-truth`` (which
overrides the earlier ``schicht-a-reminder-durability``):

  * Obligations (``document_facts``) ARE the scheduling source of truth — we do
    NOT pre-materialize ``Reminder`` rows or reuse the chat-reminder loop
    (``check_due_reminders`` fires every overdue deadline at once on restart =
    back-fire storm, broadcasts to the whole household = privacy breach vs
    ``circle_tier``, and has no row lock).
  * ONE daily idempotent scan computes the single current lead-time milestone per
    obligation and fires it once, recording a ``(fact, owner, milestone)`` row in
    the ``obligation_acknowledgements`` ledger so a pod restart / re-run never
    re-fires it. The scan surviving restarts is the safety property (the
    missed-deadline scar).
  * **Owner-targeted**: each reminder goes only to the obligation's owner
    (``atoms.owner_user_id``), never broadcast — ``privacy="personal"``.
  * **Legal-gate kinds** (Widerspruch/Einspruch/Klage) are notified too (they're
    the most important to surface) but flagged human-gated → the message points
    at ``/brain/review`` and urgency is raised; the notifier never auto-acts.
  * A user ``"confirmed"`` ack (the agenda's Bestätigen) suppresses all further
    milestones for that obligation.

Delivery reuses :class:`NotificationService` (persist + dedup + hook delivery);
it degrades gracefully when ``proactive_enabled`` is off or no ha_glue delivery
hook is registered (the notification is persisted, just not broadcast).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any

from loguru import logger
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from models.database import (
    DOC_FACT_CATEGORY_OBLIGATION,
    OBLIGATION_MILESTONE_CONFIRMED,
    ObligationAcknowledgement,
)
from services.kg_reconciler_service import _resolve_lock_engine
from services.notification_service import NotificationService
from utils.config import settings

# Lead-time milestones (days before the printed Frist) the scan fires, plus the
# implicit "due" (==0) and "overdue" (<0) buckets. ASCENDING so
# current_milestone() returns the SINGLE most-urgent current bucket — first
# enable on an obligation already 2 days out fires only "3d", never 14d+7d+3d at
# once (the back-fire-storm failure mode).
_LEAD_DAYS = (1, 3, 7, 14)
_MAX_LEAD = _LEAD_DAYS[-1]

# pg_advisory_lock namespace (int4); objid is the user_id. Distinct from the KG
# reconciler's 0x4B47 so the two daily per-user scans never share a lock slot.
_NOTIFIER_LOCK_NS = 0x4F42  # "OB"

EVENT_TYPE = "obligation.deadline"
SOURCE = "obligation_notifier"


def current_milestone(days_until: int) -> str | None:
    """The single lead-time bucket an obligation is in *now*, or None if it is
    still further out than the largest lead milestone.

    Monotonic as the Frist approaches (14d → 7d → 3d → 1d → due → overdue) and
    returns exactly ONE bucket, so the notifier fires at most one reminder per
    obligation per scan and a first run never back-fires every already-crossed
    milestone.
    """
    if days_until < 0:
        return "overdue"
    if days_until == 0:
        return "due"
    for t in _LEAD_DAYS:  # ascending: smallest (most urgent) matching bucket wins
        if days_until <= t:
            return f"{t}d"
    return None


@dataclass
class NotifyReport:
    user_id: int
    scanned: int = 0
    notified: int = 0
    skipped_confirmed: int = 0
    skipped_ledger: int = 0
    errors: int = 0
    notes: list[str] = field(default_factory=list)


def _build_message(*, kind: str, days_until: int, ob_date: date,
                   amount_value: Any, amount_currency: str | None,
                   legal_gate: bool) -> tuple[str, str, str]:
    """(title, message, urgency) for one obligation reminder, in German.

    urgency: ``critical`` for legal-gate, overdue, or due/1-day items; ``info``
    otherwise. The message is owner-private (``privacy="personal"`` upstream).
    """
    date_str = ob_date.strftime("%d.%m.%Y")
    title = f"Frist: {kind}"
    if days_until < 0:
        body = f"Überfällige Frist: „{kind}“ war am {date_str} fällig."
    elif days_until == 0:
        body = f"Heute fällig: „{kind}“ ({date_str})."
    elif days_until == 1:
        body = f"Morgen fällig: „{kind}“ ({date_str})."
    else:
        body = f"„{kind}“ ist in {days_until} Tagen fällig ({date_str})."

    if amount_value is not None:
        cur = (amount_currency or "").strip()
        body += f" Betrag: {amount_value}{(' ' + cur) if cur else ''}."

    if legal_gate:
        body += " ⚑ Rechtliche Frist — bitte in /brain/review bestätigen."
        urgency = "critical"
    elif days_until <= 1:
        urgency = "critical"
    else:
        urgency = "info"
    return title, body, urgency


class ObligationDeadlineNotifier:
    """Daily owner-targeted obligation-deadline scan + notified-ledger."""

    def __init__(self, db: AsyncSession):
        self.db = db

    def _window(self, today: date) -> tuple[date, date]:
        """[min_date, max_date] the scan considers: from ``overdue_grace`` days
        in the past (so a just-passed Frist still fires its one "overdue") to the
        largest lead milestone in the future (beyond which there is no bucket)."""
        from datetime import timedelta
        grace = max(0, settings.obligation_notifier_overdue_grace_days)
        return today - timedelta(days=grace), today + timedelta(days=_MAX_LEAD)

    async def list_owner_user_ids(self, *, today: date | None = None) -> list[int]:
        """Owners with ≥1 dated obligation in the active window — the per-user
        scheduler iterates only these, not the whole user table."""
        today = today or date.today()
        min_date, max_date = self._window(today)
        rows = await self.db.execute(
            text(
                "SELECT DISTINCT a.owner_user_id "
                "FROM document_facts df JOIN atoms a ON a.atom_id = df.atom_id "
                "WHERE df.category = :ob AND df.obligation_date IS NOT NULL "
                "AND df.obligation_date >= :min_date AND df.obligation_date <= :max_date"
            ),
            {"ob": DOC_FACT_CATEGORY_OBLIGATION, "min_date": min_date, "max_date": max_date},
        )
        return [int(r[0]) for r in rows.fetchall()]

    async def run_for_user(self, user_id: int, *, today: date | None = None) -> NotifyReport:
        """One scan pass for a user, serialized per-user by a non-blocking
        advisory lock (an overlapping run is a no-op). The lock lives on a
        DEDICATED connection because :meth:`NotificationService.process_webhook`
        commits ``self.db`` mid-pass, which can return self.db's connection to the
        pool — a session-level lock on self.db would not survive that.
        """
        report = NotifyReport(user_id=user_id)
        today = today or date.today()
        dialect = self.db.bind.dialect.name if self.db.bind is not None else ""
        if dialect != "postgresql":
            return await self._scan_pass(user_id, today, report)

        lock_engine = _resolve_lock_engine(self.db.bind)
        if lock_engine is None:  # no async connectable → run unlocked (single daily scheduler)
            return await self._scan_pass(user_id, today, report)

        async with lock_engine.connect() as lock_conn:
            got = (await lock_conn.execute(
                text("SELECT pg_try_advisory_lock(:ns, :uid)"),
                {"ns": _NOTIFIER_LOCK_NS, "uid": user_id},
            )).scalar()
            if not got:
                report.notes.append("skipped: another notifier run holds this user's lock")
                return report
            try:
                return await self._scan_pass(user_id, today, report)
            finally:
                await lock_conn.execute(
                    text("SELECT pg_advisory_unlock(:ns, :uid)"),
                    {"ns": _NOTIFIER_LOCK_NS, "uid": user_id},
                )

    async def _scan_pass(self, user_id: int, today: date, report: NotifyReport) -> NotifyReport:
        min_date, max_date = self._window(today)
        rows = (await self.db.execute(
            text(
                "SELECT df.id, df.kind, df.obligation_date, df.legal_gate, "
                "       df.amount_value, df.amount_currency, df.document_id "
                "FROM document_facts df JOIN atoms a ON a.atom_id = df.atom_id "
                "WHERE a.owner_user_id = :uid AND df.category = :ob "
                "  AND df.obligation_date IS NOT NULL "
                "  AND df.obligation_date >= :min_date AND df.obligation_date <= :max_date "
                "ORDER BY df.obligation_date ASC, df.id"
            ),
            {"uid": user_id, "ob": DOC_FACT_CATEGORY_OBLIGATION,
             "min_date": min_date, "max_date": max_date},
        )).fetchall()
        if not rows:
            return report

        # Preload this user's acks for the scanned facts in one query → in-memory
        # confirmed/ledger checks (no per-obligation round-trip).
        fact_ids = [int(r[0]) for r in rows]
        ack_rows = (await self.db.execute(
            text(
                "SELECT document_fact_id, milestone FROM obligation_acknowledgements "
                "WHERE user_id = :uid AND document_fact_id = ANY(:ids)"
            ),
            {"uid": user_id, "ids": fact_ids},
        )).fetchall()
        acks: set[tuple[int, str]] = {(int(fid), ms) for fid, ms in ack_rows}

        notifier_svc = NotificationService(self.db)
        for r in rows:
            report.scanned += 1
            fact_id = int(r[0])
            try:
                if (fact_id, OBLIGATION_MILESTONE_CONFIRMED) in acks:
                    report.skipped_confirmed += 1
                    continue
                ob_date: date = r[2]
                days_until = (ob_date - today).days
                ms = current_milestone(days_until)
                if ms is None:
                    continue  # still further out than the largest lead bucket
                if (fact_id, ms) in acks:
                    report.skipped_ledger += 1
                    continue

                legal = bool(r[3])
                title, body, urgency = _build_message(
                    kind=str(r[1]), days_until=days_until, ob_date=ob_date,
                    amount_value=r[4], amount_currency=r[5], legal_gate=legal,
                )
                try:
                    await notifier_svc.process_webhook(
                        event_type=EVENT_TYPE,
                        title=title,
                        message=body,
                        urgency=urgency,
                        source=SOURCE,
                        privacy="personal",
                        target_user_id=user_id,
                        data={
                            "document_fact_id": fact_id,
                            "document_id": int(r[6]),
                            "milestone": ms,
                            "legal_gate": legal,
                            "obligation_date": ob_date.isoformat(),
                        },
                    )
                    report.notified += 1
                except ValueError as e:
                    # Content-hash dedup / feedback suppression — treat as handled
                    # so we still record the ledger row and don't retry forever.
                    report.notes.append(f"fact {fact_id} {ms}: suppressed ({e})")

                await self._record(user_id, fact_id, ms)
                acks.add((fact_id, ms))
            except Exception as e:  # one bad obligation must not abort the user
                report.errors += 1
                logger.warning(f"Obligation notifier: fact {fact_id} failed: {e}")
        return report

    async def _record(self, user_id: int, fact_id: int, milestone: str) -> None:
        """Insert the (fact, user, milestone) ledger row, tolerant of a unique
        race (the advisory lock already serializes per-user; this is defense)."""
        self.db.add(ObligationAcknowledgement(
            document_fact_id=fact_id, user_id=user_id, milestone=milestone,
        ))
        try:
            await self.db.commit()
        except Exception:
            await self.db.rollback()  # already recorded by a concurrent run


async def scan_all_users() -> NotifyReport | None:
    """Scheduler entry point: per-user session + scan, mirroring the KG
    reconciler tick. Returns None (the per-user reports are logged)."""
    from services.database import AsyncSessionLocal

    async with AsyncSessionLocal() as enum_session:
        user_ids = await ObligationDeadlineNotifier(enum_session).list_owner_user_ids()
    for uid in user_ids:
        try:
            async with AsyncSessionLocal() as per_user_db:
                report = await ObligationDeadlineNotifier(per_user_db).run_for_user(uid)
                if report.notified or report.errors:
                    logger.info(
                        f"Obligation notifier user {uid}: "
                        f"notified={report.notified} skipped_confirmed={report.skipped_confirmed} "
                        f"skipped_ledger={report.skipped_ledger} errors={report.errors}"
                    )
        except Exception as e:
            logger.warning(f"Obligation notifier failed for user {uid}: {e}")
    return None
