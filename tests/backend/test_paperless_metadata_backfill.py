"""Created-date backfill for Paperless documents whose post-consume PATCH never ran
(``bin/backfill_paperless_metadata.py --mode created-date``).

What this encodes:
* a dry run writes NOTHING to Paperless;
* a commit patches only the consume-date fallback (``created == added``) and never
  overwrites a value that may be a human edit;
* a second commit run is a no-op (idempotent);
* a Paperless doc linked from KB rows that disagree on the date is skipped;
* every MCP call is paced below the 60/min limit and a rate-limit rejection is
  retried with backoff instead of being counted as a failure;
* the batch is capped and resumable.
"""
import importlib.util
import json
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path

import pytest

from models.database import PAPERLESS_STATE_DONE, PAPERLESS_STATE_PENDING, Document
from services import paperless_metadata_backfill as bf


class _FakePaperless:
    """``docs``: pid → {"created", "added"}. Records every call; ``update_document``
    mutates state so idempotency can be observed. ``reject_first`` rate-limit
    rejections are returned before answering normally."""

    def __init__(self, docs: dict[int, dict], *, reject_first: int = 0, unreachable=()):
        self.docs = docs
        self.reject_left = reject_first
        self.unreachable = set(unreachable)
        self.calls: list[tuple[str, dict]] = []

    async def execute_tool(self, tool, params, **kw):
        self.calls.append((tool, params))
        if self.reject_left > 0:
            self.reject_left -= 1
            return {"success": False, "message": "Rate limit exceeded for MCP server 'paperless'"}
        pid = params["document_id"]
        if pid in self.unreachable:
            return {"success": True, "message": json.dumps({"error": f"Document {pid} not found"})}
        if tool == "mcp.paperless.get_document":
            d = self.docs[pid]
            return {"success": True, "message": json.dumps({"id": pid, **d})}
        if tool == "mcp.paperless.update_document":
            self.docs[pid]["created"] = params["created_date"]
            return {"success": True, "message": json.dumps({"id": pid})}
        raise AssertionError(f"unexpected tool {tool}")

    def updates(self) -> list[dict]:
        return [p for t, p in self.calls if t == "mcp.paperless.update_document"]


async def _no_sleep(_s):
    return None


def _pacer():
    return bf.RatePacer(60_000, sleep=_no_sleep)


def _factory(session):
    @asynccontextmanager
    async def _cm():
        yield session

    return _cm


async def _doc(session, *, pid, doc_date, state=PAPERLESS_STATE_DONE, n=[0]):
    n[0] += 1
    session.add(Document(
        filename=f"f{n[0]}.pdf", file_path=f"/tmp/f{n[0]}.pdf", status="completed",
        paperless_state=state, paperless_document_id=pid, document_date=doc_date,
    ))
    await session.flush()


@pytest.mark.unit
class TestRatePacer:
    async def test_spaces_calls_to_the_rate(self):
        clock = {"t": 0.0}
        slept: list[float] = []

        async def _sleep(s):
            slept.append(s)
            clock["t"] += s

        pacer = bf.RatePacer(30, clock=lambda: clock["t"], sleep=_sleep)
        for _ in range(3):
            await pacer.wait()
        assert slept == [2.0, 2.0]  # 30/min → 2 s apart; the first call is free

    async def test_no_wait_when_calls_are_already_slow(self):
        clock = {"t": 0.0}
        slept: list[float] = []

        async def _sleep(s):
            slept.append(s)

        pacer = bf.RatePacer(60, clock=lambda: clock["t"], sleep=_sleep)
        await pacer.wait()
        clock["t"] = 5.0
        await pacer.wait()
        assert slept == []


@pytest.mark.unit
class TestCallPaced:
    async def test_rate_limit_rejection_is_retried_with_backoff(self):
        mcp = _FakePaperless({1: {"created": "2030-01-01", "added": "2030-01-01T10:00:00+00:00"}}, reject_first=2)
        slept: list[float] = []

        async def _sleep(s):
            slept.append(s)

        res = await bf.call_paced(
            mcp, "mcp.paperless.get_document", {"document_id": 1}, _pacer(),
            sleep=_sleep, backoff_s=10.0,
        )
        assert res["id"] == 1
        assert slept == [10.0, 20.0]  # linear backoff
        assert len(mcp.calls) == 3

    async def test_gives_up_after_retries(self):
        mcp = _FakePaperless({1: {}}, reject_first=100)
        res = await bf.call_paced(
            mcp, "mcp.paperless.get_document", {"document_id": 1}, _pacer(),
            sleep=_no_sleep, retries=2,
        )
        assert res == {"error": "rate_limited"}
        assert len(mcp.calls) == 3


@pytest.mark.database
class TestBackfillCreatedDates:
    async def test_dry_run_writes_nothing(self, db_session):
        await _doc(db_session, pid=10, doc_date=date(2026, 3, 4))
        mcp = _FakePaperless({10: {"created": "2026-08-20", "added": "2026-08-20T12:00:00+02:00"}})
        report = await bf.backfill_created_dates(
            _factory(db_session), mcp, commit=False, pacer=_pacer(), sleep=_no_sleep,
        )
        assert report.would_patch == [10]
        assert report.patched == []
        assert mcp.updates() == []
        assert mcp.docs[10]["created"] == "2026-08-20"

    async def test_commit_patches_only_the_consume_date_fallback(self, db_session):
        await _doc(db_session, pid=10, doc_date=date(2026, 3, 4))   # created == added → patch
        await _doc(db_session, pid=11, doc_date=date(2026, 3, 5))   # created differs → human? skip
        await _doc(db_session, pid=12, doc_date=date(2026, 3, 6))   # already correct
        await _doc(db_session, pid=13, doc_date=date(2026, 3, 7))   # unreachable
        mcp = _FakePaperless({
            10: {"created": "2026-08-20", "added": "2026-08-20T12:00:00+02:00"},
            11: {"created": "2025-12-24", "added": "2026-08-20T12:00:00+02:00"},
            12: {"created": "2026-03-06", "added": "2026-08-20T12:00:00+02:00"},
        }, unreachable=[13])
        report = await bf.backfill_created_dates(
            _factory(db_session), mcp, commit=True, pacer=_pacer(), sleep=_no_sleep,
        )
        assert report.patched == [10]
        assert report.skipped_not_consume_date == [11]
        assert report.already_correct == 1
        assert report.unreachable == [13]
        assert mcp.updates() == [{"document_id": 10, "created_date": "2026-03-04"}]
        assert mcp.docs[11]["created"] == "2025-12-24"  # never overwritten

    async def test_commit_is_idempotent(self, db_session):
        await _doc(db_session, pid=10, doc_date=date(2026, 3, 4))
        mcp = _FakePaperless({10: {"created": "2026-08-20", "added": "2026-08-20T12:00:00+02:00"}})
        first = await bf.backfill_created_dates(
            _factory(db_session), mcp, commit=True, pacer=_pacer(), sleep=_no_sleep,
        )
        second = await bf.backfill_created_dates(
            _factory(db_session), mcp, commit=True, pacer=_pacer(), sleep=_no_sleep,
        )
        assert first.patched == [10]
        assert second.patched == [] and second.already_correct == 1
        assert len(mcp.updates()) == 1

    async def test_legacy_datetime_created_is_compared_by_date(self, db_session):
        await _doc(db_session, pid=10, doc_date=date(2026, 3, 4))
        mcp = _FakePaperless({10: {"created": "2026-03-04T00:00:00+01:00", "added": "2026-08-20T12:00:00+02:00"}})
        report = await bf.backfill_created_dates(
            _factory(db_session), mcp, commit=True, pacer=_pacer(), sleep=_no_sleep,
        )
        assert report.already_correct == 1 and mcp.updates() == []

    async def test_disagreeing_kb_rows_are_ambiguous_and_skipped(self, db_session):
        await _doc(db_session, pid=20, doc_date=date(2026, 1, 1))
        await _doc(db_session, pid=20, doc_date=date(2026, 2, 2))
        await _doc(db_session, pid=21, doc_date=date(2026, 5, 5))
        await _doc(db_session, pid=21, doc_date=date(2026, 5, 5))  # agree → one candidate
        mcp = _FakePaperless({21: {"created": "2026-08-20", "added": "2026-08-20T12:00:00+02:00"}})
        report = await bf.backfill_created_dates(
            _factory(db_session), mcp, commit=True, pacer=_pacer(), sleep=_no_sleep,
        )
        assert report.ambiguous == [20]
        assert report.candidates == 1
        assert [p["document_id"] for p in mcp.updates()] == [21]
        assert all(p["document_id"] != 20 for _, p in mcp.calls)

    async def test_only_done_linked_dated_documents(self, db_session):
        await _doc(db_session, pid=30, doc_date=date(2026, 1, 1), state=PAPERLESS_STATE_PENDING)
        await _doc(db_session, pid=None, doc_date=date(2026, 1, 1))
        await _doc(db_session, pid=31, doc_date=None)
        mcp = _FakePaperless({})
        report = await bf.backfill_created_dates(
            _factory(db_session), mcp, commit=True, pacer=_pacer(), sleep=_no_sleep,
        )
        assert report.candidates == 0
        assert mcp.calls == []

    async def test_limit_caps_batch_and_after_pid_resumes(self, db_session):
        docs = {}
        for pid in (40, 41, 42):
            await _doc(db_session, pid=pid, doc_date=date(2026, 3, 4))
            docs[pid] = {"created": "2026-08-20", "added": "2026-08-20T12:00:00+02:00"}
        mcp = _FakePaperless(docs)
        first = await bf.backfill_created_dates(
            _factory(db_session), mcp, commit=True, limit=2, pacer=_pacer(), sleep=_no_sleep,
        )
        assert first.patched == [40, 41] and first.last_pid == 41
        second = await bf.backfill_created_dates(
            _factory(db_session), mcp, commit=True, limit=2, after_pid=first.last_pid,
            pacer=_pacer(), sleep=_no_sleep,
        )
        assert second.patched == [42]

    async def test_rate_limit_rejection_does_not_fail_the_document(self, db_session):
        await _doc(db_session, pid=50, doc_date=date(2026, 3, 4))
        mcp = _FakePaperless({50: {"created": "2026-08-20", "added": "2026-08-20T12:00:00+02:00"}}, reject_first=3)
        report = await bf.backfill_created_dates(
            _factory(db_session), mcp, commit=True, pacer=_pacer(), sleep=_no_sleep,
        )
        assert report.patched == [50]
        assert report.failed == [] and report.unreachable == []

    async def test_summary_is_counts_only(self, db_session):
        await _doc(db_session, pid=60, doc_date=date(2026, 3, 4))
        mcp = _FakePaperless({60: {"created": "2026-08-20", "added": "2026-08-20T12:00:00+02:00", "title": "SECRET"}})
        report = await bf.backfill_created_dates(
            _factory(db_session), mcp, commit=False, pacer=_pacer(), sleep=_no_sleep,
        )
        rendered = json.dumps(report.summary())
        assert "SECRET" not in rendered
        assert all(isinstance(v, (int, str)) for v in report.summary().values())


def _load_cli():
    path = Path(__file__).resolve().parents[2] / "bin" / "backfill_paperless_metadata.py"
    spec = importlib.util.spec_from_file_location("backfill_paperless_metadata_cli", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.unit
class TestCli:
    def test_dry_run_is_the_default(self):
        args = _load_cli().build_parser().parse_args(["--mode", "created-date"])
        assert args.commit is False

    def test_commit_flag(self):
        args = _load_cli().build_parser().parse_args(["--mode", "created-date", "--commit"])
        assert args.commit is True

    def test_mode_is_required(self):
        with pytest.raises(SystemExit):
            _load_cli().build_parser().parse_args(["--commit"])

    def test_dry_run_and_commit_are_exclusive(self):
        with pytest.raises(SystemExit):
            _load_cli().build_parser().parse_args(["--mode", "created-date", "--dry-run", "--commit"])
