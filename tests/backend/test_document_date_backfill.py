"""Re-derivation of stored document dates (``bin/backfill_document_dates.py --rederive``).

What this encodes:
* a dry run writes nothing;
* the ``future`` scope only touches documents dated past import + tolerance, the
  ``all`` scope also repairs a wrong PAST date (a deadline taken as document date);
* a correct stored date is never rewritten, and a second commit run is a no-op;
* outcomes are split into changed_earlier / changed_later / cleared;
* only changed documents feed the Paperless created-date list — cleared ones are
  reported separately and never handed to that backfill.
All data is synthetic.
"""
from contextlib import asynccontextmanager
from datetime import date, datetime

import pytest

from models.database import Atom, Document, DocumentFact
from services import document_date_backfill as rb

pytestmark = [pytest.mark.unit]

IMPORTED = datetime(2026, 3, 1, 10, 0)


def _factory(session):
    @asynccontextmanager
    async def _cm():
        yield session

    return _cm


async def _doc(session, *, stored, facts, pid=None, title=None):
    doc = Document(
        filename="scan.pdf", file_path="/tmp/scan.pdf", status="completed",
        document_date=stored, created_at=IMPORTED, paperless_document_id=pid,
        generated_title=title,
    )
    session.add(doc)
    await session.flush()
    atom = Atom(atom_id=f"fact-atom-rederive-{doc.id}", atom_type="document_fact",
                source_table="document_facts", source_id=str(doc.id), owner_user_id=1,
                policy={"tier": 0})
    session.add(atom)
    await session.flush()
    for category, kind, value in facts:
        session.add(DocumentFact(document_id=doc.id, category=category, kind=kind,
                                 value=value, atom_id=atom.atom_id))
    await session.flush()
    return doc


async def _seed(session):
    """correct / future-deadline / past-deadline / nothing-trusted-left."""
    ok = await _doc(session, stored=date(2026, 2, 10), pid=11,
                    facts=[("universal", "rechnungsdatum", "10.02.2026")])
    future = await _doc(session, stored=date(2026, 9, 30), pid=12,
                        facts=[("obligation", "zahlung", "zahlbar bis 30.09.2026"),
                               ("universal", "belegdatum", "20.02.2026")])
    # A past deadline within import + tolerance: invisible to the narrow scope.
    past_wrong = await _doc(session, stored=date(2026, 3, 5), pid=13,
                            facts=[("universal", "faelligkeitsdatum", "05.03.2026"),
                                   ("universal", "dokumentdatum", "05.02.2026")])
    cleared = await _doc(session, stored=date(2026, 12, 31), pid=14,
                         facts=[("universal", "gueltigkeit", "gueltig bis 31.12.2026")])
    await session.commit()
    return ok, future, past_wrong, cleared


async def test_dry_run_writes_nothing(db_session):
    ok, future, past_wrong, cleared = await _seed(db_session)
    report = await rb.rederive_document_dates(_factory(db_session), scope=rb.SCOPE_ALL)
    assert report.commit is False
    for doc, stored in ((future, date(2026, 9, 30)), (past_wrong, date(2026, 3, 5)),
                        (cleared, date(2026, 12, 31))):
        await db_session.refresh(doc)
        assert doc.document_date == stored


async def test_future_scope_only_touches_dates_after_import(db_session):
    ok, future, past_wrong, cleared = await _seed(db_session)
    report = await rb.rederive_document_dates(_factory(db_session), scope=rb.SCOPE_FUTURE)
    assert report.scanned == 2  # the future deadline + the validity end
    assert report.changed_earlier == [future.id]
    assert report.cleared == [cleared.id]
    assert past_wrong.id not in report.changed_earlier + report.changed_later + report.cleared


async def test_all_scope_repairs_past_deadline_and_keeps_correct_dates(db_session):
    ok, future, past_wrong, cleared = await _seed(db_session)
    report = await rb.rederive_document_dates(_factory(db_session), scope=rb.SCOPE_ALL, commit=True)
    assert report.scanned == 4
    assert report.unchanged == 1
    assert sorted(report.changed_earlier) == sorted([future.id, past_wrong.id])
    assert report.changed_later == []
    assert report.cleared == [cleared.id]
    for doc in (ok, future, past_wrong, cleared):
        await db_session.refresh(doc)
    assert ok.document_date == date(2026, 2, 10)
    assert future.document_date == date(2026, 2, 20)
    assert past_wrong.document_date == date(2026, 2, 5)
    assert cleared.document_date is None


async def test_changed_later_is_its_own_outcome(db_session):
    doc = await _doc(db_session, stored=date(2026, 1, 5), pid=21,
                     facts=[("universal", "tatdatum", "05.01.2026"),
                            ("universal", "schreibdatum", "20.02.2026")])
    await db_session.commit()
    report = await rb.rederive_document_dates(_factory(db_session), scope=rb.SCOPE_ALL)
    assert report.changed_later == [doc.id]
    assert report.changed_earlier == [] and report.cleared == []


async def test_second_commit_run_is_a_no_op(db_session):
    await _seed(db_session)
    first = await rb.rederive_document_dates(_factory(db_session), scope=rb.SCOPE_ALL, commit=True)
    assert first.changed_earlier and first.cleared
    second = await rb.rederive_document_dates(_factory(db_session), scope=rb.SCOPE_ALL, commit=True)
    # the cleared document no longer has a date and drops out of scope
    assert second.scanned == 3
    assert second.unchanged == 3
    assert (second.changed_earlier, second.changed_later, second.cleared) == ([], [], [])
    assert second.paperless_ids == []


async def test_paperless_ids_exclude_cleared_documents(db_session):
    ok, future, past_wrong, cleared = await _seed(db_session)
    report = await rb.rederive_document_dates(_factory(db_session), scope=rb.SCOPE_ALL)
    assert report.paperless_ids == [12, 13]
    assert report.cleared_paperless_ids == [14]
    assert 11 not in report.paperless_ids  # unchanged → nothing to re-patch


async def test_unknown_scope_is_rejected(db_session):
    with pytest.raises(ValueError):
        await rb.rederive_document_dates(_factory(db_session), scope="everything")
