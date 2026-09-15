"""Re-derive stored ``documents.document_date`` values with the current
``services.document_date.derive_document_date`` — the testable core of
``bin/backfill_document_dates.py --rederive``.

**Scopes.**

* ``future`` (the default, narrow): only documents whose stored date lies more
  than ``FUTURE_TOLERANCE_DAYS`` after their import — the visible shape of the
  pre-2026-09-15 derivation (deadline / validity / next-period dates).
* ``all`` (broad): every completed document that already has a date. This also
  repairs the invisible shape — a PAST deadline or an unrelated event date taken
  as the document date.

Only documents that already carry a date are touched; NULL dates are the job of
the plain backfill. A document is written only when the fresh derivation
DIFFERS, so a correct stored date is never overwritten, and a second run over
the same data reports every document ``unchanged`` (idempotent).

**Outcomes.** ``unchanged``, ``changed_earlier``, ``changed_later``, ``cleared``
(no trusted source remains → NULL).

**Paperless.** ``paperless_ids`` lists the linked Paperless documents of
``changed_*`` rows only — the input for the follow-up
``bin/backfill_paperless_metadata.py --mode created-date`` run. ``cleared`` rows
are listed separately (``cleared_paperless_ids``) and must NOT be handed to that
backfill: it sources the Paperless ``created`` date from ``document_date`` and
already skips NULL dates, so a cleared document keeps whatever Paperless has —
there is no "unset" to push, and guessing a date would reintroduce the bug.

Output is document ids, Paperless ids and counts only — never titles, filenames,
fact values or dates.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from sqlalchemy import select

from models.database import DOC_STATUS_COMPLETED, Document, DocumentFact
from services.document_date import FUTURE_TOLERANCE_DAYS, derive_document_date

SCOPE_FUTURE = "future"
SCOPE_ALL = "all"
SCOPES = (SCOPE_FUTURE, SCOPE_ALL)


@dataclass
class RederiveReport:
    scope: str
    commit: bool
    scanned: int = 0
    unchanged: int = 0
    changed_earlier: list[int] = field(default_factory=list)
    changed_later: list[int] = field(default_factory=list)
    cleared: list[int] = field(default_factory=list)
    paperless_ids: list[int] = field(default_factory=list)
    cleared_paperless_ids: list[int] = field(default_factory=list)

    def summary(self) -> dict:
        return {
            "scope": self.scope,
            "commit": self.commit,
            "scanned": self.scanned,
            "unchanged": self.unchanged,
            "changed_earlier": len(self.changed_earlier),
            "changed_later": len(self.changed_later),
            "cleared": len(self.cleared),
            "paperless_ids": len(self.paperless_ids),
            "cleared_paperless_ids": len(self.cleared_paperless_ids),
        }


async def derive_for_document(db: Any, doc: Document):
    """Fresh derivation for one stored document (facts from the DB, import date
    as the reference)."""
    facts = (
        await db.execute(
            select(
                DocumentFact.category,
                DocumentFact.kind,
                DocumentFact.normalized_value,
                DocumentFact.value,
            ).where(DocumentFact.document_id == doc.id)
        )
    ).all()
    created = doc.created_at.date() if doc.created_at else None
    return derive_document_date(
        [tuple(f) for f in facts],
        [doc.generated_title, doc.title],
        reference_date=created,
    )


def _in_scope(doc: Document, scope: str) -> bool:
    if scope == SCOPE_ALL:
        return True
    if doc.created_at is None:
        return False
    return doc.document_date > doc.created_at.date() + timedelta(days=FUTURE_TOLERANCE_DAYS)


async def rederive_document_dates(
    session_factory: Callable[[], Any],
    *,
    scope: str = SCOPE_FUTURE,
    commit: bool = False,
    limit: int | None = None,
) -> RederiveReport:
    """Dry-run (default) or apply the re-derivation. See the module docstring."""
    if scope not in SCOPES:
        raise ValueError(f"unknown scope {scope!r}; expected one of {SCOPES}")
    report = RederiveReport(scope=scope, commit=commit)
    paperless: set[int] = set()
    cleared_paperless: set[int] = set()

    async with session_factory() as db:
        docs = (
            await db.execute(
                select(Document)
                .where(
                    Document.status == DOC_STATUS_COMPLETED,
                    Document.document_date.is_not(None),
                )
                .order_by(Document.id)
            )
        ).scalars().all()
        docs = [d for d in docs if _in_scope(d, scope)]
        if limit:
            docs = docs[:limit]

        for doc in docs:
            report.scanned += 1
            fresh = await derive_for_document(db, doc)
            stored = doc.document_date
            if fresh == stored:
                report.unchanged += 1
                continue
            if fresh is None:
                report.cleared.append(doc.id)
                if doc.paperless_document_id is not None:
                    cleared_paperless.add(int(doc.paperless_document_id))
            else:
                (report.changed_earlier if fresh < stored else report.changed_later).append(doc.id)
                if doc.paperless_document_id is not None:
                    paperless.add(int(doc.paperless_document_id))
            if commit:
                doc.document_date = fresh

        if commit and (report.changed_earlier or report.changed_later or report.cleared):
            await db.commit()

    # A Paperless document linked from several KB rows with differing outcomes
    # stays on the created-date list only if some row still carries a date; the
    # created-date backfill's own ambiguity check decides from there.
    report.paperless_ids = sorted(paperless)
    report.cleared_paperless_ids = sorted(cleared_paperless - paperless)
    return report
