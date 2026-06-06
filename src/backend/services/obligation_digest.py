"""Weekly obligation digest — the safety floor under the per-milestone notifier.

The per-milestone notifier (``obligation_deadline_notifier``) only scans a bounded
window ``[today − overdue_grace, today + 14d]``, so a document OCR'd weeks after
its Frist is never scanned and its deadline is silently dropped. This digest is
the floor: once per ISO week it sends each owner ONE summary of every OPEN
obligation with **no lower date bound**, so late-extracted / very-overdue
deadlines still surface. (It cannot catch a *never-extracted* deadline — that
failure has to stay observable upstream, per the durability learning.)

Same design discipline as the notifier: obligations are the source of truth,
owner-targeted (``privacy="personal"``), per-user advisory lock, and a restart-
safe dedup — here a ``(user, ISO-week)`` row in ``obligation_digest_log`` so a
mid-week pod restart never re-sends. Gated on ``obligation_digest_enabled`` AND
``proactive_enabled`` (delivery runs through the proactive subsystem).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

from loguru import logger
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from models.database import (
    DOC_FACT_CATEGORY_OBLIGATION,
    OBLIGATION_MILESTONE_CONFIRMED,
    ObligationDigestLog,
)
from services.kg_reconciler_service import _resolve_lock_engine
from services.notification_service import NotificationService
from utils.config import settings

# pg_advisory_lock namespace (int4); objid is the user_id. Distinct from the
# notifier (0x4F42) and KG reconciler (0x4B47) so the daily/weekly scans never
# share a lock slot.
_DIGEST_LOCK_NS = 0x4F44  # "OD"

EVENT_TYPE = "obligation.digest"
SOURCE = "obligation_digest"

# How many obligations to name explicitly in the digest body before summarizing
# the rest as a count — keeps the message readable.
_MAX_LISTED = 5


def period_key(d: date) -> str:
    """ISO ``YYYY-Www`` for a date — the per-user weekly dedup key."""
    iso = d.isocalendar()
    return f"{iso[0]}-W{iso[1]:02d}"


@dataclass
class DigestReport:
    user_id: int
    sent: int = 0
    obligations: int = 0
    skipped_already_sent: bool = False
    notes: list[str] = field(default_factory=list)


def _build_digest(obligations: list[Any], today: date) -> tuple[str, str]:
    """(title, message) for one user's weekly digest, German."""
    total = len(obligations)
    overdue = sum(1 for r in obligations if r.obligation_date < today)
    this_week = sum(1 for r in obligations if today <= r.obligation_date <= today + timedelta(days=7))
    legal = sum(1 for r in obligations if bool(r.legal_gate))

    noun = "offene Frist" if total == 1 else "offene Fristen"
    # Calendar week in the title keeps each week's digest content-distinct, so
    # the proactive content-hash dedup never collapses two legitimate weekly
    # digests (the per-week obligation_digest_log is the real dedup).
    title = f"Wochenübersicht (KW {today.isocalendar()[1]:02d}): {total} {noun}"

    parts: list[str] = []
    summary = f"Du hast {total} {noun}"
    detail = []
    if overdue:
        detail.append(f"{overdue} überfällig")
    if this_week:
        detail.append(f"{this_week} diese Woche")
    if detail:
        summary += " (" + ", ".join(detail) + ")"
    summary += "."
    parts.append(summary)

    for r in obligations[:_MAX_LISTED]:
        date_str = r.obligation_date.strftime("%d.%m.%Y")
        flag = " ⚑ rechtlich" if bool(r.legal_gate) else ""
        parts.append(f'• „{r.kind}“ am {date_str}{flag}')
    if total > _MAX_LISTED:
        parts.append(f"… und {total - _MAX_LISTED} weitere.")

    if legal:
        parts.append("Rechtliche Fristen bitte in /brain/review bestätigen.")
    return title, "\n".join(parts)


class ObligationDigest:
    """Weekly owner-targeted open-obligation digest + per-week sent-marker."""

    def __init__(self, db: AsyncSession):
        self.db = db

    def _max_date(self, today: date) -> date:
        horizon = max(0, settings.obligation_digest_horizon_days)
        return today + timedelta(days=horizon)

    # SELECT for a user's OPEN obligations: owner's, not confirmed, dated at or
    # before the horizon. NO lower bound — that is the whole point (overdue of
    # any age is included). Shared by list + scan.
    _OPEN_WHERE = (
        "df.category = :ob AND df.obligation_date IS NOT NULL "
        "AND df.obligation_date <= :max_date "
        "AND NOT EXISTS (SELECT 1 FROM obligation_acknowledgements oa "
        "                WHERE oa.document_fact_id = df.id AND oa.user_id = :uid "
        "                  AND oa.milestone = :confirmed)"
    )

    async def list_owner_user_ids(self, *, today: date | None = None) -> list[int]:
        """Owners with ≥1 open obligation in the horizon — the scheduler iterates
        only these."""
        today = today or date.today()
        rows = await self.db.execute(
            text(
                "SELECT DISTINCT a.owner_user_id "
                "FROM document_facts df JOIN atoms a ON a.atom_id = df.atom_id "
                f"WHERE {self._OPEN_WHERE.replace(':uid', 'a.owner_user_id')}"
            ),
            {"ob": DOC_FACT_CATEGORY_OBLIGATION, "max_date": self._max_date(today),
             "confirmed": OBLIGATION_MILESTONE_CONFIRMED},
        )
        return [int(r[0]) for r in rows.fetchall()]

    async def run_for_user(self, user_id: int, *, today: date | None = None) -> DigestReport:
        """One weekly digest pass for a user, serialized per-user by a
        non-blocking advisory lock (mirrors the notifier). The lock lives on a
        dedicated connection because ``process_webhook`` commits ``self.db``
        mid-pass."""
        report = DigestReport(user_id=user_id)
        today = today or date.today()
        dialect = self.db.bind.dialect.name if self.db.bind is not None else ""
        if dialect != "postgresql":
            return await self._digest_pass(user_id, today, report)

        lock_engine = _resolve_lock_engine(self.db.bind)
        if lock_engine is None:
            return await self._digest_pass(user_id, today, report)

        async with lock_engine.connect() as lock_conn:
            got = (await lock_conn.execute(
                text("SELECT pg_try_advisory_lock(:ns, :uid)"),
                {"ns": _DIGEST_LOCK_NS, "uid": user_id},
            )).scalar()
            if not got:
                report.notes.append("skipped: another digest run holds this user's lock")
                return report
            try:
                return await self._digest_pass(user_id, today, report)
            finally:
                await lock_conn.execute(
                    text("SELECT pg_advisory_unlock(:ns, :uid)"),
                    {"ns": _DIGEST_LOCK_NS, "uid": user_id},
                )

    async def _digest_pass(self, user_id: int, today: date, report: DigestReport) -> DigestReport:
        pk = period_key(today)
        already = (await self.db.execute(
            text("SELECT 1 FROM obligation_digest_log WHERE user_id = :uid AND period_key = :pk LIMIT 1"),
            {"uid": user_id, "pk": pk},
        )).first()
        if already is not None:
            report.skipped_already_sent = True
            return report

        rows = (await self.db.execute(
            text(
                "SELECT df.id, df.kind, df.obligation_date, df.legal_gate "
                "FROM document_facts df JOIN atoms a ON a.atom_id = df.atom_id "
                f"WHERE a.owner_user_id = :uid AND {self._OPEN_WHERE} "
                "ORDER BY df.obligation_date ASC, df.id"
            ),
            {"uid": user_id, "ob": DOC_FACT_CATEGORY_OBLIGATION,
             "max_date": self._max_date(today), "confirmed": OBLIGATION_MILESTONE_CONFIRMED},
        )).fetchall()
        report.obligations = len(rows)
        if not rows:
            return report  # nothing open → no digest, no log row (re-checks next week)

        title, message = _build_digest(rows, today)
        try:
            await NotificationService(self.db).process_webhook(
                event_type=EVENT_TYPE,
                title=title,
                message=message,
                urgency="info",
                source=SOURCE,
                privacy="personal",
                target_user_id=user_id,
                data={"period_key": pk, "open_count": len(rows)},
            )
            report.sent = 1
        except ValueError as e:
            report.notes.append(f"digest suppressed ({e})")

        # Record the per-week marker even on a dedup ValueError (it was handled),
        # so we don't retry this week. Tolerant of a unique race.
        self.db.add(ObligationDigestLog(user_id=user_id, period_key=pk))
        try:
            await self.db.commit()
        except Exception:
            await self.db.rollback()
        return report


async def scan_all_users() -> None:
    """Scheduler entry point: per-user session + digest pass."""
    from services.database import AsyncSessionLocal

    async with AsyncSessionLocal() as enum_session:
        user_ids = await ObligationDigest(enum_session).list_owner_user_ids()
    for uid in user_ids:
        try:
            async with AsyncSessionLocal() as per_user_db:
                report = await ObligationDigest(per_user_db).run_for_user(uid)
                if report.sent:
                    logger.info(
                        f"Obligation digest user {uid}: sent ({report.obligations} open)"
                    )
        except Exception as e:
            logger.warning(f"Obligation digest failed for user {uid}: {e}")
