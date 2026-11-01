"""Schicht A re-extraction concurrency guard.

Two overlapping re-extractions of ONE document each do write-new-then-purge-old
and would leave BOTH new fact sets behind (duplicate facts). The hook now takes a
per-document Postgres advisory lock (on a dedicated connection, so it spans the
mid-flight commit + post-commit purge); the loser skips.

- The `_resolve_lock_engine` / `_reindex_lock` no-op path is unit-tested (no PG).
- The hook behavior (writes unlocked, skips when locked, no duplicates under real
  concurrency) is tested against real Postgres — the hook opens its OWN
  AsyncSessionLocal and commits, so it needs a committed schema (pg_async_engine,
  which drop_all's on teardown), NOT the outer-txn `pg_db_session`.
"""
from __future__ import annotations

import asyncio
import datetime as dt
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from services.schicht_a_extractor import (
    _SCHICHT_A_REINDEX_LOCK_NS,
    ExtractedFact,
    SchichtAResult,
    _reindex_lock,
    _resolve_lock_engine,
    schicht_a_post_document_ingest_hook,
)

# ============================================================================
# Unit: the helper's non-Postgres no-op path (no DB)
# ============================================================================


class _FakeDialect:
    def __init__(self, name):
        self.name = name


class _FakeBind:
    def __init__(self, name):
        self.dialect = _FakeDialect(name)


@pytest.mark.unit
def test_resolve_lock_engine_unknown_bind_is_none():
    assert _resolve_lock_engine(object()) is None
    assert _resolve_lock_engine(None) is None


@pytest.mark.unit
async def test_reindex_lock_noop_on_sqlite():
    """Non-Postgres bind → always yields True (proceed unguarded), no SQL."""
    async with _reindex_lock(_FakeBind("sqlite"), 42) as got:
        assert got is True


@pytest.mark.unit
async def test_reindex_lock_noop_when_document_id_none():
    async with _reindex_lock(_FakeBind("postgresql"), None) as got:
        assert got is True


@pytest.mark.unit
async def test_reindex_lock_degrades_to_unlocked_on_connect_failure(monkeypatch):
    """Pool pressure / connect failure → degrade to UNLOCKED (yield True), never
    block or raise into the ingest path."""
    from services import schicht_a_extractor as mod

    class _BadEngine:
        def connect(self):
            async def _boom():
                raise RuntimeError("pool exhausted")
            return _boom()

    monkeypatch.setattr(mod, "_resolve_lock_engine", lambda bind: _BadEngine())
    async with mod._reindex_lock(_FakeBind("postgresql"), 7) as got:
        assert got is True


@pytest.mark.unit
async def test_reindex_lock_degrades_to_unlocked_on_connect_timeout(monkeypatch):
    """A hung lock-connection acquire times out and degrades to UNLOCKED."""
    from services import schicht_a_extractor as mod

    class _HangEngine:
        def connect(self):
            async def _hang():
                await asyncio.sleep(30)
            return _hang()

    monkeypatch.setattr(mod, "_resolve_lock_engine", lambda bind: _HangEngine())
    monkeypatch.setattr(mod, "_LOCK_CONN_ACQUIRE_TIMEOUT_S", 0.05)
    async with mod._reindex_lock(_FakeBind("postgresql"), 7) as got:
        assert got is True


# ============================================================================
# Real Postgres: the hook wires the lock in
# ============================================================================

pg = [pytest.mark.postgres, pytest.mark.asyncio]

_seq = 0


def _two_facts() -> SchichtAResult:
    return SchichtAResult(facts=[
        ExtractedFact(category="identifier", kind="steuernummer",
                      value="114/5876/5293", normalized_value="11458765293",
                      source="deterministic"),
        ExtractedFact(category="obligation", kind="zahlungsfrist", value="Rechnung",
                      obligation_date=dt.date(2026, 3, 15), source="llm"),
    ])


async def _seed_user_and_doc(engine, *, tier: int = 0) -> tuple[int, int]:
    """Create a committed user + document (no atom → hook owner = user_id)."""
    global _seq
    from models.database import Document, Role, User

    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as s:
        _seq += 1
        role = Role(name=f"reidx_role_{_seq}")
        s.add(role)
        await s.flush()
        user = User(username=f"reidx_u_{_seq}", email=f"reidx{_seq}@ex.test",
                    password_hash="x", role_id=role.id, is_active=True)
        s.add(user)
        await s.flush()
        doc = Document(filename="d.pdf", file_path="/x/d.pdf", status="completed",
                       circle_tier=tier)
        s.add(doc)
        await s.flush()
        uid, did = user.id, doc.id
        await s.commit()
    return uid, did


async def _count_facts(engine, doc_id: int) -> int:
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as s:
        return int((await s.execute(
            text("SELECT count(*) FROM document_facts WHERE document_id = :d"),
            {"d": doc_id},
        )).scalar())


@pytest.fixture
def _patch_extractor(monkeypatch):
    """Enable the flag + stub the LLM extract/title so the hook writes a fixed set."""
    from services import schicht_a_extractor as mod

    monkeypatch.setattr(mod.settings, "schicht_a_extraction_enabled", True)
    monkeypatch.setattr(mod.SchichtAExtractor, "extract",
                        AsyncMock(return_value=_two_facts()))
    monkeypatch.setattr(mod, "generate_document_title", AsyncMock(return_value=None))


@pytest.fixture
def _wire_session(monkeypatch, pg_async_engine):
    """Point the hook's own AsyncSessionLocal at the test Postgres engine."""
    import services.database as db_mod

    monkeypatch.setattr(
        db_mod, "AsyncSessionLocal",
        async_sessionmaker(pg_async_engine, expire_on_commit=False),
    )


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_hook_writes_when_unlocked(pg_async_engine, _patch_extractor, _wire_session):
    uid, did = await _seed_user_and_doc(pg_async_engine)
    await schicht_a_post_document_ingest_hook([], document_id=did, user_id=uid,
                                              field_text="some field text")
    assert await _count_facts(pg_async_engine, did) == 2


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_hook_skips_when_document_lock_held(pg_async_engine, _patch_extractor, _wire_session):
    """A held per-document advisory lock makes the hook a no-op (writes nothing)."""
    uid, did = await _seed_user_and_doc(pg_async_engine)
    async with pg_async_engine.connect() as holder:
        got = (await holder.execute(
            text("SELECT pg_try_advisory_lock(:ns, :doc)"),
            {"ns": _SCHICHT_A_REINDEX_LOCK_NS, "doc": did},
        )).scalar()
        assert got is True
        try:
            await schicht_a_post_document_ingest_hook([], document_id=did, user_id=uid,
                                                      field_text="some field text")
            assert await _count_facts(pg_async_engine, did) == 0  # skipped
        finally:
            await holder.execute(
                text("SELECT pg_advisory_unlock(:ns, :doc)"),
                {"ns": _SCHICHT_A_REINDEX_LOCK_NS, "doc": did},
            )


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_concurrent_hooks_leave_exactly_one_fact_set(pg_async_engine, _patch_extractor, _wire_session):
    """Two concurrent re-extractions of the same document → exactly ONE fact set
    (no duplicates). Without the lock this leaves 4 (2 sets); with it, 2."""
    uid, did = await _seed_user_and_doc(pg_async_engine)
    await asyncio.gather(
        schicht_a_post_document_ingest_hook([], document_id=did, user_id=uid,
                                            field_text="txt"),
        schicht_a_post_document_ingest_hook([], document_id=did, user_id=uid,
                                            field_text="txt"),
    )
    assert await _count_facts(pg_async_engine, did) == 2


# ============================================================================
# A document that ends with NO facts says so (xidra doc 669, 2026-10-27)
# ============================================================================
# No facts means no generated title and no document date. That exit returned
# silently, and extract()'s own failure line named no document, so a factless
# document could not be traced back to its cause from the logs.


def _warnings(mock_logger) -> str:
    return " ".join(str(c.args[0]) for c in mock_logger.warning.call_args_list)


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_hook_names_the_document_when_extraction_yields_no_facts(
    pg_async_engine, _patch_extractor, _wire_session, monkeypatch
):
    from unittest.mock import patch

    from services import schicht_a_extractor as mod

    monkeypatch.setattr(mod.SchichtAExtractor, "extract", AsyncMock(
        return_value=SchichtAResult(facts=[], error="llm_extraction_failed")
    ))
    uid, did = await _seed_user_and_doc(pg_async_engine)

    with patch("services.schicht_a_extractor.logger") as mock_logger:
        await schicht_a_post_document_ingest_hook([], document_id=did, user_id=uid,
                                                  field_text="some field text")

    assert await _count_facts(pg_async_engine, did) == 0
    msg = _warnings(mock_logger)
    assert f"doc {did} produced no facts" in msg
    assert "llm_extraction_failed" in msg
    assert "some field text" not in msg  # counts only, never the content


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_hook_names_the_document_on_a_partial_extraction(
    pg_async_engine, _patch_extractor, _wire_session, monkeypatch
):
    """FIRST extraction: the LLM failed but the deterministic layer found facts.
    There is nothing to keep, so they are stored, and the failure is still
    attributed to the document."""
    from unittest.mock import patch

    from services import schicht_a_extractor as mod

    monkeypatch.setattr(mod.SchichtAExtractor, "extract",
                        AsyncMock(return_value=_partial()))
    uid, did = await _seed_user_and_doc(pg_async_engine)

    with patch("services.schicht_a_extractor.logger") as mock_logger:
        await schicht_a_post_document_ingest_hook([], document_id=did, user_id=uid,
                                                  field_text="some field text")

    assert await _fact_values(pg_async_engine, did) == ["114/5876/5293"]
    assert f"extraction for doc {did} failed (llm_extraction_failed)" in _warnings(mock_logger)


# ============================================================================
# A RE-extraction whose LLM step failed keeps the prior fact set
# ============================================================================
# On a reindex during an LLM outage the result holds only the deterministic
# subset. Write-new-then-purge-old replaced the full prior set with it, and the
# title and document date derived from the facts went with it, for good.


def _partial() -> SchichtAResult:
    """What extract() returns when the LLM fails: the deterministic facts only."""
    return SchichtAResult(
        facts=[f for f in _two_facts().facts if f.source == "deterministic"],
        error="llm_extraction_failed",
    )


async def _fact_values(engine, doc_id: int) -> list[str]:
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as s:
        return sorted(r[0] for r in (await s.execute(
            text("SELECT value FROM document_facts WHERE document_id = :d"),
            {"d": doc_id},
        )).all())


async def _doc_title_and_date(engine, doc_id: int):
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as s:
        return tuple((await s.execute(
            text("SELECT generated_title, document_date FROM documents WHERE id = :d"),
            {"d": doc_id},
        )).one())


async def _ingest_full_set(engine, monkeypatch) -> tuple[int, int]:
    """A document whose first extraction succeeded: two facts and a title."""
    from services import schicht_a_extractor as mod

    monkeypatch.setattr(mod, "generate_document_title",
                        AsyncMock(return_value="Rechnung Stadtwerke"))
    uid, did = await _seed_user_and_doc(engine)
    await schicht_a_post_document_ingest_hook([], document_id=did, user_id=uid,
                                              field_text="some field text")
    assert await _fact_values(engine, did) == ["114/5876/5293", "Rechnung"]
    return uid, did


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_reindex_with_llm_error_keeps_the_existing_facts(
    pg_async_engine, _patch_extractor, _wire_session, monkeypatch
):
    from unittest.mock import patch

    from services import schicht_a_extractor as mod

    uid, did = await _ingest_full_set(pg_async_engine, monkeypatch)
    before = await _doc_title_and_date(pg_async_engine, did)
    assert before[0] == "Rechnung Stadtwerke"

    monkeypatch.setattr(mod.SchichtAExtractor, "extract",
                        AsyncMock(return_value=_partial()))
    title = AsyncMock(return_value="Steuernummer")
    monkeypatch.setattr(mod, "generate_document_title", title)
    with patch("services.schicht_a_extractor.logger") as mock_logger:
        await schicht_a_post_document_ingest_hook([], document_id=did, user_id=uid,
                                                  field_text="some field text")

    assert await _fact_values(pg_async_engine, did) == ["114/5876/5293", "Rechnung"]
    assert await _doc_title_and_date(pg_async_engine, did) == before
    title.assert_not_awaited()
    msg = _warnings(mock_logger)
    assert f"doc {did} re-extraction incomplete; kept the existing 2 fact(s)" in msg
    assert "some field text" not in msg


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_reindex_without_error_replaces_the_facts(
    pg_async_engine, _patch_extractor, _wire_session, monkeypatch
):
    from services import schicht_a_extractor as mod

    uid, did = await _ingest_full_set(pg_async_engine, monkeypatch)

    fresh = SchichtAResult(facts=[
        ExtractedFact(category="identifier", kind="kundennummer", value="K-77",
                      normalized_value="k77", source="llm"),
    ])
    monkeypatch.setattr(mod.SchichtAExtractor, "extract", AsyncMock(return_value=fresh))
    monkeypatch.setattr(mod, "generate_document_title",
                        AsyncMock(return_value="Kundenkonto K-77"))
    await schicht_a_post_document_ingest_hook([], document_id=did, user_id=uid,
                                              field_text="some field text")

    assert await _fact_values(pg_async_engine, did) == ["K-77"]
    assert (await _doc_title_and_date(pg_async_engine, did))[0] == "Kundenkonto K-77"
